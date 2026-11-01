"""Opt-in event retention and the shared batched-delete helper (issue #66, C4).

Retention deletes history, so these tests pin exactly which rows go:

* with the shipped defaults (both windows ``0``) nothing is deleted;
* old info/debug and old cleared alerts go; uncleared alerts, audit/security
  events and anything inside the window stay;
* the audit exclusion is NULL-safe (an untyped old info event *is* pruned);
* the newest row is never deleted, so the dispatcher's in-memory high-water
  mark can never be undercut by SQLite reusing a freed id.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, select

from timelapse_manager.config.settings import (
    EventRetentionSettings,
    MonitoringSettings,
    Settings,
)
from timelapse_manager.db.batched_delete import prune_in_batches
from timelapse_manager.db.models import Event
from timelapse_manager.db.session import session_scope
from timelapse_manager.monitoring import NotificationDispatcher
from timelapse_manager.monitoring.channels import (
    NotificationChannel,
    NotificationMessage,
)
from timelapse_manager.monitoring.event_retention import prune_events
from timelapse_manager.monitoring.events import AUDIT_EVENT_TYPES


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _add(
    factory: Any,
    *,
    level: str,
    age_days: float,
    cleared_days_ago: float | None = None,
    event_type: str | None = None,
    message: str = "e",
) -> int:
    """Insert one event ``age_days`` old; return its id."""
    now = _now()
    metadata: dict[str, Any] | None = {"type": event_type} if event_type else None
    with session_scope(factory) as session:
        event = Event(
            scope="system",
            scope_id=None,
            level=level,
            message=message,
            timestamp=now - timedelta(days=age_days),
            event_metadata=metadata,
            alert_cleared_at=(
                None
                if cleared_days_ago is None
                else now - timedelta(days=cleared_days_ago)
            ),
            alert_clear_reason=None if cleared_days_ago is None else "manual",
        )
        session.add(event)
        session.flush()
        return event.id


def _ids(factory: Any) -> set[int]:
    with session_scope(factory) as session:
        return set(session.execute(select(Event.id)).scalars())


_ON = EventRetentionSettings(info_days=30, cleared_alert_days=30)


class TestRetentionSettings:
    def test_ships_off(self) -> None:
        retention = Settings().monitoring.event_retention
        assert retention.info_days == 0
        assert retention.cleared_alert_days == 0
        assert retention.batch_size == 5000

    @pytest.mark.parametrize(
        "kwargs",
        [{"info_days": -1}, {"cleared_alert_days": -1}, {"batch_size": 0}],
    )
    def test_rejects_out_of_range(self, kwargs: dict[str, int]) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            EventRetentionSettings(**kwargs)


class TestPruneEvents:
    def _seed_mix(self, factory: Any) -> dict[str, int]:
        ids = {
            "old_info": _add(factory, level="info", age_days=90),
            "old_debug": _add(factory, level="debug", age_days=90),
            "old_cleared_warning": _add(
                factory, level="warning", age_days=90, cleared_days_ago=60
            ),
            "old_uncleared_error": _add(factory, level="error", age_days=90),
            "old_uncleared_warning": _add(factory, level="warning", age_days=90),
            "recently_cleared_old_warning": _add(
                factory, level="warning", age_days=90, cleared_days_ago=1
            ),
            "new_info": _add(factory, level="info", age_days=1),
        }
        for event_type in sorted(AUDIT_EVENT_TYPES):
            ids[f"old_audit:{event_type}"] = _add(
                factory, level="info", age_days=90, event_type=event_type
            )
        ids["old_typed_info"] = _add(
            factory, level="info", age_days=90, event_type="render.complete"
        )
        # Newest row last, so it is the max id.
        ids["newest"] = _add(factory, level="info", age_days=0)
        return ids

    def test_defaults_delete_nothing(self, migrated_factory: Any) -> None:
        ids = self._seed_mix(migrated_factory)

        deleted = prune_events(migrated_factory, EventRetentionSettings(), _now())

        assert deleted == 0
        assert _ids(migrated_factory) == set(ids.values())

    def test_defaults_open_no_session(self) -> None:
        def factory() -> Any:
            raise AssertionError("retention off must not touch the database")

        assert prune_events(factory, EventRetentionSettings(), _now()) == 0  # type: ignore[arg-type]

    def test_deletes_exactly_the_expected_rows(self, migrated_factory: Any) -> None:
        ids = self._seed_mix(migrated_factory)

        deleted = prune_events(migrated_factory, _ON, _now())

        gone = {"old_info", "old_debug", "old_cleared_warning", "old_typed_info"}
        assert deleted == len(gone)
        expected = {ids[name] for name in ids if name not in gone}
        assert _ids(migrated_factory) == expected

    def test_each_window_is_independent(self, migrated_factory: Any) -> None:
        ids = self._seed_mix(migrated_factory)

        prune_events(migrated_factory, EventRetentionSettings(info_days=30), _now())
        remaining = _ids(migrated_factory)
        assert ids["old_info"] not in remaining
        assert ids["old_cleared_warning"] in remaining

        prune_events(
            migrated_factory, EventRetentionSettings(cleared_alert_days=30), _now()
        )
        assert ids["old_cleared_warning"] not in _ids(migrated_factory)

    def test_never_deletes_the_newest_row(self, migrated_factory: Any) -> None:
        old = [_add(migrated_factory, level="info", age_days=90) for _ in range(3)]

        deleted = prune_events(migrated_factory, _ON, _now())

        assert deleted == 2
        assert _ids(migrated_factory) == {max(old)}

    def test_stops_at_the_batch_cap(self, migrated_factory: Any) -> None:
        for _ in range(11):
            _add(migrated_factory, level="info", age_days=90)
        _add(migrated_factory, level="info", age_days=0)
        retention = EventRetentionSettings(info_days=30, batch_size=2)

        first = prune_events(migrated_factory, retention, _now(), max_batches=3)
        assert first == 6
        assert len(_ids(migrated_factory)) == 6

        # The next pass continues where the cap stopped this one.
        rest = prune_events(migrated_factory, retention, _now(), max_batches=10)
        assert rest == 5
        assert len(_ids(migrated_factory)) == 1


class TestPruneInBatches:
    def test_loops_until_a_short_batch(self, migrated_factory: Any) -> None:
        for _ in range(7):
            _add(migrated_factory, level="info", age_days=1)
        statements: list[int] = []

        def build(limit: int) -> Any:
            statements.append(limit)
            ids = select(Event.id).order_by(Event.id).limit(limit)
            return delete(Event).where(Event.id.in_(ids))

        assert prune_in_batches(migrated_factory, build, 3, 10) == 7
        # 3 + 3 + 1: the short third batch ends the loop.
        assert statements == [3, 3, 3]

    def test_each_batch_commits(self, migrated_factory: Any) -> None:
        """A failing later batch keeps the rows earlier batches deleted."""
        for _ in range(4):
            _add(migrated_factory, level="info", age_days=1)
        calls = 0

        def build(limit: int) -> Any:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("boom")
            ids = select(Event.id).order_by(Event.id).limit(limit)
            return delete(Event).where(Event.id.in_(ids))

        with pytest.raises(RuntimeError):
            prune_in_batches(migrated_factory, build, 2, 10)
        assert len(_ids(migrated_factory)) == 2

    @pytest.mark.parametrize(("batch_size", "max_batches"), [(0, 1), (1, 0)])
    def test_rejects_non_positive_bounds(
        self, batch_size: int, max_batches: int
    ) -> None:
        with pytest.raises(ValueError):
            prune_in_batches(
                lambda: None,  # type: ignore[arg-type,return-value]
                lambda limit: select(Event.id),
                batch_size,
                max_batches,
            )


class _Channel(NotificationChannel):
    def __init__(self) -> None:
        self.received: list[NotificationMessage] = []

    @property
    def name(self) -> str:
        return "fake"

    async def send(self, message: NotificationMessage) -> None:
        self.received.append(message)


class TestDispatcherAfterPrune:
    async def test_new_event_after_full_prune_is_delivered(
        self, migrated_factory: Any
    ) -> None:
        """Every row prunable, watermark at the max id: a later event still goes.

        ``event`` ids are not AUTOINCREMENT. Without the newest-row guard the
        prune would empty the table and the next insert would reuse an id at or
        below the dispatcher's in-memory mark, so the poll would skip it.
        """
        for _ in range(3):
            _add(migrated_factory, level="info", age_days=90)
        channel = _Channel()
        dispatcher = NotificationDispatcher(
            session_factory=migrated_factory,
            channels=[channel],
            settings=MonitoringSettings(autostart=False, max_retries=1),
            routing_rules_fn=lambda: [
                {
                    "event_types": ["render.complete"],
                    "min_level": "info",
                    "channels": ["fake"],
                }
            ],
        )
        await dispatcher.run_once()  # consume the backlog: mark = max id
        mark = max(_ids(migrated_factory))

        await asyncio.to_thread(prune_events, migrated_factory, _ON, _now())
        assert _ids(migrated_factory) == {mark}

        new_id = _add(
            migrated_factory,
            level="info",
            age_days=0,
            event_type="render.complete",
            message="after prune",
        )
        assert new_id > mark
        await dispatcher.run_once()
        await asyncio.sleep(0)
        for _ in range(50):
            if channel.received:
                break
            await asyncio.sleep(0.01)
        assert [m.message for m in channel.received] == ["after prune"]
