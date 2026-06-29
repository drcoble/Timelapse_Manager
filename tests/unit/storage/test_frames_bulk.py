"""Unit tests for the bulk lifecycle helpers and the bulk-summary builder.

Pins two contracts:

* The storage ``*_many`` helpers apply a lifecycle mutation across an id-set,
  **skip-not-raise**: a missing id lands in ``BulkResult.failed`` and the batch
  continues, the found ids land in ``succeeded`` and get one audit event each.
* The web summary-builder shapes a ``BulkResult`` into the response contract and
  materialises the correct inverse operation for Undo over the succeeded ids.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from timelapse_manager.db.models import Camera, Event, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.storage.frames import (
    BulkResult,
    exclude_many,
    include_many,
    restore_many,
    soft_delete_many,
)
from timelapse_manager.web.routers.frames import _build_bulk_summary

_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_STEP = timedelta(minutes=5)
_ACTOR_USER_ID = 1  # sentinel admin


def _seed(session, n: int, *, name: str = "bulk") -> tuple[int, list[int]]:
    cam = Camera(name=f"{name}-cam", address="127.0.0.1", protocol="vapix")
    session.add(cam)
    session.flush()
    proj = Project(camera_id=cam.id, name=name, lifecycle_state="active")
    session.add(proj)
    session.flush()
    ids: list[int] = []
    for i in range(n):
        frame = Frame(
            project_id=proj.id,
            sequence_index=i,
            capture_timestamp=(_T0 + _STEP * i).replace(tzinfo=None),
            file_path=f"/frames/{i:08d}.jpg",
            capture_status="captured",
            origin="captured",
            lifecycle_state="active",
        )
        session.add(frame)
        session.flush()
        ids.append(frame.id)
    session.flush()
    return proj.id, ids


class TestManyHelpersSkipNotRaise:
    def test_exclude_many_skips_bad_id(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            _pid, ids = _seed(s, 3)
            bad = 9_999_999
            result = exclude_many(s, [ids[0], bad, ids[1], ids[2]], _ACTOR_USER_ID)
        assert isinstance(result, BulkResult)
        assert result.succeeded == [ids[0], ids[1], ids[2]]
        assert result.failed == [bad]
        with session_scope(migrated_factory) as s:
            for fid in ids:
                frame = s.get(Frame, fid)
                assert frame is not None
                assert frame.excluded_at is not None

    def test_soft_delete_and_restore_many(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            _pid, ids = _seed(s, 2)
            soft_delete_many(s, ids, _ACTOR_USER_ID)
        with session_scope(migrated_factory) as s:
            for fid in ids:
                assert s.get(Frame, fid).lifecycle_state == "soft_deleted"
        with session_scope(migrated_factory) as s:
            restore_many(s, ids, _ACTOR_USER_ID)
        with session_scope(migrated_factory) as s:
            for fid in ids:
                assert s.get(Frame, fid).lifecycle_state == "active"

    def test_include_many_clears_exclusion(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            _pid, ids = _seed(s, 2)
            exclude_many(s, ids, _ACTOR_USER_ID)
            include_many(s, ids, _ACTOR_USER_ID)
        with session_scope(migrated_factory) as s:
            for fid in ids:
                assert s.get(Frame, fid).excluded_at is None

    def test_all_bad_ids_yields_no_mutation(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            _pid, _ids = _seed(s, 1)
            result = exclude_many(s, [111, 222], _ACTOR_USER_ID)
        assert result.succeeded == []
        assert result.failed == [111, 222]

    def test_one_event_per_succeeded_frame_only(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid, ids = _seed(s, 2)
            exclude_many(s, [ids[0], 9_999_998, ids[1]], _ACTOR_USER_ID)
        with session_scope(migrated_factory) as s:
            events = (
                s.query(Event)
                .filter(Event.scope == "project")
                .filter(Event.scope_id == pid)
                .filter(Event.actor_user_id == _ACTOR_USER_ID)
                .all()
            )
        exclude_events = [
            e for e in events if e.event_metadata.get("action") == "exclude"
        ]
        assert len(exclude_events) == 2
        assert {e.event_metadata.get("frame_id") for e in exclude_events} == set(ids)


class TestBulkSummaryBuilder:
    def test_shape_and_counts(self) -> None:
        result = BulkResult(succeeded=[1, 2, 3], failed=[9])
        summary = _build_bulk_summary("delete", result)
        assert summary["operation"] == "delete"
        assert summary["succeeded"] == 3
        assert summary["failed"] == 1
        assert summary["failed_ids"] == [9]
        assert summary["affected_ids"] == [1, 2, 3]

    def test_undo_is_materialised_inverse_over_succeeded(self) -> None:
        # delete -> restore; exclude -> include; and back, over the succeeded set.
        mapping = {
            "delete": "restore",
            "restore": "delete",
            "exclude": "include",
            "include": "exclude",
        }
        for op, inverse in mapping.items():
            summary = _build_bulk_summary(op, BulkResult(succeeded=[5, 6], failed=[]))
            assert summary["undo"]["operation"] == inverse
            # The inverse acts on exactly the ids that succeeded, captured now.
            assert summary["undo"]["frame_ids"] == [5, 6]

    def test_small_set_does_not_flag_window_reload(self) -> None:
        summary = _build_bulk_summary(
            "exclude", BulkResult(succeeded=[1, 2], failed=[])
        )
        assert summary["reload_window"] is False

    def test_large_set_flags_window_reload(self) -> None:
        big = list(range(1, 200))
        summary = _build_bulk_summary("exclude", BulkResult(succeeded=big, failed=[]))
        assert summary["reload_window"] is True
