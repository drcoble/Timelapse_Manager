"""In-process diagnostics for a stalled or misbehaving process (issue #7).

On 2026-10-14 seven capture runners stalled silently and the only evidence --
which coroutine or thread each was stuck in -- was lost on restart. This module
holds the tools that inspect a live process in place:

* **Data builders** (:func:`describe_task`, :func:`tasks_snapshot`,
  :func:`threads_snapshot`, :func:`executor_snapshot`,
  :func:`executors_snapshot`, :func:`runner_snapshots`,
  :func:`build_task_dump`) return plain,
  JSON-serialisable dicts and never log. The admin debug endpoints serve them,
  and the "report a problem" snapshot (#8) can embed them.
* **The SIGUSR2 dump** (:func:`dump_tasks`, :func:`install_task_dump_signal`)
  writes the same data to the log, one structured record per task.
* **Runtime helpers**: :class:`LoopLagMonitor` (the ``loop-lag`` ticker behind
  ``tlm_event_loop_lag_seconds``), :class:`LogLevelOverrides` (the temporary,
  self-reverting log-level override), :class:`LoopDbWatch` (database work on
  the event loop) and the named-executor registry
  (:func:`register_executor`), through which the dumps find the capture and
  jobs pools.

Two rules hold throughout:

* **Nothing here may raise into its caller** or stall the loop it is
  diagnosing. Every builder degrades a failure to an ``error`` field, and none
  of them uses the default executor (``asyncio.to_thread``): a saturated
  executor is one of the stalls these tools exist to diagnose.
* **No secrets.** Only code locations, task and thread names, and a short
  allow-list of scalar locals (a sleep delay, a to-thread function name) are
  read; frame locals are never dumped wholesale. Callers that serve the data
  still pass it through :func:`timelapse_manager.logging.redact` as a final pass.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import inspect
import logging
import re
import signal
import sys
import threading
import time
import traceback
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from types import FrameType
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine, event

from ..executors import CountingThreadPoolExecutor
from ..logging import request_id_ctx

if TYPE_CHECKING:
    from ..capture import CaptureSupervisor

logger = logging.getLogger(__name__)

# Bounds on how much of a chain or stack is reported, so a pathological chain
# (or deep recursion) cannot make a dump unbounded.
_MAX_AWAIT_DEPTH = 32
_MAX_STACK_FRAMES = 30

# The package root a log-level override may target; nothing outside it.
_PACKAGE_LOGGER = "timelapse_manager"
_LOGGER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$")

# The levels an override may set (mirrors ``LoggingSettings.level``).
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

# Ceiling on a single override's lifetime, in minutes.
MAX_OVERRIDE_MINUTES = 240

# Name of the timer task that reverts expired overrides.
_REVERT_TASK_NAME = "log-level-revert"

# Name of the event-loop lag ticker task.
_LOOP_LAG_TASK_NAME = "loop-lag"

# The name the loop's default executor is reported under.
DEFAULT_EXECUTOR_NAME = "default"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _frame_location(frame: FrameType) -> str:
    """Render a frame as ``file:line function``."""
    code = frame.f_code
    return f"{code.co_filename}:{frame.f_lineno} {code.co_name}"


def _iso(value: datetime | None) -> str | None:
    """Return ``value`` as ISO-8601, or ``None``."""
    return value.isoformat() if value is not None else None


def _age_seconds(value: datetime | None, now: datetime) -> float | None:
    """Seconds from ``value`` to ``now`` (aware or naive UTC), or ``None``."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return round((now - value).total_seconds(), 3)


def _jsonable(value: Any) -> Any:
    """Convert a dataclass field value to a JSON-friendly scalar."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    return value


# ---------------------------------------------------------------------------
# Await-chain walker
# ---------------------------------------------------------------------------


def _frame_of(awaitable: Any) -> FrameType | None:
    """Return the suspended frame of a coroutine or generator, if any."""
    frame = getattr(awaitable, "cr_frame", None)
    if frame is None:
        frame = getattr(awaitable, "gi_frame", None)
    return frame if isinstance(frame, FrameType) else None


def _next_awaitable(awaitable: Any) -> Any:
    """Return what ``awaitable`` is itself awaiting, or ``None`` at the end."""
    nxt = getattr(awaitable, "cr_await", None)
    if nxt is None:
        nxt = getattr(awaitable, "gi_yieldfrom", None)
    return nxt


def _safe_local(frame: FrameType, name: str) -> Any:
    """Read one named local from a suspended frame, or ``None``."""
    try:
        return frame.f_locals.get(name)
    except Exception:  # noqa: BLE001 - a diagnostics read must never raise
        return None


def _callable_name(func: Any) -> str:
    """A callable's qualified name, never its arguments or repr."""
    module = getattr(func, "__module__", None)
    qualname = getattr(func, "__qualname__", None) or type(func).__name__
    return f"{module}.{qualname}" if module else str(qualname)


