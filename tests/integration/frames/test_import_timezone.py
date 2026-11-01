"""Import time resolution: Exif offsets, project zone, override (#67 R10).

Exif stores a local wall-clock time. These tests pin the order the importer
resolves it in: the image's own UTC offset, else the caller's ``exif_timezone``,
else the project's schedule timezone, else (a UTC project) the earlier behaviour
of taking the naive value as UTC.
"""

from __future__ import annotations

import struct
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from timelapse_manager.cameras._imageinfo import read_capture_timestamp
from timelapse_manager.config.settings import (
    CaptureSettings,
    DatabaseSettings,
    LoggingSettings,
    PathsSettings,
    Settings,
)
from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.services import frames as frame_service
from timelapse_manager.services._base import Actor
from timelapse_manager.services.errors import ValidationError
from timelapse_manager.storage import frames as frame_storage
from timelapse_manager.storage.frames import (
    InvalidExifTimezoneError,
    parse_exif_timezone,
    resolve_exif_instant,
)

_FALLBACK = datetime(2030, 1, 1, 0, 0, 0)

# ---------------------------------------------------------------------------
# Exif builder with the offset tags
# ---------------------------------------------------------------------------


def _ascii(text: str) -> bytes:
    return text.encode("ascii") + b"\x00"


def _exif_jpeg(
    original: str | None = "2026:01:15 08:30:00",
    *,
    offset_original: str | None = None,
    offset_time: str | None = None,
    big_endian: bool = False,
) -> bytes:
    """A JPEG whose Exif sub-IFD carries the given date and offset tags."""
    bo = ">" if big_endian else "<"
    entries: list[tuple[int, bytes]] = []
    if original is not None:
        entries.append((0x9003, _ascii(original)))
    if offset_time is not None:
        entries.append((0x9010, _ascii(offset_time)))
    if offset_original is not None:
        entries.append((0x9011, _ascii(offset_original)))
    entries.sort()
    exif_ifd_at = 8 + 2 + 12 + 4
    data_at = exif_ifd_at + 2 + 12 * len(entries) + 4
    ifd0 = (
        struct.pack(f"{bo}H", 1)
        + struct.pack(f"{bo}HHII", 0x8769, 4, 1, exif_ifd_at)
        + struct.pack(f"{bo}I", 0)
    )
    exif_ifd = struct.pack(f"{bo}H", len(entries))
    blob = b""
    for tag, value in entries:
        exif_ifd += struct.pack(f"{bo}HHII", tag, 2, len(value), data_at + len(blob))
        blob += value
    exif_ifd += struct.pack(f"{bo}I", 0)
    tiff = (
        (b"MM" if big_endian else b"II")
        + struct.pack(f"{bo}H", 0x002A)
        + struct.pack(f"{bo}I", 8)
        + ifd0
        + exif_ifd
        + blob
    )
    payload = b"Exif\x00\x00" + tiff
    app1 = b"\xff\xe1" + struct.pack(">H", 2 + len(payload)) + payload
    sof = (
        b"\xff\xc0"
        + struct.pack(">H", 17)
        + b"\x08"
        + struct.pack(">H", 480)
        + struct.pack(">H", 640)
        + b"\x01\x01\x11\x00"
    )
    return b"\xff\xd8" + app1 + sof + b"\xff\xd9"


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class TestReadOffsetTags:
    @pytest.mark.parametrize("big_endian", [False, True])
    def test_offset_original_makes_the_time_aware(self, big_endian: bool) -> None:
        data = _exif_jpeg(offset_original="+02:00", big_endian=big_endian)
        result = read_capture_timestamp(data)
        assert result == datetime(
            2026, 1, 15, 8, 30, tzinfo=timezone(timedelta(hours=2))
        )
        assert result is not None and result.utcoffset() == timedelta(hours=2)

    def test_negative_offset(self) -> None:
        result = read_capture_timestamp(_exif_jpeg(offset_original="-05:30"))
        assert result is not None
        assert result.utcoffset() == -timedelta(hours=5, minutes=30)

    def test_offset_time_is_the_fallback(self) -> None:
        result = read_capture_timestamp(_exif_jpeg(offset_time="+09:00"))
        assert result is not None and result.utcoffset() == timedelta(hours=9)

    def test_offset_original_wins_over_offset_time(self) -> None:
        data = _exif_jpeg(offset_original="+01:00", offset_time="+09:00")
        result = read_capture_timestamp(data)
        assert result is not None and result.utcoffset() == timedelta(hours=1)

    @pytest.mark.parametrize(
        "offset", ["      ", "+25:00", "-13:00", "+02:75", "02:00", "UTC", "+2:00"]
    )
    def test_unusable_offset_leaves_the_time_naive(self, offset: str) -> None:
        result = read_capture_timestamp(_exif_jpeg(offset_original=offset))
        assert result == datetime(2026, 1, 15, 8, 30)
        assert result is not None and result.tzinfo is None

    def test_no_offset_tags_stays_naive(self) -> None:
        result = read_capture_timestamp(_exif_jpeg())
        assert result is not None and result.tzinfo is None


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------


