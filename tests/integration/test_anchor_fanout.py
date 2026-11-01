"""Many anchor-only runners sharing one exact-time anchor (issue #6).

Production had ten anchor-only projects with the same 12:00 clock anchor; on the
first day seven produced no fire-log row at all while the status API kept saying
``running``. These tests drive real runner loops concurrently against a real
SQLite file and real worker threads (``asyncio.to_thread``), on a virtual clock
that runs faster than wall time, and assert that every project fires -- including
when one runner hits a transient error mid-cycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.exc import OperationalError

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
from timelapse_manager.db.models import Camera, ExactTimeFire, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.storage.monitor import DiskSpaceMonitor

NY = ZoneInfo("America/New_York")
N_PROJECTS = 10
# Virtual seconds per real second: the 300 s idle ceiling is 6 s of wall time and
# the 30 min anchor grace window is 36 s, so a loaded CI host cannot push a fire
# out of grace before the bounded wait below gives up.
SPEED = 50.0
# Wall-time ceiling for a scenario to reach its expected outcome.
LIMIT_SECONDS = 20.0
ANCHOR = {"id": "noon", "kind": "clock", "time": "12:00", "offset_minutes": 0}


class VirtualClock:
    """Shared clock advancing ``SPEED`` times faster than wall time.

    Every runner sees the same ``now``; ``sleep`` really sleeps (scaled), so the
    runners interleave on the event loop and in worker threads as in production.
    """

    def __init__(self, start: datetime) -> None:
        self._start = start
        self._t0 = time.monotonic()

    def now(self) -> datetime:
        return self._start + timedelta(seconds=(time.monotonic() - self._t0) * SPEED)

    def monotonic(self) -> float:
        return (time.monotonic() - self._t0) * SPEED

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds) / SPEED)


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
        capture=CaptureSettings(
            autostart=False,
            timeout_seconds=5.0,
            max_idle_sleep_seconds=300.0,
            backoff_base_seconds=0.1,
            backoff_max_seconds=1.0,
        ),
    )


def _seed(factory: Any, tmp_path: Path) -> list[CaptureTarget]:
    targets = []
    for i in range(1, N_PROJECTS + 1):
        storage = tmp_path / "frames" / f"p{i}"
        storage.mkdir(parents=True, exist_ok=True)
        with session_scope(factory) as session:
            cam = Camera(name=f"cam-{i}", address=f"10.0.0.{i}", protocol="vapix")
            session.add(cam)
            session.flush()
            proj = Project(
                camera_id=cam.id,
                name=f"anchor-{i}",
                capture_interval_seconds=None,
                lifecycle_state="active",
                operational_status="idle",
                storage_path=str(storage),
                schedule={"enabled": True, "timezone": "America/New_York"},
                exact_time_anchors=[dict(ANCHOR, enabled=True)],
            )
            session.add(proj)
            session.flush()
            targets.append(
                CaptureTarget(
                    project_id=proj.id,
                    project_name=proj.name,
                    camera_id=cam.id,
                    interval_seconds=None,
                    schedule={"enabled": True, "timezone": "America/New_York"},
                    storage_path=str(storage),
                    exact_time_anchors=[dict(ANCHOR, enabled=True)],
                )
            )
    return targets


async def _run_all(
    supervisor: CaptureSupervisor,
    targets: list[CaptureTarget],
    until: Callable[[], bool],
    *,
    min_seconds: float = 0.0,
) -> list[asyncio.Task[None]]:
    """Run every target's real loop until ``until()`` holds, then cancel.

    Polls the outcome instead of sleeping a fixed time so a slow, loaded CI host
    only makes the test slower, not flaky; gives up after ``LIMIT_SECONDS`` and
    lets the caller's assertions report what was reached. ``min_seconds`` keeps
    the loops running at least that long (e.g. for a timeout to elapse).
    Returns the tasks still alive at the end (the survivors).
    """
    with (
        patch.object(supervisor, "_load_camera", return_value=MagicMock()),
        patch(
            "timelapse_manager.capture.one_shot.build_adapter",
            side_effect=lambda *a, **k: FakeAdapter(),
        ),
    ):
        tasks = [
            asyncio.create_task(
                supervisor._run_project(
                    t, CaptureState(project_id=t.project_id, camera_id=t.camera_id)
                )
            )
            for t in targets
        ]
        loop = asyncio.get_running_loop()
        started = loop.time()
        while loop.time() - started < LIMIT_SECONDS:
            if loop.time() - started >= min_seconds and until():
                break
            await asyncio.sleep(0.05)
        survivors = [t for t in tasks if not t.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    return survivors


def _fire_rows(factory: Any) -> dict[int, str]:
    with session_scope(factory) as session:
        rows = session.query(ExactTimeFire).filter_by(local_date="2026-10-14").all()
        return {r.project_id: r.status for r in rows}


@pytest.fixture()
def start() -> datetime:
    # 11:59:58 New York on a normal (non-DST-change) day.
    return datetime(2026, 10, 14, 11, 59, 58, tzinfo=NY).astimezone(UTC)


class TestAnchorFanOut:
    async def test_ten_runners_on_one_anchor_all_capture(
        self, migrated_factory: Any, tmp_path: Path, start: datetime
    ) -> None:
        targets = _seed(migrated_factory, tmp_path)
        supervisor = CaptureSupervisor(
            _settings(tmp_path),
            migrated_factory,
            clock=VirtualClock(start),
            disk_monitor=_permissive_disk_monitor(),
        )
        try:
            survivors = await _run_all(
                supervisor,
                targets,
                until=lambda: (
                    list(_fire_rows(migrated_factory).values()).count("captured")
                    == N_PROJECTS
                ),
            )
        finally:
            await supervisor.stop()
        fires = _fire_rows(migrated_factory)
        assert sorted(fires) == [t.project_id for t in targets]
        assert set(fires.values()) == {"captured"}
        assert len(survivors) == N_PROJECTS

    async def test_transient_claim_error_does_not_kill_the_runner(
        self, migrated_factory: Any, tmp_path: Path, start: datetime
    ) -> None:
        """One runner's claim hits ``database is locked`` once; it must recover.

        On 0.1.0 the exception escaped ``_run_project`` and ended the task
        silently: no fire-log row, status still ``running`` -- the #6 symptom.
        """
        targets = _seed(migrated_factory, tmp_path)
        supervisor = CaptureSupervisor(
            _settings(tmp_path),
            migrated_factory,
            clock=VirtualClock(start),
            disk_monitor=_permissive_disk_monitor(),
        )
        unlucky = targets[3].project_id
        real_claim = supervisor._claim_exact_time_fire
        failed_once: list[int] = []

        def flaky_claim(project_id: int, *args: Any, **kwargs: Any) -> int | None:
            if project_id == unlucky and not failed_once:
                failed_once.append(project_id)
                raise OperationalError("INSERT", {}, Exception("database is locked"))
            return real_claim(project_id, *args, **kwargs)

        try:
            with patch.object(supervisor, "_claim_exact_time_fire", flaky_claim):
                survivors = await _run_all(
                    supervisor,
                    targets,
                    until=lambda: (
                        list(_fire_rows(migrated_factory).values()).count("captured")
                        == N_PROJECTS
                    ),
                )
        finally:
            await supervisor.stop()
        assert failed_once == [unlucky]
        fires = _fire_rows(migrated_factory)
        assert fires.get(unlucky) == "captured", fires
        assert len(fires) == N_PROJECTS
        assert len(survivors) == N_PROJECTS


class _ManualClock:
    def __init__(self, now: datetime) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.current.timestamp()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(0)


def _bare_supervisor(tmp_path: Path, factory: Any, clock: Any) -> CaptureSupervisor:
    return CaptureSupervisor(
        _settings(tmp_path),
        factory,
        clock=clock,
        disk_monitor=_permissive_disk_monitor(),
    )


class TestRunnerHealth:
    async def _reconcile_with(
        self, supervisor: CaptureSupervisor, target: CaptureTarget
    ) -> None:
        with (
            patch.object(supervisor, "_load_targets", return_value=[target]),
            patch.object(supervisor, "_load_listener_targets", return_value=[]),
            patch.object(supervisor, "_log_resume_gap", return_value=None),
        ):
            await supervisor._reconcile_once()

    async def test_dead_runner_is_relaunched_with_an_error_event(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        target = _seed(migrated_factory, tmp_path)[0]
        clock = _ManualClock(datetime(2026, 10, 14, 16, 0, tzinfo=UTC))
        supervisor = _bare_supervisor(tmp_path, migrated_factory, clock)
        runs: list[int] = []
        events: list[dict[str, Any]] = []

        async def fake_run(t: CaptureTarget, state: CaptureState) -> None:
            runs.append(t.project_id)
            if len(runs) == 1:
                raise RuntimeError("boom")
            await asyncio.Event().wait()

        with (
            patch.object(supervisor, "_run_project", fake_run),
            patch.object(supervisor, "_write_event", lambda **kw: events.append(kw)),
        ):
            await self._reconcile_with(supervisor, target)
            await asyncio.sleep(0.01)  # first runner dies
            await self._reconcile_with(supervisor, target)
            await asyncio.sleep(0.01)
            runner = supervisor._runners[target.project_id]
            assert runner.task is not None and not runner.task.done()
            assert runner.state.restarts == 1
            assert len(runs) == 2
            assert [e["metadata"]["reason"] for e in events] == ["runner_relaunch"]
            assert "RuntimeError: boom" in events[0]["message"]
            await supervisor.stop()

    async def test_wedged_runner_is_relaunched(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        target = _seed(migrated_factory, tmp_path)[0]
        clock = _ManualClock(datetime(2026, 10, 14, 16, 0, tzinfo=UTC))
        supervisor = _bare_supervisor(tmp_path, migrated_factory, clock)
        runs: list[int] = []

        async def stuck_run(t: CaptureTarget, state: CaptureState) -> None:
            runs.append(t.project_id)
            state.last_cycle_at = clock.now()
            state.last_cycle_mono = clock.monotonic()
            await asyncio.Event().wait()  # never cycles again

        with (
            patch.object(supervisor, "_run_project", stuck_run),
            patch.object(supervisor, "_write_event", lambda **kw: None),
        ):
            await self._reconcile_with(supervisor, target)
            await asyncio.sleep(0.01)
            clock.current += supervisor._wedge_threshold() - timedelta(seconds=1)
            await self._reconcile_with(supervisor, target)
            assert len(runs) == 1, "a runner inside its threshold must be left alone"
            clock.current += timedelta(seconds=2)
            await self._reconcile_with(supervisor, target)
            await asyncio.sleep(0.01)
            assert len(runs) == 2
            assert supervisor._runners[target.project_id].state.restarts == 1
            await supervisor.stop()

    async def test_hanging_anchor_fire_times_out_and_the_runner_continues(
        self, migrated_factory: Any, tmp_path: Path, start: datetime
    ) -> None:
        targets = _seed(migrated_factory, tmp_path)[:2]
        supervisor = _bare_supervisor(tmp_path, migrated_factory, VirtualClock(start))
        hung = targets[0].project_id

        async def capture(*args: Any, **kwargs: Any) -> Any:
            if args[1].project_id == hung:
                await asyncio.Event().wait()
            return await real_capture(*args, **kwargs)

        from timelapse_manager.capture import supervisor as sup_mod

        real_capture = sup_mod.capture_one_now
        with (
            patch.object(sup_mod, "capture_one_now", capture),
            patch.object(sup_mod, "_ANCHOR_FIRE_MARGIN_SECONDS", 5.0),
            patch.object(supervisor._settings.capture, "timeout_seconds", 0.05),
        ):
            survivors = await _run_all(
                supervisor,
                targets,
                until=lambda: (
                    _fire_rows(migrated_factory).get(targets[1].project_id)
                    == "captured"
                ),
                # Past the ~5 s fire timeout (2 x 0.05 s capture + 5 s margin), so
                # the hung runner had to move on. The margin is generous so a
                # healthy capture on a loaded CI host never hits it.
                min_seconds=6.0,
            )
        await supervisor.stop()
        fires = _fire_rows(migrated_factory)
        # The hung attempt timed out and counts as a failed attempt (#69): it is
        # either parked for a retry or re-attempted (provisional ``failed``),
        # never captured, and the other runner was not held up.
        assert fires[hung] in {"failed", "retry_pending"}
        assert fires[targets[1].project_id] == "captured"
        assert len(survivors) == 2


class TestAnchorJitter:
    def test_jitter_is_stable_bounded_and_spread(self) -> None:
        from timelapse_manager.capture.anchors import EXACT_TIME_GRACE
        from timelapse_manager.capture.supervisor import (
            ANCHOR_JITTER_MAX,
            _anchor_jitter,
        )

        values = [_anchor_jitter(pid) for pid in range(1, 51)]
        assert all(timedelta(0) <= v < ANCHOR_JITTER_MAX for v in values)
        assert values == [_anchor_jitter(pid) for pid in range(1, 51)]
        assert len({round(v.total_seconds(), 3) for v in values}) > 40
        assert ANCHOR_JITTER_MAX * 10 < EXACT_TIME_GRACE
