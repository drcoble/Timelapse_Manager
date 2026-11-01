"""Unit tests for the in-process diagnostics (issue #7).

Covers the await-chain walker on nested coroutines (``to_thread``, ``sleep``,
``Lock.acquire``, ``Event.wait``, awaiting another task), the thread and
executor snapshots, the SIGUSR2 log dump, the loop-lag ticker, and the
self-reverting log-level override driven by a fake clock.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
from collections.abc import AsyncIterator, Generator
from datetime import UTC, datetime, timedelta

import pytest

from timelapse_manager.capture.supervisor import (
    CaptureState,
    RunnerPlan,
    RunnerSnapshot,
)
from timelapse_manager.monitoring import diagnostics
from timelapse_manager.monitoring.diagnostics import (
    LogLevelOverrides,
    LoopLagMonitor,
    describe_task,
    dump_tasks,
    executor_snapshot,
    install_task_dump_signal,
    remove_task_dump_signal,
    runner_snapshots,
    tasks_snapshot,
    threads_snapshot,
    validate_logger_name,
    walk_await_chain,
)


@pytest.fixture(autouse=True)
def _enabled_logger() -> Generator[None, None, None]:
    """Re-enable the logger under test for this module's tests.

    An Alembic migration run earlier in the same worker calls
    ``logging.config.fileConfig`` with ``disable_existing_loggers=True``, which
    disables already-imported loggers (a test-harness artifact, not the SUT).
    """
    target = logging.getLogger(diagnostics.__name__)
    was_disabled = target.disabled
    target.disabled = False
    yield
    target.disabled = was_disabled


# ---------------------------------------------------------------------------
# Nested coroutines to walk
# ---------------------------------------------------------------------------


async def _outer(inner: object) -> None:
    await inner  # type: ignore[misc]


async def _sleeps() -> None:
    await asyncio.sleep(3600)


async def _locks(lock: asyncio.Lock) -> None:
    async with lock:
        pass


async def _waits(event: asyncio.Event) -> None:
    await event.wait()


def _blocking_work(release: threading.Event) -> None:
    release.wait(30)


async def _threads(release: threading.Event) -> None:
    await asyncio.to_thread(_blocking_work, release)


@pytest.fixture()
async def started() -> AsyncIterator[list[asyncio.Task[None]]]:
    """Collect tasks a test starts and cancel them all afterwards."""
    tasks: list[asyncio.Task[None]] = []
    yield tasks
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _start(
    started: list[asyncio.Task[None]], coro: object, name: str
) -> asyncio.Task[None]:
    task = asyncio.create_task(coro, name=name)  # type: ignore[arg-type]
    started.append(task)
    # Two turns: one to start the task, one for it to reach its await.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    return task


async def test_walker_finds_sleep_through_nested_coroutines(
    started: list[asyncio.Task[None]],
) -> None:
    task = await _start(started, _outer(_sleeps()), "sleeper")
    described = describe_task(task)
    assert described["name"] == "sleeper"
    assert described["state"] == "pending"
    assert described["awaiting"]["kind"] == "asyncio.tasks.sleep"
    assert described["awaiting"]["delay_seconds"] == 3600
    # The deepest application frame, not asyncio's own sleep().
    assert described["awaited_from"].endswith(" _sleeps")
    names = [link.get("name") for link in described["await_chain"]]
    assert names[:3] == ["_outer", "_sleeps", "sleep"]
    assert all("location" in link for link in described["await_chain"][:3])
    assert described["stack"] and "_outer" in described["stack"][0]


async def test_walker_finds_lock_acquire(started: list[asyncio.Task[None]]) -> None:
    lock = asyncio.Lock()
    await lock.acquire()
    try:
        task = await _start(started, _outer(_locks(lock)), "locker")
        awaiting = describe_task(task)["awaiting"]
        assert awaiting["kind"] == "asyncio.locks.Lock.acquire"
        assert "locks.py" in awaiting["location"]
    finally:
        lock.release()


async def test_walker_finds_event_wait(started: list[asyncio.Task[None]]) -> None:
    task = await _start(started, _outer(_waits(asyncio.Event())), "waiter")
    assert describe_task(task)["awaiting"]["kind"] == "asyncio.locks.Event.wait"


async def test_walker_finds_to_thread_and_names_the_function(
    started: list[asyncio.Task[None]],
) -> None:
    release = threading.Event()
    try:
        task = await _start(started, _outer(_threads(release)), "threader")
        described = describe_task(task)
        assert described["awaiting"]["kind"] == "asyncio.threads.to_thread"
        assert described["awaiting"]["function"].endswith("_blocking_work")
        assert described["waiting_on"]["done"] is False
        stats = executor_snapshot(asyncio.get_running_loop())
        assert stats["created"] is True
        assert stats["threads"] >= 1
        assert stats["max_workers"] >= 1
        assert stats["queue_depth"] >= 0
    finally:
        release.set()


async def test_waiting_on_another_task_names_it(
    started: list[asyncio.Task[None]],
) -> None:
    child = await _start(started, _sleeps(), "child")
    parent = await _start(started, _outer(child), "parent")
    assert describe_task(parent)["waiting_on"] == {
        "type": "task",
        "name": "child",
        "done": False,
    }


async def test_finished_tasks_report_state_without_a_chain() -> None:
    async def boom() -> None:
        raise RuntimeError("secret detail that must not be echoed")

    done = asyncio.create_task(asyncio.sleep(0), name="done")
    failed = asyncio.create_task(boom(), name="failed")
    cancelled = asyncio.create_task(asyncio.sleep(10), name="cancelled")
    await asyncio.sleep(0)
    cancelled.cancel()
    await asyncio.gather(done, failed, cancelled, return_exceptions=True)

    assert describe_task(done)["state"] == "done"
    failed_description = describe_task(failed)
    assert failed_description["state"] == "failed"
    assert failed_description["exception"] == "RuntimeError"
    assert "secret detail" not in str(failed_description)
    assert describe_task(cancelled)["cancelled"] is True


def test_walker_stops_at_an_opaque_terminal_and_never_raises() -> None:
    assert walk_await_chain(None) == []
    assert walk_await_chain(object()) == [{"type": "object"}]


async def test_walker_handles_an_async_generator_consumer(
    started: list[asyncio.Task[None]],
) -> None:
    async def source() -> AsyncIterator[int]:
        await asyncio.sleep(3600)
        yield 1

    async def consume() -> None:
        async for _ in source():
            pass

    task = await _start(started, consume(), "listener")
    described = describe_task(task)
    assert described["state"] == "pending"
    assert described["await_chain"][0]["name"].endswith("consume")


async def test_tasks_snapshot_is_sorted_by_name(
    started: list[asyncio.Task[None]],
) -> None:
    await _start(started, _sleeps(), "zz-last")
    await _start(started, _sleeps(), "aa-first")
    names = [task["name"] for task in tasks_snapshot()]
    assert names == sorted(names)
    assert {"aa-first", "zz-last"} <= set(names)


def test_executor_snapshot_before_the_pool_exists() -> None:
    loop = asyncio.new_event_loop()
    try:
        stats = executor_snapshot(loop)
    finally:
        loop.close()
    assert stats["created"] is False
    assert stats["threads"] == 0
    assert executor_snapshot(None)["created"] is False


def test_threads_snapshot_names_every_thread() -> None:
    release = threading.Event()
    worker = threading.Thread(
        target=release.wait, args=(30,), name="diag-test-worker", daemon=True
    )
    worker.start()
    try:
        threads = {thread["name"]: thread for thread in threads_snapshot()}
    finally:
        release.set()
        worker.join()
    assert "MainThread" in threads
    stack = threads["diag-test-worker"]["stack"]
    # Innermost frame last: the worker is parked in Event.wait.
    assert stack[-1].endswith(" wait")


# ---------------------------------------------------------------------------
# Runner snapshots and the log dump
# ---------------------------------------------------------------------------


class _FakeSupervisor:
    def __init__(self, snapshots: list[RunnerSnapshot]) -> None:
        self._snapshots = snapshots

    def runner_snapshots(self) -> list[RunnerSnapshot]:
        return self._snapshots


class _BrokenSupervisor:
    def runner_snapshots(self) -> list[RunnerSnapshot]:
        raise RuntimeError("boom")


def _snapshot(now: datetime) -> RunnerSnapshot:
    state = CaptureState(
        project_id=7,
        camera_id=3,
        state="running",
        last_cycle_at=now - timedelta(seconds=42),
        restarts=2,
    )
    plan = RunnerPlan(
        planned_at=now - timedelta(seconds=42),
        action="wait",
        sleep_seconds=60.0,
        schedule_open=True,
        disk_ok=True,
        next_change=None,
        next_anchor_wake=now + timedelta(hours=1),
        next_retry_at=None,
    )
    return RunnerSnapshot(
        project_name="Roof",
        state=state,
        plan=plan,
        task_name="capture-project-7",
        task_done=False,
    )


def test_runner_snapshots_report_age_and_plan() -> None:
    now = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    [runner] = runner_snapshots(_FakeSupervisor([_snapshot(now)]), now)  # type: ignore[arg-type]
    assert runner["project_id"] == 7
    assert runner["last_cycle_age_seconds"] == 42.0
    assert runner["state"]["restarts"] == 2
    assert runner["state"]["last_cycle_at"] == "2026-10-22T11:59:18+00:00"
    assert runner["plan"]["decision"] == "wait"
    assert runner["plan"]["next_wake_at"] == "2026-10-22T12:00:18+00:00"


def test_runner_snapshots_never_raise() -> None:
    assert runner_snapshots(None) == []
    [entry] = runner_snapshots(_BrokenSupervisor())  # type: ignore[arg-type]
    assert "error" in entry


async def test_dump_tasks_logs_one_record_per_task(
    started: list[asyncio.Task[None]], caplog: pytest.LogCaptureFixture
) -> None:
    await _start(started, _outer(_sleeps()), "dump-me")
    now = datetime.now(UTC)
    with caplog.at_level(logging.WARNING, logger=diagnostics.__name__):
        dump_tasks(asyncio.get_running_loop(), _FakeSupervisor([_snapshot(now)]))  # type: ignore[arg-type]
    records = [r for r in caplog.records if r.name == diagnostics.__name__]
    task_records = [r for r in records if getattr(r, "diag_kind", "") == "asyncio_task"]
    names = {r.diag["name"] for r in task_records}  # type: ignore[attr-defined]
    assert "dump-me" in names
    dumped = next(r for r in task_records if r.diag["name"] == "dump-me")  # type: ignore[attr-defined]
    assert "asyncio.tasks.sleep" in dumped.getMessage()
    kinds = [getattr(r, "diag_kind", None) for r in records]
    assert kinds[0] == "asyncio_dump_begin"
    assert kinds[-1] == "asyncio_dump_end"
    assert "executor" in kinds
    assert "capture_runner" in kinds


async def test_dump_tasks_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=diagnostics.__name__):
        dump_tasks(asyncio.get_running_loop(), _BrokenSupervisor())  # type: ignore[arg-type]


@pytest.mark.skipif(not hasattr(signal, "SIGUSR2"), reason="POSIX only")
async def test_sigusr2_runs_the_dump_on_the_loop() -> None:
    loop = asyncio.get_running_loop()
    fired = asyncio.Event()
    assert install_task_dump_signal(loop, fired.set) is True
    try:
        os.kill(os.getpid(), signal.SIGUSR2)
        await asyncio.wait_for(fired.wait(), timeout=5)
    finally:
        remove_task_dump_signal(loop)


def test_signal_install_off_the_main_thread_is_skipped() -> None:
    result: list[bool] = []

    def run() -> None:
        loop = asyncio.new_event_loop()
        try:
            result.append(install_task_dump_signal(loop, lambda: None))
        finally:
            loop.close()

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    assert result == [False]


# ---------------------------------------------------------------------------
# Loop lag
# ---------------------------------------------------------------------------


async def test_loop_lag_monitor_measures_a_blocked_loop() -> None:
    monitor = LoopLagMonitor(interval_seconds=0.05)
    monitor.start()
    try:
        assert monitor.is_running()
        assert monitor.loop is asyncio.get_running_loop()
        await asyncio.sleep(0.01)
        threading.Event().wait(0.2)  # block the loop synchronously
        await asyncio.sleep(0.15)
        # The block shows as one late tick and stays visible in the window
        # after later ticks.
        assert monitor.lag_seconds >= 0.1
        names = {task.get_name() for task in asyncio.all_tasks()}
        assert "loop-lag" in names
    finally:
        await monitor.stop()
    assert not monitor.is_running()
    await monitor.stop()  # idempotent


# ---------------------------------------------------------------------------
# Log-level overrides (fake clock)
# ---------------------------------------------------------------------------


class _FakeClock:
    """Monotonic clock whose ``sleep`` advances time instead of waiting."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