def _classify_wait(awaitable: Any) -> dict[str, Any]:
    """Describe the innermost coroutine of a chain in operator terms.

    The chain of a suspended task ends at an opaque future iterator, so the
    *innermost coroutine* is what says what the task is doing: an
    ``asyncio.sleep``, a ``Lock.acquire``, an ``Event.wait``, a ``to_thread``
    call, or application code awaiting a bare future. Only a short allow-list
    of scalar locals is read (the sleep delay, the to-thread function name).
    """
    qualname = getattr(awaitable, "__qualname__", None) or type(awaitable).__name__
    frame = _frame_of(awaitable)
    module = ""
    if frame is not None:
        module = str(frame.f_globals.get("__name__", ""))
    description: dict[str, Any] = {
        "kind": f"{module}.{qualname}" if module else qualname
    }
    if frame is not None:
        description["location"] = _frame_location(frame)
    if not module.startswith("asyncio"):
        return description
    if qualname == "sleep":
        delay = _safe_local(frame, "delay") if frame is not None else None
        if isinstance(delay, int | float):
            description["delay_seconds"] = delay
    elif qualname == "to_thread":
        func = _safe_local(frame, "func") if frame is not None else None
        if func is not None:
            description["function"] = _callable_name(func)
    return description


def walk_await_chain(coro: Any) -> list[dict[str, Any]]:
    """Walk ``cr_await`` / ``gi_yieldfrom`` from ``coro`` to the innermost awaitable.

    Returns one entry per link, outermost first: ``{"name", "type",
    "location"}`` for a coroutine or generator, and ``{"type"}`` for the opaque
    terminal object (a future iterator, an async-generator ``asend``). Capped at
    a fixed depth; never raises.
    """
    chain: list[dict[str, Any]] = []
    current = coro
    try:
        while current is not None and len(chain) < _MAX_AWAIT_DEPTH:
            link: dict[str, Any] = {"type": type(current).__name__}
            qualname = getattr(current, "__qualname__", None)
            if qualname is not None:
                link["name"] = qualname
            frame = _frame_of(current)
            if frame is not None:
                link["location"] = _frame_location(frame)
            chain.append(link)
            current = _next_awaitable(current)
    except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
        chain.append({"error": type(exc).__name__})
    return chain


def _innermost_coroutine(coro: Any) -> Any:
    """Return the deepest coroutine/generator in ``coro``'s await chain."""
    innermost = coro
    current = coro
    depth = 0
    while current is not None and depth < _MAX_AWAIT_DEPTH:
        if _frame_of(current) is not None:
            innermost = current
        current = _next_awaitable(current)
        depth += 1
    return innermost


def _awaited_from(coro: Any) -> str | None:
    """Location of the deepest non-asyncio frame in the chain.

    The innermost coroutine is usually an asyncio primitive (``sleep``,
    ``Event.wait``); the line that *called* it is what an operator needs.
    """
    location: str | None = None
    current = coro
    depth = 0
    while current is not None and depth < _MAX_AWAIT_DEPTH:
        frame = _frame_of(current)
        if frame is not None:
            module = str(frame.f_globals.get("__name__", ""))
            if not module.startswith("asyncio"):
                location = _frame_location(frame)
        current = _next_awaitable(current)
        depth += 1
    return location


def _waiting_on(task: asyncio.Task[Any]) -> dict[str, Any] | None:
    """Describe the future a task is blocked on, from the task's waiter slot.

    ``_fut_waiter`` is a CPython implementation detail, read defensively: when
    present it distinguishes awaiting another task (named) from a bare future
    (a to-thread result, a lock or event waiter, a timer).
    """
    waiter = getattr(task, "_fut_waiter", None)
    if waiter is None:
        return None
    if isinstance(waiter, asyncio.Task):
        return {"type": "task", "name": waiter.get_name(), "done": waiter.done()}
    done = waiter.done() if hasattr(waiter, "done") else None
    return {"type": type(waiter).__name__, "done": done}


def _task_state(task: asyncio.Task[Any]) -> str:
    """``pending``, ``cancelled``, ``failed`` or ``done``."""
    if not task.done():
        return "pending"
    if task.cancelled():
        return "cancelled"
    if task.exception() is not None:
        return "failed"
    return "done"


