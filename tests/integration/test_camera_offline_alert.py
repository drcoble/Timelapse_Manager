"""Integration tests for the camera offline/recovery alert emission.

Drives ``_attempt_capture`` / ``_capture_once`` directly (patching build_adapter
to inject a failing or working adapter) against a real migrated database, then
asserts the persisted ``event`` rows:

* crossing ``offline_failure_threshold`` consecutive failures emits exactly one
  ``camera_offline`` warning (no per-retry spam while still offline);
* the first success after the outage emits a ``camera_recovered`` info event;
* a healthy camera (successes only) emits no recovery event.
"""

from __future__ import annotations

import random
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy import select

from tests.conftest import FakeAdapter
from timelapse_manager.cameras.base import UnreachableCaptureError
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


class UnreachableAdapter(FakeAdapter):
    async def capture(self):  # type: ignore[override]
        raise UnreachableCaptureError("simulated network unreachable")

    async def close(self) -> None:  # type: ignore[override]
        return None


def _reasons_for(factory, project_id: int) -> list[str]:
    """Return the ``reason`` markers of every event for a project, oldest first."""
    with session_scope(factory) as session:
        rows = (
            session.execute(
                select(Event).where(Event.scope_id == project_id).order_by(Event.id)
            )
            .scalars()
            .all()
        )
        out: list[str] = []
        for row in rows:
            details = row.event_metadata or {}
            if isinstance(details, dict) and "reason" in details:
                out.append(str(details["reason"]))
        return out


def _seed_camera_project(factory, tmp_path: Path) -> tuple[int, int, Path]:
    storage = tmp_path / "frames"
    storage.mkdir(exist_ok=True)
    with session_scope(factory) as session:
        cam = Camera(
            name="off-cam",
            address="127.0.0.1",
            protocol="vapix",
            snapshot_uri="http://127.0.0.1/snap",
        )
        session.add(cam)
        session.flush()
        cam_id = cam.id
        proj = Project(
            camera_id=cam_id,
            name="off-proj",
            capture_interval_seconds=60,
            lifecycle_state="active",
            operational_status="idle",
            storage_path=str(storage),
        )
        session.add(proj)
        session.flush()
        return proj.id, cam_id, storage


async def _attempt(
    supervisor: CaptureSupervisor,
    target: CaptureTarget,
    state: CaptureState,
    adapter_cls: type,
) -> None:
    rng = random.Random(target.project_id)
    with (
        patch.object(supervisor, "_load_camera", return_value=MagicMock()),
        patch.object(supervisor, "_load_default_credentials", return_value=None),
        patch(
            "timelapse_manager.capture.supervisor.build_adapter",
            return_value=adapter_cls(),
        ),
    ):
        await supervisor._attempt_capture(target, state, rng)


class TestCameraOfflineAlert:
    async def test_threshold_emits_single_offline_event(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path, offline_failure_threshold=3)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        project_id, cam_id, _ = _seed_camera_project(migrated_factory, tmp_path)
        target = CaptureTarget(
            project_id=project_id,
            project_name="off-proj",
            camera_id=cam_id,
            interval_seconds=60,
        )
        state = CaptureState(project_id=project_id, camera_id=cam_id)

        # Five consecutive failures: threshold is 3.
        for _ in range(5):
            await _attempt(supervisor, target, state, UnreachableAdapter)

        reasons = _reasons_for(migrated_factory, project_id)
        # Exactly one offline event despite five failures (no spam).
        assert reasons.count("camera_offline") == 1
        assert state.offline_alerted is True
        await supervisor.stop()

    async def test_no_offline_event_below_threshold(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path, offline_failure_threshold=3)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        project_id, cam_id, _ = _seed_camera_project(migrated_factory, tmp_path)
        target = CaptureTarget(
            project_id=project_id,
            project_name="off-proj",
            camera_id=cam_id,
            interval_seconds=60,
        )
        state = CaptureState(project_id=project_id, camera_id=cam_id)

        for _ in range(2):  # below threshold
            await _attempt(supervisor, target, state, UnreachableAdapter)

        assert _reasons_for(migrated_factory, project_id).count("camera_offline") == 0
        assert state.offline_alerted is False
        await supervisor.stop()

    async def test_recovery_after_offline_emits_recovered_event(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path, offline_failure_threshold=2)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        project_id, cam_id, _ = _seed_camera_project(migrated_factory, tmp_path)
        target = CaptureTarget(
            project_id=project_id,
            project_name="off-proj",
            camera_id=cam_id,
            interval_seconds=60,
        )
        state = CaptureState(project_id=project_id, camera_id=cam_id)

        # Cross the threshold (offline), then one success (recovery).
        await _attempt(supervisor, target, state, UnreachableAdapter)
        await _attempt(supervisor, target, state, UnreachableAdapter)
        await _attempt(supervisor, target, state, FakeAdapter)

        reasons = _reasons_for(migrated_factory, project_id)
        assert reasons.count("camera_offline") == 1
        assert reasons.count("camera_recovered") == 1
        # offline emitted before recovery.
        assert reasons.index("camera_offline") < reasons.index("camera_recovered")
        assert state.offline_alerted is False
        await supervisor.stop()

    async def test_healthy_camera_emits_no_recovery(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path, offline_failure_threshold=2)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        project_id, cam_id, _ = _seed_camera_project(migrated_factory, tmp_path)
        target = CaptureTarget(
            project_id=project_id,
            project_name="off-proj",
            camera_id=cam_id,
            interval_seconds=60,
        )
        state = CaptureState(project_id=project_id, camera_id=cam_id)

        for _ in range(3):  # only successes
            await _attempt(supervisor, target, state, FakeAdapter)

        reasons = _reasons_for(migrated_factory, project_id)
        assert "camera_recovered" not in reasons
        assert "camera_offline" not in reasons
        await supervisor.stop()
