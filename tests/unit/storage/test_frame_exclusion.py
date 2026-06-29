"""Unit tests for the frame render-exclusion state.

These pin the storage contract for the ``excluded_at`` flag:

* ``exclude`` / ``include`` set and clear the flag, idempotently, each writing a
  per-frame audit event.
* ``list_frames`` keeps excluded frames by default (they stay visible in the
  browser) and drops them only when ``include_excluded=False``.
* Exclusion is orthogonal to soft-delete: a frame can be both, and each flag is
  filtered independently.
* The encoder ``gather_frames`` omits excluded frames -- the single query that
  honours the flag.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from timelapse_manager.config.settings import (
    CaptureSettings,
    DatabaseSettings,
    LoggingSettings,
    PathsSettings,
    Settings,
)
from timelapse_manager.db.models import Camera, Event, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.encode.frame_source import gather_frames
from timelapse_manager.storage.frames import (
    FrameNotFoundError,
    exclude,
    include,
    list_frames,
)

_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_STEP = timedelta(minutes=5)
_ACTOR_USER_ID = 1  # sentinel admin


def _seed(session, n: int, *, name: str = "exc") -> tuple[int, list[int]]:
    """Seed a project with ``n`` timed frames; return (project_id, frame_ids)."""
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


class TestExcludeInclude:
    def test_exclude_sets_excluded_at(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            _, ids = _seed(s, 1)
            exclude(s, ids[0], _ACTOR_USER_ID)
        with session_scope(migrated_factory) as s:
            frame = s.get(Frame, ids[0])
        assert frame is not None
        assert frame.excluded_at is not None

    def test_include_clears_excluded_at(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            _, ids = _seed(s, 1)
            exclude(s, ids[0], _ACTOR_USER_ID)
        with session_scope(migrated_factory) as s:
            include(s, ids[0], _ACTOR_USER_ID)
        with session_scope(migrated_factory) as s:
            frame = s.get(Frame, ids[0])
        assert frame is not None
        assert frame.excluded_at is None

    def test_exclude_is_idempotent(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            _, ids = _seed(s, 1)
            exclude(s, ids[0], _ACTOR_USER_ID)
            exclude(s, ids[0], _ACTOR_USER_ID)
        with session_scope(migrated_factory) as s:
            frame = s.get(Frame, ids[0])
        assert frame is not None
        # Re-excluding re-stamps; the only invariant is that it stays excluded.
        assert frame.excluded_at is not None

    def test_include_is_idempotent(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            _, ids = _seed(s, 1)
            include(s, ids[0], _ACTOR_USER_ID)
            include(s, ids[0], _ACTOR_USER_ID)
        with session_scope(migrated_factory) as s:
            frame = s.get(Frame, ids[0])
        assert frame is not None
        assert frame.excluded_at is None

    def test_exclude_writes_audit_event(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid, ids = _seed(s, 1)
            exclude(s, ids[0], _ACTOR_USER_ID)
        with session_scope(migrated_factory) as s:
            events = (
                s.query(Event)
                .filter(Event.scope == "project")
                .filter(Event.scope_id == pid)
                .filter(Event.actor_user_id == _ACTOR_USER_ID)
                .all()
            )
        assert any(e.event_metadata.get("action") == "exclude" for e in events)

    def test_include_writes_audit_event(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid, ids = _seed(s, 1)
            include(s, ids[0], _ACTOR_USER_ID)
        with session_scope(migrated_factory) as s:
            events = (
                s.query(Event)
                .filter(Event.scope_id == pid)
                .filter(Event.actor_user_id == _ACTOR_USER_ID)
                .all()
            )
        assert any(e.event_metadata.get("action") == "include" for e in events)

    def test_exclude_unknown_frame_raises(self, migrated_factory) -> None:
        with (
            session_scope(migrated_factory) as s,
            pytest.raises(FrameNotFoundError),
        ):
            exclude(s, 99999, _ACTOR_USER_ID)

    def test_include_unknown_frame_raises(self, migrated_factory) -> None:
        with (
            session_scope(migrated_factory) as s,
            pytest.raises(FrameNotFoundError),
        ):
            include(s, 99999, _ACTOR_USER_ID)


class TestListFramesExclusionVisibility:
    def test_excluded_frame_visible_by_default(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid, ids = _seed(s, 3)
            exclude(s, ids[1], _ACTOR_USER_ID)
        with session_scope(migrated_factory) as s:
            frames = list_frames(s, pid, limit=100, offset=0)
        # Default keeps excluded frames -- they stay visible in the browser.
        assert ids[1] in {f.id for f in frames}
        assert len(frames) == 3

    def test_excluded_frame_dropped_when_include_excluded_false(
        self, migrated_factory
    ) -> None:
        with session_scope(migrated_factory) as s:
            pid, ids = _seed(s, 3)
            exclude(s, ids[1], _ACTOR_USER_ID)
        with session_scope(migrated_factory) as s:
            frames = list_frames(s, pid, limit=100, offset=0, include_excluded=False)
        assert ids[1] not in {f.id for f in frames}
        assert len(frames) == 2

    def test_both_soft_deleted_and_excluded(self, migrated_factory) -> None:
        """A frame that is both soft-deleted and excluded is filtered by each
        flag independently; restoring soft-delete preserves the exclusion bit."""
        from timelapse_manager.storage.frames import restore, soft_delete

        with session_scope(migrated_factory) as s:
            pid, ids = _seed(s, 1)
            fid = ids[0]
            exclude(s, fid, _ACTOR_USER_ID)
            soft_delete(s, fid, _ACTOR_USER_ID)

        # Hidden from the default browse path (soft-deleted), regardless of the
        # exclusion bit.
        with session_scope(migrated_factory) as s:
            default = list_frames(s, pid, limit=100, offset=0)
        assert fid not in {f.id for f in default}

        # Shown with include_deleted, even though it is excluded (exclusion does
        # not hide it in the browser).
        with session_scope(migrated_factory) as s:
            with_deleted = list_frames(
                s, pid, limit=100, offset=0, include_deleted=True
            )
        assert fid in {f.id for f in with_deleted}

        # Restoring soft-delete leaves the exclusion bit untouched.
        with session_scope(migrated_factory) as s:
            restore(s, fid, _ACTOR_USER_ID)
        with session_scope(migrated_factory) as s:
            frame = s.get(Frame, fid)
        assert frame is not None
        assert frame.lifecycle_state == "active"
        assert frame.excluded_at is not None


class TestEncoderOmitsExcluded:
    def test_gather_frames_skips_excluded(self, migrated_factory) -> None:
        settings = Settings(
            database=DatabaseSettings(url="sqlite:///:memory:"),
            logging=LoggingSettings(level="WARNING", format="text"),
            paths=PathsSettings(),
            capture=CaptureSettings(autostart=False),
        )
        with session_scope(migrated_factory) as s:
            pid, ids = _seed(s, 3)
            exclude(s, ids[1], _ACTOR_USER_ID)
        with session_scope(migrated_factory) as s:
            sequence = gather_frames(s, settings, pid)
        gathered_seqs = {ref.sequence_index for ref in sequence.frames}
        # seq 0 and 2 render; seq 1 (excluded) is omitted.
        assert gathered_seqs == {0, 2}