def describe_task(task: asyncio.Task[Any]) -> dict[str, Any]:
    """Describe one asyncio task as a plain dict. Never raises.

    Keys: ``name``, ``state``, ``done``, ``cancelled``, ``coroutine``,
    ``awaiting`` (the innermost coroutine, classified), ``awaited_from`` (the
    deepest application frame in the chain), ``waiting_on`` (the
    future in the task's waiter slot), ``await_chain`` (every link, outermost
    first) and ``stack`` (``task.get_stack()`` as ``file:line function``). A
    failed task carries ``exception`` -- the exception *type* only, since a
    message can quote arbitrary data.
    """
    name = "<unknown>"
    try:
        name = task.get_name()
        description: dict[str, Any] = {
            "name": name,
            "state": _task_state(task),
            "done": task.done(),
            "cancelled": task.cancelled(),
        }
        coro = task.get_coro()
        description["coroutine"] = getattr(coro, "__qualname__", type(coro).__name__)
        if task.done():
            if not task.cancelled() and task.exception() is not None:
                description["exception"] = type(task.exception()).__name__
            return description
        description["awaiting"] = _classify_wait(_innermost_coroutine(coro))
        description["awaited_from"] = _awaited_from(coro)
        description["waiting_on"] = _waiting_on(task)
        description["await_chain"] = walk_await_chain(coro)
        description["stack"] = [
            _frame_location(frame) for frame in task.get_stack(limit=_MAX_STACK_FRAMES)
        ]
        return description
    except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
        return {"name": name, "error": f"describe failed: {type(exc).__name__}"}


def tasks_snapshot(
    loop: asyncio.AbstractEventLoop | None = None,
) -> list[dict[str, Any]]:
    """Describe every task on ``loop`` (default: the running loop), by name.

    Must be called on the loop's own thread: ``asyncio.all_tasks`` is not safe
    to call from another thread while the loop runs.
    """
    try:
        tasks = asyncio.all_tasks(loop)
    except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
        return [{"error": f"all_tasks failed: {type(exc).__name__}"}]
    described = [describe_task(task) for task in tasks]
    described.sort(key=lambda item: str(item.get("name", "")))
    return described


# ---------------------------------------------------------------------------
# Threads and the default executor
# ---------------------------------------------------------------------------


def threads_snapshot() -> list[dict[str, Any]]:
    """Every thread's Python stack, innermost frame last, with thread names.

    Uses ``sys._current_frames()`` (no signal needed) and does not read source
    lines, so it costs no file I/O. Never raises.
    """
    try:
        frames = sys._current_frames()  # noqa: SLF001 - the documented way to do this
        by_ident = {thread.ident: thread for thread in threading.enumerate()}
        threads: list[dict[str, Any]] = []
        for ident, frame in frames.items():
            thread = by_ident.get(ident)
            summary = traceback.StackSummary.extract(
                traceback.walk_stack(frame),
                limit=_MAX_STACK_FRAMES,
                lookup_lines=False,
            )
            summary.reverse()
            threads.append(
                {
                    "name": thread.name if thread is not None else f"<unknown {ident}>",
                    "ident": ident,
                    "native_id": getattr(thread, "native_id", None),
                    "daemon": thread.daemon if thread is not None else None,
                    "stack": [f"{fs.filename}:{fs.lineno} {fs.name}" for fs in summary],
                }
            )
        threads.sort(key=lambda item: str(item["name"]))
        return threads
    except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
        return [{"error": f"thread snapshot failed: {type(exc).__name__}"}]


def thread_name_map() -> list[tuple[int | None, int | None, str]]:
    """``(ident, native_id, name)`` of every live thread, sorted by name.

    ``faulthandler`` (``SIGUSR1``) prints only thread idents; this is the key
    that makes such a dump readable (#276). Never raises.
    """
    try:
        return sorted(
            (
                (t.ident, getattr(t, "native_id", None), t.name)
                for t in threading.enumerate()
            ),
            key=lambda item: (item[2], item[0] or 0),
        )
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        return []


_executors_lock = threading.Lock()
_named_executors: dict[str, concurrent.futures.Executor] = {}


def register_executor(name: str, executor: concurrent.futures.Executor) -> None:
    """Report ``executor`` under ``name`` in the dumps (#161).

    The application registers its dedicated pools (``tlm-capture``,
    ``tlm-jobs``) at startup and unregisters them at shutdown. Registering a
    name again replaces the earlier executor.
    """
    with _executors_lock:
        _named_executors[name] = executor


def unregister_executor(
    name: str, executor: concurrent.futures.Executor | None = None
) -> None:
    """Stop reporting ``name``; with ``executor``, only if it is still that one.

    Idempotent, so a teardown after a failed startup is safe.
    """
    with _executors_lock:
        current = _named_executors.get(name)
        if current is not None and (executor is None or current is executor):
            del _named_executors[name]


