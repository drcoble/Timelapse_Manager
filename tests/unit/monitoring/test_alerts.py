"""Unit tests for the active-alerts model, query, clear service, and auto-clear.

Covers:
* migration 012 columns exist and default NULL on a fresh DB;
* the active-alerts query (level floor, cleared exclusion, count, scope filter);
* manual clear one / clear-all (attribution, row retention, idempotency, the
  non-alert / already-cleared no-ops);
* the auto-clear-on-resolve evaluator, including info-level resolve signals and
  scope isolation.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import inspect

from timelapse_manager.db.models import Event, User
from timelapse_manager.db.session import session_scope
from timelapse_manager.monitoring.alerts import (
    CLEAR_REASON_AUTO,
    CLEAR_REASON_MANUAL,
    auto_clear_for_event,
    clear_alert,
    clear_all_alerts,
    get_active_alerts,
)

# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _seed_event(
    factory,
    *,
    scope: str = "project",
    scope_id: int | None = 1,
    level: str = "warning",
    message: str = "event",
    reason: str | None = None,
    event_type: str | None = None,
    cleared: bool = False,
) -> int:
    """Insert one event row directly and return its id."""
    metadata: dict | None = None
    if reason is not None or event_type is not None:
        metadata = {}
        if reason is not None:
            metadata["reason"] = reason
        if event_type is not None:
            metadata["type"] = event_type
    with session_scope(factory) as session:
        event = Event(
            scope=scope,
            scope_id=scope_id,
            level=level,
            message=message,
            timestamp=_now(),
            event_metadata=metadata,
        )
        if cleared:
            event.alert_cleared_at = _now()
            event.alert_clear_reason = CLEAR_REASON_MANUAL
        session.add(event)
        session.flush()
        return event.id


def _ensure_user(factory, user_id: int = 1) -> None:
    with session_scope(factory) as session:
        if session.get(User, user_id) is None:
            session.add(
                User(id=user_id, username="system", auth_source="local", role="admin")
            )


# ---------------------------------------------------------------------------
# Migration 012 columns
# ---------------------------------------------------------------------------


class TestMigrationColumns:
    def test_alert_columns_exist_and_default_null(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as session:
            engine = session.get_bind()
            cols = {c["name"]: c for c in inspect(engine).get_columns("event")}
        for name in ("alert_cleared_at", "alert_cleared_by", "alert_clear_reason"):
            assert name in cols
            assert cols[name]["nullable"] is True
        event_id = _seed_event(migrated_factory)
        with session_scope(migrated_factory) as session:
            row = session.get(Event, event_id)
            assert row.alert_cleared_at is None
            assert row.alert_cleared_by is None
            assert row.alert_clear_reason is None


# ---------------------------------------------------------------------------
# Active-alerts query
# ---------------------------------------------------------------------------


class TestActiveAlertsQuery:
    def test_includes_warning_error_critical_excludes_info(
        self, migrated_factory
    ) -> None:
        _seed_event(migrated_factory, level="warning", message="warn")
        _seed_event(migrated_factory, level="error", message="err")
        _seed_event(migrated_factory, level="critical", message="crit")
        _seed_event(migrated_factory, level="info", message="info")
        with session_scope(migrated_factory) as session:
            alerts, total = get_active_alerts(session)
        assert total == 3
        messages = {a.message for a in alerts}
        assert messages == {"warn", "err", "crit"}

    def test_excludes_cleared_events(self, migrated_factory) -> None:
        _seed_event(migrated_factory, level="warning", message="active")
        _seed_event(migrated_factory, level="warning", message="gone", cleared=True)
        with session_scope(migrated_factory) as session:
            alerts, total = get_active_alerts(session)
        assert total == 1
        assert alerts[0].message == "active"

    def test_newest_first(self, migrated_factory) -> None:
        first = _seed_event(migrated_factory, level="warning", message="first")
        second = _seed_event(migrated_factory, level="warning", message="second")
        with session_scope(migrated_factory) as session:
            alerts, _ = get_active_alerts(session)
        assert [a.id for a in alerts] == [second, first]

    def test_scope_filter(self, migrated_factory) -> None:
        _seed_event(migrated_factory, scope="project", scope_id=1, message="p1")
        _seed_event(migrated_factory, scope="project", scope_id=2, message="p2")
        with session_scope(migrated_factory) as session:
            alerts, total = get_active_alerts(session, scope="project", scope_id=2)
        assert total == 1
        assert alerts[0].message == "p2"

    def test_reason_and_type_surfaced(self, migrated_factory) -> None:
        _seed_event(
            migrated_factory,
            level="warning",
            reason="low_disk",
            event_type="storage.disk_low",
        )
        with session_scope(migrated_factory) as session:
            alerts, _ = get_active_alerts(session)
        assert alerts[0].reason == "low_disk"
        assert alerts[0].event_type == "storage.disk_low"


# ---------------------------------------------------------------------------
# Manual clear
# ---------------------------------------------------------------------------


class TestManualClear:
    def test_clear_one_sets_attribution_and_retains_row(self, migrated_factory) -> None:
        _ensure_user(migrated_factory)
        event_id = _seed_event(migrated_factory, level="warning")
        with session_scope(migrated_factory) as session:
            cleared = clear_alert(session, event_id=event_id, user_id=1)
        assert cleared == 1
        with session_scope(migrated_factory) as session:
            row = session.get(Event, event_id)
            assert row is not None  # row retained, not deleted
            assert row.alert_cleared_at is not None
            assert row.alert_cleared_by == 1
            assert row.alert_clear_reason == CLEAR_REASON_MANUAL

    def test_clear_one_removes_from_active_list(self, migrated_factory) -> None:
        _ensure_user(migrated_factory)
        event_id = _seed_event(migrated_factory, level="warning")
        with session_scope(migrated_factory) as session:
            clear_alert(session, event_id=event_id, user_id=1)
        with session_scope(migrated_factory) as session:
            _, total = get_active_alerts(session)
        assert total == 0

    def test_clear_already_cleared_is_idempotent(self, migrated_factory) -> None:
        _ensure_user(migrated_factory)
        event_id = _seed_event(migrated_factory, level="warning")
        with session_scope(migrated_factory) as session:
            assert clear_alert(session, event_id=event_id, user_id=1) == 1
        with session_scope(migrated_factory) as session:
            assert clear_alert(session, event_id=event_id, user_id=1) == 0

    def test_clear_info_event_is_noop(self, migrated_factory) -> None:
        _ensure_user(migrated_factory)
        event_id = _seed_event(migrated_factory, level="info")
        with session_scope(migrated_factory) as session:
            assert clear_alert(session, event_id=event_id, user_id=1) == 0

    def test_clear_unknown_id_is_noop(self, migrated_factory) -> None:
        _ensure_user(migrated_factory)
        with session_scope(migrated_factory) as session:
            assert clear_alert(session, event_id=999, user_id=1) == 0

    def test_clear_all(self, migrated_factory) -> None:
        _ensure_user(migrated_factory)
        _seed_event(migrated_factory, level="warning", message="a")
        _seed_event(migrated_factory, level="error", message="b")
        _seed_event(migrated_factory, level="info", message="c")  # not an alert
        with session_scope(migrated_factory) as session:
            cleared = clear_all_alerts(session, user_id=1)
        assert cleared == 2
        with session_scope(migrated_factory) as session:
            _, total = get_active_alerts(session)
        assert total == 0

    def test_clear_all_empty_is_zero(self, migrated_factory) -> None:
        _ensure_user(migrated_factory)
        with session_scope(migrated_factory) as session:
            assert clear_all_alerts(session, user_id=1) == 0


# ---------------------------------------------------------------------------
# Auto-clear-on-resolve evaluator
# ---------------------------------------------------------------------------


class TestAutoClearEvaluator:
    def test_disk_recovered_clears_low_disk_for_same_scope(
        self, migrated_factory
    ) -> None:
        alert_id = _seed_event(
            migrated_factory,
            scope="project",
            scope_id=7,
            level="warning",
            reason="low_disk",
        )
        with session_scope(migrated_factory) as session:
            cleared = auto_clear_for_event(
                session, scope="project", scope_id=7, reason="disk_recovered"
            )
        assert cleared == 1
        with session_scope(migrated_factory) as session:
            row = session.get(Event, alert_id)
            assert row.alert_cleared_at is not None
            assert row.alert_clear_reason == CLEAR_REASON_AUTO
            assert row.alert_cleared_by is None  # auto-clear is unattributed

    def test_resolve_does_not_clear_other_scope(self, migrated_factory) -> None:
        other_id = _seed_event(
            migrated_factory,
            scope="project",
            scope_id=8,
            level="warning",
            reason="low_disk",
        )
        with session_scope(migrated_factory) as session:
            cleared = auto_clear_for_event(
                session, scope="project", scope_id=7, reason="disk_recovered"
            )
        assert cleared == 0
        with session_scope(migrated_factory) as session:
            assert session.get(Event, other_id).alert_cleared_at is None

    def test_camera_recovered_clears_camera_offline(self, migrated_factory) -> None:
        alert_id = _seed_event(
            migrated_factory,
            scope="project",
            scope_id=3,
            level="warning",
            reason="camera_offline",
        )
        with session_scope(migrated_factory) as session:
            cleared = auto_clear_for_event(
                session, scope="project", scope_id=3, reason="camera_recovered"
            )
        assert cleared == 1
        with session_scope(migrated_factory) as session:
            assert session.get(Event, alert_id).alert_clear_reason == CLEAR_REASON_AUTO

    def test_non_resolve_reason_is_noop(self, migrated_factory) -> None:
        _seed_event(migrated_factory, scope="project", scope_id=1, reason="low_disk")
        with session_scope(migrated_factory) as session:
            # A fresh low_disk raise must NOT clear anything (raise reasons are
            # never triggers); reason=None likewise.
            assert (
                auto_clear_for_event(
                    session, scope="project", scope_id=1, reason="low_disk"
                )
                == 0
            )
            assert (
                auto_clear_for_event(session, scope="project", scope_id=1, reason=None)
                == 0
            )

    def test_resolve_does_not_unclear_a_later_recurrence(
        self, migrated_factory
    ) -> None:
        # An already-cleared low_disk alert is left alone; a recurrence is a new
        # row that auto-clear leaves active until its own resolve.
        old_id = _seed_event(
            migrated_factory,
            scope="project",
            scope_id=5,
            level="warning",
            reason="low_disk",
            cleared=True,
        )
        new_id = _seed_event(
            migrated_factory,
            scope="project",
            scope_id=5,
            level="warning",
            reason="low_disk",
        )
        with session_scope(migrated_factory) as session:
            cleared = auto_clear_for_event(
                session, scope="project", scope_id=5, reason="disk_recovered"
            )
        # Only the new active one is cleared; the old cleared row is untouched.
        assert cleared == 1
        with session_scope(migrated_factory) as session:
            assert session.get(Event, new_id).alert_clear_reason == CLEAR_REASON_AUTO
            # old row retains its manual clear reason (not overwritten to auto)
            assert session.get(Event, old_id).alert_clear_reason == CLEAR_REASON_MANUAL
