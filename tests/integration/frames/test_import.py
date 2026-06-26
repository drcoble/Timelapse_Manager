"""Integration tests for frame import: EXIF parsing, chronological interleave,
re-sequencing, batch limits, and the no-overwrite guarantee for UUID-named files.

Tests are grouped into:
- TestReadCaptureTimestamp: unit-level EXIF byte parsing
- TestImportFramesEndToEnd: import_frames service layer (storage + DB)
- TestResequenceProject: resequence_project directly
- TestNoOverwriteAfterResequence: UUID file uniqueness guarantee after re-sequence
- TestImportBatchCap: 201-file batch raises ImportBatchTooLargeError
- TestBadFileSkipped: malformed file is skipped; rest still import
"""

from __future__ import annotations

import struct
from datetime import UTC, datetime
from pathlib import Path

import pytest

from timelapse_manager.cameras._imageinfo import read_capture_timestamp
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
from timelapse_manager.encode.frame_source import gather_frames
from timelapse_manager.storage import frames as frame_service
from timelapse_manager.storage.frames import ImportBatchTooLargeError
from timelapse_manager.storage.paths import frames_root as get_frames_root
from timelapse_manager.storage.paths import resolve_absolute

_UTC = UTC
_ACTOR = 1


# ---------------------------------------------------------------------------
# Minimal image builders
# ---------------------------------------------------------------------------


def _sof_bytes(width: int = 640, height: int = 480) -> bytes:
    """Return a minimal SOF0 marker so the dimension reader succeeds."""
    return (
        b"\xff\xc0"
        + struct.pack(">H", 17)
        + b"\x08"
        + struct.pack(">H", height)
        + struct.pack(">H", width)
        + b"\x01\x01\x11\x00"
    )


def _make_plain_jpeg(width: int = 640, height: int = 480) -> bytes:
    """Minimal JPEG with no EXIF segment (no APP1)."""
    return b"\xff\xd8" + _sof_bytes(width, height) + b"\xff\xd9"


def _tiff_block(
    dt_string: str, *, big_endian: bool = False, use_exif_ifd: bool = False
) -> bytes:
    """Build a minimal TIFF block embedding dt_string as an EXIF datetime.

    Layout when use_exif_ifd=False (IFD0 has tag 0x0132 directly):
        [0-1]  byte order mark
        [2-3]  magic 0x002A
        [4-7]  IFD0 offset = 8
        IFD0 at 8: entry_count=1, entry (tag,type,count,offset/value), next_ifd=0
        ASCII data immediately after IFD0

    Layout when use_exif_ifd=True (IFD0 → ExifIFD pointer → DateTimeOriginal):
        IFD0 has one entry: tag=0x8769 (ExifIFD pointer), type=LONG,
            count=1, value=<exif_offset>
        ExifIFD has one entry: tag=0x9003 (DateTimeOriginal), type=ASCII,
            count=20, value_offset=<string_offset>

    All offsets are relative to the start of this TIFF block.
    """
    bo = ">" if big_endian else "<"
    mark = b"MM" if big_endian else b"II"
    magic = struct.pack(f"{bo}H", 0x002A)
    ifd0_at = 8  # always place IFD0 at byte 8

    ascii_data = dt_string.encode() + b"\x00"  # NUL-terminated; must be 20 bytes
    assert len(ascii_data) == 20, (
        f"datetime string must produce 20 bytes including NUL, got {len(ascii_data)}"
    )

    if not use_exif_ifd:
        # IFD0: entry_count(2) + 1 entry(12) + next_ifd(4) = 18 bytes → data at 8+18=26
        entry_count = struct.pack(f"{bo}H", 1)
        string_offset = ifd0_at + 2 + 12 + 4  # = 26
        # tag=0x0132 DateTime, type=2 ASCII, count=20, value_offset=26
        entry = struct.pack(f"{bo}HHII", 0x0132, 2, 20, string_offset)
        next_ifd = struct.pack(f"{bo}I", 0)
        ifd0 = entry_count + entry + next_ifd
        return mark + magic + struct.pack(f"{bo}I", ifd0_at) + ifd0 + ascii_data
    else:
        # IFD0 has the ExifIFD pointer.
        # IFD0: 2 + 12 + 4 = 18 bytes, so ExifIFD starts at 8+18=26
        exif_ifd_at = ifd0_at + 2 + 12 + 4  # = 26
        # ExifIFD: 2 + 12 + 4 = 18 bytes, so string at 26+18=44
        string_offset = exif_ifd_at + 2 + 12 + 4  # = 44

        entry_count = struct.pack(f"{bo}H", 1)

        # IFD0 entry: tag=0x8769 ExifIFD pointer, type=4 LONG,
        # count=1, value=exif_ifd_at
        ifd0_entry = struct.pack(f"{bo}HHII", 0x8769, 4, 1, exif_ifd_at)
        next_ifd0 = struct.pack(f"{bo}I", 0)
        ifd0 = entry_count + ifd0_entry + next_ifd0

        # ExifIFD entry: tag=0x9003 DateTimeOriginal, type=2 ASCII,
        # count=20, value=string_offset
        exif_entry = struct.pack(f"{bo}HHII", 0x9003, 2, 20, string_offset)
        next_exif = struct.pack(f"{bo}I", 0)
        exif_ifd = entry_count + exif_entry + next_exif

        block = (
            mark + magic + struct.pack(f"{bo}I", ifd0_at) + ifd0 + exif_ifd + ascii_data
        )
        # Verify layout
        assert len(block) == 44 + 20, f"expected 64 bytes, got {len(block)}"
        return block


