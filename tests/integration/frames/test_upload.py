"""Integration tests for frame upload via the storage.frames.upload_frame service.

Tests:
- JPEG upload: origin="uploaded", dims read, seq=max+1, file on disk, Event written
- PNG upload: same assertions
- Invalid bytes → InvalidImageError, no row, no file
- declared format/bytes mismatch → InvalidImageError
- seq continuity: upload after captured frames
"""

from __future__ import annotations

import contextlib
import struct
from datetime import UTC, datetime
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
from timelapse_manager.db.models import Camera, Event, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.storage import frames as frame_service
from timelapse_manager.storage.frames import InvalidImageError
from timelapse_manager.storage.paths import (
    frames_root as get_frames_root,
)
from timelapse_manager.storage.paths import (
    resolve_absolute,
)

_UTC = UTC
_ACTOR = 1


# ---------------------------------------------------------------------------
# Minimal valid image helpers (from the task spec)
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


def make_png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _seed_project(migrated_factory, tmp_path: Path, name: str = "up-proj") -> dict:
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


# ---------------------------------------------------------------------------
# JPEG upload
# ---------------------------------------------------------------------------


class TestJpegUpload:
    def test_jpeg_upload_creates_frame_with_correct_origin(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "up-jpeg-origin")
        project_id = ctx["project_id"]
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=_UTC)

        frame = frame_service.upload_frame(
            migrated_factory,
            settings,
            project_id,
            make_jpeg(640, 480),
            "jpeg",
            ts,
            _ACTOR,
        )

        assert frame.origin == "uploaded"

    def test_jpeg_upload_reads_width_and_height(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "up-jpeg-dims")
        project_id = ctx["project_id"]
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=_UTC)

        frame = frame_service.upload_frame(
            migrated_factory,
            settings,
            project_id,
            make_jpeg(1920, 1080),
            "jpeg",
            ts,
            _ACTOR,
        )

        assert frame.width == 1920
        assert frame.height == 1080

    def test_jpeg_upload_sequence_is_max_plus_1(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "up-jpeg-seq")
        project_id = ctx["project_id"]

        # Pre-write 2 captured frames
        root = get_frames_root(settings)
        writer = FrameWriter(migrated_factory, root)
        for _ in range(2):
            writer.write(
                project_id,
                CapturedFrame(
                    image_bytes=make_jpeg(640, 480),
                    width=640,
                    height=480,
                    format="jpeg",
                    captured_at=datetime.now(_UTC),
                ),
            )

        ts = datetime(2026, 3, 1, 12, 0, 0, tzinfo=_UTC)
        frame = frame_service.upload_frame(
            migrated_factory,
            settings,
            project_id,
            make_jpeg(640, 480),
            "jpeg",
            ts,
            _ACTOR,
        )

        assert frame.sequence_index == 3

    def test_jpeg_upload_file_written_to_disk(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "up-jpeg-disk")
        project_id = ctx["project_id"]
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=_UTC)

        frame = frame_service.upload_frame(
            migrated_factory,
            settings,
            project_id,
            make_jpeg(640, 480),
            "jpeg",
            ts,
            _ACTOR,
        )

        # Resolve the stored path to an absolute path and check it exists
        with session_scope(migrated_factory) as session:
            row = session.get(Frame, frame.id)
            assert row is not None
            abs_path = resolve_absolute(settings, project_id, row.file_path)
        assert abs_path.exists()

    def test_jpeg_upload_writes_audit_event(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "up-jpeg-event")
        project_id = ctx["project_id"]
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=_UTC)

        frame_service.upload_frame(
            migrated_factory,
            settings,
            project_id,
            make_jpeg(640, 480),
            "jpeg",
            ts,
            _ACTOR,
        )

        with session_scope(migrated_factory) as session:
            events = (
                session.query(Event)
                .filter(Event.scope_id == project_id)
                .filter(Event.actor_user_id == _ACTOR)
                .all()
            )
        assert len(events) >= 1


# ---------------------------------------------------------------------------
# PNG upload
# ---------------------------------------------------------------------------


class TestPngUpload:
    def test_png_upload_creates_frame_with_correct_origin(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "up-png-origin")
        project_id = ctx["project_id"]
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=_UTC)

        frame = frame_service.upload_frame(
            migrated_factory,
            settings,
            project_id,
            make_png(320, 240),
            "png",
            ts,
            _ACTOR,
        )

        assert frame.origin == "uploaded"

    def test_png_upload_reads_dimensions(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "up-png-dims")
        project_id = ctx["project_id"]
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=_UTC)

        frame = frame_service.upload_frame(
            migrated_factory,
            settings,
            project_id,
            make_png(800, 600),
            "png",
            ts,
            _ACTOR,
        )

        assert frame.width == 800
        assert frame.height == 600

    def test_png_upload_file_written_to_disk(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "up-png-disk")
        project_id = ctx["project_id"]
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=_UTC)

        frame = frame_service.upload_frame(
            migrated_factory,
            settings,
            project_id,
            make_png(320, 240),
            "png",
            ts,
            _ACTOR,
        )

        with session_scope(migrated_factory) as session:
            row = session.get(Frame, frame.id)
            assert row is not None
            abs_path = resolve_absolute(settings, project_id, row.file_path)
        assert abs_path.exists()


# ---------------------------------------------------------------------------
# Invalid bytes
# ---------------------------------------------------------------------------


class TestInvalidUpload:
    def test_invalid_bytes_raises_invalid_image_error(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "up-invalid-bytes")
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=_UTC)

        with pytest.raises(InvalidImageError):
            frame_service.upload_frame(
                migrated_factory,
                settings,
                ctx["project_id"],
                b"this is not an image",
                None,
                ts,
                _ACTOR,
            )

    def test_invalid_bytes_leaves_no_row(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "up-no-row")
        project_id = ctx["project_id"]
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=_UTC)

        with contextlib.suppress(InvalidImageError):
            frame_service.upload_frame(
                migrated_factory, settings, project_id, b"garbage", None, ts, _ACTOR
            )

        with session_scope(migrated_factory) as session:
            count = session.query(Frame).filter(Frame.project_id == project_id).count()
        assert count == 0

    def test_format_mismatch_raises_invalid_image_error(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """Declared format=jpeg but bytes are PNG → InvalidImageError."""
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "up-mismatch")
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=_UTC)

        with pytest.raises(InvalidImageError, match="mismatch|declared|format"):
            frame_service.upload_frame(
                migrated_factory,
                settings,
                ctx["project_id"],
                make_png(320, 240),  # PNG bytes
                "jpeg",  # declared as JPEG → mismatch
                ts,
                _ACTOR,
            )

    def test_format_mismatch_leaves_no_row(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "up-mismatch-norow")
        project_id = ctx["project_id"]
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=_UTC)

        with contextlib.suppress(InvalidImageError):
            frame_service.upload_frame(
                migrated_factory,
                settings,
                project_id,
                make_png(320, 240),
                "jpeg",
                ts,
                _ACTOR,
            )

        with session_scope(migrated_factory) as session:
            count = session.query(Frame).filter(Frame.project_id == project_id).count()
        assert count == 0

    def test_upload_without_declared_format_accepts_valid_bytes(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """No declared format: autodetect from bytes."""
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "up-no-fmt")
        project_id = ctx["project_id"]
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=_UTC)

        frame = frame_service.upload_frame(
            migrated_factory,
            settings,
            project_id,
            make_jpeg(320, 240),
            None,
            ts,
            _ACTOR,
        )

        assert frame.id is not None
        assert frame.origin == "uploaded"