class TestResolveExifInstant:
    def test_aware_is_converted_to_naive_utc(self) -> None:
        aware = datetime(2026, 1, 15, 12, 0, tzinfo=timezone(timedelta(hours=2)))
        assert resolve_exif_instant(aware, None) == datetime(2026, 1, 15, 10, 0)

    def test_aware_ignores_the_zone(self) -> None:
        aware = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        zone = parse_exif_timezone("America/New_York")
        assert resolve_exif_instant(aware, zone) == datetime(2026, 1, 15, 12, 0)

    def test_naive_without_a_zone_is_unchanged(self) -> None:
        assert resolve_exif_instant(datetime(2026, 1, 15, 8, 0), None) == datetime(
            2026, 1, 15, 8, 0
        )

    def test_naive_is_read_in_the_zone_with_dst(self) -> None:
        zone = parse_exif_timezone("America/New_York")
        assert resolve_exif_instant(datetime(2026, 1, 15, 8, 0), zone) == datetime(
            2026, 1, 15, 13, 0
        )
        assert resolve_exif_instant(datetime(2026, 7, 15, 8, 0), zone) == datetime(
            2026, 7, 15, 12, 0
        )


class TestParseExifTimezone:
    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_unset(self, value: str | None) -> None:
        assert parse_exif_timezone(value) is None

    def test_valid_name(self) -> None:
        zone = parse_exif_timezone(" Europe/Berlin ")
        assert zone is not None and zone.key == "Europe/Berlin"

    @pytest.mark.parametrize(
        "value",
        [
            "Not/AZone",
            "../../etc/passwd",
            "/etc/passwd",
            "America/",
            "UTC; DROP",
            "x" * 200,
            "Europe\nBerlin",
        ],
    )
    def test_rejects_unknown_and_unsafe_names(self, value: str) -> None:
        with pytest.raises(InvalidExifTimezoneError):
            parse_exif_timezone(value)


# ---------------------------------------------------------------------------
# End to end through the storage importer
# ---------------------------------------------------------------------------


def _settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    return Settings(
        database=DatabaseSettings(url=f"sqlite:///{tmp_path / 'test.db'}"),
        logging=LoggingSettings(level="WARNING", format="text"),
        paths=PathsSettings(
            data_dir=data_dir,
            frames_root=data_dir / "frames",
            token_file=data_dir / ".local-token",
        ),
        capture=CaptureSettings(autostart=False),
    )


def _project(factory, schedule: dict | None) -> int:
    with session_scope(factory) as session:
        camera = Camera(
            name="tz-cam",
            address="127.0.0.1",
            protocol="vapix",
            snapshot_uri="http://127.0.0.1/snap",
        )
        session.add(camera)
        session.flush()
        project = Project(
            camera_id=camera.id,
            name="tz-proj",
            capture_interval_seconds=60,
            lifecycle_state="active",
            operational_status="idle",
            frame_count=0,
            schedule=schedule,
        )
        session.add(project)
        session.flush()
        return project.id


def _import(
    factory, tmp_path: Path, project_id: int, data: bytes, **kwargs: str | None
) -> datetime:
    result = frame_storage.import_frames(
        factory,
        _settings(tmp_path),
        project_id,
        [("a.jpg", data)],
        _FALLBACK,
        1,
        **kwargs,
    )
    assert result.imported_count == 1, result.skipped
    with session_scope(factory) as session:
        frame = session.query(Frame).filter_by(project_id=project_id).one()
        return frame.capture_timestamp