def pool_snapshot(executor: concurrent.futures.Executor | None) -> dict[str, Any]:
    """Statistics for one executor: its size, busy workers and queued work.

    ``max_workers`` is the pool's size, ``active`` the workers running a work
    item and ``queue_depth`` the items waiting for one. For a
    :class:`~timelapse_manager.executors.CountingThreadPoolExecutor` (the
    ``tlm-capture`` and ``tlm-jobs`` pools) ``active`` is an exact in-flight
    count. For any other pool (the loop's default executor) it is an estimate
    derived from CPython's idle-worker semaphore, a thread-reuse heuristic that
    drifts and overstates busy workers after bursts (#276); such a pool also
    carries the same number as ``active_estimate``. ``created`` is false for ``None`` (a
    default executor not built yet). Reads CPython ``ThreadPoolExecutor``
    internals defensively; a different executor type reports only its type.
    Safe to call from any thread; never raises.
    """
    stats: dict[str, Any] = {
        "created": False,
        "type": None,
        "max_workers": None,
        "threads": 0,
        "live_threads": 0,
        "active": 0,
        "queue_depth": 0,
    }
    if executor is None:
        return stats
    try:
        stats["created"] = True
        stats["type"] = type(executor).__name__
        if isinstance(executor, concurrent.futures.ThreadPoolExecutor):
            workers = list(getattr(executor, "_threads", ()))
            live = sum(1 for worker in workers if worker.is_alive())
            stats["max_workers"] = getattr(executor, "_max_workers", None)
            stats["threads"] = len(workers)
            stats["live_threads"] = live
            if isinstance(executor, CountingThreadPoolExecutor):
                stats["active"] = executor.active_count
            else:
                idle_semaphore = getattr(executor, "_idle_semaphore", None)
                idle = getattr(idle_semaphore, "_value", None)
                estimate = max(0, live - idle) if isinstance(idle, int) else None
                stats["active"] = estimate
                stats["active_estimate"] = estimate
            work_queue = getattr(executor, "_work_queue", None)
            if work_queue is not None:
                stats["queue_depth"] = work_queue.qsize()
    except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
        stats["error"] = type(exc).__name__
    return stats


def executor_snapshot(loop: asyncio.AbstractEventLoop | None) -> dict[str, Any]:
    """Statistics for ``loop``'s default executor (what ``to_thread`` uses).

    Safe to call from any thread. ``created`` is false until the first
    ``to_thread``/``run_in_executor(None, ...)`` builds the pool. See
    :func:`pool_snapshot` for the fields.
    """
    try:
        executor = (
            getattr(loop, "_default_executor", None) if loop is not None else None
        )
    except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
        return {**pool_snapshot(None), "error": type(exc).__name__}
    return pool_snapshot(executor)


def executors_snapshot(loop: asyncio.AbstractEventLoop | None) -> list[dict[str, Any]]:
    """Every named pool: the loop's default executor first, then the
    registered ones (``tlm-capture``, ``tlm-jobs``) by name (#161).

    Each entry is :func:`pool_snapshot`'s dict plus ``name``. Safe to call
    from any thread; never raises.
    """
    pools = [{"name": DEFAULT_EXECUTOR_NAME, **executor_snapshot(loop)}]
    with _executors_lock:
        named = sorted(_named_executors.items())
    pools.extend({"name": name, **pool_snapshot(executor)} for name, executor in named)
    return pools


# ---------------------------------------------------------------------------
# Capture runners
# ---------------------------------------------------------------------------


