"""The single seam through which capture code runs blocking work off the loop.

Every synchronous hop in :mod:`timelapse_manager.capture` (a database read or
write, a disk probe, a hash) goes through :func:`run_blocking`, reached via
``CaptureSupervisor._run_blocking`` or ``EventListenerFactory._run_blocking``.
Keeping one seam means the executor that capture work runs on is chosen in one
place: with no executor it is the event loop's default executor (exactly
``asyncio.to_thread``), and a dedicated capture executor can be injected at
construction without touching any call site.

A unit test fails on any other ``asyncio.to_thread(`` under ``capture/``.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
from collections.abc import Callable
from concurrent.futures import Executor
from typing import ParamSpec, TypeVar

_P = ParamSpec("_P")
_T = TypeVar("_T")


async def run_blocking(
    executor: Executor | None,
    fn: Callable[_P, _T],
    /,
    *args: _P.args,
    **kwargs: _P.kwargs,
) -> _T:
    """Run ``fn(*args, **kwargs)`` in a worker thread and return its result.

    :param executor: the executor to run on, or ``None`` for the loop's default
        executor (the behaviour of :func:`asyncio.to_thread`).
    :param fn: the synchronous callable.

    The caller's context variables are copied into the worker, as
    :func:`asyncio.to_thread` does, so request ids and similar context reach log
    records written by ``fn``.
    """
    if executor is None:
        return await asyncio.to_thread(fn, *args, **kwargs)
    loop = asyncio.get_running_loop()
    context = contextvars.copy_context()
    call = functools.partial(fn, *args, **kwargs)
    return await loop.run_in_executor(executor, context.run, call)
