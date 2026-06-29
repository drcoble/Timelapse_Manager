"""Integration tests for frame editing, browsing, and dimension_mismatch.

Tests:
- PATCH capture_timestamp: updates field and re-orders by capture_timestamp asc
- PATCH unknown field: raises (via dict validation — service accepts datetime only)
- list_frames ordered by capture_timestamp asc
- pagination (limit/offset)
- include_deleted shows soft-deleted frames
- dimension_mismatch: True when frame dims differ from modal baseline, False otherwise
- predominant_dimensions: modal active-frame (width,height)
- dimension_mismatch: 0–1 active frames → always False
- dimension_mismatch: null dims → False
"""

from __future__ import annotations

import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from timelapse_manager.cameras.base import CapturedFrame
from timelapse_manager.capture.frame_writer import FrameWriter
from timelapse_manager.config.settings import (
    CaptureSettings,
    DatabaseSettings,
    LoggingSettings,
    PathsSettings,
    Settings,
)
from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.storage import frames as frame_service
from timelapse_manager.storage.frames import FrameNotFoundError
from timelapse_manager.storage.paths import frames_root as get_frames_root

_UTC = UTC
_ACTOR = 1


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


def _seed_project(migrated_factory, tmp_path: Path, name: str = "eb-proj") -> dict:
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


def _write_frame(
    migrated_factory,
    settings: Settings,
    project_id: int,
    captured_at: datetime,
    width: int = 640,
    height: int = 480,
) -> int:
    root = get_frames_root(settings)
    writer = FrameWriter(migrated_factory, root)
    written = writer.write(
        project_id,
        CapturedFrame(
            image_bytes=make_jpeg(width, height),
            width=width,
            height=height,
            format="jpeg",
            captured_at=captured_at,
        ),
    )
    return written.frame_id


def _write_frame_no_dims(
    migrated_factory,
    settings: Settings,
    project_id: int,
    captured_at: datetime,
) -> int:
    """Write a frame then set its width/height to None to simulate null dims."""
    root = get_frames_root(settings)
    writer = FrameWriter(migrated_factory, root)
    written = writer.write(
        project_id,
        CapturedFrame(
            image_bytes=make_jpeg(640, 480),
            width=640,
            height=480,
            format="jpeg",
            captured_at=captured_at,
        ),
    )
    with session_scope(migrated_factory) as session:
        row = session.get(Frame, written.frame_id)
        assert row is not None
        row.width = None
        row.height = None
    return written.frame_id


# ---------------------------------------------------------------------------
# Edit capture_timestamp
# ---------------------------------------------------------------------------


class TestEditCaptureTimestamp:
    def test_edit_updates_capture_timestamp(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "et-update")
        project_id = ctx["project_id"]
        frame_id = _write_frame(
            migrated_factory,
            settings,
            project_id,
            datetime(2026, 1, 1, 0, 0, 0, tzinfo=_UTC),
        )
        new_ts = datetime(2026, 6, 15, 12, 30, 0, tzinfo=_UTC)

        with session_scope(migrated_factory) as session:
            frame_service.edit_capture_timestamp(session, frame_id, new_ts, _ACTOR)

        with session_scope(migrated_factory) as session:
            frame = session.get(Frame, frame_id)
        assert frame is not None
        # Stored as naive UTC
        assert frame.capture_timestamp is not None
        assert frame.capture_timestamp.year == 2026
        assert frame.capture_timestamp.month == 6
        assert frame.capture_timestamp.day == 15

    def test_edit_unknown_frame_raises(self, migrated_factory, tmp_path: Path) -> None:
        new_ts = datetime(2026, 6, 1, tzinfo=_UTC)
        with (
            session_scope(migrated_factory) as session,
            pytest.raises(FrameNotFoundError),
        ):
            frame_service.edit_capture_timestamp(session, 99999, new_ts, _ACTOR)

    def test_edit_changes_ordering(self, migrated_factory, tmp_path: Path) -> None:
        """Editing a timestamp should change list_frames ordering."""
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "et-order")
        project_id = ctx["project_id"]
        # Write frames with ascending timestamps
        base = datetime(2026, 1, 1, tzinfo=_UTC)
        fid_a = _write_frame(migrated_factory, settings, project_id, base)
        fid_b = _write_frame(
            migrated_factory, settings, project_id, base + timedelta(hours=1)
        )

        # Move frame A to be after frame B
        with session_scope(migrated_factory) as session:
            frame_service.edit_capture_timestamp(
                session, fid_a, base + timedelta(hours=2), _ACTOR
            )

        with session_scope(migrated_factory) as session:
            frames = frame_service.list_frames(session, project_id, limit=100, offset=0)
        ids = [f.id for f in frames]
        assert ids[0] == fid_b
        assert ids[1] == fid_a