def runner_snapshots(
    supervisor: CaptureSupervisor | None, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Every capture runner's state, heartbeat age and last plan, by project.

    Each entry carries every :class:`CaptureState` field, the heartbeat age
    (``last_cycle_age_seconds``), the runner task's name and liveness, and the
    runner's most recent plan (decision, sleep, next wake) when it has run a
    cycle. Never raises.
    """
    if supervisor is None:
        return []
    now = now if now is not None else datetime.now(UTC)
    try:
        snapshots = supervisor.runner_snapshots()
    except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
        return [{"error": f"runner snapshot failed: {type(exc).__name__}"}]
    runners: list[dict[str, Any]] = []
    for snapshot in snapshots:
        state = {key: _jsonable(value) for key, value in asdict(snapshot.state).items()}
        entry: dict[str, Any] = {
            "project_id": snapshot.state.project_id,
            "project_name": snapshot.project_name,
            "task_name": snapshot.task_name,
            "task_done": snapshot.task_done,
            "state": state,
            "last_cycle_age_seconds": _age_seconds(snapshot.state.last_cycle_at, now),
            "plan": None,
        }
        plan = snapshot.plan
        if plan is not None:
            entry["plan"] = {
                "planned_at": _iso(plan.planned_at),
                "decision": plan.action,
                "sleep_seconds": round(plan.sleep_seconds, 3),
                "next_wake_at": _iso(plan.next_wake_at),
                "schedule_open": plan.schedule_open,
                "disk_ok": plan.disk_ok,
                "next_change": _iso(plan.next_change),
                "next_anchor_wake": _iso(plan.next_anchor_wake),
                "next_retry_at": _iso(plan.next_retry_at),
            }
        runners.append(entry)
    runners.sort(key=lambda item: int(item["project_id"]))
    return runners


def build_task_dump(
    loop: asyncio.AbstractEventLoop | None,
    supervisor: CaptureSupervisor | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The full SIGUSR2 / ``debug/tasks`` payload as one plain dict.

    Call on ``loop``'s thread (it lists the loop's tasks).
    """
    executors = executors_snapshot(loop)
    return {
        "generated_at": _iso(now if now is not None else datetime.now(UTC)),
        "tasks": tasks_snapshot(loop),
        # The default executor alone, kept for existing readers.
        "executor": {k: v for k, v in executors[0].items() if k != "name"},
        "executors": executors,
        "runners": runner_snapshots(supervisor, now),
    }


# ---------------------------------------------------------------------------
# SIGUSR2: log the task dump
# ---------------------------------------------------------------------------


def _summarise_awaiting(task: dict[str, Any]) -> str:
    """One-line summary of a task's innermost await, for the text log format."""
    awaiting = task.get("awaiting")
    if not isinstance(awaiting, dict):
        return "-"
    parts = [str(awaiting.get("kind", "?"))]
    if "function" in awaiting:
        parts.append(f"function={awaiting['function']}")
    if "delay_seconds" in awaiting:
        parts.append(f"delay={awaiting['delay_seconds']}")
    waiting_on = task.get("waiting_on")
    if isinstance(waiting_on, dict) and waiting_on.get("type") == "task":
        parts.append(f"on_task={waiting_on.get('name')}")
    return " ".join(parts)


def dump_tasks(
    loop: asyncio.AbstractEventLoop | None,
    supervisor: CaptureSupervisor | None,
) -> None:
    """Log every asyncio task, the executor, and each runner. Never raises.

    One WARNING record per task (so the dump is visible at any configured
    level), then one per executor pool (the default, ``tlm-capture``,
    ``tlm-jobs``) and one per capture runner. The
    message is self-contained for the plain-text format; the JSON format also
    carries the full structured entry under ``diag``.
    """
    try:
        dump = build_task_dump(loop, supervisor)
        tasks = dump["tasks"]
        logger.warning(
            "asyncio task dump begin: %d task(s)",
            len(tasks),
            extra={"diag_kind": "asyncio_dump_begin"},
        )
        for task in tasks:
            logger.warning(
                "asyncio task name=%s state=%s awaiting=%s at=%s stack=%s",
                task.get("name"),
                task.get("state", task.get("error")),
                _summarise_awaiting(task),
                task.get("awaited_from") or "-",
                " <- ".join(reversed(task.get("stack", []))) or "-",
                extra={"diag_kind": "asyncio_task", "diag": task},
            )
        for executor in dump["executors"]:
            logger.warning(
                "%s executor created=%s max_workers=%s threads=%s "
                "live_threads=%s active=%s%s queue_depth=%s",
                executor.get("name"),
                executor.get("created"),
                executor.get("max_workers"),
                executor.get("threads"),
                executor.get("live_threads"),
                executor.get("active"),
                " (estimate)" if "active_estimate" in executor else "",
                executor.get("queue_depth"),
                extra={"diag_kind": "executor", "diag": executor},
            )
        # faulthandler (SIGUSR1) prints thread idents only; this map names them.
        for ident, native_id, name in thread_name_map():
            logger.warning(
                "thread ident=%s native_id=%s name=%s",
                ident,
                native_id,
                name,
                extra={"diag_kind": "thread_name"},
            )
        for runner in dump["runners"]:
            state = runner.get("state", {})
            plan = runner.get("plan") or {}
            logger.warning(
                "capture runner project=%s task=%s task_done=%s state=%s "
                "last_cycle_at=%s age=%ss decision=%s next_wake_at=%s restarts=%s",
                runner.get("project_id"),
                runner.get("task_name"),
                runner.get("task_done"),
                state.get("state"),
                state.get("last_cycle_at"),
                runner.get("last_cycle_age_seconds"),
                plan.get("decision"),
                plan.get("next_wake_at"),
                state.get("restarts"),
                extra={"diag_kind": "capture_runner", "diag": runner},
            )
        logger.warning("asyncio task dump end", extra={"diag_kind": "asyncio_dump_end"})
    except Exception:  # noqa: BLE001 - a diagnostic must never take the loop down
        with contextlib.suppress(Exception):
            logger.exception("asyncio task dump failed")


def install_task_dump_signal(
    loop: asyncio.AbstractEventLoop, handler: Callable[[], None]
) -> bool:
    """Run ``handler`` on ``loop`` whenever the process receives ``SIGUSR2``.

    Returns whether the handler was installed. Not available on Windows, and
    ``add_signal_handler`` only works from the main thread (a test client runs
    the lifespan elsewhere); either case is logged and skipped, never raised.
    The handler runs *on* the loop, so a dump that never appears means the loop
    itself is blocked -- use ``SIGUSR1`` (threads) or ``py-spy`` instead.
    """
    sig = getattr(signal, "SIGUSR2", None)
    if sig is None:
        return False
    try:
        loop.add_signal_handler(sig, handler)
    except (NotImplementedError, RuntimeError, ValueError):
        logger.debug("SIGUSR2 task-dump handler not installed (not the main thread)")
        return False
    except Exception:  # noqa: BLE001 - diagnostics must never block startup
        logger.warning("could not install the SIGUSR2 task-dump handler")
        return False
    return True


def remove_task_dump_signal(loop: asyncio.AbstractEventLoop) -> None:
    """Undo :func:`install_task_dump_signal`. Never raises.

    ``remove_signal_handler`` restores the default disposition, which for
    ``SIGUSR2`` is to terminate; by then the process is shutting down anyway.
    """
    sig = getattr(signal, "SIGUSR2", None)
    if sig is None:
        return
    with contextlib.suppress(Exception):
        loop.remove_signal_handler(sig)


# ---------------------------------------------------------------------------
# Event-loop lag
# ---------------------------------------------------------------------------


class LoopLagMonitor:
    """Measure event-loop lag with a cheap 1 s ticker task named ``loop-lag``.

    Each tick sleeps ``interval`` seconds and records how much later than
    planned it woke. A healthy loop reports a few milliseconds; a loop blocked
    by synchronous work reports the length of the block. :attr:`lag_seconds` is
    the worst tick over the last ``window`` ticks (a minute by default), so a
    scrape every few minutes still sees a brief stall rather than only the
    latest, usually healthy, tick. The ticker also holds the loop reference, so
    a metrics scrape served from a worker thread can read the loop's executor
    statistics.
    """

    def __init__(self, interval_seconds: float = 1.0, window: int = 60) -> None:
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._samples: deque[float] = deque(maxlen=max(1, window))
        self._started = False

    @property
    def lag_seconds(self) -> float:
        """Worst lag over the recent window, in seconds (0.0 before any tick)."""
        samples = list(self._samples)
        return max(samples) if samples else 0.0

    @property
    def last_lag_seconds(self) -> float:
        """Lag of the most recent tick, in seconds (0.0 before any tick)."""
        return self._samples[-1] if self._samples else 0.0

    def recent_lags(self, count: int) -> list[float]:
        """Lag of the last ``count`` ticks, oldest first (fewer before warm-up)."""
        samples = list(self._samples)
        return samples[-count:] if count > 0 else []

    def start(self) -> None:
        """Start the ticker on the running loop. Idempotent."""
        if self._started:
            return
        self._started = True
        self.loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._run(), name=_LOOP_LAG_TASK_NAME)

    def is_running(self) -> bool:
        """Whether the ticker task is alive."""
        return self._task is not None and not self._task.done()

    async def stop(self) -> None:
        """Cancel and await the ticker. Idempotent; never raises."""
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _run(self) -> None:
        while True:
            planned = time.monotonic() + self._interval
            await asyncio.sleep(self._interval)
            self._samples.append(max(0.0, time.monotonic() - planned))


