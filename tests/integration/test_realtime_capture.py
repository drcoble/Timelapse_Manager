"""Real-clock cadence test: sustain a short capture interval in wall-clock time.

The soak test proves the scheduling *logic* under a fake clock that advances
instantly. This complements it by running the real capture loop against the real
``asyncio`` clock for a short, bounded window and confirming frames are actually
produced at the configured few-second cadence -- the part a fake clock cannot
demonstrate.

Bounded by a wall-clock cutoff so it cannot hang. Marked @pytest.mark.slow.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import FakeAdapter
from timelapse_manager.cameras.base import CapturedFrame
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

# A few-second capture cadence (the system must sustain intervals as short as a
# few seconds) and a wall-clock window generous enough to see at least two
# captures even on a loaded host.
_INTERVAL_SECONDS = 1
_RUN_SECONDS = 5.0


class _RealtimeAdapter(FakeAdapter):
    """Like FakeAdapter but stamps each capture with the real wall-clock instant.

    The base FakeAdapter fixes ``captured_at`` at construction, which would make
    every frame share one timestamp; stamping per call lets the test observe the
    real cadence between captures.
    """

    async def capture(self) -> CapturedFrame:
        return CapturedFrame(
            image_bytes=self._BYTES,
            width=1,
            height=1,
            format="jpeg",
            captured_at=datetime.now(UTC),
        )


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


def _make_settings(tmp_path: Path) -> Settings:
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
        capture=CaptureSettings(
            autostart=False,
            timeout_seconds=5.0,
            max_idle_sleep_seconds=float(_INTERVAL_SECONDS),
            backoff_base_seconds=0.1,
            backoff_max_seconds=1.0,
        ),
    )


def _seed_project(migrated_factory, tmp_path: Path) -> tuple[int, int]:
    storage = tmp_path / "frames" / "rt-proj"
    storage.mkdir(parents=True, exist_ok=True)
    with session_scope(migrated_factory) as session:
        cam = Camera(
            name="rt-cam",
            address="127.0.0.1",
            protocol="vapix",
            snapshot_uri="http://127.0.0.1/snap",
        )
        session.add(cam)
        session.flush()
        cam_id = cam.id
        proj = Project(
            camera_id=cam_id,
            name="rt-proj",
            capture_interval_seconds=_INTERVAL_SECONDS,
            lifecycle_state="active",
            operational_status="idle",
            storage_path=str(storage),
            schedule=None,
        )
        session.add(proj)
        session.flush()
        return cam_id, proj.id


@pytest.mark.slow
@pytest.mark.flaky(reruns=2, reruns_delay=1)
async def test_sustains_short_interval_on_real_clock(
    migrated_factory, tmp_path: Path
) -> None:
    settings = _make_settings(tmp_path)
    # No injected clock -> the supervisor uses the real asyncio clock.
    supervisor = CaptureSupervisor(
        settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
    )
    cam_id, project_id = _seed_project(migrated_factory, tmp_path)
    target = CaptureTarget(
        project_id=project_id,
        project_name="rt-proj",
        camera_id=cam_id,
        interval_seconds=_INTERVAL_SECONDS,
        schedule=None,
    )
    state = CaptureState(project_id=project_id, camera_id=cam_id)

    started = datetime.now(UTC)
    with (
        patch.object(supervisor, "_load_camera", return_value=MagicMock()),
        patch(
            "timelapse_manager.capture.supervisor.build_adapter",
            return_value=_RealtimeAdapter(),
        ),
        contextlib.suppress(asyncio.TimeoutError),
    ):
        # Run the real loop for a bounded wall-clock window, then cancel it.
        await asyncio.wait_for(
            supervisor._run_project(target, state), timeout=_RUN_SECONDS
        )
    elapsed = (datetime.now(UTC) - started).total_seconds()

    with session_scope(migrated_factory) as session:
        frames = (
            session.query(Frame)
            .filter(Frame.project_id == project_id)
            .order_by(Frame.sequence_index)
            .all()
        )
        timestamps = [f.capture_timestamp for f in frames]

    # Over ~5s at a 1s cadence we expect 3-4 frames; require at least 2 to
    # prove real-time progress on any loaded host.
    assert len(frames) >= 2, f"too few frames ({len(frames)}) in {elapsed:.1f}s"
    # Consecutive captures must be spaced at least half an interval apart,
    # proving they are gated by the cadence and not fired back-to-back.
    # No upper bound: a temporarily starved event loop stretching one gap is
    # not a correctness failure and is the primary source of flakiness.
    pairs = zip(timestamps, timestamps[1:], strict=False)
    gaps = [(b - a).total_seconds() for a, b in pairs]
    assert all(g >= 0.5 for g in gaps), f"captures fired back-to-back: {gaps}"

    await supervisor.stop()
