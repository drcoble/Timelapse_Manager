"""Test-only guard: no write transaction held open across a request's await (#66).

SQLite has one writer. A request that writes (an ``UPDATE`` the session flushes)
and then, before committing, awaits something slow -- a camera probe, a sleep, a
network call -- holds the database write lock for that whole wait, so every
other writer (capture above all) waits out the busy timeout behind it. This is
the #66 C1 failure mode: the session re-check's ``UPDATE`` stayed uncommitted
while a probe route awaited the camera. WP-66-A fixed the known sites; this
guard keeps the class fixed.

What is watched, per request:

* An ``after_cursor_execute`` listener (every ``Engine``) marks the request's
  DBAPI connection when it runs a DML statement (``INSERT``, ``UPDATE``,
  ``DELETE``, ``REPLACE``). The request is known through a context variable,
  which a threadpool hop inherits, so a write in a ``def`` route, a sync
  dependency or a ``run_in_threadpool`` hop counts.
* ``commit`` and ``rollback`` (every ``Engine``) and pool ``checkin`` (every
  ``Pool``) clear the mark: the write lock is gone.
* The request's coroutine is driven through :func:`watch`, which looks at every
  point where the request task suspends. A suspension while one of its
  connections is still marked is a violation, **unless** the request has a
  thread hop in flight: a hop that commits, or that is the sync route itself,
  is how the work is meant to be done. Hops are counted where every one of them
  passes, so the count holds on uvloop (the e2e server) as on asyncio:
  ``anyio.to_thread.run_sync`` (``run_in_threadpool``, ``def`` routes, sync
  dependencies) and ``ThreadPoolExecutor.submit`` (``asyncio.to_thread``,
  ``executors.run_in``, any ``run_in_executor``). A hop the request starts but
  does not await would hide a later wait; no route does that.

There is no wall-time threshold: it would flake on a loaded CI runner. A
violation is recorded once per request, keyed like the loop guard's request
violations (``"METHOD /route/template"``), and allowed only through
:data:`KNOWN_OPEN_WRITE_TXN`, which is empty and must stay empty.

Known gap: work a route moves into a task of its own (an anyio task group, a
streamed response body) suspends outside the watched coroutine, so it is not
checked. The session dependency commits before the response is sent, so a
streamed body never carries a write transaction today.

:func:`install` is called by :func:`tests.loop_guard_support.install`, which
also wires the request coroutine through :func:`watch` and turns each
violation into a ``LoopDbViolation`` (``guard="write-txn"``), so the autouse
fixture in ``tests/conftest.py`` and the e2e server's file sink handle both
guards the same way.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import re
import threading
from collections.abc import Callable, Coroutine, Generator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Any

import anyio.to_thread
from sqlalchemy import Engine, event
from sqlalchemy.pool import Pool
from starlette.types import Scope

# Requests that hold a write transaction open across an await, as
# "METHOD /route/template" -> the issue that commits before the await. Empty
# since #66 (WP-66-A committed the session re-check before the probe routes'
# camera I/O, and the record run of the whole suite after WP-66-K found
# nothing). It must stay empty: commit in the last thread hop before the await.
KNOWN_OPEN_WRITE_TXN: dict[str, int] = {}

_DML = re.compile(r"^\s*(?:INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)

# Bound on the await chain walked at each suspension.
_MAX_CHAIN = 64
_STACK_DEPTH = 8
_REPO_ROOT = str(Path(__file__).resolve().parents[1]) + os.sep
_VENV_MARKER = f"{os.sep}site-packages{os.sep}"

#: Called with ``(scope, statement, stack)`` for each violation.
ViolationSink = Callable[[Scope, str, tuple[str, ...]], None]


@dataclass(eq=False)
class _RequestTxn:
    """One request's open write transactions, keyed by DBAPI connection id."""

    scope: Scope
    open_writes: dict[int, str] = field(default_factory=dict)
    hops: int = 0
    reported: bool = False


_current: contextvars.ContextVar[_RequestTxn | None] = contextvars.ContextVar(
    "txn_guard_request", default=None
)
_lock = threading.Lock()
# DBAPI connection id -> the request whose write it carries.
_owner: dict[int, _RequestTxn] = {}
_sink: ViolationSink | None = None
_installed = False


def _dbapi_id(conn: Any) -> int | None:
    """Return the id of a SQLAlchemy ``Connection``'s DBAPI connection."""
    try:
        return id(conn.connection.dbapi_connection)
    except Exception:  # noqa: BLE001 - an invalidated connection has none
        return None


def _after_cursor_execute(
    conn: Any,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
) -> None:
    state = _current.get()
    if state is None or not _DML.match(statement):
        return
    key = _dbapi_id(conn)
    if key is None:
        return
    with _lock:
        state.open_writes.setdefault(key, " ".join(statement.split())[:120])
        _owner[key] = state


def _clear(key: int | None) -> None:
    if key is None:
        return
    with _lock:
        state = _owner.pop(key, None)
        if state is not None:
            state.open_writes.pop(key, None)


def _on_commit(conn: Any) -> None:
    _clear(_dbapi_id(conn))


def _on_rollback(conn: Any) -> None:
    _clear(_dbapi_id(conn))


def _on_checkin(dbapi_connection: Any, record: Any) -> None:
    _clear(id(dbapi_connection) if dbapi_connection is not None else None)


