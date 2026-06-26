"""Integration tests for capture reconnect / exponential backoff.

Drives _attempt_capture and _capture_once directly (patching build_adapter
to inject a fake or error adapter). Does NOT wait on real asyncio.sleep.
Asserts computed state values: attempt_count, state, next_retry_at.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.conftest import ErrorAdapter, FakeAdapter
from timelapse_manager.cameras.base import UnreachableCaptureError
from timelapse_manager.capture.supervisor import (
    CaptureState,
    CaptureSupervisor,
    CaptureTarget,
    _plan_next,
)
from timelapse_manager.config.settings import (
    CaptureSettings,
    DatabaseSettings,
    LoggingSettings,
    PathsSettings,
    Settings,
)
from timelapse_manager.db.models import Camera, Project
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


def _make_settings(tmp_path: Path, **capture_kw: object) -> Settings:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    db_path = tmp_path / "test.db"
    capture = CaptureSettings(autostart=False, timeout_seconds=0.05, **capture_kw)
    return Settings(
        database=DatabaseSettings(url=f"sqlite:///{db_path}"),
        logging=LoggingSettings(level="WARNING", format="text"),
        paths=PathsSettings(
            data_dir=data_dir,
            frames_root=data_dir / "frames",
            token_file=data_dir / ".local-token",
        ),
        capture=capture,
    )


class UnreachableAdapter(ErrorAdapter):
    """Adapter that raises UnreachableCaptureError instead of OtherCaptureError."""

    async def capture(self):  # type: ignore[override]
        raise UnreachableCaptureError("simulated network unreachable")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _run_one_attempt(
    supervisor: CaptureSupervisor,
    target: CaptureTarget,
    state: CaptureState,
    adapter_cls: type,
) -> None:
    """Run exactly one _attempt_capture with the given adapter class."""
    fake_config = MagicMock()
    import random

    rng = random.Random(target.project_id)
    with (
        patch.object(supervisor, "_load_camera", return_value=fake_config),
        patch(
            "timelapse_manager.capture.supervisor.build_adapter",
            return_value=adapter_cls(),
        ),
    ):
        await supervisor._attempt_capture(target, state, rng)


# ---------------------------------------------------------------------------
# Backoff state accumulation
# ---------------------------------------------------------------------------


class TestReconnectBackoff:
    async def test_first_failure_sets_state_error(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings, MagicMock(), disk_monitor=_permissive_disk_monitor()
        )
        target = CaptureTarget(
            project_id=1, project_name="p", camera_id=1, interval_seconds=60
        )
        state = CaptureState(project_id=1, camera_id=1)

        await _run_one_attempt(supervisor, target, state, UnreachableAdapter)

        assert state.state == "error"
        await supervisor.stop()

    async def test_first_failure_increments_attempt_count(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings, MagicMock(), disk_monitor=_permissive_disk_monitor()
        )
        target = CaptureTarget(
            project_id=1, project_name="p", camera_id=1, interval_seconds=60
        )
        state = CaptureState(project_id=1, camera_id=1)

        await _run_one_attempt(supervisor, target, state, UnreachableAdapter)

        assert state.attempt_count == 1
        await supervisor.stop()

    async def test_first_failure_sets_next_retry_at(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path, backoff_base_seconds=5.0)
        supervisor = CaptureSupervisor(
            settings, MagicMock(), disk_monitor=_permissive_disk_monitor()
        )
        target = CaptureTarget(
            project_id=1, project_name="p", camera_id=1, interval_seconds=60
        )
        state = CaptureState(project_id=1, camera_id=1)

        before = datetime.now(_UTC)
        await _run_one_attempt(supervisor, target, state, UnreachableAdapter)

        assert state.next_retry_at is not None
        assert state.next_retry_at > before
        # backoff = min(5*2**0, 300) * (1 ± 0.1) = [4.5, 5.5]
        assert state.next_retry_at - before <= timedelta(seconds=10)

    async def test_multiple_failures_grow_attempt_count(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings, MagicMock(), disk_monitor=_permissive_disk_monitor()
        )
        target = CaptureTarget(
            project_id=1, project_name="p", camera_id=1, interval_seconds=60
        )
        state = CaptureState(project_id=1, camera_id=1)

        for _ in range(3):
            await _run_one_attempt(supervisor, target, state, UnreachableAdapter)

        assert state.attempt_count == 3
        assert state.last_error is not None
        await supervisor.stop()

    async def test_success_resets_attempt_count_and_state(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )

        # Seed a camera and project for the writer
        storage = tmp_path / "frames"
        storage.mkdir()
        with session_scope(migrated_factory) as session:
            cam = Camera(
                name="rc-cam",
                address="127.0.0.1",
                protocol="vapix",
                snapshot_uri="http://127.0.0.1/snap",
            )
            session.add(cam)
            session.flush()
            cam_id = cam.id
            proj = Project(
                camera_id=cam_id,
                name="rc-proj",
                capture_interval_seconds=60,
                lifecycle_state="active",
                operational_status="idle",
                storage_path=str(storage),
            )
            session.add(proj)
            session.flush()
            project_id = proj.id

        target = CaptureTarget(
            project_id=project_id,
            project_name="rc-proj",
            camera_id=cam_id,
            interval_seconds=60,
        )
        state = CaptureState(project_id=project_id, camera_id=cam_id)
        state.attempt_count = 5
        state.state = "error"
        state.next_retry_at = datetime.now(_UTC) - timedelta(seconds=1)

        fake_config = MagicMock()
        with (
            patch.object(supervisor, "_load_camera", return_value=fake_config),
            patch(
                "timelapse_manager.capture.supervisor.build_adapter",
                return_value=FakeAdapter(),
            ),
        ):
            await supervisor._capture_once(target, state)

        assert state.attempt_count == 0
        assert state.state == "running"
        assert state.next_retry_at is None
        await supervisor.stop()


# ---------------------------------------------------------------------------
# Window-closed gate blocks retry (verified via _plan_next)
# ---------------------------------------------------------------------------


class TestWindowClosedBlocksRetry:
    def test_closed_gate_waits_even_with_past_retry_at(self) -> None:
        now = datetime(2026, 6, 1, 12, 0, tzinfo=_UTC)
        # Retry is due (in the past), but gate is closed
        past_retry = now - timedelta(seconds=10)
        decision = _plan_next(
            now,
            is_open=False,
            next_change=now + timedelta(hours=4),
            interval=60.0,
            max_idle_sleep=300.0,
            last_capture=None,
            next_retry_at=past_retry,
        )
        assert decision.action == "wait"

    def test_open_gate_with_past_retry_at_captures(self) -> None:
        now = datetime(2026, 6, 1, 12, 0, tzinfo=_UTC)
        past_retry = now - timedelta(seconds=10)
        decision = _plan_next(
            now,
            is_open=True,
            next_change=None,
            interval=60.0,
            max_idle_sleep=300.0,
            last_capture=None,
            next_retry_at=past_retry,
        )
        assert decision.action == "capture"


# ---------------------------------------------------------------------------
# last_error stores message text
# ---------------------------------------------------------------------------


class TestLastError:
    async def test_last_error_set_on_failure(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings, MagicMock(), disk_monitor=_permissive_disk_monitor()
        )
        target = CaptureTarget(
            project_id=1, project_name="p", camera_id=1, interval_seconds=60
        )
        state = CaptureState(project_id=1, camera_id=1)

        await _run_one_attempt(supervisor, target, state, UnreachableAdapter)

        assert state.last_error is not None
        assert len(state.last_error) > 0
        await supervisor.stop()

    async def test_last_error_cleared_on_success(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )

        storage = tmp_path / "frames"
        storage.mkdir()
        with session_scope(migrated_factory) as session:
            cam = Camera(
                name="le-cam",
                address="127.0.0.1",
                protocol="vapix",
                snapshot_uri="http://127.0.0.1/snap",
            )
            session.add(cam)
            session.flush()
            cam_id = cam.id
            proj = Project(
                camera_id=cam_id,
                name="le-proj",
                capture_interval_seconds=60,
                lifecycle_state="active",
                operational_status="idle",
                storage_path=str(storage),
            )
            session.add(proj)
            session.flush()
            project_id = proj.id

        target = CaptureTarget(
            project_id=project_id,
            project_name="le-proj",
            camera_id=cam_id,
            interval_seconds=60,
        )
        state = CaptureState(project_id=project_id, camera_id=cam_id)
        state.last_error = "previous error"
        state.attempt_count = 2

        fake_config = MagicMock()
        with (
            patch.object(supervisor, "_load_camera", return_value=fake_config),
            patch(
                "timelapse_manager.capture.supervisor.build_adapter",
                return_value=FakeAdapter(),
            ),
        ):
            await supervisor._capture_once(target, state)

        assert state.last_error is None
        await supervisor.stop()
