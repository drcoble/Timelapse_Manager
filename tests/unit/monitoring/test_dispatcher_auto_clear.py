"""Auto-clear-on-resolve driven through the dispatcher poll (non-vacuous).

These tests assert the two hollow-risk traps are handled:

* TRAP 1 -- resolve signals are INFO level. The evaluator runs over every polled
  event regardless of level, so an info-level ``disk_recovered`` is seen and
  clears the matching warning alert.
* TRAP 2 -- the evaluator hooks the dispatcher's existing poll over persisted
  ``event`` rows, which sees BOTH write paths (``log_event`` and the supervisor's
  untyped ``_write_event``). It also runs independently of notification routing:
  a resolve event routed to NO channel still auto-clears.

The disk pair is time-separated in production: the low_disk warning is logged in
one poll cycle (advancing past the high-water mark), the disk_recovered info
event in a later cycle. These tests reproduce that two-cycle separation so a
within-batch pairing implementation would fail them.
"""

from __future__ import annotations

from datetime import UTC, datetime

from timelapse_manager.config.settings import MonitoringSettings
from timelapse_manager.db.models import Event
from timelapse_manager.db.session import session_scope
from timelapse_manager.monitoring import NotificationDispatcher, get_active_alerts
from timelapse_manager.monitoring.channels import (
    NotificationChannel,
    NotificationMessage,
)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class FakeChannel(NotificationChannel):
    """Records every delivered message."""

    def __init__(self, name: str = "fake") -> None:
        self._name = name
        self.received: list[NotificationMessage] = []

    @property
    def name(self) -> str:
        return self._name

    async def send(self, message: NotificationMessage) -> None:
        self.received.append(message)


def _settings() -> MonitoringSettings:
    return MonitoringSettings(
        autostart=False,
        poll_interval_seconds=0.1,
        max_retries=1,
        retry_backoff_seconds=0.0,
        debounce_window_seconds=60.0,
        channel_send_timeout_seconds=30.0,
    )


def _dispatcher(factory, channel, rules) -> NotificationDispatcher:
    return NotificationDispatcher(
        session_factory=factory,
        channels=[channel],
        settings=_settings(),
        routing_rules_fn=lambda: rules,
    )


def _write_supervisor_style_event(
    factory,
    *,
    scope_id: int,
    level: str,
    reason: str,
    message: str = "event",
) -> int:
    """Insert an untyped event exactly as the supervisor's ``_write_event`` does.

    No ``type`` key -- only ``level`` and ``reason`` in the metadata -- so this
    exercises the ``_write_event`` write path (not ``log_event``).
    """
    with session_scope(factory) as session:
        event = Event(
            scope="project",
            scope_id=scope_id,
            level=level,
            message=message,
            timestamp=_now(),
            event_metadata={"reason": reason},
        )
        session.add(event)
        session.flush()
        return event.id


# A routing rule that matches NOTHING the resolve event carries: it only routes
# a specific render type. Proves auto-clear is independent of routing/delivery.
_NON_MATCHING_RULE = {
    "event_types": ["render.complete"],
    "min_level": "info",
    "channels": ["fake"],
}


class TestAutoClearThroughPoll:
    async def test_disk_recovered_info_event_clears_low_disk_across_polls(
        self, migrated_factory
    ) -> None:
        """TRAP 1 + 2: info resolve via _write_event path, two separate polls."""
        channel = FakeChannel()
        disp = _dispatcher(migrated_factory, channel, [_NON_MATCHING_RULE])

        # Poll 1: the low_disk warning is raised and consumed past the HWM.
        alert_id = _write_supervisor_style_event(
            migrated_factory, scope_id=42, level="warning", reason="low_disk"
        )
        await disp.run_once()
        with session_scope(migrated_factory) as session:
            assert session.get(Event, alert_id).alert_cleared_at is None  # still active
            _, total = get_active_alerts(session)
            assert total == 1

        # Poll 2 (a later cycle): the info-level disk_recovered resolve arrives.
        _write_supervisor_style_event(
            migrated_factory, scope_id=42, level="info", reason="disk_recovered"
        )
        await disp.run_once()

        with session_scope(migrated_factory) as session:
            row = session.get(Event, alert_id)
            assert row.alert_cleared_at is not None
            assert row.alert_clear_reason == "auto"
            assert row.alert_cleared_by is None
            _, total = get_active_alerts(session)
            assert total == 0
        # The non-matching rule routed neither event anywhere: auto-clear did not
        # depend on delivery.
        assert channel.received == []

    async def test_resolve_for_other_scope_leaves_alert_active(
        self, migrated_factory
    ) -> None:
        channel = FakeChannel()
        disp = _dispatcher(migrated_factory, channel, [_NON_MATCHING_RULE])
        alert_id = _write_supervisor_style_event(
            migrated_factory, scope_id=1, level="warning", reason="low_disk"
        )
        await disp.run_once()
        # Recovery for a DIFFERENT project must not clear scope_id=1's alert.
        _write_supervisor_style_event(
            migrated_factory, scope_id=2, level="info", reason="disk_recovered"
        )
        await disp.run_once()
        with session_scope(migrated_factory) as session:
            assert session.get(Event, alert_id).alert_cleared_at is None

    async def test_camera_recovered_clears_camera_offline_through_poll(
        self, migrated_factory
    ) -> None:
        channel = FakeChannel()
        disp = _dispatcher(migrated_factory, channel, [_NON_MATCHING_RULE])
        alert_id = _write_supervisor_style_event(
            migrated_factory, scope_id=9, level="warning", reason="camera_offline"
        )
        await disp.run_once()
        _write_supervisor_style_event(
            migrated_factory, scope_id=9, level="info", reason="camera_recovered"
        )
        await disp.run_once()
        with session_scope(migrated_factory) as session:
            row = session.get(Event, alert_id)
            assert row.alert_cleared_at is not None
            assert row.alert_clear_reason == "auto"

    async def test_dispatch_still_delivers_with_auto_clear_active(
        self, migrated_factory
    ) -> None:
        """Regression: notification dispatch is unaffected by the auto-clear hook."""
        channel = FakeChannel()
        rule = {
            "event_types": ["all"],
            "min_level": "info",
            "channels": ["fake"],
        }
        disp = _dispatcher(migrated_factory, channel, [rule])
        _write_supervisor_style_event(
            migrated_factory, scope_id=1, level="warning", reason="low_disk"
        )
        count = await disp.run_once()
        assert count == 1
        assert len(channel.received) == 1