@pytest.fixture()
def capture_logger() -> Generator[logging.Logger, None, None]:
    target = logging.getLogger("timelapse_manager.capture")
    original = target.level
    target.setLevel(logging.NOTSET)
    yield target
    target.setLevel(original)


async def _drain(overrides: LogLevelOverrides) -> None:
    """Let the thread-safe reschedule run, then wait for the timer to finish."""
    await asyncio.sleep(0)
    task = overrides.timer_task
    assert task is not None
    await asyncio.wait_for(task, timeout=5)


async def test_override_reverts_to_the_original_level_after_expiry(
    capture_logger: logging.Logger,
) -> None:
    clock = _FakeClock()
    overrides = LogLevelOverrides(
        asyncio.get_running_loop(), clock=clock, sleep=clock.sleep
    )
    applied = overrides.apply("timelapse_manager.capture", "DEBUG", 30)
    assert applied["previous_level"] == "NOTSET"
    assert capture_logger.level == logging.DEBUG
    assert overrides.revert_expired() == []  # not yet due

    await _drain(overrides)

    assert clock.slept == [1800.0]
    assert capture_logger.level == logging.NOTSET
    assert overrides.active() == []


async def test_reapplying_keeps_the_original_baseline_and_extends(
    capture_logger: logging.Logger,
) -> None:
    clock = _FakeClock()
    overrides = LogLevelOverrides(
        asyncio.get_running_loop(), clock=clock, sleep=clock.sleep
    )
    overrides.apply("timelapse_manager.capture", "DEBUG", 10)
    clock.now += 300  # five minutes in, extend to a fresh 20 minutes
    second = overrides.apply("timelapse_manager.capture", "INFO", 20)
    assert second["previous_level"] == "NOTSET"
    assert capture_logger.level == logging.INFO

    await _drain(overrides)

    assert clock.now == 1000.0 + 300 + 1200
    assert capture_logger.level == logging.NOTSET