class TestImportResolution:
    def test_offset_in_the_image_is_converted(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        pid = _project(migrated_factory, {"timezone": "America/New_York"})
        stamp = _import(
            migrated_factory,
            tmp_path,
            pid,
            _exif_jpeg("2026:01:15 12:00:00", offset_original="+02:00"),
        )
        assert stamp == datetime(2026, 1, 15, 10, 0)

    def test_naive_time_uses_the_project_schedule_zone(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        pid = _project(migrated_factory, {"timezone": "America/New_York"})
        stamp = _import(
            migrated_factory, tmp_path, pid, _exif_jpeg("2026:01:15 08:00:00")
        )
        assert stamp == datetime(2026, 1, 15, 13, 0)

    def test_override_wins_over_the_project_zone(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        pid = _project(migrated_factory, {"timezone": "America/New_York"})
        stamp = _import(
            migrated_factory,
            tmp_path,
            pid,
            _exif_jpeg("2026:01:15 08:00:00"),
            exif_timezone="Europe/Berlin",
        )
        assert stamp == datetime(2026, 1, 15, 7, 0)

    def test_image_offset_wins_over_the_override(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        pid = _project(migrated_factory, None)
        stamp = _import(
            migrated_factory,
            tmp_path,
            pid,
            _exif_jpeg("2026:01:15 08:00:00", offset_original="+00:00"),
            exif_timezone="Asia/Tokyo",
        )
        assert stamp == datetime(2026, 1, 15, 8, 0)

    @pytest.mark.parametrize("schedule", [None, {}, {"timezone": "UTC"}])
    def test_utc_project_is_unchanged(
        self, migrated_factory, tmp_path: Path, schedule: dict | None
    ) -> None:
        pid = _project(migrated_factory, schedule)
        stamp = _import(
            migrated_factory, tmp_path, pid, _exif_jpeg("2026:01:15 08:00:00")
        )
        assert stamp == datetime(2026, 1, 15, 8, 0)

    def test_malformed_schedule_falls_back_to_utc(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        pid = _project(migrated_factory, {"timezone": "Mars/Olympus"})
        stamp = _import(
            migrated_factory, tmp_path, pid, _exif_jpeg("2026:01:15 08:00:00")
        )
        assert stamp == datetime(2026, 1, 15, 8, 0)

    def test_missing_exif_still_uses_the_fallback_and_is_inferred(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        pid = _project(migrated_factory, {"timezone": "America/New_York"})
        no_exif = b"\xff\xd8" + _exif_jpeg(None)[2:]
        stamp = _import(migrated_factory, tmp_path, pid, no_exif)
        assert stamp == _FALLBACK

    def test_existing_frames_are_not_rewritten(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        pid = _project(migrated_factory, None)
        first = _import(
            migrated_factory, tmp_path, pid, _exif_jpeg("2026:01:15 08:00:00")
        )
        with session_scope(migrated_factory) as session:
            project = session.get(Project, pid)
            assert project is not None
            project.schedule = {"timezone": "America/New_York"}
        with session_scope(migrated_factory) as session:
            assert session.query(Frame).one().capture_timestamp == first


class TestInvalidOverride:
    def test_storage_rejects_before_writing_anything(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        pid = _project(migrated_factory, None)
        with pytest.raises(InvalidExifTimezoneError):
            frame_storage.import_frames(
                migrated_factory,
                _settings(tmp_path),
                pid,
                [("a.jpg", _exif_jpeg())],
                _FALLBACK,
                1,
                exif_timezone="Not/AZone",
            )
        with session_scope(migrated_factory) as session:
            assert session.query(Frame).count() == 0
        assert not list((tmp_path / "data").rglob("*.jpg"))

    def test_service_maps_it_to_a_validation_error(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        pid = _project(migrated_factory, None)
        with pytest.raises(ValidationError):
            frame_service.import_frames(
                migrated_factory,
                _settings(tmp_path),
                Actor(user_id=1, role="operator"),
                pid,
                [("a.jpg", _exif_jpeg())],
                _FALLBACK,
                "Not/AZone",
            )
