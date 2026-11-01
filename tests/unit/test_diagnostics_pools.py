"""Every named executor pool in the diagnostics (#161).

The debug endpoint and the SIGUSR2 dump report the loop's default executor
and each registered pool (``tlm-capture``, ``tlm-jobs``): size, busy workers
and queued work.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

from timelapse_manager.executors import (
    CountingThreadPoolExecutor,
    create_jobs_executor,
    run_in,
)
from timelapse_manager.monitoring import diagnostics
from timelapse_manager.monitoring.diagnostics import (
    build_task_dump,
    dump_tasks,
    executors_snapshot,
    pool_snapshot,
    register_executor,
    unregister_executor,
)

_LOGGER = "timelapse_manager.monitoring.diagnostics"


@pytest.fixture()
def busy_pool() -> Iterator[ThreadPoolExecutor]:
    """A one-worker pool with one job running and one queued behind it."""
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tlm-test")
    started = threading.Event()
    release = threading.Event()

    def block() -> None:
        started.set()
        release.wait(timeout=10)

    pool.submit(block)
    assert started.wait(timeout=10)
    pool.submit(lambda: None)
    try:
        yield pool
    finally:
        release.set()
        pool.shutdown(wait=True)


@pytest.fixture()
def registry() -> Iterator[None]:
    """Leave the process-wide registry as it was found."""
    before = dict(diagnostics._named_executors)
    try:
        yield
    finally:
        with diagnostics._executors_lock:
            diagnostics._named_executors.clear()
            diagnostics._named_executors.update(before)


def test_pool_snapshot_reports_size_active_and_queued(
    busy_pool: ThreadPoolExecutor,
) -> None:
    stats = pool_snapshot(busy_pool)

    assert stats["created"] is True
    assert stats["type"] == "ThreadPoolExecutor"
    assert stats["max_workers"] == 1
    assert stats["threads"] == stats["live_threads"] == 1
    assert stats["active"] == 1
    assert stats["queue_depth"] == 1


def test_pool_snapshot_of_an_idle_pool_has_no_active_worker() -> None:
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        pool.submit(lambda: None).result(timeout=10)
        stats = pool_snapshot(pool)
    finally:
        pool.shutdown(wait=True)

    assert stats["max_workers"] == 2
    assert stats["active"] == 0
    assert stats["queue_depth"] == 0


def test_pool_snapshot_never_raises() -> None:
    class Broken(ThreadPoolExecutor):
        @property
        def _threads(self) -> set[threading.Thread]:  # type: ignore[override]
            raise RuntimeError("boom")

        @_threads.setter
        def _threads(self, value: set[threading.Thread]) -> None:
            pass

    stats = pool_snapshot(Broken(max_workers=1))
    assert stats["error"] == "RuntimeError"
    assert pool_snapshot(None)["created"] is False


def test_executors_snapshot_lists_default_then_registered_pools(
    busy_pool: ThreadPoolExecutor, registry: None
) -> None:
    jobs = ThreadPoolExecutor(max_workers=2)
    register_executor("tlm-jobs", jobs)
    register_executor("tlm-capture", busy_pool)
    try:
        pools = executors_snapshot(None)
    finally:
        jobs.shutdown(wait=True)

    assert [pool["name"] for pool in pools] == ["default", "tlm-capture", "tlm-jobs"]
    default, capture, jobs_stats = pools
    assert default["created"] is False  # no loop given: no default executor
    assert (capture["max_workers"], capture["active"], capture["queue_depth"]) == (
        1,
        1,
        1,
    )
    assert jobs_stats["max_workers"] == 2


def test_unregister_only_removes_the_named_executor(registry: None) -> None:
    first = ThreadPoolExecutor(max_workers=1)
    second = ThreadPoolExecutor(max_workers=1)
    try:
        register_executor("tlm-capture", first)
        unregister_executor("tlm-capture", second)  # a stale teardown
        assert [p["name"] for p in executors_snapshot(None)] == [
            "default",
            "tlm-capture",
        ]
        unregister_executor("tlm-capture", first)
        unregister_executor("tlm-capture", first)  # idempotent
        assert [p["name"] for p in executors_snapshot(None)] == ["default"]
    finally:
        first.shutdown(wait=True)
        second.shutdown(wait=True)


async def test_task_dump_carries_every_pool(
    busy_pool: ThreadPoolExecutor, registry: None
) -> None:
    await asyncio.to_thread(lambda: None)  # build the default executor
    register_executor("tlm-capture", busy_pool)

    dump = build_task_dump(asyncio.get_running_loop(), None)

    names = [pool["name"] for pool in dump["executors"]]
    assert names == ["default", "tlm-capture"]
    assert dump["executor"]["created"] is True  # the default alone, as before
    assert "name" not in dump["executor"]
    assert dump["executors"][1]["active"] == 1


async def test_sigusr2_dump_logs_one_record_per_pool(
    busy_pool: ThreadPoolExecutor,
    registry: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    register_executor("tlm-capture", busy_pool)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        dump_tasks(asyncio.get_running_loop(), None)

    pools = [
        record
        for record in caplog.records
        if getattr(record, "diag_kind", None) == "executor"
    ]
    assert [record.diag["name"] for record in pools] == [  # type: ignore[attr-defined]
        "default",
        "tlm-capture",
    ]
    capture = pools[1].getMessage()
    assert capture.startswith("tlm-capture executor ")
    assert pools[0].getMessage().startswith("default executor ")
    assert "max_workers=1" in capture
    assert "active=1" in capture
    assert "queue_depth=1" in capture


# ---------------------------------------------------------------------------
# Exact in-flight count (#276)
# ---------------------------------------------------------------------------


def _run_blocking(pool: ThreadPoolExecutor, count: int) -> tuple[threading.Event, list]:
    """Start ``count`` items that block until the returned event is set."""
    release = threading.Event()
    started = threading.Barrier(count + 1)

    def block() -> None:
        started.wait(timeout=10)
        release.wait(timeout=10)

    futures = [pool.submit(block) for _ in range(count)]
    started.wait(timeout=10)
    return release, futures


def test_counting_pool_active_is_the_in_flight_count_then_zero() -> None:
    pool = CountingThreadPoolExecutor(max_workers=4, thread_name_prefix="tlm-test")
    try:
        assert pool_snapshot(pool)["active"] == 0
        release, futures = _run_blocking(pool, 3)
        snap = pool_snapshot(pool)
        assert snap["active"] == 3
        assert snap["queue_depth"] == 0
        assert "active_estimate" not in snap
        release.set()
        for future in futures:
            future.result(timeout=10)
        assert pool_snapshot(pool)["active"] == 0
        # Reused idle workers must not make a later idle pool look busy.
        for _ in range(8):
            pool.submit(lambda: None).result(timeout=10)
        assert pool_snapshot(pool)["active"] == 0
    finally:
        pool.shutdown(wait=True)


def test_counting_pool_excludes_queued_items_and_survives_exceptions() -> None:
    pool = CountingThreadPoolExecutor(max_workers=1, thread_name_prefix="tlm-test")
    try:
        release, futures = _run_blocking(pool, 1)
        queued = pool.submit(lambda: 1 / 0)
        snap = pool_snapshot(pool)
        assert (snap["active"], snap["queue_depth"]) == (1, 1)
        release.set()
        futures[0].result(timeout=10)
        with pytest.raises(ZeroDivisionError):
            queued.result(timeout=10)
        assert pool_snapshot(pool)["active"] == 0
    finally:
        pool.shutdown(wait=True)


@pytest.mark.asyncio
async def test_run_in_work_is_counted() -> None:
    pool = CountingThreadPoolExecutor(max_workers=2, thread_name_prefix="tlm-test")
    started = threading.Event()
    release = threading.Event()

    def block() -> str:
        started.set()
        release.wait(timeout=10)
        return "done"

    try:
        task = asyncio.create_task(run_in(pool, block))
        assert await asyncio.to_thread(started.wait, 10)
        assert pool_snapshot(pool)["active"] == 1
        release.set()
        assert await task == "done"
        assert pool_snapshot(pool)["active"] == 0
    finally:
        release.set()
        pool.shutdown(wait=True)


def test_jobs_executor_is_counting() -> None:
    pool = create_jobs_executor(2)
    try:
        assert isinstance(pool, CountingThreadPoolExecutor)
    finally:
        pool.shutdown(wait=True)


def test_plain_pool_reports_a_labelled_estimate(busy_pool: ThreadPoolExecutor) -> None:
    snap = pool_snapshot(busy_pool)
    assert "active_estimate" in snap
    assert snap["active"] == snap["active_estimate"]


def test_thread_name_map_names_the_pool_workers() -> None:
    pool = CountingThreadPoolExecutor(max_workers=1, thread_name_prefix="tlm-mapcheck")
    try:
        pool.submit(lambda: None).result(timeout=10)
        entries = diagnostics.thread_name_map()
        assert any(name.startswith("tlm-mapcheck") for _, _, name in entries)
        assert all(isinstance(ident, int) for ident, _, _ in entries)
    finally:
        pool.shutdown(wait=True)


@pytest.mark.asyncio
async def test_sigusr2_dump_logs_the_thread_name_map(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    dump_tasks(asyncio.get_running_loop(), None)
    lines = [
        r.getMessage() for r in caplog.records if "thread ident=" in r.getMessage()
    ]
    assert any("name=MainThread" in line for line in lines)