# ---------------------------------------------------------------------------
# List ordering by capture_timestamp asc
# ---------------------------------------------------------------------------


class TestListOrdering:
    def test_list_returns_frames_ordered_by_capture_timestamp_asc(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "lo-order")
        project_id = ctx["project_id"]
        base = datetime(2026, 1, 1, tzinfo=_UTC)

        # Write frames in reverse order of desired output
        fid_c = _write_frame(
            migrated_factory, settings, project_id, base + timedelta(hours=2)
        )
        fid_a = _write_frame(migrated_factory, settings, project_id, base)
        fid_b = _write_frame(
            migrated_factory, settings, project_id, base + timedelta(hours=1)
        )

        with session_scope(migrated_factory) as session:
            frames = frame_service.list_frames(session, project_id, limit=100, offset=0)

        ids = [f.id for f in frames]
        assert ids == [fid_a, fid_b, fid_c]

    def test_list_returns_empty_for_unknown_project(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        with session_scope(migrated_factory) as session:
            frames = frame_service.list_frames(session, 99999, limit=100, offset=0)
        assert frames == []


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class TestPagination:
    def test_limit_restricts_returned_count(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "pag-limit")
        project_id = ctx["project_id"]
        base = datetime(2026, 1, 1, tzinfo=_UTC)
        for i in range(5):
            _write_frame(
                migrated_factory, settings, project_id, base + timedelta(minutes=i)
            )

        with session_scope(migrated_factory) as session:
            frames = frame_service.list_frames(session, project_id, limit=3, offset=0)
        assert len(frames) == 3

    def test_offset_skips_frames(self, migrated_factory, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "pag-offset")
        project_id = ctx["project_id"]
        base = datetime(2026, 1, 1, tzinfo=_UTC)
        frame_ids = []
        for i in range(5):
            fid = _write_frame(
                migrated_factory, settings, project_id, base + timedelta(minutes=i)
            )
            frame_ids.append(fid)

        with session_scope(migrated_factory) as session:
            frames = frame_service.list_frames(session, project_id, limit=100, offset=2)
        ids = [f.id for f in frames]
        assert ids == frame_ids[2:]

    def test_limit_zero_returns_empty(self, migrated_factory, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "pag-zero")
        project_id = ctx["project_id"]
        _write_frame(
            migrated_factory, settings, project_id, datetime(2026, 1, 1, tzinfo=_UTC)
        )

        with session_scope(migrated_factory) as session:
            frames = frame_service.list_frames(session, project_id, limit=0, offset=0)
        assert frames == []


# ---------------------------------------------------------------------------
# include_deleted
# ---------------------------------------------------------------------------


class TestIncludeDeleted:
    def test_soft_deleted_hidden_by_default(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "id-hidden")
        project_id = ctx["project_id"]
        fid = _write_frame(
            migrated_factory, settings, project_id, datetime(2026, 1, 1, tzinfo=_UTC)
        )

        with session_scope(migrated_factory) as session:
            frame_service.soft_delete(session, fid, _ACTOR)

        with session_scope(migrated_factory) as session:
            frames = frame_service.list_frames(session, project_id, limit=100, offset=0)
        assert all(f.id != fid for f in frames)

    def test_soft_deleted_shown_with_flag(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "id-shown")
        project_id = ctx["project_id"]
        fid = _write_frame(
            migrated_factory, settings, project_id, datetime(2026, 1, 1, tzinfo=_UTC)
        )

        with session_scope(migrated_factory) as session:
            frame_service.soft_delete(session, fid, _ACTOR)

        with session_scope(migrated_factory) as session:
            frames = frame_service.list_frames(
                session, project_id, limit=100, offset=0, include_deleted=True
            )
        assert any(f.id == fid for f in frames)


# ---------------------------------------------------------------------------
# dimension_mismatch
# ---------------------------------------------------------------------------


class TestDimensionMismatch:
    def test_mismatch_false_when_zero_active_frames(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        ctx = _seed_project(migrated_factory, tmp_path, "dm-zero")
        project_id = ctx["project_id"]

        with session_scope(migrated_factory) as session:
            predominant = frame_service.predominant_dimensions(session, project_id)
        assert predominant is None

    def test_mismatch_false_when_one_active_frame(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """One active frame is its own baseline → never mismatched."""
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "dm-one")
        project_id = ctx["project_id"]
        fid = _write_frame(
            migrated_factory,
            settings,
            project_id,
            datetime(2026, 1, 1, tzinfo=_UTC),
            640,
            480,
        )

        with session_scope(migrated_factory) as session:
            predominant = frame_service.predominant_dimensions(session, project_id)
            frame = session.get(Frame, fid)
            result = frame_service.dimension_mismatch(frame, predominant)

        assert result is False

    def test_mismatch_false_for_majority_dims(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "dm-majority")
        project_id = ctx["project_id"]
        base = datetime(2026, 1, 1, tzinfo=_UTC)

        # 3 frames at 640x480, 1 at 1920x1080
        majority_ids = []
        for i in range(3):
            fid = _write_frame(
                migrated_factory,
                settings,
                project_id,
                base + timedelta(minutes=i),
                640,
                480,
            )
            majority_ids.append(fid)

        with session_scope(migrated_factory) as session:
            predominant = frame_service.predominant_dimensions(session, project_id)
            frame = session.get(Frame, majority_ids[0])
            result = frame_service.dimension_mismatch(frame, predominant)

        assert predominant == (640, 480)
        assert result is False

    def test_mismatch_true_for_minority_dims(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "dm-minority")
        project_id = ctx["project_id"]
        base = datetime(2026, 1, 1, tzinfo=_UTC)

        # 3 frames at 640x480 (majority), 1 at 1920x1080 (minority)
        for i in range(3):
            _write_frame(
                migrated_factory,
                settings,
                project_id,
                base + timedelta(minutes=i),
                640,
                480,
            )
        odd_id = _write_frame(
            migrated_factory,
            settings,
            project_id,
            base + timedelta(minutes=10),
            1920,
            1080,
        )

        with session_scope(migrated_factory) as session:
            predominant = frame_service.predominant_dimensions(session, project_id)
            frame = session.get(Frame, odd_id)
            result = frame_service.dimension_mismatch(frame, predominant)

        assert result is True

    def test_mismatch_false_when_dims_null(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """A frame with null dims is never considered mismatched."""
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "dm-null")
        project_id = ctx["project_id"]
        base = datetime(2026, 1, 1, tzinfo=_UTC)

        # Write 3 frames at 640x480 to establish a baseline
        for i in range(3):
            _write_frame(
                migrated_factory,
                settings,
                project_id,
                base + timedelta(minutes=i),
                640,
                480,
            )
        null_id = _write_frame_no_dims(
            migrated_factory, settings, project_id, base + timedelta(minutes=10)
        )

        with session_scope(migrated_factory) as session:
            predominant = frame_service.predominant_dimensions(session, project_id)
            frame = session.get(Frame, null_id)
            result = frame_service.dimension_mismatch(frame, predominant)

        assert result is False

    def test_mismatch_false_when_no_predominant(self) -> None:
        """Stub: if predominant is None, every frame returns False."""
        frame = Frame()
        frame.width = 1920
        frame.height = 1080
        result = frame_service.dimension_mismatch(frame, None)
        assert result is False

    def test_soft_deleted_frames_excluded_from_predominant(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """Soft-deleted frames are excluded from the baseline calculation."""
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "dm-softdel")
        project_id = ctx["project_id"]
        base = datetime(2026, 1, 1, tzinfo=_UTC)

        # 3 frames at 1920x1080 (will be soft-deleted)
        deleted_ids = []
        for i in range(3):
            fid = _write_frame(
                migrated_factory,
                settings,
                project_id,
                base + timedelta(minutes=i),
                1920,
                1080,
            )
            deleted_ids.append(fid)

        # 1 active frame at 640x480 (the only active one)
        _write_frame(
            migrated_factory,
            settings,
            project_id,
            base + timedelta(minutes=10),
            640,
            480,
        )

        # Soft-delete the 1920x1080 frames
        for fid in deleted_ids:
            with session_scope(migrated_factory) as session:
                frame_service.soft_delete(session, fid, _ACTOR)

        # Predominant should be based only on active frames: (640, 480)
        with session_scope(migrated_factory) as session:
            predominant = frame_service.predominant_dimensions(session, project_id)
        assert predominant == (640, 480)