async def test_stop_reverts_every_override_immediately(
    capture_logger: logging.Logger,
) -> None:
    clock = _FakeClock()
    overrides = LogLevelOverrides(
        asyncio.get_running_loop(), clock=clock, sleep=asyncio.sleep
    )
    overrides.apply("timelapse_manager.capture", "DEBUG", 240)
    await asyncio.sleep(0)
    await overrides.stop()
    assert capture_logger.level == logging.NOTSET
    assert overrides.active() == []


@pytest.mark.parametrize(
    ("logger_name", "level", "minutes"),
    [
        ("root", "DEBUG", 5),
        ("timelapse_managerX", "DEBUG", 5),
        ("timelapse_manager.", "DEBUG", 5),
        ("timelapse_manager.capture", "TRACE", 5),
        ("timelapse_manager.capture", "DEBUG", 0),
        ("timelapse_manager.capture", "DEBUG", 241),
    ],
)
async def test_override_validation(logger_name: str, level: str, minutes: int) -> None:
    overrides = LogLevelOverrides(asyncio.get_running_loop())
    with pytest.raises(ValueError):
        overrides.apply(logger_name, level, minutes)
    assert overrides.active() == []


def test_validate_logger_name_accepts_the_package_and_children() -> None:
    assert validate_logger_name("timelapse_manager") == "timelapse_manager"
    assert validate_logger_name("timelapse_manager.capture.supervisor")


def test_loop_lag_recent_lags_returns_the_newest_ticks_oldest_first() -> None:
    monitor = LoopLagMonitor(window=4)
    for value in (0.1, 0.2, 0.3, 0.4, 0.5):
        monitor._samples.append(value)
    assert monitor.recent_lags(3) == [0.3, 0.4, 0.5]
    assert monitor.recent_lags(10) == [0.2, 0.3, 0.4, 0.5]
    assert monitor.recent_lags(0) == []
    assert LoopLagMonitor().recent_lags(3) == []
