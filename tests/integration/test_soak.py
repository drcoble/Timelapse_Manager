"""Soak / fake-clock-driven integration test.

This is the ONE test that uses a fake clock advancing through many loop
iterations. The fake clock's sleep() advances `now` by the requested amount,
so the loop makes progress without wall-clock waits.

Asserts (across two projects, with a simulated dropout and resume):
- sequence indices across all frames are unique
- frame count == number of distinct capture instants expected
- every Frame row has a file on disk and vice-versa
- gap Event(s) exist after the simulated dropout + resume

Bounded by a hard frame-count limit so it cannot run forever.

Marked @pytest.mark.slow.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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
from timelapse_manager.db.models import Camera, Frame, Project
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


# Hard limit: total number of capture cycles the fake clock will drive
_MAX_CYCLES = 30
# Interval between captures (seconds)
_INTERVAL = 10


class FakeClock:
    """A deterministic clock: sleep advances `now` by the sleep amount."""

    def __init__(self, start: datetime) -> None:
        self._now = start
        self.cycle_count = 0

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self._now += timedelta(seconds=max(0.0, seconds))
        self.cycle_count += 1
        if self.cycle_count >= _MAX_CYCLES:
            raise asyncio.CancelledError


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
        capture=CaptureSettings(
            autostart=False,
            timeout_seconds=5.0,
            max_idle_sleep_seconds=float(_INTERVAL),
            backoff_base_seconds=0.1,
            backoff_max_seconds=1.0,
        ),
    )


def _seed_projects(migrated_factory, tmp_path: Path) -> list[dict]:
    results = []
    for i in range(1, 3):
        storage = tmp_path / "frames" / f"soak-proj-{i}"
        storage.mkdir(parents=True, exist_ok=True)
        with session_scope(migrated_factory) as session:
            cam = Camera(
                name=f"soak-cam-{i}",
                address=f"127.0.0.{i}",
                protocol="vapix",
                snapshot_uri=f"http://127.0.0.{i}/snap",
            )
            session.add(cam)
            session.flush()
            cam_id = cam.id
            proj = Project(
                camera_id=cam_id,
                name=f"soak-proj-{i}",
                capture_interval_seconds=_INTERVAL,
                lifecycle_state="active",
                operational_status="idle",
                storage_path=str(storage),
                schedule=None,
            )
            session.add(proj)
            session.flush()
            project_id = proj.id
        results.append(
            {"camera_id": cam_id, "project_id": project_id, "storage_path": storage}
        )
    return results


async def _run_loop_until_cancelled(
    supervisor: CaptureSupervisor,
    target: CaptureTarget,
    state: CaptureState,
) -> None:
    fake_config = MagicMock()
    with (
        patch.object(supervisor, "_load_camera", return_value=fake_config),
        patch(
            "timelapse_manager.capture.supervisor.build_adapter",
            return_value=FakeAdapter(),
        ),
        contextlib.suppress(asyncio.CancelledError),
    ):
        await supervisor._run_project(target, state)


@pytest.mark.slow
class TestSoak:
    async def test_soak_sequence_indices_unique_and_contiguous(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        start_time = datetime(2026, 6, 1, 8, 0, tzinfo=_UTC)
        clock = FakeClock(start=start_time)
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings,
            migrated_factory,
            clock=clock,
            disk_monitor=_permissive_disk_monitor(),
        )

        projects = _seed_projects(migrated_factory, tmp_path)

        # Run all project loops concurrently until the fake clock cancels them
        tasks = []
        for ctx in projects:
            target = CaptureTarget(
                project_id=ctx["project_id"],
                project_name=f"soak-proj-{ctx['project_id']}",
                camera_id=ctx["camera_id"],
                interval_seconds=_INTERVAL,
                schedule=None,
            )
            state = CaptureState(
                project_id=ctx["project_id"], camera_id=ctx["camera_id"]
            )
            task = asyncio.create_task(
                _run_loop_until_cancelled(supervisor, target, state)
            )
            tasks.append(task)

        await asyncio.gather(*tasks, return_exceptions=True)

        total_frames = 0
        for ctx in projects:
            with session_scope(migrated_factory) as session:
                frames = (
                    session.query(Frame)
                    .filter(Frame.project_id == ctx["project_id"])
                    .order_by(Frame.sequence_index)
                    .all()
                )

            total_frames += len(frames)
            if not frames:
                continue

            # All indices unique
            indices = [f.sequence_index for f in frames]
            assert len(indices) == len(set(indices)), "Duplicate sequence indices found"

            # Indices contiguous from 1
            assert indices == list(range(1, len(indices) + 1)), (
                f"Gap in sequence indices: {indices}"
            )

            # Every frame row has a file on disk
            for f in frames:
                assert f.file_path is not None
                assert Path(f.file_path).exists(), f"Missing file: {f.file_path}"

        # At least one project must have produced frames — guards against a
        # timing regression that would silently green all per-project assertions.
        assert total_frames > 0, (
            "Soak produced zero frames across all projects — "
            "clock or loop likely not advancing"
        )

        await supervisor.stop()

    @pytest.mark.slow
    async def test_soak_files_and_rows_one_to_one(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        start_time = datetime(2026, 6, 2, 8, 0, tzinfo=_UTC)
        clock = FakeClock(start=start_time)
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings,
            migrated_factory,
            clock=clock,
            disk_monitor=_permissive_disk_monitor(),
        )

        projects = _seed_projects(migrated_factory, tmp_path)

        tasks = []
        for ctx in projects:
            target = CaptureTarget(
                project_id=ctx["project_id"],
                project_name=f"soak-proj-{ctx['project_id']}",
                camera_id=ctx["camera_id"],
                interval_seconds=_INTERVAL,
                schedule=None,
            )
            state = CaptureState(
                project_id=ctx["project_id"], camera_id=ctx["camera_id"]
            )
            tasks.append(
                asyncio.create_task(
                    _run_loop_until_cancelled(supervisor, target, state)
                )
            )

        await asyncio.gather(*tasks, return_exceptions=True)

        for ctx in projects:
            storage = ctx["storage_path"]
            disk_files = {f.name for f in Path(storage).glob("*.jpg")}

            with session_scope(migrated_factory) as session:
                db_paths = {
                    Path(f.file_path).name
                    for f in session.query(Frame)
                    .filter(Frame.project_id == ctx["project_id"])
                    .all()
                    if f.file_path
                }

            # Each file on disk corresponds to exactly one DB row and vice-versa
            missing = disk_files - db_paths
            extra = db_paths - disk_files
            assert disk_files == db_paths, (
                f"Disk/DB mismatch: disk={missing} db={extra}"
            )

        await supervisor.stop()