def _chain(coro: Any) -> Generator[Any, None, None]:
    """Yield ``coro`` and everything it is awaiting, outermost first."""
    current = coro
    for _ in range(_MAX_CHAIN):
        if current is None:
            return
        yield current
        nxt = getattr(current, "cr_await", None)
        if nxt is None:
            nxt = getattr(current, "gi_yieldfrom", None)
        current = nxt


def _frame(link: Any) -> FrameType | None:
    frame = getattr(link, "cr_frame", None) or getattr(link, "gi_frame", None)
    return frame if isinstance(frame, FrameType) else None


def _enter_hop(state: _RequestTxn) -> None:
    with _lock:
        state.hops += 1


def _leave_hop(state: _RequestTxn) -> None:
    with _lock:
        state.hops -= 1


_original_run_sync = anyio.to_thread.run_sync
_original_submit = ThreadPoolExecutor.submit


async def _counted_run_sync(func: Any, *args: Any, **kwargs: Any) -> Any:
    """``anyio.to_thread.run_sync``, counted as a hop of the current request."""
    state = _current.get()
    if state is None:
        return await _original_run_sync(func, *args, **kwargs)
    _enter_hop(state)
    try:
        return await _original_run_sync(func, *args, **kwargs)
    finally:
        _leave_hop(state)


def _counted_submit(
    self: ThreadPoolExecutor, fn: Any, /, *args: Any, **kwargs: Any
) -> Future[Any]:
    """``ThreadPoolExecutor.submit``, counted as a hop of the current request.

    The count drops on the event loop, through ``call_soon_threadsafe`` from the
    worker's done callback, not in the worker: a fast job can finish before the
    request task has even suspended on it, and the task's suspension must still
    see the hop in flight. The callback is added before the loop's own
    (``wrap_future`` chains after ``submit`` returns), so the count drops before
    the awaiting task resumes. Off a loop (a nested hop), it drops in the worker.
    """
    future = _original_submit(self, fn, *args, **kwargs)
    state = _current.get()
    if state is None:
        return future
    _enter_hop(state)
    loop = asyncio._get_running_loop()  # noqa: SLF001 - non-raising probe

    def leave(_done: Future[Any]) -> None:
        if loop is not None:
            try:
                loop.call_soon_threadsafe(_leave_hop, state)
                return
            except RuntimeError:  # the loop has closed
                pass
        _leave_hop(state)

    future.add_done_callback(leave)
    return future


def _await_stack(coro: Any) -> tuple[str, ...]:
    """The repository frames of the suspended chain, innermost last."""
    frames: list[str] = []
    for link in _chain(coro):
        frame = _frame(link)
        if frame is None:
            continue
        filename = frame.f_code.co_filename
        if (
            filename.startswith(_REPO_ROOT)
            and _VENV_MARKER not in filename
            and filename != __file__
        ):
            name = frame.f_code.co_name
            frames.append(f"{Path(filename).name}:{frame.f_lineno} {name}")
    return tuple(frames[-_STACK_DEPTH:])


def _check_suspension(state: _RequestTxn, coro: Any) -> None:
    """Record a violation if the request suspends holding a write, off a hop."""
    if state.reported:
        return
    with _lock:
        statement = next(iter(state.open_writes.values()), None)
        in_hop = state.hops > 0
    if statement is None or in_hop:
        return
    state.reported = True
    if _sink is not None:
        _sink(state.scope, statement, _await_stack(coro))


class _Watched:
    """Drive a request coroutine, checking each suspension (see module docs)."""

    def __init__(self, coro: Coroutine[Any, Any, Any], state: _RequestTxn) -> None:
        self._coro = coro
        self._state = state

    def __await__(self) -> Generator[Any, Any, Any]:
        coro = self._coro
        to_send: Any = None
        to_throw: BaseException | None = None
        while True:
            try:
                if to_throw is None:
                    yielded = coro.send(to_send)
                else:
                    error, to_throw = to_throw, None
                    yielded = coro.throw(error)
            except StopIteration as stop:
                return stop.value
            _check_suspension(self._state, coro)
            try:
                to_send = yield yielded
            except GeneratorExit:
                coro.close()
                raise
            except BaseException as exc:  # noqa: BLE001 - forwarded verbatim
                to_send = None
                to_throw = exc


async def watch(coro: Coroutine[Any, Any, Any], scope: Scope) -> Any:
    """Run a request's ASGI coroutine under the write-transaction guard."""
    state = _RequestTxn(scope)
    token = _current.set(state)
    try:
        return await _Watched(coro, state)
    finally:
        _current.reset(token)
        with _lock:
            for key in list(state.open_writes):
                if _owner.get(key) is state:
                    del _owner[key]
            state.open_writes.clear()


def install(sink: ViolationSink) -> None:
    """Install the SQLAlchemy listeners and set the violation sink (idempotent)."""
    global _installed, _sink
    _sink = sink
    if _installed:
        return
    if not event.contains(Engine, "after_cursor_execute", _after_cursor_execute):
        event.listen(Engine, "after_cursor_execute", _after_cursor_execute)
    if not event.contains(Engine, "commit", _on_commit):
        event.listen(Engine, "commit", _on_commit)
    if not event.contains(Engine, "rollback", _on_rollback):
        event.listen(Engine, "rollback", _on_rollback)
    if not event.contains(Pool, "checkin", _on_checkin):
        event.listen(Pool, "checkin", _on_checkin)
    # Module attributes, looked up at each call by Starlette and FastAPI.
    anyio.to_thread.run_sync = _counted_run_sync  # type: ignore[assignment]
    ThreadPoolExecutor.submit = _counted_submit  # type: ignore[method-assign]
    _installed = True
