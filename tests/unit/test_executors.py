"""The jobs executor and its ``run_in`` seam (#66 C3, WP-66-H)."""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time

import pytest
from pydantic import ValidationError

from timelapse_manager.config.settings import RenderSettings
from timelapse_manager.db.engine import (
    POOL_SIZE,
    REQUEST_THREADPOOL_TOKENS,
    background_executor_workers,
    capture_executor_workers,
    create_db_engine,
    pool_capacity,
)
from timelapse_manager.executors import (
    DEFAULT_JOBS_EXECUTOR_WORKERS,
    JOBS_THREAD_NAME_PREFIX,
    create_jobs_executor,
    run_in,
)

_marker: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_marker", default="unset"
)


def _read_marker_and_thread(suffix: str, *, extra: str = "") -> tuple[str, str]:
    """Return the caller's context marker (plus arguments) and this thread's name."""
    return _marker.get() + suffix + extra, threading.current_thread().name


class TestRunIn:
    async def test_runs_on_the_given_executor_with_the_callers_context(self) -> None:
        executor = create_jobs_executor(1)
        try:
            _marker.set("request-42")
            value, thread_name = await run_in(
                executor, _read_marker_and_thread, "/a", extra="/b"
            )
        finally:
            executor.shutdown(wait=True)
        assert value == "request-42/a/b"
        assert thread_name.startswith(f"{JOBS_THREAD_NAME_PREFIX}_")

    async def test_none_runs_on_the_default_executor_with_the_callers_context(
        self,
    ) -> None:
        _marker.set("request-7")
        value, thread_name = await run_in(None, _read_marker_and_thread, "")
        assert value == "request-7"
        assert not thread_name.startswith(JOBS_THREAD_NAME_PREFIX)
        assert thread_name != threading.main_thread().name

    async def test_worker_changes_do_not_leak_back(self) -> None:
        """The worker runs in a copy of the context, as ``to_thread`` does."""
        executor = create_jobs_executor(1)
        try:
            _marker.set("caller")
            await run_in(executor, _marker.set, "worker")
        finally:
            executor.shutdown(wait=True)
        assert _marker.get() == "caller"

    async def test_an_exception_propagates_to_the_awaiting_task(self) -> None:
        executor = create_jobs_executor(1)

        def boom() -> None:
            raise LookupError("from the worker")

        try:
            with pytest.raises(LookupError, match="from the worker"):
                await run_in(executor, boom)
        finally:
            executor.shutdown(wait=True)

    async def test_two_workers_bound_the_concurrency(self) -> None:
        """A third long job waits for one of the two workers."""
        executor = create_jobs_executor(2)
        release = threading.Event()
        lock = threading.Lock()
        running = 0
        started = 0

        def long_job() -> None:
            nonlocal running, started
            with lock:
                running += 1
                started += 1
            release.wait(10)
            with lock:
                running -= 1

        try:
            tasks = [asyncio.create_task(run_in(executor, long_job)) for _ in range(3)]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with lock:
                    if running == 2:
                        break
                await asyncio.sleep(0.01)
            with lock:
                assert (running, started) == (2, 2)
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 10)
        finally:
            release.set()
            executor.shutdown(wait=True)
        assert started == 3


class TestCreateJobsExecutor:
    def test_default_size_and_thread_names(self) -> None:
        executor = create_jobs_executor()
        try:
            assert executor._max_workers == DEFAULT_JOBS_EXECUTOR_WORKERS == 2
            name = executor.submit(lambda: threading.current_thread().name).result()
        finally:
            executor.shutdown(wait=True)
        assert name.startswith("tlm-jobs_")

    @pytest.mark.parametrize("workers", [0, -1])
    def test_rejects_fewer_than_one_worker(self, workers: int) -> None:
        with pytest.raises(ValueError, match="at least 1 worker"):
            create_jobs_executor(workers)


class TestJobsExecutorSetting:
    def test_default_matches_the_executor_default(self) -> None:
        assert RenderSettings().jobs_executor_workers == DEFAULT_JOBS_EXECUTOR_WORKERS

    def test_must_be_at_least_one(self) -> None:
        with pytest.raises(ValidationError):
            RenderSettings(jobs_executor_workers=0)
        assert RenderSettings(jobs_executor_workers=1).jobs_executor_workers == 1


class TestPoolCountsTheJobsExecutor:
    def test_pool_capacity_includes_the_jobs_workers(self) -> None:
        # The capture executor's default size is counted too (WP-66-I).
        base = (
            REQUEST_THREADPOOL_TOKENS
            + background_executor_workers()
            + capture_executor_workers()
        )
        assert pool_capacity() == base + DEFAULT_JOBS_EXECUTOR_WORKERS
        assert pool_capacity(5) == base + 5

    def test_engine_overflow_follows_the_configured_workers(self, tmp_path) -> None:
        engine = create_db_engine(
            f"sqlite:///{tmp_path / 'pool.db'}", jobs_executor_workers=5
        )
        try:
            # No public accessor for the overflow cap.
            overflow = engine.pool._max_overflow  # type: ignore[attr-defined]
        finally:
            engine.dispose()
        assert overflow == pool_capacity(5) - POOL_SIZE
