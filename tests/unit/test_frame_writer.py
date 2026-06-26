"""Unit tests for FrameWriter atomic persistence.

Covers:
- Sequence index starts at 1 for the first frame
- Sequence index increments by 1 for each subsequent frame
- File written before database row (no row on crash mid-write)
- _atomic_write: temp file removed on failure, final file present on success
- write raises ValueError when project does not exist
- Frame file has the correct extension for the format
- Written bytes on disk match the original image_bytes
- WrittenFrame metadata matches the captured frame

All tests use a real temp SQLite database (migrated_factory fixture) and
temp filesystem paths; no network access.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from timelapse_manager.cameras.base import CapturedFrame
from timelapse_manager.capture.frame_writer import FrameWriter, _atomic_write
from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Minimal 1x1 JPEG bytes (valid SOI + EOI markers; real dimensions 0x0 but
# enough for write tests where image correctness is not the concern).
_FAKE_JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t"
    b"\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
    b"\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\x1e41=>"
    b"\xff\xd9"
)


def _make_captured_frame(
    image_bytes: bytes = _FAKE_JPEG, fmt: str = "jpeg"
) -> CapturedFrame:
    return CapturedFrame(
        image_bytes=image_bytes,
        width=1,
        height=1,
        format=fmt,
        captured_at=datetime.now(UTC),
    )


def _seed_camera_and_project(
    factory, tmp_path: Path, *, storage_path: Path | None = None
) -> tuple[int, int]:
    """Insert a Camera + Project; return (camera_id, project_id)."""
    with session_scope(factory) as session:
        cam = Camera(
            name="fw-test-cam",
            address="127.0.0.1",
            protocol="http",
            snapshot_uri="http://127.0.0.1/snap",
        )
        session.add(cam)
        session.flush()
        camera_id = cam.id
        proj = Project(
            camera_id=camera_id,
            name="fw-test-project",
            lifecycle_state="active",
            operational_status="idle",
            storage_path=str(storage_path) if storage_path else None,
        )
        session.add(proj)
        session.flush()
        project_id = proj.id
    return camera_id, project_id


# ---------------------------------------------------------------------------
# _atomic_write (pure-function tests, no DB required)
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_file_written_with_correct_bytes(self, tmp_path: Path) -> None:
        final = tmp_path / "out.jpg"
        data = b"\xff\xd8\xff\xd9"
        _atomic_write(final, data)
        assert final.read_bytes() == data

    def test_final_file_exists_after_write(self, tmp_path: Path) -> None:
        final = tmp_path / "out.jpg"
        _atomic_write(final, b"data")
        assert final.exists()

    def test_no_temp_files_left_after_success(self, tmp_path: Path) -> None:
        final = tmp_path / "out.jpg"
        _atomic_write(final, b"data")
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == [], f"orphan temp files found: {tmp_files}"

    def test_temp_file_cleaned_up_on_write_failure(self, tmp_path: Path) -> None:
        final = tmp_path / "out.jpg"
        # Simulate a write error by patching os.fsync to raise
        with (
            patch("os.fsync", side_effect=OSError("simulated disk error")),
            pytest.raises(OSError),
        ):
            _atomic_write(final, b"data")
        # No temp file should remain
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []

    def test_final_file_absent_after_write_failure(self, tmp_path: Path) -> None:
        final = tmp_path / "out.jpg"
        with (
            patch("os.fsync", side_effect=OSError("simulated disk error")),
            pytest.raises(OSError),
        ):
            _atomic_write(final, b"data")
        assert not final.exists()


# ---------------------------------------------------------------------------
# FrameWriter.write
# ---------------------------------------------------------------------------


class TestFrameWriterCaptureTimestamp:
    """The captured instant round-trips through the writer into the DB column."""

    def test_capture_timestamp_persisted_from_captured_at(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        _, project_id = _seed_camera_and_project(migrated_factory, tmp_path)
        writer = FrameWriter(migrated_factory, tmp_path / "default_frames")
        when = datetime(2026, 3, 14, 9, 26, 53, tzinfo=UTC)
        captured = CapturedFrame(
            image_bytes=_FAKE_JPEG, width=1, height=1, format="jpeg", captured_at=when
        )

        result = writer.write(project_id, captured)

        with session_scope(migrated_factory) as session:
            frame = session.get(Frame, result.frame_id)
            assert frame is not None
            # Stored as naive UTC (tz stripped) equal to the captured instant.
            assert frame.capture_timestamp == when.replace(tzinfo=None)
        # The returned record echoes the effective (tz-aware) captured instant.
        assert result.captured_at == when

    def test_explicit_timestamp_overrides_captured_at(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        _, project_id = _seed_camera_and_project(migrated_factory, tmp_path)
        writer = FrameWriter(migrated_factory, tmp_path / "default_frames")
        grabbed = datetime(2026, 3, 14, 9, 0, 0, tzinfo=UTC)
        override = datetime(2026, 3, 14, 12, 30, 0, tzinfo=UTC)
        captured = CapturedFrame(
            image_bytes=_FAKE_JPEG,
            width=1,
            height=1,
            format="jpeg",
            captured_at=grabbed,
        )

        result = writer.write(project_id, captured, capture_timestamp=override)

        with session_scope(migrated_factory) as session:
            frame = session.get(Frame, result.frame_id)
            assert frame is not None
            assert frame.capture_timestamp == override.replace(tzinfo=None)


class TestFrameWriterSequenceIndex:
    def test_first_frame_gets_sequence_index_1(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        storage = tmp_path / "frames"
        storage.mkdir()
        _, project_id = _seed_camera_and_project(
            migrated_factory, tmp_path, storage_path=storage
        )
        writer = FrameWriter(migrated_factory, tmp_path / "default_frames")

        result = writer.write(project_id, _make_captured_frame())

        assert result.sequence_index == 1

    def test_second_frame_gets_sequence_index_2(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        storage = tmp_path / "frames"
        storage.mkdir()
        _, project_id = _seed_camera_and_project(
            migrated_factory, tmp_path, storage_path=storage
        )
        writer = FrameWriter(migrated_factory, tmp_path / "default_frames")

        writer.write(project_id, _make_captured_frame())
        result = writer.write(project_id, _make_captured_frame())

        assert result.sequence_index == 2

    def test_sequence_is_per_project(self, migrated_factory, tmp_path: Path) -> None:
        # Two independent projects each start at 1.
        storage_a = tmp_path / "frames_a"
        storage_a.mkdir()
        storage_b = tmp_path / "frames_b"
        storage_b.mkdir()

        with session_scope(migrated_factory) as session:
            cam = Camera(
                name="cam-for-two-projects",
                address="10.0.0.1",
                protocol="http",
                snapshot_uri="http://10.0.0.1/snap",
            )
            session.add(cam)
            session.flush()
            cam_id = cam.id
            proj_a = Project(
                camera_id=cam_id,
                name="proj-a",
                lifecycle_state="active",
                operational_status="idle",
                storage_path=str(storage_a),
            )
            proj_b = Project(
                camera_id=cam_id,
                name="proj-b",
                lifecycle_state="active",
                operational_status="idle",
                storage_path=str(storage_b),
            )
            session.add_all([proj_a, proj_b])
            session.flush()
            pid_a, pid_b = proj_a.id, proj_b.id

        writer = FrameWriter(migrated_factory, tmp_path / "root")
        r_a = writer.write(pid_a, _make_captured_frame())
        r_b = writer.write(pid_b, _make_captured_frame())

        assert r_a.sequence_index == 1
        assert r_b.sequence_index == 1


class TestFrameWriterFileOrdering:
    def test_file_exists_after_successful_write(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        storage = tmp_path / "frames"
        storage.mkdir()
        _, project_id = _seed_camera_and_project(
            migrated_factory, tmp_path, storage_path=storage
        )
        writer = FrameWriter(migrated_factory, tmp_path / "default_frames")

        result = writer.write(project_id, _make_captured_frame())

        assert Path(result.file_path).exists()

    def test_written_bytes_match_original(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        storage = tmp_path / "frames"
        storage.mkdir()
        _, project_id = _seed_camera_and_project(
            migrated_factory, tmp_path, storage_path=storage
        )
        writer = FrameWriter(migrated_factory, tmp_path / "default_frames")

        captured = _make_captured_frame()
        result = writer.write(project_id, captured)

        assert Path(result.file_path).read_bytes() == captured.image_bytes

    def test_frame_row_inserted_after_write(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        storage = tmp_path / "frames"
        storage.mkdir()
        _, project_id = _seed_camera_and_project(
            migrated_factory, tmp_path, storage_path=storage
        )
        writer = FrameWriter(migrated_factory, tmp_path / "default_frames")

        result = writer.write(project_id, _make_captured_frame())

        with session_scope(migrated_factory) as session:
            frame = session.get(Frame, result.frame_id)
        assert frame is not None
        assert frame.project_id == project_id
        assert frame.sequence_index == result.sequence_index

    def test_file_written_before_row_crash_leaves_no_row(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """Simulate a crash after file write but before DB commit.

        Patches session.flush() to raise so the transaction rolls back.
        Verifies the DB has no Frame row (the file may exist as an orphan,
        which is acceptable; a missing row pointing at a file is not).
        """
        storage = tmp_path / "frames"
        storage.mkdir()
        _, project_id = _seed_camera_and_project(
            migrated_factory, tmp_path, storage_path=storage
        )
        writer = FrameWriter(migrated_factory, tmp_path / "default_frames")

        from sqlalchemy.orm import Session as SASession

        def failing_flush(self, *args, **kwargs):
            # Raises; simulates a crash after the file is on disk but before commit.
            raise RuntimeError("simulated DB crash")

        with (
            patch.object(SASession, "flush", failing_flush),
            pytest.raises(RuntimeError, match="simulated DB crash"),
        ):
            writer.write(project_id, _make_captured_frame())

        with session_scope(migrated_factory) as session:
            count = session.query(Frame).filter_by(project_id=project_id).count()
        assert count == 0


class TestFrameWriterStreamAndSceneMetadata:
    """The writer persists stream provenance and scene metadata onto the row."""

    def test_persists_stream_id_and_scene_metadata(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        _, project_id = _seed_camera_and_project(migrated_factory, tmp_path)
        writer = FrameWriter(migrated_factory, tmp_path / "default_frames")
        envelope = {
            "schema_version": 1,
            "source": "vapix",
            "captured_resolution": "1x1",
            "brightness": "55",
        }
        captured = CapturedFrame(
            image_bytes=_FAKE_JPEG,
            width=1,
            height=1,
            format="jpeg",
            captured_at=datetime.now(UTC),
            scene_metadata=envelope,
        )

        result = writer.write(project_id, captured, stream_id="Quality")

        with session_scope(migrated_factory) as session:
            frame = session.get(Frame, result.frame_id)
            assert frame is not None
            assert frame.stream_id == "Quality"
            assert frame.scene_metadata == envelope

    def test_defaults_stream_id_and_scene_metadata_to_null(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """The upload path omits stream_id and carries no scene metadata: both
        columns read back as NULL without error."""
        _, project_id = _seed_camera_and_project(migrated_factory, tmp_path)
        writer = FrameWriter(migrated_factory, tmp_path / "default_frames")

        # No stream_id kwarg, no scene_metadata on the frame (default None).
        result = writer.write(project_id, _make_captured_frame(), origin="uploaded")

        with session_scope(migrated_factory) as session:
            frame = session.get(Frame, result.frame_id)
            assert frame is not None
            assert frame.stream_id is None
            assert frame.scene_metadata is None


class TestFrameWriterErrors:
    def test_raises_value_error_when_project_not_found(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        writer = FrameWriter(migrated_factory, tmp_path)
        with pytest.raises(ValueError, match="does not exist"):
            writer.write(99999, _make_captured_frame())


class TestFrameWriterMetadata:
    def test_written_frame_has_correct_size(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        storage = tmp_path / "frames"
        storage.mkdir()
        _, project_id = _seed_camera_and_project(
            migrated_factory, tmp_path, storage_path=storage
        )
        writer = FrameWriter(migrated_factory, tmp_path / "default_frames")

        captured = _make_captured_frame()
        result = writer.write(project_id, captured)

        assert result.file_size_bytes == len(captured.image_bytes)

    def test_jpeg_format_produces_jpg_extension(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        storage = tmp_path / "frames"
        storage.mkdir()
        _, project_id = _seed_camera_and_project(
            migrated_factory, tmp_path, storage_path=storage
        )
        writer = FrameWriter(migrated_factory, tmp_path / "default_frames")

        result = writer.write(project_id, _make_captured_frame(fmt="jpeg"))

        assert result.file_path.endswith(".jpg")

    def test_written_frame_project_id_matches(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        storage = tmp_path / "frames"
        storage.mkdir()
        _, project_id = _seed_camera_and_project(
            migrated_factory, tmp_path, storage_path=storage
        )
        writer = FrameWriter(migrated_factory, tmp_path / "default_frames")

        result = writer.write(project_id, _make_captured_frame())

        assert result.project_id == project_id
