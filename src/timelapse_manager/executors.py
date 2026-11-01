"""Dedicated thread executors for long blocking work (issue #66, C3).

Four pools carry the application's blocking work, so a long job in one cannot
starve the others:

* **Request work** runs on anyio's worker threadpool -- ``def`` routes, sync
  dependencies and ``run_in_threadpool`` hops from ``async def`` routes. Its size
  is pinned at startup (``db.engine.REQUEST_THREADPOOL_TOKENS``).
* **Long jobs** run on the *jobs executor* built here: render preparation,
  export zips, post-render file copies and prunes, and the codec backfill's
  candidate scan. It has few workers (``render.jobs_executor_workers``, default
  2), so two long jobs queue behind each other instead of competing with capture
  and short database hops for the loop's default executor. An import waits behind
  an export on the same pool; that is intended.
* **Capture** (the supervisor's hops, one-shot captures, the event listeners)
  runs on the *capture executor*, ``tlm-capture``, built in ``app._lifespan``
  (``capture.executor_workers``), so a capture never queues behind other work.
* Everything else (the scheduler, the dispatcher, the render queue's short
  claim and terminal writes) stays on the event loop's default executor, i.e.
  ``asyncio.to_thread``.

The debug endpoint and the ``SIGUSR2`` dump report the default, capture and
jobs executors (``monitoring.diagnostics.executors_snapshot``).

:func:`run_in` is the one way onto an executor from the event loop. ``None``
means the default executor (exactly ``asyncio.to_thread``), so a component built
without an executor -- a test, alternative wiring -- behaves as before.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import threading
from collections.abc import Callable
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from typing import Any, ParamSpec, TypeVar

_P = ParamSpec("_P")
_T = TypeVar("_T")

# Default size of the jobs executor; ``render.jobs_executor_workers`` overrides
# it. Also the default the connection pool is sized with (``db.engine``).
DEFAULT_JOBS_EXECUTOR_WORKERS = 2

# Thread-name prefix of the jobs executor's workers, so a log record's thread
# name, ``GET /api/v1/system/debug/threads`` and the SIGUSR2 dump's ident-to-name
# map show which pool a long job ran on. A SIGUSR1 (faulthandler) dump prints
# only thread idents, so read it next to that map.
JOBS_THREAD_NAME_PREFIX = "tlm-jobs"


class CountingThreadPoolExecutor(ThreadPoolExecutor):
    """A thread pool that counts the work items running right now (#276).

    ``ThreadPoolExecutor`` keeps no busy-worker count, and its idle semaphore is
    only a thread-reuse heuristic that drifts after bursts, so the diagnostics
    cannot derive one from it. Every callable submitted (``submit``, ``map`` and
    ``loop.run_in_executor`` all go through ``submit``) is wrapped so
    :attr:`active_count` is the exact number of items executing, excluding those
    still queued.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._active_lock = threading.Lock()
        self._active_count = 0

    @property
    def active_count(self) -> int:
        """Work items executing on a worker thread right now."""
        with self._active_lock:
            return self._active_count

    def submit(self, fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> Future[_T]:
        """Queue ``fn(*args, **kwargs)``; it counts as active while it runs."""
        return super().submit(self._counted, fn, *args, **kwargs)

    def _counted(self, fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
        with self._active_lock:
            self._active_count += 1
        try:
            return fn(*args, **kwargs)
        finally:
            with self._active_lock:
                self._active_count -= 1


def create_jobs_executor(
    workers: int = DEFAULT_JOBS_EXECUTOR_WORKERS,
) -> CountingThreadPoolExecutor:
    """Build the jobs executor; it starts no thread until work is submitted.

    :param workers: the most long jobs that run at once (at least 1).
    :raises ValueError: when ``workers`` is below 1.
    """
    if workers < 1:
        raise ValueError(f"the jobs executor needs at least 1 worker, got {workers}")
    return CountingThreadPoolExecutor(
        max_workers=workers, thread_name_prefix=JOBS_THREAD_NAME_PREFIX
    )


async def run_in(
    executor: Executor | None,
    fn: Callable[_P, _T],
    /,
    *args: _P.args,
    **kwargs: _P.kwargs,
) -> _T:
    """Run ``fn(*args, **kwargs)`` on ``executor`` and return its result.

    :param executor: the executor to run on, or ``None`` for the event loop's
        default executor (the behaviour of :func:`asyncio.to_thread`).
    :param fn: the synchronous callable.

    The caller's context variables (request id, test-only loop-guard markers)
    are copied into the worker, as :func:`asyncio.to_thread` does. Like
    ``to_thread``, a cancellation of the awaiting task does not stop the thread:
    the work runs to completion and its result is dropped.
    """
    if executor is None:
        return await asyncio.to_thread(fn, *args, **kwargs)
    loop = asyncio.get_running_loop()
    context = contextvars.copy_context()
    call = functools.partial(fn, *args, **kwargs)
    return await loop.run_in_executor(executor, context.run, call)