def _make_exif_jpeg(
    dt_string: str,
    *,
    big_endian: bool = False,
    use_exif_ifd: bool = False,
    width: int = 640,
    height: int = 480,
) -> bytes:
    """JPEG with SOF0 + APP1(Exif/TIFF embedding dt_string)."""
    tiff = _tiff_block(dt_string, big_endian=big_endian, use_exif_ifd=use_exif_ifd)
    app1_payload = b"Exif\x00\x00" + tiff
    app1_len = 2 + len(app1_payload)  # length field includes its own 2 bytes
    app1 = b"\xff\xe1" + struct.pack(">H", app1_len) + app1_payload
    return b"\xff\xd8" + app1 + _sof_bytes(width, height) + b"\xff\xd9"


def _make_png(width: int = 320, height: int = 240) -> bytes:
    """Minimal PNG IHDR — no EXIF."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


# ---------------------------------------------------------------------------
# Settings / project seed helpers
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


def _seed_project(migrated_factory, tmp_path: Path, name: str = "imp-proj") -> dict:
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
# TestReadCaptureTimestamp
# ---------------------------------------------------------------------------


class TestReadCaptureTimestamp:
    """Verify EXIF APP1 parsing for capture timestamps."""

    _DT_STRING = "2026:01:15 08:30:00"
    _EXPECTED = datetime(2026, 1, 15, 8, 30, 0)

    def test_le_ifd0_datetime_tag(self) -> None:
        """Little-endian TIFF with IFD0 DateTime (0x0132) tag."""
        data = _make_exif_jpeg(self._DT_STRING, big_endian=False, use_exif_ifd=False)
        result = read_capture_timestamp(data)
        assert result == self._EXPECTED

    def test_be_ifd0_datetime_tag(self) -> None:
        """Big-endian (Motorola) TIFF with IFD0 DateTime (0x0132) tag."""
        data = _make_exif_jpeg(self._DT_STRING, big_endian=True, use_exif_ifd=False)
        result = read_capture_timestamp(data)
        assert result == self._EXPECTED

    def test_le_exif_ifd_datetime_original_tag(self) -> None:
        """Little-endian TIFF via ExifIFD pointer → DateTimeOriginal (0x9003)."""
        data = _make_exif_jpeg(self._DT_STRING, big_endian=False, use_exif_ifd=True)
        result = read_capture_timestamp(data)
        assert result == self._EXPECTED

    def test_be_exif_ifd_datetime_original_tag(self) -> None:
        """Big-endian TIFF via ExifIFD pointer → DateTimeOriginal (0x9003)."""
        data = _make_exif_jpeg(self._DT_STRING, big_endian=True, use_exif_ifd=True)
        result = read_capture_timestamp(data)
        assert result == self._EXPECTED

    def test_returns_none_for_png(self) -> None:
        """PNG has no EXIF APP1 segment."""
        data = _make_png()
        assert read_capture_timestamp(data) is None

    def test_returns_none_for_jpeg_without_app1(self) -> None:
        """JPEG with no APP1 marker returns None."""
        data = _make_plain_jpeg()
        assert read_capture_timestamp(data) is None

    def test_returns_none_for_truncated_data(self) -> None:
        """Truncated bytes must not raise; returns None."""
        result = read_capture_timestamp(b"\xff\xd8\xff\xe1\x00\x20Exif\x00\x00II")
        assert result is None

    def test_returns_none_for_garbage_bytes(self) -> None:
        """Arbitrary garbage must not raise; returns None."""
        result = read_capture_timestamp(b"\x00\x01\x02\x03" * 20)
        assert result is None

    def test_returns_none_for_empty_bytes(self) -> None:
        assert read_capture_timestamp(b"") is None

    def test_returned_datetime_is_naive(self) -> None:
        """Parsed datetime must be naive (no tzinfo)."""
        data = _make_exif_jpeg(self._DT_STRING)
        result = read_capture_timestamp(data)
        assert result is not None
        assert result.tzinfo is None


# ---------------------------------------------------------------------------
# TestImportFramesEndToEnd
# ---------------------------------------------------------------------------


class TestImportFramesEndToEnd:
    """import_frames service: chronological interleave, inferred flag,
    gather_frames order."""

    _EXIF_DT = "2026:03:10 06:00:00"
    _EXIF_TIMESTAMP = datetime(2026, 3, 10, 6, 0, 0)
    _LATER_CAPTURE = datetime(2026, 3, 10, 12, 0, 0)  # later than EXIF

    def _write_captured_frame(
        self, factory, settings: Settings, project_id: int, ts: datetime
    ) -> int:
        """Pre-write a captured frame at the given timestamp; return frame id."""
        frames_root = get_frames_root(settings)
        writer = FrameWriter(factory, frames_root)
        captured = CapturedFrame(
            image_bytes=_make_plain_jpeg(),
            width=640,
            height=480,
            format="jpeg",
            captured_at=ts if ts.tzinfo else ts.replace(tzinfo=_UTC),
        )
        written = writer.write(
            project_id, captured, origin="captured", capture_timestamp=ts
        )
        return written.frame_id

    def test_import_with_exif_sets_inferred_false(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "imp-inferred-f")
        project_id = ctx["project_id"]

        jpeg_with_exif = _make_exif_jpeg(self._EXIF_DT)
        fallback = datetime(2026, 3, 15, 0, 0, 0, tzinfo=_UTC)

        result = frame_service.import_frames(
            migrated_factory,
            settings,
            project_id,
            [("with_exif.jpg", jpeg_with_exif)],
            fallback,
            _ACTOR,
        )

        assert result.imported_count == 1
        assert result.skipped_count == 0
        imported_file = result.imported[0]
        assert imported_file.inferred is False

    def test_import_without_exif_sets_inferred_true(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "imp-inferred-t")
        project_id = ctx["project_id"]

        fallback = datetime(2026, 3, 15, 0, 0, 0, tzinfo=_UTC)

        result = frame_service.import_frames(
            migrated_factory,
            settings,
            project_id,
            [("no_exif.jpg", _make_plain_jpeg())],
            fallback,
            _ACTOR,
        )

        assert result.imported_count == 1
        imported_file = result.imported[0]
        assert imported_file.inferred is True

    def test_import_with_exif_uses_exif_timestamp(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "imp-ts-exif")
        project_id = ctx["project_id"]

        jpeg_with_exif = _make_exif_jpeg(self._EXIF_DT)
        fallback = datetime(2026, 3, 15, 0, 0, 0, tzinfo=_UTC)

        result = frame_service.import_frames(
            migrated_factory,
            settings,
            project_id,
            [("with_exif.jpg", jpeg_with_exif)],
            fallback,
            _ACTOR,
        )

        assert result.imported_count == 1
        frame_id = result.imported[0].frame_id
        with session_scope(migrated_factory) as session:
            frame = session.get(Frame, frame_id)
            assert frame is not None
            assert frame.capture_timestamp == self._EXIF_TIMESTAMP
            assert frame.capture_timestamp_inferred is False

    def test_import_without_exif_uses_fallback_timestamp(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "imp-ts-fallback")
        project_id = ctx["project_id"]

        fallback = datetime(2026, 3, 15, 9, 0, 0, tzinfo=_UTC)

        result = frame_service.import_frames(
            migrated_factory,
            settings,
            project_id,
            [("no_exif.jpg", _make_plain_jpeg())],
            fallback,
            _ACTOR,
        )

        assert result.imported_count == 1
        frame_id = result.imported[0].frame_id
        with session_scope(migrated_factory) as session:
            frame = session.get(Frame, frame_id)
            assert frame is not None
            expected_ts = datetime(2026, 3, 15, 9, 0, 0)  # naive
            assert frame.capture_timestamp == expected_ts
            assert frame.capture_timestamp_inferred is True

    def test_chronological_interleave_after_import(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """An imported frame with an earlier EXIF timestamp gets a lower sequence_index
        than a pre-existing captured frame with a later timestamp."""
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "imp-interleave")
        project_id = ctx["project_id"]

        # Write one captured frame with timestamp LATER than the EXIF in the import.
        captured_frame_id = self._write_captured_frame(
            migrated_factory, settings, project_id, self._LATER_CAPTURE
        )

        # Import a frame whose EXIF timestamp is EARLIER.
        jpeg_with_exif = _make_exif_jpeg(self._EXIF_DT)
        fallback = datetime(2026, 3, 15, 0, 0, 0, tzinfo=_UTC)

        result = frame_service.import_frames(
            migrated_factory,
            settings,
            project_id,
            [("early.jpg", jpeg_with_exif)],
            fallback,
            _ACTOR,
        )

        assert result.imported_count == 1
        imported_frame_id = result.imported[0].frame_id

        with session_scope(migrated_factory) as session:
            imported = session.get(Frame, imported_frame_id)
            captured = session.get(Frame, captured_frame_id)
            assert imported is not None
            assert captured is not None
            # After re-sequence: earlier timestamp → lower sequence_index
            assert imported.sequence_index < captured.sequence_index

    def test_import_sets_origin_uploaded(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "imp-origin")
        project_id = ctx["project_id"]

        result = frame_service.import_frames(
            migrated_factory,
            settings,
            project_id,
            [("frame.jpg", _make_plain_jpeg())],
            datetime(2026, 3, 1, tzinfo=_UTC),
            _ACTOR,
        )

        assert result.imported_count == 1
        frame_id = result.imported[0].frame_id
        with session_scope(migrated_factory) as session:
            frame = session.get(Frame, frame_id)
            assert frame is not None
            assert frame.origin == "uploaded"

    def test_gather_frames_returns_chronological_order(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """After import+resequence, gather_frames returns frames in
        capture_timestamp order."""
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "imp-gather-order")
        project_id = ctx["project_id"]

        # Write two captured frames.
        later_cap = datetime(2026, 3, 10, 18, 0, 0)
        early_cap = datetime(2026, 3, 10, 6, 0, 0)
        self._write_captured_frame(migrated_factory, settings, project_id, later_cap)
        self._write_captured_frame(migrated_factory, settings, project_id, early_cap)

        # Import one frame with the earliest EXIF timestamp.
        earliest_exif = "2026:03:10 01:00:00"
        jpeg_with_exif = _make_exif_jpeg(earliest_exif)

        frame_service.import_frames(
            migrated_factory,
            settings,
            project_id,
            [("earliest.jpg", jpeg_with_exif)],
            datetime(2026, 3, 15, tzinfo=_UTC),
            _ACTOR,
        )

        # gather_frames must yield frames in chronological (timestamp) order.
        with session_scope(migrated_factory) as session:
            seq = gather_frames(session, settings, project_id)

        timestamps = [f.capture_timestamp for f in seq.frames]
        assert timestamps == sorted(timestamps)
        assert len(timestamps) == 3


# ---------------------------------------------------------------------------
# TestResequenceProject
# ---------------------------------------------------------------------------


class TestResequenceProject:
    """resequence_project: handles soft-deleted rows, null timestamps, dense result."""

    def test_resequence_returns_total_row_count(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "rseq-count")
        project_id = ctx["project_id"]

        # Write 3 frames.
        frames_root = get_frames_root(settings)
        writer = FrameWriter(migrated_factory, frames_root)
        ts_base = datetime(2026, 4, 1, 10, 0, 0, tzinfo=_UTC)
        for i in range(3):
            ts = ts_base.replace(hour=10 + i)
            cap = CapturedFrame(
                image_bytes=_make_plain_jpeg(),
                width=640,
                height=480,
                format="jpeg",
                captured_at=ts if ts.tzinfo else ts.replace(tzinfo=_UTC),
            )
            writer.write(project_id, cap, capture_timestamp=ts)

        with session_scope(migrated_factory) as session:
            k = frame_service.resequence_project(session, project_id)

        assert k == 3

    def test_resequence_produces_dense_indices(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "rseq-dense")
        project_id = ctx["project_id"]

        frames_root = get_frames_root(settings)
        writer = FrameWriter(migrated_factory, frames_root)
        ts_base = datetime(2026, 4, 2, 10, 0, 0, tzinfo=_UTC)
        frame_ids = []
        for i in range(4):
            ts = ts_base.replace(hour=6 + i * 2)
            cap = CapturedFrame(
                image_bytes=_make_plain_jpeg(),
                width=640,
                height=480,
                format="jpeg",
                captured_at=ts if ts.tzinfo else ts.replace(tzinfo=_UTC),
            )
            written = writer.write(project_id, cap, capture_timestamp=ts)
            frame_ids.append(written.frame_id)

        # Soft-delete one of the middle frames.
        with session_scope(migrated_factory) as session:
            middle = session.get(Frame, frame_ids[1])
            assert middle is not None
            middle.deleted_at = datetime(2026, 4, 3)
            session.flush()

        # Re-sequence must include the soft-deleted row and produce a dense 1..4.
        with session_scope(migrated_factory) as session:
            k = frame_service.resequence_project(session, project_id)
            indices = sorted(
                session.query(Frame.sequence_index)
                .filter(Frame.project_id == project_id)
                .all()
            )

        assert k == 4
        assert [idx for (idx,) in indices] == list(range(1, 5))

    def test_null_timestamp_rows_ordered_last(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """Frames with no capture_timestamp sort after timestamped frames
        after re-sequence."""
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "rseq-null-ts")
        project_id = ctx["project_id"]

        frames_root = get_frames_root(settings)
        writer = FrameWriter(migrated_factory, frames_root)

        # One frame with a timestamp.
        ts = datetime(2026, 4, 1, 12, 0, 0, tzinfo=_UTC)
        cap = CapturedFrame(
            image_bytes=_make_plain_jpeg(),
            width=640,
            height=480,
            format="jpeg",
            captured_at=ts,
        )
        written_ts = writer.write(project_id, cap, capture_timestamp=ts)

        # Write a second frame with a timestamp, then null it to simulate a
        # row whose capture_timestamp is genuinely absent.
        cap2 = CapturedFrame(
            image_bytes=_make_plain_jpeg(),
            width=640,
            height=480,
            format="jpeg",
            captured_at=ts,
        )
        written_null = writer.write(project_id, cap2, capture_timestamp=ts)

        # Null the timestamp directly to produce the condition under test.
        with session_scope(migrated_factory) as session:
            f = session.get(Frame, written_null.frame_id)
            assert f is not None
            f.capture_timestamp = None
            session.flush()

        # Re-sequence: null-timestamp rows must land at the highest index.
        with session_scope(migrated_factory) as session:
            frame_service.resequence_project(session, project_id)
            f_ts = session.get(Frame, written_ts.frame_id)
            f_null = session.get(Frame, written_null.frame_id)
            assert f_ts is not None and f_null is not None
            assert f_ts.sequence_index < f_null.sequence_index
            assert f_null.capture_timestamp is None


# ---------------------------------------------------------------------------
# TestNoOverwriteAfterResequence
# ---------------------------------------------------------------------------


class TestNoOverwriteAfterResequence:
    """After a re-sequence that lowers the max sequence index, a subsequent
    FrameWriter.write must not overwrite an existing on-disk file."""

    def test_write_after_resequence_creates_new_file(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "no-overwrite")
        project_id = ctx["project_id"]

        frames_root = get_frames_root(settings)
        writer = FrameWriter(migrated_factory, frames_root)

        # Write 3 frames. Collect their on-disk file paths.
        ts_base = datetime(2026, 5, 1, 10, 0, 0, tzinfo=_UTC)
        written_paths: list[Path] = []
        frame_ids = []
        for i in range(3):
            ts = ts_base.replace(hour=10 + i)
            cap = CapturedFrame(
                image_bytes=_make_plain_jpeg(),
                width=640,
                height=480,
                format="jpeg",
                captured_at=ts if ts.tzinfo else ts.replace(tzinfo=_UTC),
            )
            written = writer.write(project_id, cap, capture_timestamp=ts)
            frame_ids.append(written.frame_id)
            # Resolve actual on-disk path (stored as relative filename;
            # resolve to absolute).
            with session_scope(migrated_factory) as session:
                frame = session.get(Frame, written.frame_id)
                assert frame is not None and frame.file_path is not None
                written_paths.append(
                    resolve_absolute(settings, project_id, frame.file_path)
                )

        # Re-sequence (compacts indices back to 1..3).
        with session_scope(migrated_factory) as session:
            frame_service.resequence_project(session, project_id)

        # Write a 4th frame after re-sequence.
        ts4 = ts_base.replace(hour=14)
        cap4 = CapturedFrame(
            image_bytes=_make_plain_jpeg(),
            width=640,
            height=480,
            format="jpeg",
            captured_at=ts4 if ts4.tzinfo else ts4.replace(tzinfo=_UTC),
        )
        written4 = writer.write(project_id, cap4, capture_timestamp=ts4)

        with session_scope(migrated_factory) as session:
            frame4 = session.get(Frame, written4.frame_id)
            assert frame4 is not None and frame4.file_path is not None
            new_path = resolve_absolute(settings, project_id, frame4.file_path)

        # The new file must not be any of the existing files (UUID names differ).
        assert new_path not in written_paths
        # All previously written files must still exist (not overwritten).
        for existing_path in written_paths:
            assert existing_path.exists(), f"file overwritten: {existing_path}"


# ---------------------------------------------------------------------------
# TestImportBatchCap
# ---------------------------------------------------------------------------


class TestImportBatchCap:
    def test_raises_when_more_than_200_files(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "batch-cap")
        project_id = ctx["project_id"]

        files = [(f"frame_{i:04d}.jpg", _make_plain_jpeg()) for i in range(201)]

        with pytest.raises(ImportBatchTooLargeError):
            frame_service.import_frames(
                migrated_factory,
                settings,
                project_id,
                files,
                datetime(2026, 3, 1, tzinfo=_UTC),
                _ACTOR,
            )

    def test_exactly_200_files_does_not_raise(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "batch-200")
        project_id = ctx["project_id"]

        files = [(f"frame_{i:04d}.jpg", _make_plain_jpeg()) for i in range(200)]

        # Must not raise ImportBatchTooLargeError; may have some skips due to
        # dimensions all being identical, but that is not the concern here.
        result = frame_service.import_frames(
            migrated_factory,
            settings,
            project_id,
            files,
            datetime(2026, 3, 1, tzinfo=_UTC),
            _ACTOR,
        )
        total = result.imported_count + result.skipped_count
        assert total == 200


# ---------------------------------------------------------------------------
# TestBadFileSkipped
# ---------------------------------------------------------------------------


class TestBadFileSkipped:
    def test_bad_file_is_skipped_and_good_file_is_imported(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """A malformed file is added to skipped; remaining valid files import."""
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "bad-skip")
        project_id = ctx["project_id"]

        files = [
            ("garbage.jpg", b"\x00\x01\x02\x03garbage"),
            ("valid.jpg", _make_plain_jpeg()),
        ]

        result = frame_service.import_frames(
            migrated_factory,
            settings,
            project_id,
            files,
            datetime(2026, 3, 1, tzinfo=_UTC),
            _ACTOR,
        )

        assert result.skipped_count == 1
        assert result.imported_count == 1
        # The skipped entry must name the bad file.
        assert result.skipped[0].name == "garbage.jpg"
        assert result.skipped[0].reason is not None
