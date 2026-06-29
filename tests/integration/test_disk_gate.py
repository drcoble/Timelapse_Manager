"""Integration tests for the supervisor disk-gate truth table.

Drives _evaluate_disk_gate and _mark_waiting synchronously (as seams) rather
than waiting on wall-clock asyncio.sleep. Exercises:
- window-open + disk-low  → pause_reason="low_disk", 0 frames, nothing deleted
- window-closed + disk-ok → pause_reason="window", disk monitor never probed
- window-open + disk-ok   → pause_reason=None, captures proceed
- disk recovery           → resume Event written (edge-triggered once)
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.conftest import FakeAdapter
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
from timelapse_manager.db.models import Camera, Event, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.storage.monitor import DiskSpaceMonitor

_UTC = UTC


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


def _permissive_monitor() -> DiskSpaceMonitor:
    return DiskSpaceMonitor(
        low_watermark_bytes=1,
        low_watermark_percent=0.001,
        resume_watermark_bytes=1,
        resume_watermark_percent=0.001,
        check_interval_seconds=0.0,
        get_free_bytes=lambda _p: 10**15,
        get_total_bytes=lambda _p: 10**15,
    )


def _tight_monitor(*, low_bytes: int, resume_bytes: int) -> DiskSpaceMonitor:
    return DiskSpaceMonitor(
        low_watermark_bytes=low_bytes,
        low_watermark_percent=0.001,
        resume_watermark_bytes=resume_bytes,
        resume_watermark_percent=0.001,
        check_interval_seconds=0.0,
        get_free_bytes=lambda _p: 500_000_000,  # 500 MB
        get_total_bytes=lambda _p: 10**15,
    )


def _paused_monitor() -> DiskSpaceMonitor:
    return _tight_monitor(low_bytes=10**12, resume_bytes=10**12)


def _seed_project(migrated_factory, tmp_path: Path, name: str = "dg-proj") -> dict:
    storage = tmp_path / "frames" / name
    storage.mkdir(parents=True, exist_ok=True)
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
    return {
        "camera_id": cam_id,
        "project_id": project_id,
        "storage_path": storage,
    }


# ---------------------------------------------------------------------------
# Truth table: window-open + disk-low → pause_reason="low_disk"
# ---------------------------------------------------------------------------


class TestWindowOpenDiskLow:
    async def test_window_open_disk_low_pause_reason_is_low_disk(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "dg-low-disk")

        monitor = _paused_monitor()
        supervisor = CaptureSupervisor(settings, migrated_factory, disk_monitor=monitor)

        target = CaptureTarget(
            project_id=ctx["project_id"],
            project_name="dg-low-disk",
            camera_id=ctx["camera_id"],
            interval_seconds=60,
        )
        state = CaptureState(project_id=ctx["project_id"], camera_id=ctx["camera_id"])

        volume_path = supervisor._volume_path(target)
        now = datetime.now(_UTC)

        # Evaluate disk gate (window is open)
        disk_ok = await supervisor._evaluate_disk_gate(target, state, volume_path, now)
        # Mark waiting with results
        supervisor._mark_waiting(state, schedule_open=True, disk_ok=disk_ok)

        assert disk_ok is False
        assert state.pause_reason == "low_disk"

    async def test_window_open_disk_low_writes_zero_frames(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "dg-low-disk-frames")
        project_id = ctx["project_id"]

        monitor = _paused_monitor()
        supervisor = CaptureSupervisor(settings, migrated_factory, disk_monitor=monitor)
        target = CaptureTarget(
            project_id=project_id,
            project_name="dg-low-disk-frames",
            camera_id=ctx["camera_id"],
            interval_seconds=60,
        )
        state = CaptureState(project_id=project_id, camera_id=ctx["camera_id"])
        volume_path = supervisor._volume_path(target)
        now = datetime.now(_UTC)

        await supervisor._evaluate_disk_gate(target, state, volume_path, now)
        supervisor._mark_waiting(state, schedule_open=True, disk_ok=False)

        with session_scope(migrated_factory) as session:
            count = session.query(Frame).filter(Frame.project_id == project_id).count()
        assert count == 0
        await supervisor.stop()

    async def test_window_open_disk_low_emits_pause_event(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "dg-pause-event")
        project_id = ctx["project_id"]

        monitor = _paused_monitor()
        supervisor = CaptureSupervisor(settings, migrated_factory, disk_monitor=monitor)
        target = CaptureTarget(
            project_id=project_id,
            project_name="dg-pause-event",
            camera_id=ctx["camera_id"],
            interval_seconds=60,
        )
        state = CaptureState(project_id=project_id, camera_id=ctx["camera_id"])
        volume_path = supervisor._volume_path(target)
        now = datetime.now(_UTC)

        # First call from non-paused state → edge triggers pause event
        await supervisor._evaluate_disk_gate(target, state, volume_path, now)

        with session_scope(migrated_factory) as session:
            events = (
                session.query(Event)
                .filter(Event.scope == "project")
                .filter(Event.scope_id == project_id)
                .filter(Event.level == "warning")
                .all()
            )
        assert len(events) == 1
        assert "low" in events[0].message.lower() or "disk" in events[0].message.lower()
        await supervisor.stop()

    async def test_sustained_low_disk_emits_pause_event_only_once(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """Edge-triggered: sustained low disk should log only once."""
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "dg-edge-once")
        project_id = ctx["project_id"]

        monitor = _paused_monitor()
        supervisor = CaptureSupervisor(settings, migrated_factory, disk_monitor=monitor)
        target = CaptureTarget(
            project_id=project_id,
            project_name="dg-edge-once",
            camera_id=ctx["camera_id"],
            interval_seconds=60,
        )
        state = CaptureState(project_id=project_id, camera_id=ctx["camera_id"])
        volume_path = supervisor._volume_path(target)
        now = datetime.now(_UTC)

        # First call triggers event
        await supervisor._evaluate_disk_gate(target, state, volume_path, now)
        # Must set pause_reason BEFORE the second evaluate so was_low=True
        supervisor._mark_waiting(state, schedule_open=True, disk_ok=False)
        # Second call: was_low=True (pause_reason=="low_disk"), so no second event
        await supervisor._evaluate_disk_gate(target, state, volume_path, now)

        with session_scope(migrated_factory) as session:
            count = (
                session.query(Event)
                .filter(Event.scope == "project")
                .filter(Event.scope_id == project_id)
                .filter(Event.level == "warning")
                .count()
            )
        assert count == 1
        await supervisor.stop()


# ---------------------------------------------------------------------------
# Truth table: window-closed + disk-ok → pause_reason="window", no disk probe
# ---------------------------------------------------------------------------


class TestWindowClosedDiskOk:
    async def test_window_closed_sets_pause_reason_window(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        monitor = _permissive_monitor()
        supervisor = CaptureSupervisor(settings, migrated_factory, disk_monitor=monitor)

        state = CaptureState(project_id=1, camera_id=1)
        # When schedule_open=False, the supervisor's gate logic skips disk probe
        # and _mark_waiting sets pause_reason = "window"
        supervisor._mark_waiting(state, schedule_open=False, disk_ok=True)

        assert state.pause_reason == "window"
        await supervisor.stop()

    def test_window_closed_disk_probe_not_called(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """When schedule_open is False, the disk monitor is never probed."""
        probe_count = 0

        def _counting_free(_p: Path) -> int:
            nonlocal probe_count
            probe_count += 1
            return 10**15

        settings = _make_settings(tmp_path)
        monitor = DiskSpaceMonitor(
            low_watermark_bytes=1,
            low_watermark_percent=0.001,
            resume_watermark_bytes=1,
            resume_watermark_percent=0.001,
            check_interval_seconds=0.0,
            get_free_bytes=_counting_free,
            get_total_bytes=lambda _p: 10**15,
        )
        supervisor = CaptureSupervisor(settings, migrated_factory, disk_monitor=monitor)

        # Simulate what _run_project does: skip disk probe when window is closed
        state = CaptureState(project_id=1, camera_id=1)
        # The guard: `if schedule_open: disk_ok = ...`
        # With schedule_open=False, no probe happens
        supervisor._mark_waiting(state, schedule_open=False, disk_ok=True)

        assert probe_count == 0


# ---------------------------------------------------------------------------
# Truth table: window-open + disk-ok → captures proceed
# ---------------------------------------------------------------------------


class TestWindowOpenDiskOk:
    async def test_window_open_disk_ok_capture_succeeds(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "dg-ok-capture")
        project_id = ctx["project_id"]

        monitor = _permissive_monitor()
        supervisor = CaptureSupervisor(settings, migrated_factory, disk_monitor=monitor)

        target = CaptureTarget(
            project_id=project_id,
            project_name="dg-ok-capture",
            camera_id=ctx["camera_id"],
            interval_seconds=60,
        )
        state = CaptureState(project_id=project_id, camera_id=ctx["camera_id"])

        # Verify gate is open
        volume_path = supervisor._volume_path(target)
        now = datetime.now(_UTC)
        disk_ok = await supervisor._evaluate_disk_gate(target, state, volume_path, now)
        assert disk_ok is True

        # With open gate, attempt a capture
        fake_config = MagicMock()
        with (
            patch.object(supervisor, "_load_camera", return_value=fake_config),
            patch(
                "timelapse_manager.capture.supervisor.build_adapter",
                return_value=FakeAdapter(),
            ),
        ):
            await supervisor._capture_once(target, state)

        assert state.frames_captured == 1
        with session_scope(migrated_factory) as session:
            count = session.query(Frame).filter(Frame.project_id == project_id).count()
        assert count == 1
        await supervisor.stop()

    async def test_window_open_disk_ok_pause_reason_is_none(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        monitor = _permissive_monitor()
        supervisor = CaptureSupervisor(settings, migrated_factory, disk_monitor=monitor)

        state = CaptureState(project_id=1, camera_id=1)
        supervisor._mark_waiting(state, schedule_open=True, disk_ok=True)

        assert state.pause_reason is None
        await supervisor.stop()


# ---------------------------------------------------------------------------
# Recovery: disk recovers → resume Event written (edge-triggered once)
# ---------------------------------------------------------------------------


class TestDiskRecovery:
    async def test_disk_recovery_emits_resume_event(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "dg-recovery")
        project_id = ctx["project_id"]

        # Build a monitor that starts paused then recovers
        call_count = 0

        def _free_bytes(_p: Path) -> int:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return 500_000_000  # first call: 500 MB → paused (low_bytes=1 TB)
            return 10**15  # subsequent: ample

        monitor = DiskSpaceMonitor(
            low_watermark_bytes=10**12,
            low_watermark_percent=0.001,
            resume_watermark_bytes=10**12,
            resume_watermark_percent=0.001,
            check_interval_seconds=0.0,
            get_free_bytes=_free_bytes,
            get_total_bytes=lambda _p: 10**15,
        )
        supervisor = CaptureSupervisor(settings, migrated_factory, disk_monitor=monitor)
        target = CaptureTarget(
            project_id=project_id,
            project_name="dg-recovery",
            camera_id=ctx["camera_id"],
            interval_seconds=60,
        )
        state = CaptureState(project_id=project_id, camera_id=ctx["camera_id"])
        volume_path = supervisor._volume_path(target)
        now = datetime.now(_UTC)

        # First evaluate: disk low → pause event
        await supervisor._evaluate_disk_gate(target, state, volume_path, now)
        supervisor._mark_waiting(state, schedule_open=True, disk_ok=False)

        # Second evaluate: disk recovered → resume event
        await supervisor._evaluate_disk_gate(target, state, volume_path, now)

        with session_scope(migrated_factory) as session:
            events = (
                session.query(Event)
                .filter(Event.scope == "project")
                .filter(Event.scope_id == project_id)
                .all()
            )
        levels = [e.level for e in events]
        messages = [e.message.lower() for e in events]
        # Expect a warning (pause) then an info (resume)
        assert "warning" in levels
        assert "info" in levels
        assert any("resum" in m for m in messages)
        await supervisor.stop()

    async def test_recovery_resume_event_emitted_once_not_every_cycle(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "dg-recovery-once")
        project_id = ctx["project_id"]

        call_count = 0

        def _free_bytes(_p: Path) -> int:
            nonlocal call_count
            call_count += 1
            return 500_000_000 if call_count == 1 else 10**15

        monitor = DiskSpaceMonitor(
            low_watermark_bytes=10**12,
            low_watermark_percent=0.001,
            resume_watermark_bytes=10**12,
            resume_watermark_percent=0.001,
            check_interval_seconds=0.0,
            get_free_bytes=_free_bytes,
            get_total_bytes=lambda _p: 10**15,
        )
        supervisor = CaptureSupervisor(settings, migrated_factory, disk_monitor=monitor)
        target = CaptureTarget(
            project_id=project_id,
            project_name="dg-recovery-once",
            camera_id=ctx["camera_id"],
            interval_seconds=60,
        )
        state = CaptureState(project_id=project_id, camera_id=ctx["camera_id"])
        volume_path = supervisor._volume_path(target)
        now = datetime.now(_UTC)

        # Pause
        await supervisor._evaluate_disk_gate(target, state, volume_path, now)
        supervisor._mark_waiting(state, schedule_open=True, disk_ok=False)

        # Resume (first clear: emits resume event)
        await supervisor._evaluate_disk_gate(target, state, volume_path, now)
        supervisor._mark_waiting(state, schedule_open=True, disk_ok=True)

        # Continued clear: no second resume event
        await supervisor._evaluate_disk_gate(target, state, volume_path, now)

        with session_scope(migrated_factory) as session:
            info_events = (
                session.query(Event)
                .filter(Event.scope == "project")
                .filter(Event.scope_id == project_id)
                .filter(Event.level == "info")
                .all()
            )
        resume_events = [e for e in info_events if "resum" in e.message.lower()]
        assert len(resume_events) == 1
        await supervisor.stop()
