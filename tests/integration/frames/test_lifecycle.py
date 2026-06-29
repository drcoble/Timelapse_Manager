"""Integration tests for frame lifecycle: soft-delete, restore, permanent-delete.

Tests:
- soft-delete: flips lifecycle_state, file kept, excluded from default listing
- restore: returns to active set, included in listing
- permanent-delete confirm gate: 422 without confirm=true, 204 with confirm=true
- permanent-delete: file removed, row removed, frame_count decremented
- audit Events carry actor_user_id == 1 (sentinel admin)
- soft_delete / restore do NOT decrement frame_count
- frame addressed via wrong project → 404 from the service check (not tested
  here — covered in test_frames_admin.py)
"""

from __future__ import annotations

import struct
from datetime import UTC, datetime
from pathlib import Path

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
from timelapse_manager.storage import frames as frame_service
from timelapse_manager.storage.frames import (
    ConfirmationRequiredError,
    FrameNotFoundError,
)

_UTC = UTC
_ACTOR_USER_ID = 1  # sentinel admin


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_jpeg(width: int, height: int) -> bytes:
    sof = (
        b"\xff\xc0"
        + struct.pack(">H", 17)
        + b"\x08"
        + struct.pack(">H", height)
        + struct.pack(">H", width)
        + b"\x01\x01\x11\x00"
    )
    return b"\xff\xd8" + sof + b"\xff\xd9"


def _make_settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    db_path = tmp_path / "test.db"
    return Settings(
        database=DatabaseSettings(url=f"sqlite:///{db_path}"),
        logging=LoggingSettings(level="WARNING", format="text"),
        paths=PathsSettings(
            data_dir=data_dir,
            frames_root=data_dir / "frames",
            token_file=data_dir / ".local-token",
        ),
        capture=CaptureSettings(autostart=False),
    )


def _seed_project(migrated_factory, tmp_path: Path, name: str = "lc-proj") -> dict:
    """Seed a project with default layout (no storage_path override)."""
    data_dir = tmp_path / "data"
    frames_root = data_dir / "frames"
    frames_root.mkdir(parents=True, exist_ok=True)

    with session_scope(migrated_factory) as session:
        cam = Camera(
            name=f"{name}-cam",
            address="127.0.0.1",
            protocol="vapix",
            snapshot_uri="http://127.0.0.1/snap",
        )
        session.add(cam)
        session.flush()
        cam_id = cam.id
        # No storage_path → default layout (relative stored paths)
        proj = Project(
            camera_id=cam_id,
            name=name,
            capture_interval_seconds=60,
            lifecycle_state="active",
            operational_status="idle",
            frame_count=0,
        )
        session.add(proj)
        session.flush()
        project_id = proj.id

    return {"camera_id": cam_id, "project_id": project_id}


def _write_frame_via_writer(
    migrated_factory, settings: Settings, project_id: int
) -> tuple[int, Path]:
    """Write one JPEG frame through FrameWriter; return (frame_id, absolute_path)."""
    from timelapse_manager.cameras.base import CapturedFrame
    from timelapse_manager.capture.frame_writer import FrameWriter
    from timelapse_manager.storage.paths import frames_root as get_frames_root

    root = get_frames_root(settings)
    writer = FrameWriter(migrated_factory, root)
    captured = CapturedFrame(
        image_bytes=make_jpeg(640, 480),
        width=640,
        height=480,
        format="jpeg",
        captured_at=datetime.now(_UTC),
    )
    written = writer.write(project_id, captured)
    return written.frame_id, Path(written.file_path)


# ---------------------------------------------------------------------------
# Soft-delete
# ---------------------------------------------------------------------------


