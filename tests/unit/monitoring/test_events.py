"""Unit tests for the event logging and query layer.

Covers: log_event persistence and redaction; system events with actor_user_id=None;
get_events filtering, pagination, and totals; get_audit_events permission gating;
secret scrubbing before DB persist.
"""

from __future__ import annotations

import types

import pytest

from timelapse_manager.db.session import session_scope
from timelapse_manager.monitoring import (
    EventType,
    get_audit_events,
    get_events,
    log_event,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _admin_user() -> object:
    return types.SimpleNamespace(role="admin")


def _viewer_user() -> object:
    return types.SimpleNamespace(role="viewer")


def _seed(
    factory,
    *,
    scope: str = "system",
    scope_id: int | None = None,
    level: str = "info",
    message: str = "test event",
    event_type: str | None = None,
    metadata: dict | None = None,
) -> None:
    with session_scope(factory) as session:
        log_event(
            session,
            scope=scope,
            scope_id=scope_id,
            level=level,
            message=message,
            type=event_type,
            metadata=metadata,
        )


# ---------------------------------------------------------------------------
# log_event — persistence basics
# ---------------------------------------------------------------------------


class TestLogEventPersists:
    def test_event_stored_with_correct_scope(self, migrated_factory) -> None:
        """log_event persists the scope field correctly."""
        _seed(migrated_factory, scope="camera", scope_id=42)
        with session_scope(migrated_factory) as session:
            rows, total = get_events(session, scope="camera")
        assert total == 1
        assert rows[0].scope == "camera"

    def test_event_stored_with_correct_scope_id(self, migrated_factory) -> None:
        _seed(migrated_factory, scope="project", scope_id=7)
        with session_scope(migrated_factory) as session:
            rows, _ = get_events(session, scope="project", scope_id=7)
        assert rows[0].scope_id == 7

    def test_event_stored_with_correct_level(self, migrated_factory) -> None:
        _seed(migrated_factory, level="warning", message="warn event")
        with session_scope(migrated_factory) as session:
            rows, _ = get_events(session)
        assert rows[0].level == "warning"

    def test_event_type_stored_in_metadata(self, migrated_factory) -> None:
        """Event type is stored under the 'type' key of event_metadata, not a column."""
        _seed(
            migrated_factory,
            event_type=EventType.CAPTURE_GAP.value,
            message="gap detected",
        )
        with session_scope(migrated_factory) as session:
            rows, _ = get_events(session)
        assert rows[0].event_metadata is not None
        assert rows[0].event_metadata.get("type") == EventType.CAPTURE_GAP.value

    def test_event_with_no_type_has_null_metadata(self, migrated_factory) -> None:
        """A bare event with no type and no metadata stores NULL metadata."""
        _seed(migrated_factory, message="bare event")
        with session_scope(migrated_factory) as session:
            rows, _ = get_events(session)
        assert rows[0].event_metadata is None

    def test_metadata_values_stored_alongside_type(self, migrated_factory) -> None:
        """Extra metadata keys survive alongside the type key."""
        _seed(
            migrated_factory,
            event_type=EventType.STORAGE_DISK_LOW.value,
            metadata={"free_gb": 2.5},
        )
        with session_scope(migrated_factory) as session:
            rows, _ = get_events(session)
        md = rows[0].event_metadata
        assert md is not None
        assert md["free_gb"] == 2.5
        assert md["type"] == EventType.STORAGE_DISK_LOW.value


# ---------------------------------------------------------------------------
# log_event — system event with actor_user_id=None must not FK-violate
# ---------------------------------------------------------------------------


class TestSystemEventActorNone:
    def test_system_event_without_actor_user_id_does_not_raise(
        self, migrated_factory
    ) -> None:
        """actor_user_id=None is the default for system/operational events.

        The Event table has an FK on actor_user_id → user.id (ON DELETE SET NULL).
        Passing None must persist without a FK violation — this is the normal
        operational path where no human user triggered the event.
        """
        with session_scope(migrated_factory) as session:
            log_event(
                session,
                scope="system",
                scope_id=None,
                level="info",
                message="system startup complete",
                actor_user_id=None,
            )
        with session_scope(migrated_factory) as session:
            rows, total = get_events(session)
        assert total == 1
        assert rows[0].actor_user_id is None


# ---------------------------------------------------------------------------
# log_event — secret redaction before persist
# ---------------------------------------------------------------------------


class TestLogEventRedaction:
    def test_rtsp_credentials_in_message_are_redacted_before_persist(
        self, migrated_factory
    ) -> None:
        """A camera URL with credentials must be scrubbed from the stored message."""
        raw = "rtsp://admin:s3cr3t@192.0.2.10/stream"
        _seed(migrated_factory, message=raw)
        with session_scope(migrated_factory) as session:
            rows, _ = get_events(session)
        stored_msg = rows[0].message
        assert "s3cr3t" not in stored_msg
        assert "rtsp://" in stored_msg  # scheme preserved

    def test_password_key_in_metadata_is_redacted_before_persist(
        self, migrated_factory
    ) -> None:
        """A 'password' key in metadata is masked before the row is stored."""
        _seed(
            migrated_factory,
            metadata={"password": "hunter2", "camera": "cam1"},
        )
        with session_scope(migrated_factory) as session:
            rows, _ = get_events(session)
        md = rows[0].event_metadata
        assert md is not None
        assert "hunter2" not in str(md)
        assert md["camera"] == "cam1"

    def test_https_token_in_message_is_redacted(self, migrated_factory) -> None:
        """URL with a token in userinfo (https://token@host) is scrubbed."""
        raw = "https://mytoken@api.example.com/hook"
        _seed(migrated_factory, message=raw)
        with session_scope(migrated_factory) as session:
            rows, _ = get_events(session)
        assert "mytoken" not in rows[0].message


# ---------------------------------------------------------------------------
# get_events — filtering
# ---------------------------------------------------------------------------


class TestGetEventsFilters:
    def test_filter_by_scope_excludes_other_scopes(self, migrated_factory) -> None:
        _seed(migrated_factory, scope="system", message="sys event")
        _seed(migrated_factory, scope="camera", scope_id=1, message="cam event")
        with session_scope(migrated_factory) as session:
            rows, total = get_events(session, scope="camera")
        assert total == 1
        assert rows[0].scope == "camera"

    def test_filter_by_scope_id_excludes_other_ids(self, migrated_factory) -> None:
        _seed(migrated_factory, scope="camera", scope_id=1, message="cam1")
        _seed(migrated_factory, scope="camera", scope_id=2, message="cam2")
        with session_scope(migrated_factory) as session:
            rows, total = get_events(session, scope="camera", scope_id=1)
        assert total == 1
        assert rows[0].scope_id == 1

    def test_level_floor_info_returns_info_and_above(self, migrated_factory) -> None:
        _seed(migrated_factory, level="info", message="info event")
        _seed(migrated_factory, level="error", message="error event")
        with session_scope(migrated_factory) as session:
            rows, total = get_events(session, level_floor="info")
        assert total == 2

    def test_level_floor_error_excludes_info_and_warning(
        self, migrated_factory
    ) -> None:
        _seed(migrated_factory, level="info", message="info event")
        _seed(migrated_factory, level="warning", message="warn event")
        _seed(migrated_factory, level="error", message="error event")
        with session_scope(migrated_factory) as session:
            rows, total = get_events(session, level_floor="error")
        assert total == 1
        assert rows[0].level == "error"

    def test_level_floor_warning_excludes_info_keeps_warning_and_error(
        self, migrated_factory
    ) -> None:
        _seed(migrated_factory, level="info", message="info event")
        _seed(migrated_factory, level="warning", message="warn event")
        _seed(migrated_factory, level="critical", message="critical event")
        with session_scope(migrated_factory) as session:
            rows, total = get_events(session, level_floor="warning")
        assert total == 2
        levels = {r.level for r in rows}
        assert levels == {"warning", "critical"}

    def test_unknown_level_floor_returns_all(self, migrated_factory) -> None:
        """An unrecognised level floor applies no filter (tolerant behaviour)."""
        _seed(migrated_factory, level="info")
        _seed(migrated_factory, level="error")
        with session_scope(migrated_factory) as session:
            rows, total = get_events(session, level_floor="nonsense")
        assert total == 2

    def test_no_filter_returns_all_events(self, migrated_factory) -> None:
        for msg in ("a", "b", "c"):
            _seed(migrated_factory, message=msg)
        with session_scope(migrated_factory) as session:
            rows, total = get_events(session)
        assert total == 3


# ---------------------------------------------------------------------------
# get_events — pagination
# ---------------------------------------------------------------------------


class TestGetEventsPagination:
    def test_limit_restricts_returned_rows(self, migrated_factory) -> None:
        for i in range(5):
            _seed(migrated_factory, message=f"event {i}")
        with session_scope(migrated_factory) as session:
            rows, total = get_events(session, limit=3)
        assert len(rows) == 3
        assert total == 5

    def test_offset_skips_rows(self, migrated_factory) -> None:
        for i in range(5):
            _seed(migrated_factory, message=f"event {i}")
        with session_scope(migrated_factory) as session:
            page1, _ = get_events(session, limit=2, offset=0)
            page2, _ = get_events(session, limit=2, offset=2)
        ids_p1 = {r.id for r in page1}
        ids_p2 = {r.id for r in page2}
        assert ids_p1.isdisjoint(ids_p2), "Pages must not overlap"

    def test_total_is_full_count_not_page_count(self, migrated_factory) -> None:
        for i in range(7):
            _seed(migrated_factory, message=f"event {i}")
        with session_scope(migrated_factory) as session:
            rows, total = get_events(session, limit=3)
        assert total == 7
        assert len(rows) == 3

    def test_results_ordered_newest_first(self, migrated_factory) -> None:
        for i in range(3):
            _seed(migrated_factory, message=f"event {i}")
        with session_scope(migrated_factory) as session:
            rows, _ = get_events(session)
        ids = [r.id for r in rows]
        assert ids == sorted(ids, reverse=True)


# ---------------------------------------------------------------------------
# get_audit_events — permission gating
# ---------------------------------------------------------------------------


class TestGetAuditEventsPermissions:
    def test_viewer_raises_permission_error(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as session, pytest.raises(PermissionError):
            get_audit_events(session, _viewer_user())

    def test_admin_can_call_without_error(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as session:
            rows, total = get_audit_events(session, _admin_user())
        assert isinstance(rows, list)
        assert isinstance(total, int)


class TestGetAuditEventsContent:
    def test_admin_gets_audit_events(self, migrated_factory) -> None:
        """Audit-typed rows surface for admin but not for viewer."""
        _seed(
            migrated_factory,
            event_type=EventType.AUDIT_CONTROL_ACTION.value,
            message="admin pressed stop",
            level="info",
        )
        with session_scope(migrated_factory) as session:
            rows, total = get_audit_events(session, _admin_user())
        assert total == 1
        assert rows[0].event_metadata["type"] == EventType.AUDIT_CONTROL_ACTION.value

    def test_admin_gets_security_events(self, migrated_factory) -> None:
        _seed(
            migrated_factory,
            event_type=EventType.SECURITY_AUTH_EVENT.value,
            message="login from unknown ip",
            level="warning",
        )
        with session_scope(migrated_factory) as session:
            rows, total = get_audit_events(session, _admin_user())
        assert total == 1

    def test_audit_query_does_not_return_operational_events(
        self, migrated_factory
    ) -> None:
        """Operational rows are not surfaced in the audit view even for admin."""
        _seed(migrated_factory, event_type=EventType.CAPTURE_GAP.value)
        _seed(migrated_factory, message="no-type operational")
        _seed(
            migrated_factory,
            event_type=EventType.AUDIT_CONTROL_ACTION.value,
            message="audit row",
        )
        with session_scope(migrated_factory) as session:
            rows, total = get_audit_events(session, _admin_user())
        assert total == 1
        assert rows[0].event_metadata["type"] == EventType.AUDIT_CONTROL_ACTION.value
