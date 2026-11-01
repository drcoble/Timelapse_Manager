"""Interval cadence, retry cap and wedge detection run on the monotonic clock.

#69 CAM-R5 (grid cadence, retry capped at the next slot, auth fast path),
CAM-R13 and #67-R13 (the wedge heartbeat), and #116 (``/healthz`` reads the
supervisor's own definition of a stuck runner).
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from timelapse_manager.cameras.base import (
    AuthCaptureError,
    CameraAdapter,
    CapturedFrame,
    OtherCaptureError,
)
from timelapse_manager.capture.schedule import parse_schedule
from timelapse_manager.capture.supervisor import (
    CaptureState,
    CaptureSupervisor,
    CaptureTarget,
    _ProjectRunner,
)
from timelapse_manager.config.settings import (
    CaptureSettings,
    DatabaseSettings,
    PathsSettings,
    Settings,
)
from timelapse_manager.monitoring import readiness
from timelapse_manager.storage.monitor import DiskSpaceMonitor

_START = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class SteppableClock:
    """Wall and monotonic time that move independently.

    ``step_wall`` jumps the wall clock (NTP, a manual change, a VM resume)
    without moving the monotonic reading; ``advance`` moves both, as real time
    does.
    """

    def __init__(self) -> None:
        self.wall = _START
        self.mono = 5_000.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds

    def step_wall(self, seconds: float) -> None:
        self.wall += timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:  # pragma: no cover - unused
        raise AssertionError("not expected")


def _supervisor(
    tmp_path: Path, clock: SteppableClock, **capture: Any
) -> CaptureSupervisor:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    settings = Settings(
        database=DatabaseSettings(url=f"sqlite:///{tmp_path / 'x.db'}"),
        paths=PathsSettings(
            data_dir=data_dir,
            frames_root=data_dir / "frames",
            token_file=data_dir / ".local-token",
        ),
        capture=CaptureSettings(autostart=False, **capture),
    )
    monitor = DiskSpaceMonitor(
        low_watermark_bytes=1,
        low_watermark_percent=0.001,
        resume_watermark_bytes=1,
        resume_watermark_percent=0.001,
        check_interval_seconds=0.0,
        get_free_bytes=lambda _p: 10**15,
        get_total_bytes=lambda _p: 10**15,
    )
    return CaptureSupervisor(settings, MagicMock(), disk_monitor=monitor, clock=clock)


def _target(project_id: int = 5, interval: int | None = 60) -> CaptureTarget:
    return CaptureTarget(
        project_id=project_id,
        project_name="Roof",
        camera_id=1,
        interval_seconds=interval,
    )


async def _cycle(supervisor: CaptureSupervisor, state: CaptureState) -> float | None:
    target = _target()
    supervisor._runners[target.project_id] = _ProjectRunner(  # noqa: SLF001
        state=state, target=target
    )
    return await supervisor._run_cycle(  # noqa: SLF001
        target,
        state,
        interval=60.0,
        max_idle=300.0,
        rng=random.Random(5),
        schedule=parse_schedule(None),
        volume_path=Path("/"),
    )


class TestCadenceIgnoresTheWallClock:
    async def test_a_backward_wall_step_does_not_stall_the_cadence(
        self, tmp_path: Path
    ) -> None:
        clock = SteppableClock()
        supervisor = _supervisor(tmp_path, clock)
        state = CaptureState(project_id=5, camera_id=1)
        state.next_due_mono = clock.mono + 30.0

        clock.step_wall(-120.0)
        assert await _cycle(supervisor, state) == pytest.approx(30.0)

        clock.advance(30.0)
        attempt = AsyncMock(return_value=None)
        with patch.object(supervisor, "_attempt_capture", attempt):
            assert await _cycle(supervisor, state) == 0.0
        attempt.assert_awaited_once()
        await supervisor.stop()

    async def test_a_forward_wall_step_does_not_burst_a_capture(
        self, tmp_path: Path
    ) -> None:
        clock = SteppableClock()
        supervisor = _supervisor(tmp_path, clock)
        state = CaptureState(project_id=5, camera_id=1)
        state.next_due_mono = clock.mono + 30.0

        clock.step_wall(3600.0)
        attempt = AsyncMock(return_value=None)
        with patch.object(supervisor, "_attempt_capture", attempt):
            assert await _cycle(supervisor, state) == pytest.approx(30.0)
        attempt.assert_not_awaited()
        await supervisor.stop()

    async def test_launch_starts_the_grid_at_once(self, tmp_path: Path) -> None:
        clock = SteppableClock()
        supervisor = _supervisor(tmp_path, clock)
        with patch.object(supervisor, "_run_project", AsyncMock()):
            supervisor._launch(_target())  # noqa: SLF001
        state = supervisor._runners[5].state  # noqa: SLF001
        assert state.next_due_mono == clock.mono == state.started_mono
        await supervisor.stop()


class TestAdvanceAndRetry:
    def test_attempt_moves_the_grid_past_now_by_whole_intervals(
        self, tmp_path: Path
    ) -> None:
        clock = SteppableClock()
        supervisor = _supervisor(tmp_path, clock)
        state = CaptureState(project_id=5, camera_id=1)
        state.next_due_mono = clock.mono
        clock.advance(1.5)  # the capture took 1.5 s
        supervisor._advance_cadence(_target(), state)  # noqa: SLF001
        assert state.next_due_mono == 5_000.0 + 60.0

    def test_anchor_only_project_has_no_grid(self, tmp_path: Path) -> None:
        supervisor = _supervisor(tmp_path, SteppableClock())
        state = CaptureState(project_id=5, camera_id=1)
        supervisor._advance_cadence(_target(interval=None), state)  # noqa: SLF001
        assert state.next_due_mono is None

    def test_retry_never_lies_beyond_the_next_slot(self, tmp_path: Path) -> None:
        clock = SteppableClock()
        supervisor = _supervisor(
            tmp_path, clock, backoff_base_seconds=100.0, backoff_jitter_fraction=0.0
        )
        state = CaptureState(project_id=5, camera_id=1)
        state.next_due_mono = clock.mono + 60.0
        supervisor._record_transient_failure(  # noqa: SLF001
            state, OtherCaptureError("down"), random.Random(0)
        )
        assert state.next_retry_mono == clock.mono + 60.0
        assert state.next_retry_at == clock.wall + timedelta(seconds=60)

    def test_short_backoff_is_kept(self, tmp_path: Path) -> None:
        clock = SteppableClock()
        supervisor = _supervisor(
            tmp_path, clock, backoff_base_seconds=5.0, backoff_jitter_fraction=0.0
        )
        state = CaptureState(project_id=5, camera_id=1)
        state.next_due_mono = clock.mono + 60.0
        supervisor._record_transient_failure(  # noqa: SLF001
            state, OtherCaptureError("down"), random.Random(0)
        )
        assert state.next_retry_mono == clock.mono + 5.0

    def test_a_refused_login_goes_straight_to_the_ceiling(self, tmp_path: Path) -> None:
        clock = SteppableClock()
        supervisor = _supervisor(
            tmp_path,
            clock,
            backoff_base_seconds=1.0,
            backoff_max_seconds=300.0,
            backoff_jitter_fraction=0.0,
        )
        state = CaptureState(project_id=5, camera_id=1)
        supervisor._record_transient_failure(  # noqa: SLF001
            state, AuthCaptureError("401"), random.Random(0)
        )
        # No grid yet, so nothing caps it: the 300 s ceiling, first failure.
        assert state.next_retry_mono == clock.mono + 300.0

    def test_a_refused_login_is_still_capped_at_the_next_slot(
        self, tmp_path: Path
    ) -> None:
        clock = SteppableClock()
        supervisor = _supervisor(tmp_path, clock, backoff_jitter_fraction=0.0)
        state = CaptureState(project_id=5, camera_id=1)
        state.next_due_mono = clock.mono + 60.0
        supervisor._record_transient_failure(  # noqa: SLF001
            state, AuthCaptureError("401"), random.Random(0)
        )
        assert state.next_retry_mono == clock.mono + 60.0

    def test_success_clears_the_retry(self, tmp_path: Path) -> None:
        supervisor = _supervisor(tmp_path, SteppableClock())
        state = CaptureState(project_id=5, camera_id=1, next_retry_mono=1.0)
        supervisor._record_success(state)  # noqa: SLF001
        assert state.next_retry_mono is None

    async def test_failed_attempt_advances_the_grid_then_caps_the_retry(
        self, tmp_path: Path
    ) -> None:
        clock = SteppableClock()
        supervisor = _supervisor(
            tmp_path, clock, backoff_base_seconds=300.0, backoff_jitter_fraction=0.0
        )
        state = CaptureState(project_id=5, camera_id=1)
        state.next_due_mono = clock.mono

        async def fail(target: object, state: object) -> int:
            clock.advance(10.0)
            raise OtherCaptureError("down")

        with (
            patch.object(supervisor, "_capture_once", fail),
            patch.object(supervisor, "_report_camera_outcome", AsyncMock()),
        ):
            await supervisor._attempt_capture(  # noqa: SLF001
                _target(), state, random.Random(0)
            )
        assert state.next_due_mono == 5_000.0 + 60.0
        assert state.next_retry_mono == 5_000.0 + 60.0
        await supervisor.stop()


class _SlowAdapter(CameraAdapter):
    """Takes ``delay`` seconds; declares ``budget`` as its own total."""

    def __init__(self, delay: float, budget: float | None) -> None:
        self._delay = delay
        self._budget = budget

    @property
    def capture_budget_seconds(self) -> float | None:
        return self._budget

    async def capture(self) -> CapturedFrame:
        await asyncio.sleep(self._delay)
        return MagicMock(spec=CapturedFrame)

    async def validate_connection(self) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def get_geolocation(self) -> Any:  # pragma: no cover
        return None

    async def capabilities(self) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:
        return None


class TestCaptureBudget:
    async def test_an_adapter_budget_replaces_a_smaller_shared_timeout(
        self, tmp_path: Path
    ) -> None:
        supervisor = _supervisor(tmp_path, SteppableClock(), timeout_seconds=0.05)
        frame = await supervisor._capture_with_timeout(  # noqa: SLF001
            _SlowAdapter(delay=0.2, budget=2.0)
        )
        assert frame is not None
        await supervisor.stop()

    async def test_without_a_budget_the_shared_timeout_applies(
        self, tmp_path: Path
    ) -> None:
        supervisor = _supervisor(tmp_path, SteppableClock(), timeout_seconds=0.05)
        frame = await supervisor._capture_with_timeout(  # noqa: SLF001
            _SlowAdapter(delay=0.2, budget=None)
        )
        assert frame is None
        await supervisor.stop()

    def test_the_anchor_attempt_outlasts_the_longest_rtsp_grab(
        self, tmp_path: Path
    ) -> None:
        """The default ceiling (45 s socket timeout, +5 s) fits the attempt."""
        supervisor = _supervisor(tmp_path, SteppableClock(), rtsp_timeout_seconds=45.0)
        grab = 45.0 + 5.0
        scene = 2.5  # SCENE_METADATA_TOTAL_SECONDS
        attempt = supervisor._anchor_fire_timeout()  # noqa: SLF001
        slot_wait = supervisor._slot_wait_seconds()  # noqa: SLF001
        assert attempt > slot_wait + grab + scene


class TestWedgeHeartbeatIsMonotonic:
    def _runner(self, clock: SteppableClock, *, beat_age: float) -> _ProjectRunner:
        state = CaptureState(
            project_id=5,
            camera_id=1,
            state="running",
            started_at=clock.wall - timedelta(days=1),
            started_mono=clock.mono - 86_400.0,
            last_cycle_at=clock.wall - timedelta(seconds=beat_age),
            last_cycle_mono=clock.mono - beat_age,
        )
        task = MagicMock()
        task.done.return_value = False
        return _ProjectRunner(state=state, target=_target(), task=task)

    @pytest.mark.parametrize("step", [-3600.0, 3600.0])
    def test_a_wall_step_does_not_relaunch_a_healthy_runner(
        self, tmp_path: Path, step: float
    ) -> None:
        clock = SteppableClock()
        supervisor = _supervisor(tmp_path, clock)
        runner = self._runner(clock, beat_age=5.0)
        clock.step_wall(step)
        assert supervisor._runner_problem(runner) is None  # noqa: SLF001

    @pytest.mark.parametrize("step", [-3600.0, 0.0, 3600.0])
    def test_a_wall_step_does_not_mask_a_wedged_runner(
        self, tmp_path: Path, step: float
    ) -> None:
        clock = SteppableClock()
        supervisor = _supervisor(tmp_path, clock)
        threshold = supervisor._wedge_threshold().total_seconds()  # noqa: SLF001
        runner = self._runner(clock, beat_age=threshold + 1.0)
        clock.step_wall(step)
        problem = supervisor._runner_problem(runner)  # noqa: SLF001
        assert problem is not None and "wedged" in problem

    def test_just_inside_the_threshold_is_healthy(self, tmp_path: Path) -> None:
        clock = SteppableClock()
        supervisor = _supervisor(tmp_path, clock)
        threshold = supervisor._wedge_threshold().total_seconds()  # noqa: SLF001
        runner = self._runner(clock, beat_age=threshold - 1.0)
        assert supervisor._runner_problem(runner) is None  # noqa: SLF001

    def test_a_runner_that_never_cycled_is_judged_from_its_launch(
        self, tmp_path: Path
    ) -> None:
        clock = SteppableClock()
        supervisor = _supervisor(tmp_path, clock)
        runner = self._runner(clock, beat_age=0.0)
        runner.state.last_cycle_mono = None
        runner.state.last_cycle_at = None
        assert supervisor._runner_problem(runner) is not None  # noqa: SLF001

    async def test_the_loop_stamps_the_monotonic_heartbeat(
        self, tmp_path: Path
    ) -> None:
        clock = SteppableClock()
        supervisor = _supervisor(tmp_path, clock)
        state = CaptureState(project_id=5, camera_id=1)

        async def stop_after_one(*args: Any, **kwargs: Any) -> None:
            return None

        with patch.object(supervisor, "_run_cycle", stop_after_one):
            await supervisor._run_project(_target(), state)  # noqa: SLF001
        assert state.last_cycle_mono == clock.mono
        assert state.last_cycle_at == clock.wall
        await supervisor.stop()


async def _done_task(exc: BaseException | None = None) -> asyncio.Task[None]:
    async def run() -> None:
        if exc is not None:
            raise exc

    task = asyncio.ensure_future(run())
    await asyncio.wait([task])
    return task


class TestRunnerProblemsAgreeWithReadiness:
    """#116: ``/healthz`` counts the supervisor's own list, not a copy of it."""

    async def test_readiness_count_equals_the_supervisors_own_verdicts(
        self, tmp_path: Path
    ) -> None:
        clock = SteppableClock()
        supervisor = _supervisor(tmp_path, clock)
        threshold = supervisor._wedge_threshold().total_seconds()  # noqa: SLF001

        def state(project_id: int, **kw: Any) -> CaptureState:
            fields: dict[str, Any] = {
                "state": "running",
                "started_mono": clock.mono,
                "last_cycle_mono": clock.mono,
            }
            fields.update(kw)
            return CaptureState(project_id=project_id, camera_id=1, **fields)

        def live() -> Any:
            task = MagicMock()
            task.done.return_value = False
            return task

        cases: dict[int, tuple[CaptureState, Any]] = {
            1: (state(1), live()),
            2: (state(2, last_cycle_mono=clock.mono - threshold - 1), live()),
            3: (state(3, state="error"), live()),
            4: (state(4, state="stopped"), await _done_task()),
            5: (state(5), await _done_task()),
            6: (state(6), await _done_task(RuntimeError("boom"))),
        }
        for project_id, (st, task) in cases.items():
            supervisor._runners[project_id] = _ProjectRunner(  # noqa: SLF001
                state=st, target=_target(project_id), task=task
            )

        expected = {
            project_id
            for project_id, runner in supervisor._runners.items()  # noqa: SLF001
            if supervisor._runner_problem(runner) is not None  # noqa: SLF001
        }
        assert expected == {2, 5, 6}
        assert {p.project_id for p in supervisor.runner_problems()} == expected

        context = SimpleNamespace(capture_supervisor=supervisor)
        with patch.object(readiness, "_running", return_value=True):
            check = readiness._runner_problem_check(context)  # type: ignore[arg-type]  # noqa: SLF001
        assert (check.status, check.value) == ("degraded", len(expected))

    async def test_no_problems_is_ok(self, tmp_path: Path) -> None:
        supervisor = _supervisor(tmp_path, SteppableClock())
        assert supervisor.runner_problems() == []
        context = SimpleNamespace(capture_supervisor=supervisor)
        with patch.object(readiness, "_running", return_value=True):
            check = readiness._runner_problem_check(context)  # type: ignore[arg-type]  # noqa: SLF001
        assert (check.status, check.value) == ("ok", 0)