# ---------------------------------------------------------------------------
# Database work on the event loop: requests (#46-#50), background work (#62),
# startup and shutdown (#66 L4)
# ---------------------------------------------------------------------------

# Modules whose functions are route handlers; the outermost such frame names the
# offending route. Anything else (a background delivery task) falls back to the
# outermost application frame.
_HANDLER_MODULE_PREFIXES = ("timelapse_manager.web.routers", "timelapse_manager.api")


def _offending_function(frame: FrameType | None) -> str:
    """Name the route handler (or outermost app function) on ``frame``'s stack."""
    handler: str | None = None
    outermost: str | None = None
    while frame is not None:
        module = str(frame.f_globals.get("__name__", ""))
        if module.startswith(_PACKAGE_LOGGER + "."):
            name = f"{module}.{frame.f_code.co_name}"
            outermost = name
            if module.startswith(_HANDLER_MODULE_PREFIXES):
                handler = name
        frame = frame.f_back
    return handler or outermost or "unknown"


# The session plumbing and this module never name the origin of loop work.
_ORIGIN_EXCLUDED_PREFIXES = (_PACKAGE_LOGGER + ".db", __name__)
_COROUTINE_FLAGS = inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR
# The frame from which the event loop runs a callback or a task step: frames
# above it belong to the loop (and whatever started it), not to the work.
_HANDLE_RUN_CODE = asyncio.events.Handle._run.__code__


