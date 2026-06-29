"""Integration tests for frozen-frame detection.

Exercises _check_frozen_frame and _capture_once via patched build_adapter.
Asserts:
- Identical bytes x N triggers exactly one warning Event at threshold N.
- Capture continues (no exception raised, frames written).
- A unique frame resets the run counter.
- Detection is content-hash based (same bytes => frozen regardless of timestamp).
- frozen_frame_threshold=1 means 1 identical frame triggers a warn.
- disabled frozen detection never warns.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.conftest import FakeAdapter
from timelapse_manager.cameras.base import (
    CameraAdapter,
    CameraCapabilities,
    CapturedFrame,
    GeoLocation,
    ValidationResult,
)
from timelapse_manager.capture.supervisor import (
    CaptureState,
    CaptureSupervisor,
    CaptureTarget,
)
from timelapse_manager.config.settings import (
    CaptureSettings,
    DatabaseSettings,
    LoggingSettings,
    PathsSettings,
    Settings,
)
from timelapse_manager.db.models import Camera, Event, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.storage.monitor import DiskSpaceMonitor

_UTC = UTC


def _permissive_disk_monitor() -> DiskSpaceMonitor:
    return DiskSpaceMonitor(
        low_watermark_bytes=1,
        low_watermark_percent=0.001,
        resume_watermark_bytes=1,
        resume_watermark_percent=0.001,
        check_interval_seconds=0.0,
        get_free_bytes=lambda _p: 10**15,
        get_total_bytes=lambda _p: 10**15,
    )


_BYTES_A = FakeAdapter._BYTES
_BYTES_B = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t"
    b"\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
    b"\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\x1e42=>"
    b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
    b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b"
    b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xfb\x04\xff\xd9"
)
assert _BYTES_A != _BYTES_B, "Test bytes must differ"


class FixedBytesAdapter(CameraAdapter):
    """Adapter that always returns the same configurable bytes."""

    def __init__(self, image_bytes: bytes) -> None:
        self._bytes = image_bytes

    async def capture(self) -> CapturedFrame:
        return CapturedFrame(
            image_bytes=self._bytes,
            width=1,
            height=1,
            format="jpeg",
            captured_at=datetime.now(_UTC),
        )

    async def validate_connection(self) -> ValidationResult:  # pragma: no cover
        return ValidationResult(ok=True, reason=None, message="ok")

    async def get_geolocation(self) -> GeoLocation | None:  # pragma: no cover
        return None

    async def capabilities(self) -> CameraCapabilities:  # pragma: no cover
        return CameraCapabilities(supported_resolutions=[])

    async def close(self) -> None:
        return None


def _make_settings(
    tmp_path: Path, threshold: int = 5, enabled: bool = True
) -> Settings:
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
        capture=CaptureSettings(
            autostart=False,
            timeout_seconds=5.0,
            frozen_frame_enabled=enabled,
            frozen_frame_threshold=threshold,
        ),
    )


def _seed_project(migrated_factory, tmp_path: Path, name: str = "frozen-proj") -> dict:
    storage = tmp_path / "frames"
    storage.mkdir(exist_ok=True)
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
            storage_path=str(storage),
        )
        session.add(proj)
        session.flush()
        project_id = proj.id
    return {"camera_id": cam_id, "project_id": project_id, "storage_path": storage}


async def _capture_n_times(
    supervisor: CaptureSupervisor,
    target: CaptureTarget,
    state: CaptureState,
    image_bytes: bytes,
    n: int,
) -> None:
    fake_config = MagicMock()
    for _ in range(n):
        with (
            patch.object(supervisor, "_load_camera", return_value=fake_config),
            patch(
                "timelapse_manager.capture.supervisor.build_adapter",
                return_value=FixedBytesAdapter(image_bytes),
            ),
        ):
            await supervisor._capture_once(target, state)


def _count_events(migrated_factory, project_id: int, level: str) -> int:
    with session_scope(migrated_factory) as session:
        return (
            session.query(Event)
            .filter(Event.scope == "project")
            .filter(Event.scope_id == project_id)
            .filter(Event.level == level)
            .count()
        )


# ---------------------------------------------------------------------------
# Frozen frame detection at threshold
# ---------------------------------------------------------------------------


class TestFrozenFrameDetection:
    async def test_n_identical_frames_triggers_exactly_one_warning(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        threshold = 3
        settings = _make_settings(tmp_path, threshold=threshold)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        ctx = _seed_project(migrated_factory, tmp_path)
        project_id = ctx["project_id"]
        cam_id = ctx["camera_id"]

        target = CaptureTarget(
            project_id=project_id,
            project_name="frozen-proj",
            camera_id=cam_id,
            interval_seconds=60,
        )
        state = CaptureState(project_id=project_id, camera_id=cam_id)

        # Capture exactly threshold identical frames
        await _capture_n_times(supervisor, target, state, _BYTES_A, threshold)

        warning_count = _count_events(migrated_factory, project_id, "warning")
        assert warning_count == 1
        await supervisor.stop()

    async def test_n_minus_1_identical_frames_triggers_no_warning(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        threshold = 5
        settings = _make_settings(tmp_path, threshold=threshold)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        ctx = _seed_project(migrated_factory, tmp_path, name="frozen-proj-2")
        project_id = ctx["project_id"]
        cam_id = ctx["camera_id"]

        target = CaptureTarget(
            project_id=project_id,
            project_name="frozen-proj-2",
            camera_id=cam_id,
            interval_seconds=60,
        )
        state = CaptureState(project_id=project_id, camera_id=cam_id)

        # One short of threshold — no warning
        await _capture_n_times(supervisor, target, state, _BYTES_A, threshold - 1)

        warning_count = _count_events(migrated_factory, project_id, "warning")
        assert warning_count == 0
        await supervisor.stop()

    async def test_two_threshold_runs_triggers_two_warnings(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        threshold = 3
        settings = _make_settings(tmp_path, threshold=threshold)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        ctx = _seed_project(migrated_factory, tmp_path, name="frozen-proj-3")
        project_id = ctx["project_id"]
        cam_id = ctx["camera_id"]

        target = CaptureTarget(
            project_id=project_id,
            project_name="frozen-proj-3",
            camera_id=cam_id,
            interval_seconds=60,
        )
        state = CaptureState(project_id=project_id, camera_id=cam_id)

        # 2 × threshold identical frames → 2 events
        await _capture_n_times(supervisor, target, state, _BYTES_A, threshold * 2)

        warning_count = _count_events(migrated_factory, project_id, "warning")
        assert warning_count == 2
        await supervisor.stop()

    async def test_unique_frame_resets_run_counter(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        threshold = 3
        settings = _make_settings(tmp_path, threshold=threshold)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        ctx = _seed_project(migrated_factory, tmp_path, name="frozen-proj-4")
        project_id = ctx["project_id"]
        cam_id = ctx["camera_id"]

        target = CaptureTarget(
            project_id=project_id,
            project_name="frozen-proj-4",
            camera_id=cam_id,
            interval_seconds=60,
        )
        state = CaptureState(project_id=project_id, camera_id=cam_id)

        fake_config = MagicMock()
        # Capture (N-1) identical, then one different, then (N-1) more identical
        # Total: (N-1) + 1 + (N-1) = 2N-1 frames — no warning expected
        for _ in range(threshold - 1):
            with (
                patch.object(supervisor, "_load_camera", return_value=fake_config),
                patch(
                    "timelapse_manager.capture.supervisor.build_adapter",
                    return_value=FixedBytesAdapter(_BYTES_A),
                ),
            ):
                await supervisor._capture_once(target, state)

        # Different frame — resets counter
        with (
            patch.object(supervisor, "_load_camera", return_value=fake_config),
            patch(
                "timelapse_manager.capture.supervisor.build_adapter",
                return_value=FixedBytesAdapter(_BYTES_B),
            ),
        ):
            await supervisor._capture_once(target, state)

        for _ in range(threshold - 1):
            with (
                patch.object(supervisor, "_load_camera", return_value=fake_config),
                patch(
                    "timelapse_manager.capture.supervisor.build_adapter",
                    return_value=FixedBytesAdapter(_BYTES_A),
                ),
            ):
                await supervisor._capture_once(target, state)

        warning_count = _count_events(migrated_factory, project_id, "warning")
        assert warning_count == 0
        await supervisor.stop()

    async def test_identical_bytes_with_different_timestamp_still_frozen(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """Content-hash (not path or timestamp) drives detection."""
        threshold = 2
        settings = _make_settings(tmp_path, threshold=threshold)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        ctx = _seed_project(migrated_factory, tmp_path, name="frozen-proj-5")
        project_id = ctx["project_id"]
        cam_id = ctx["camera_id"]

        target = CaptureTarget(
            project_id=project_id,
            project_name="frozen-proj-5",
            camera_id=cam_id,
            interval_seconds=60,
        )
        state = CaptureState(project_id=project_id, camera_id=cam_id)

        fake_config = MagicMock()
        for _ in range(threshold):
            # Same bytes, different captured_at — hash is on bytes, so still frozen
            with (
                patch.object(supervisor, "_load_camera", return_value=fake_config),
                patch(
                    "timelapse_manager.capture.supervisor.build_adapter",
                    return_value=FixedBytesAdapter(_BYTES_A),
                ),
            ):
                await supervisor._capture_once(target, state)

        warning_count = _count_events(migrated_factory, project_id, "warning")
        assert warning_count == 1
        await supervisor.stop()

    async def test_capture_continues_after_frozen_warning(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        threshold = 2
        settings = _make_settings(tmp_path, threshold=threshold)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        ctx = _seed_project(migrated_factory, tmp_path, name="frozen-proj-6")
        project_id = ctx["project_id"]
        cam_id = ctx["camera_id"]

        target = CaptureTarget(
            project_id=project_id,
            project_name="frozen-proj-6",
            camera_id=cam_id,
            interval_seconds=60,
        )
        state = CaptureState(project_id=project_id, camera_id=cam_id)

        # Trigger frozen warning, then capture more frames — state remains running
        await _capture_n_times(supervisor, target, state, _BYTES_A, threshold + 2)

        assert state.state == "running"
        assert state.frames_captured == threshold + 2
        await supervisor.stop()


# ---------------------------------------------------------------------------
# Disabled detection
# ---------------------------------------------------------------------------


class TestFrozenFrameDisabled:
    async def test_disabled_detection_never_emits_warning(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path, threshold=2, enabled=False)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        ctx = _seed_project(migrated_factory, tmp_path, name="frozen-proj-7")
        project_id = ctx["project_id"]
        cam_id = ctx["camera_id"]

        target = CaptureTarget(
            project_id=project_id,
            project_name="frozen-proj-7",
            camera_id=cam_id,
            interval_seconds=60,
        )
        state = CaptureState(project_id=project_id, camera_id=cam_id)

        # Many identical frames — disabled, so no event
        await _capture_n_times(supervisor, target, state, _BYTES_A, 10)

        warning_count = _count_events(migrated_factory, project_id, "warning")
        assert warning_count == 0
        await supervisor.stop()
