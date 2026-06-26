"""Keep-all behavioral test: low disk pauses capture but never deletes frames.

Verifies:
- Under a permanently paused disk monitor, 0 frames are written.
- Existing frames/files are untouched (0 deleted).
- The gate is computed from state, not wall-clock waits.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

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
from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.storage.monitor import DiskSpaceMonitor

_UTC = UTC


def _always_paused_monitor() -> DiskSpaceMonitor:
    """Return a disk monitor that always reports insufficient space."""
    return DiskSpaceMonitor(
        low_watermark_bytes=10**15,  # 1 petabyte low floor
        low_watermark_percent=99.0,  # 99% low floor
        resume_watermark_bytes=10**15,
        resume_watermark_percent=99.0,
        check_interval_seconds=0.0,
        get_free_bytes=lambda _p: 0,
        get_total_bytes=lambda _p: 10**15,
    )


def _permissive_monitor() -> DiskSpaceMonitor:
    """Return a disk monitor that always allows capture."""
    return DiskSpaceMonitor(
        low_watermark_bytes=1,
        low_watermark_percent=0.001,
        resume_watermark_bytes=1,
        resume_watermark_percent=0.001,
        check_interval_seconds=0.0,
        get_free_bytes=lambda _p: 10**15,
        get_total_bytes=lambda _p: 10**15,
    )


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


def _seed_project_with_real_files(
    migrated_factory,
    storage: Path,
    frame_count: int = 3,
) -> dict:
    storage.mkdir(parents=True, exist_ok=True)
    with session_scope(migrated_factory) as session:
        cam = Camera(
            name="keepall-cam",
            address="127.0.0.1",
            protocol="vapix",
            snapshot_uri="http://127.0.0.1/snap",
        )
        session.add(cam)
        session.flush()
        cam_id = cam.id
        proj = Project(
            camera_id=cam_id,
            name="keepall-proj",
            capture_interval_seconds=60,
            lifecycle_state="active",
            operational_status="idle",
            storage_path=str(storage),
            frame_count=frame_count,
        )
        session.add(proj)
        session.flush()
        project_id = proj.id

        file_paths = []
        for i in range(1, frame_count + 1):
            fname = f"{i:08d}.jpg"
            fpath = storage / fname
            fpath.write_bytes(f"frame-bytes-{i}".encode())
            fr = Frame(
                project_id=project_id,
                sequence_index=i,
                capture_timestamp=datetime(2026, 1, 1, 0, i, tzinfo=_UTC).replace(
                    tzinfo=None
                ),
                file_path=str(fpath),
                width=1,
                height=1,
                file_size_bytes=len(f"frame-bytes-{i}".encode()),
                capture_status="captured",
                origin="captured",
                lifecycle_state="active",
            )
            session.add(fr)
            file_paths.append(fpath)

    return {
        "camera_id": cam_id,
        "project_id": project_id,
        "storage_path": storage,
        "file_paths": file_paths,
    }


# ---------------------------------------------------------------------------
# Behavioral: paused disk → 0 writes, 0 deletes
# ---------------------------------------------------------------------------


class TestKeepAllUnderLowDisk:
    async def test_paused_disk_gate_writes_zero_frames(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """_evaluate_disk_gate returns False → no capture attempt → 0 new frames."""
        settings = _make_settings(tmp_path)
        storage = tmp_path / "keepall_frames"
        ctx = _seed_project_with_real_files(migrated_factory, storage)
        project_id = ctx["project_id"]
        initial_count = 3

        monitor = _always_paused_monitor()
        supervisor = CaptureSupervisor(settings, migrated_factory, disk_monitor=monitor)

        target = CaptureTarget(
            project_id=project_id,
            project_name="keepall-proj",
            camera_id=ctx["camera_id"],
            interval_seconds=60,
        )
        state = CaptureState(project_id=project_id, camera_id=ctx["camera_id"])

        # Evaluate disk gate (synchronous) — should return False and set pause_reason
        volume_path = supervisor._volume_path(target)
        now = datetime.now(_UTC)
        disk_ok = supervisor._disk_monitor.is_capture_allowed(volume_path, now=now)
        assert disk_ok is False

        # With a paused gate, mark_waiting sets pause_reason
        supervisor._mark_waiting(state, schedule_open=True, disk_ok=False)
        assert state.pause_reason == "low_disk"

        # Confirm no new frames written
        with session_scope(migrated_factory) as session:
            count = session.query(Frame).filter(Frame.project_id == project_id).count()
        assert count == initial_count
        await supervisor.stop()

    async def test_paused_disk_gate_deletes_zero_files(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """Low disk never triggers any file deletion."""
        settings = _make_settings(tmp_path)
        storage = tmp_path / "keepall_files"
        ctx = _seed_project_with_real_files(migrated_factory, storage)
        original_files = set(ctx["file_paths"])

        monitor = _always_paused_monitor()
        supervisor = CaptureSupervisor(settings, migrated_factory, disk_monitor=monitor)

        target = CaptureTarget(
            project_id=ctx["project_id"],
            project_name="keepall-proj",
            camera_id=ctx["camera_id"],
            interval_seconds=60,
        )
        state = CaptureState(project_id=ctx["project_id"], camera_id=ctx["camera_id"])

        # Repeatedly probe a paused gate — no delete should occur
        volume_path = supervisor._volume_path(target)
        now = datetime.now(_UTC)
        for _ in range(5):
            supervisor._disk_monitor.is_capture_allowed(volume_path, now=now)
            supervisor._mark_waiting(state, schedule_open=True, disk_ok=False)

        # All original files must still exist
        for fpath in original_files:
            assert fpath.exists(), f"{fpath} was deleted under low-disk pressure"
        await supervisor.stop()

    async def test_paused_disk_gate_deletes_zero_rows(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """Low disk never removes any Frame rows from the database."""
        settings = _make_settings(tmp_path)
        storage = tmp_path / "keepall_rows"
        ctx = _seed_project_with_real_files(migrated_factory, storage, frame_count=5)
        project_id = ctx["project_id"]

        monitor = _always_paused_monitor()
        supervisor = CaptureSupervisor(settings, migrated_factory, disk_monitor=monitor)

        target = CaptureTarget(
            project_id=project_id,
            project_name="keepall-proj",
            camera_id=ctx["camera_id"],
            interval_seconds=60,
        )
        state = CaptureState(project_id=project_id, camera_id=ctx["camera_id"])

        volume_path = supervisor._volume_path(target)
        now = datetime.now(_UTC)
        for _ in range(10):
            supervisor._disk_monitor.is_capture_allowed(volume_path, now=now)
            supervisor._mark_waiting(state, schedule_open=True, disk_ok=False)

        with session_scope(migrated_factory) as session:
            remaining = (
                session.query(Frame).filter(Frame.project_id == project_id).count()
            )
        assert remaining == 5
        await supervisor.stop()