def _loop_work_origin(frame: FrameType | None) -> str | None:
    """Name the application code doing database work on the loop, off a request.

    The innermost application coroutine on the stack (``module:qualname``),
    else the innermost application function outside the session plumbing; the
    same walk as the test suite's guard (``tests/loop_guard_support.py``),
    with the package name standing in for the repository path. ``None`` when
    no application frame is on the stack (a library's own work).
    """
    function: FrameType | None = None
    while frame is not None:
        code = frame.f_code
        if code is _HANDLE_RUN_CODE:
            break
        module = str(frame.f_globals.get("__name__", ""))
        if module.startswith(_PACKAGE_LOGGER + ".") and not module.startswith(
            _ORIGIN_EXCLUDED_PREFIXES
        ):
            if code.co_flags & _COROUTINE_FLAGS:
                return f"{module}:{code.co_qualname}"
            if function is None:
                function = frame
        frame = frame.f_back
    if function is None:
        return None
    module = str(function.f_globals.get("__name__", ""))
    return f"{module}:{function.f_code.co_qualname}"


def _current_task_name() -> str:
    """The running task's name, or ``-`` outside a task. Never raises."""
    try:
        task = asyncio.current_task()
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        return "-"
    return task.get_name() if task is not None else "-"


class LoopDbWatch:
    """Warn once per origin when database work runs on the event loop.

    A Session call on the event loop -- from an ``async def`` route, a
    background task or the lifespan -- makes every request wait while it runs,
    and a query that meets SQLite's write lock stalls them for the busy timeout
    (#37). The test suite fails on these (``tests/loop_guard_support.py``);
    this is the production signal, enabled with
    ``observability.debug_endpoints_enabled``.

    Listeners on the application's engine (``before_cursor_execute``) and its
    pool (``checkout``, which fires even when a query never reaches the
    cursor) check whether they run on a thread with a running event loop.
    During a request (its id is set) the offending route handler is named;
    otherwise the innermost application coroutine on the stack, with the
    asyncio task's name in the message (#66). One WARNING per origin per
    process. Thread hops inherit the request id but run with no loop, so they
    are never reported.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def install(self) -> None:
        """Start watching the engine and its pool. Idempotent."""
        if not event.contains(self._engine, "before_cursor_execute", self._check):
            event.listen(self._engine, "before_cursor_execute", self._check)
        if not event.contains(self._engine, "checkout", self._check):
            event.listen(self._engine, "checkout", self._check)

    def remove(self) -> None:
        """Stop watching the engine and its pool. Idempotent."""
        if event.contains(self._engine, "before_cursor_execute", self._check):
            event.remove(self._engine, "before_cursor_execute", self._check)
        if event.contains(self._engine, "checkout", self._check):
            event.remove(self._engine, "checkout", self._check)

    def _check(self, *_args: Any) -> None:
        if asyncio._get_running_loop() is None:  # noqa: SLF001 - non-raising probe
            return
        request_id = request_id_ctx.get()
        frame = sys._getframe(1)  # noqa: SLF001 - cheap stack walk
        if request_id is not None:
            where = _offending_function(frame)
        else:
            origin = _loop_work_origin(frame)
            if origin is None:
                return
            where = origin
        with self._lock:
            if where in self._seen:
                return
            self._seen.add(where)
        if request_id is not None:
            logger.warning(
                "database query on the event loop in %s (request %s); it stalls"
                " every request while it runs (see #46-#50)",
                where,
                request_id,
            )
        else:
            logger.warning(
                "database work on the event loop in %s (task %s); it stalls every"
                " request while it runs (see #62, #66)",
                where,
                _current_task_name(),
            )


# ---------------------------------------------------------------------------
# Temporary log-level overrides
# ---------------------------------------------------------------------------


def validate_logger_name(name: str) -> str:
    """Return ``name`` if it is ``timelapse_manager`` or one of its children.

    :raises ValueError: for any other logger (the root, a library, or a
        look-alike such as ``timelapse_managerX``).
    """
    if not _LOGGER_NAME_PATTERN.match(name):
        raise ValueError("logger name must be a dotted Python identifier")
    if name != _PACKAGE_LOGGER and not name.startswith(_PACKAGE_LOGGER + "."):
        raise ValueError(f"logger must be {_PACKAGE_LOGGER!r} or one of its children")
    return name


@dataclass
class _Override:
    """One active override: the level to restore and when to restore it."""

    original_level: int
    level: int
    expires_at: float
    expires_at_wall: datetime


class LogLevelOverrides:
    """Temporarily change a ``timelapse_manager.*`` logger's level, then revert.

    :meth:`apply` sets the level at once (logging is thread-safe) and schedules
    a timer task, ``log-level-revert``, on the server's loop that restores the
    logger's *original* level when the override expires. A child logger
    normally has no level of its own (``NOTSET``, inheriting the package's), so
    the original is restored exactly rather than forced to ``INFO``. Applying
    again to a logger that is already overridden replaces the level and expiry
    but keeps the original baseline. :meth:`stop` reverts everything.

    ``clock`` and ``sleep`` are injectable so the expiry can be tested without
    waiting.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._loop = loop
        self._clock = clock
        self._sleep = sleep
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._overrides: dict[str, _Override] = {}
        self._task: asyncio.Task[None] | None = None

    def apply(self, logger_name: str, level: str, minutes: int) -> dict[str, Any]:
        """Set ``logger_name`` to ``level`` for ``minutes``. Callable from any thread.

        :raises ValueError: for a logger outside the package, an unknown level,
            or a duration outside ``1..MAX_OVERRIDE_MINUTES``.
        """
        validate_logger_name(logger_name)
        if level not in LOG_LEVELS:
            raise ValueError(f"level must be one of {', '.join(LOG_LEVELS)}")
        if not 1 <= minutes <= MAX_OVERRIDE_MINUTES:
            raise ValueError(f"minutes must be between 1 and {MAX_OVERRIDE_MINUTES}")
        numeric = logging.getLevelName(level)
        target = logging.getLogger(logger_name)
        with self._lock:
            existing = self._overrides.get(logger_name)
            original = existing.original_level if existing else target.level
            override = _Override(
                original_level=original,
                level=numeric,
                expires_at=self._clock() + minutes * 60,
                expires_at_wall=self._wall_clock() + timedelta(minutes=minutes),
            )
            self._overrides[logger_name] = override
            target.setLevel(numeric)
        # The timer lives on the server's loop; a request handler may run in a
        # worker thread, so hand the (re)schedule over thread-safely.
        self._loop.call_soon_threadsafe(self._reschedule)
        logger.warning(
            "log level override: logger=%s level=%s minutes=%d (reverts to %s)",
            logger_name,
            level,
            minutes,
            logging.getLevelName(original),
        )
        return {
            "logger": logger_name,
            "level": level,
            "minutes": minutes,
            "previous_level": logging.getLevelName(original),
            "expires_at": override.expires_at_wall.isoformat(),
        }

    def active(self) -> list[dict[str, Any]]:
        """The overrides currently in force."""
        with self._lock:
            return [
                {
                    "logger": name,
                    "level": logging.getLevelName(item.level),
                    "previous_level": logging.getLevelName(item.original_level),
                    "expires_at": item.expires_at_wall.isoformat(),
                }
                for name, item in sorted(self._overrides.items())
            ]

    def revert_expired(self) -> list[str]:
        """Restore every override whose expiry has passed; return their names."""
        now = self._clock()
        reverted: list[str] = []
        with self._lock:
            for name, item in list(self._overrides.items()):
                if item.expires_at <= now:
                    logging.getLogger(name).setLevel(item.original_level)
                    del self._overrides[name]
                    reverted.append(name)
        for name in reverted:
            logger.warning("log level override expired: logger=%s reverted", name)
        return reverted

    def _next_expiry(self) -> float | None:
        with self._lock:
            if not self._overrides:
                return None
            return min(item.expires_at for item in self._overrides.values())

    def _reschedule(self) -> None:
        """(Re)start the revert timer so it wakes at the earliest expiry."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = self._loop.create_task(self._revert_loop(), name=_REVERT_TASK_NAME)

    async def _revert_loop(self) -> None:
        while True:
            expiry = self._next_expiry()
            if expiry is None:
                return
            await self._sleep(max(0.0, expiry - self._clock()))
            self.revert_expired()

    @property
    def timer_task(self) -> asyncio.Task[None] | None:
        """The current revert timer task, if any (exposed for tests)."""
        return self._task

    async def stop(self) -> None:
        """Cancel the timer and restore every logger now. Never raises."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        with self._lock:
            for name, item in self._overrides.items():
                logging.getLogger(name).setLevel(item.original_level)
            self._overrides.clear()