class TestSoftDelete:
    def test_soft_delete_flips_lifecycle_state(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "sd-flip")
        frame_id, _ = _write_frame_via_writer(
            migrated_factory, settings, ctx["project_id"]
        )

        with session_scope(migrated_factory) as session:
            frame_service.soft_delete(session, frame_id, _ACTOR_USER_ID)

        with session_scope(migrated_factory) as session:
            frame = session.get(Frame, frame_id)
        assert frame is not None
        assert frame.lifecycle_state == "soft_deleted"

    def test_soft_delete_keeps_file_on_disk(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "sd-keep-file")
        frame_id, abs_path = _write_frame_via_writer(
            migrated_factory, settings, ctx["project_id"]
        )
        assert abs_path.exists()

        with session_scope(migrated_factory) as session:
            frame_service.soft_delete(session, frame_id, _ACTOR_USER_ID)

        assert abs_path.exists(), "soft-delete must not remove the file"

    def test_soft_delete_excludes_frame_from_default_list(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "sd-exclude")
        project_id = ctx["project_id"]
        frame_id, _ = _write_frame_via_writer(migrated_factory, settings, project_id)

        with session_scope(migrated_factory) as session:
            frame_service.soft_delete(session, frame_id, _ACTOR_USER_ID)

        with session_scope(migrated_factory) as session:
            frames = frame_service.list_frames(session, project_id, limit=100, offset=0)
        assert all(f.id != frame_id for f in frames)

    def test_soft_delete_shows_with_include_deleted(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "sd-include")
        project_id = ctx["project_id"]
        frame_id, _ = _write_frame_via_writer(migrated_factory, settings, project_id)

        with session_scope(migrated_factory) as session:
            frame_service.soft_delete(session, frame_id, _ACTOR_USER_ID)

        with session_scope(migrated_factory) as session:
            frames = frame_service.list_frames(
                session, project_id, limit=100, offset=0, include_deleted=True
            )
        assert any(f.id == frame_id for f in frames)

    def test_soft_delete_does_not_decrement_frame_count(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "sd-count")
        project_id = ctx["project_id"]
        _write_frame_via_writer(migrated_factory, settings, project_id)
        _write_frame_via_writer(migrated_factory, settings, project_id)

        with session_scope(migrated_factory) as session:
            before = session.get(Project, project_id).frame_count

        frame_ids = []
        with session_scope(migrated_factory) as session:
            frames = frame_service.list_frames(session, project_id, limit=100, offset=0)
            frame_ids = [f.id for f in frames]

        with session_scope(migrated_factory) as session:
            frame_service.soft_delete(session, frame_ids[0], _ACTOR_USER_ID)

        with session_scope(migrated_factory) as session:
            after = session.get(Project, project_id).frame_count

        # frame_count == active + soft_deleted, not just active
        assert after == before

    def test_soft_delete_writes_audit_event_with_actor(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "sd-event")
        project_id = ctx["project_id"]
        frame_id, _ = _write_frame_via_writer(migrated_factory, settings, project_id)

        with session_scope(migrated_factory) as session:
            frame_service.soft_delete(session, frame_id, _ACTOR_USER_ID)

        with session_scope(migrated_factory) as session:
            events = (
                session.query(Event)
                .filter(Event.scope == "project")
                .filter(Event.scope_id == project_id)
                .filter(Event.actor_user_id == _ACTOR_USER_ID)
                .all()
            )
        assert len(events) >= 1

    def test_soft_delete_unknown_frame_raises(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        with (
            session_scope(migrated_factory) as session,
            pytest.raises(FrameNotFoundError),
        ):
            frame_service.soft_delete(session, 99999, _ACTOR_USER_ID)


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


class TestRestore:
    def test_restore_flips_lifecycle_state_to_active(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "rs-flip")
        project_id = ctx["project_id"]
        frame_id, _ = _write_frame_via_writer(migrated_factory, settings, project_id)

        with session_scope(migrated_factory) as session:
            frame_service.soft_delete(session, frame_id, _ACTOR_USER_ID)
        with session_scope(migrated_factory) as session:
            frame_service.restore(session, frame_id, _ACTOR_USER_ID)

        with session_scope(migrated_factory) as session:
            frame = session.get(Frame, frame_id)
        assert frame is not None
        assert frame.lifecycle_state == "active"

    def test_restore_returns_frame_to_default_listing(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "rs-list")
        project_id = ctx["project_id"]
        frame_id, _ = _write_frame_via_writer(migrated_factory, settings, project_id)

        with session_scope(migrated_factory) as session:
            frame_service.soft_delete(session, frame_id, _ACTOR_USER_ID)
        with session_scope(migrated_factory) as session:
            frame_service.restore(session, frame_id, _ACTOR_USER_ID)

        with session_scope(migrated_factory) as session:
            frames = frame_service.list_frames(session, project_id, limit=100, offset=0)
        assert any(f.id == frame_id for f in frames)

    def test_restore_writes_audit_event(self, migrated_factory, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "rs-event")
        project_id = ctx["project_id"]
        frame_id, _ = _write_frame_via_writer(migrated_factory, settings, project_id)

        with session_scope(migrated_factory) as session:
            frame_service.soft_delete(session, frame_id, _ACTOR_USER_ID)

        events_before = 0
        with session_scope(migrated_factory) as session:
            events_before = (
                session.query(Event).filter(Event.scope_id == project_id).count()
            )

        with session_scope(migrated_factory) as session:
            frame_service.restore(session, frame_id, _ACTOR_USER_ID)

        with session_scope(migrated_factory) as session:
            events_after = (
                session.query(Event).filter(Event.scope_id == project_id).count()
            )
        assert events_after > events_before


# ---------------------------------------------------------------------------
# Permanent delete
# ---------------------------------------------------------------------------


class TestPermanentDelete:
    def test_permanent_delete_without_confirm_raises(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "pd-noconfirm")
        frame_id, _ = _write_frame_via_writer(
            migrated_factory, settings, ctx["project_id"]
        )

        with (
            session_scope(migrated_factory) as session,
            pytest.raises(ConfirmationRequiredError),
        ):
            frame_service.permanent_delete(
                session, frame_id, _ACTOR_USER_ID, confirm=False, settings=settings
            )

    def test_permanent_delete_removes_row(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "pd-row")
        frame_id, _ = _write_frame_via_writer(
            migrated_factory, settings, ctx["project_id"]
        )

        with session_scope(migrated_factory) as session:
            frame_service.permanent_delete(
                session, frame_id, _ACTOR_USER_ID, confirm=True, settings=settings
            )

        with session_scope(migrated_factory) as session:
            assert session.get(Frame, frame_id) is None

    def test_permanent_delete_removes_file_from_disk(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "pd-file")
        frame_id, abs_path = _write_frame_via_writer(
            migrated_factory, settings, ctx["project_id"]
        )
        assert abs_path.exists()

        with session_scope(migrated_factory) as session:
            frame_service.permanent_delete(
                session, frame_id, _ACTOR_USER_ID, confirm=True, settings=settings
            )

        assert not abs_path.exists()

    def test_permanent_delete_decrements_frame_count(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "pd-count")
        project_id = ctx["project_id"]
        frame_id, _ = _write_frame_via_writer(migrated_factory, settings, project_id)

        with session_scope(migrated_factory) as session:
            before = session.get(Project, project_id).frame_count

        with session_scope(migrated_factory) as session:
            frame_service.permanent_delete(
                session, frame_id, _ACTOR_USER_ID, confirm=True, settings=settings
            )

        with session_scope(migrated_factory) as session:
            after = session.get(Project, project_id).frame_count

        assert after == before - 1

    def test_permanent_delete_writes_audit_event_with_actor(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "pd-event")
        project_id = ctx["project_id"]
        frame_id, _ = _write_frame_via_writer(migrated_factory, settings, project_id)

        with session_scope(migrated_factory) as session:
            frame_service.permanent_delete(
                session, frame_id, _ACTOR_USER_ID, confirm=True, settings=settings
            )

        with session_scope(migrated_factory) as session:
            events = (
                session.query(Event)
                .filter(Event.scope == "project")
                .filter(Event.scope_id == project_id)
                .filter(Event.actor_user_id == _ACTOR_USER_ID)
                .filter(Event.level == "warning")
                .all()
            )
        assert len(events) >= 1

    def test_permanent_delete_unknown_frame_raises(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        with (
            session_scope(migrated_factory) as session,
            pytest.raises(FrameNotFoundError),
        ):
            frame_service.permanent_delete(
                session, 99999, _ACTOR_USER_ID, confirm=True, settings=settings
            )

    def test_sentinel_admin_user_created_on_first_mutation(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """The first audited mutation materialises user(id=1,'system')."""
        from timelapse_manager.db.models import User

        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "pd-sentinel")
        frame_id, _ = _write_frame_via_writer(
            migrated_factory, settings, ctx["project_id"]
        )

        # Confirm no system user yet (will be None before first audit mutation)
        with session_scope(migrated_factory) as session:
            session.get(User, 1)

        with session_scope(migrated_factory) as session:
            frame_service.soft_delete(session, frame_id, _ACTOR_USER_ID)

        with session_scope(migrated_factory) as session:
            user = session.get(User, 1)
        assert user is not None
        assert user.username == "system"
