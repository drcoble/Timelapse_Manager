"""Test-only guard: no database work on the event loop (#46-#50, #62).

FastAPI runs a plain ``def`` route (and a sync dependency) in the threadpool, but
an ``async def`` route runs on the event loop. A Session call from there makes
the whole server wait for SQLite: a write that meets the write lock stalls every
request for up to the busy timeout, which is the #37 failure mode.

:func:`install` adds two process-wide hooks:

* ``FastAPI.__call__`` is wrapped so every HTTP and websocket request runs with
  its ASGI scope in a context variable. Starlette's router later adds the
  matched route to that same scope dict, so the route is known by the time the
  handler queries. A lifespan scope is marked separately (see *Startup and
  shutdown* below).
* SQLAlchemy ``before_cursor_execute`` (every ``Engine``) and pool ``checkout``
  (every ``Pool``) listeners record a :class:`LoopDbViolation` when they run on
  a thread with a running event loop while a request is marked. Work handed to
  the threadpool (``def`` routes, ``run_in_threadpool``, ``asyncio.to_thread``)
  inherits the context variable but runs on a worker thread with no loop, so it
  is not recorded.

Background work is watched too (#62). A statement on the event-loop thread with
no request marked is a *background* violation when the asyncio task running it
was started from the application's own code -- its root coroutine lives under
``src/timelapse_manager/``: the render queue's drain loop and job tasks, the
render scheduler, the notification dispatcher and its sends, report delivery, the
capture supervisor's runners and listeners. Such a violation is keyed by the
innermost application coroutine on the stack, as ``module:qualname`` (for
example ``timelapse_manager.render.queue:RenderQueue._run_job``), and allowed
only through :data:`KNOWN_BACKGROUND_LOOP_DB`.

**Startup and shutdown** (#66 L4) are watched too. The lifespan runs in the
server's own lifespan task (uvicorn's or the test client's), whose root is not
application code, so it is not background work; the wrapped ``__call__`` marks
it instead. A statement on the loop while the lifespan is marked is a
*lifespan* violation, keyed by the innermost repository coroutine on the stack
(``timelapse_manager.app:_lifespan``, or a component's ``start``/``stop``), and
allowed only through :data:`KNOWN_LIFESPAN_LOOP_DB`. Startup work that blocks
the loop delays every request the server would otherwise serve and the shutdown
of every other component, so it hops to a thread like everything else. Tasks
*started* by the lifespan inherit the mark, but their root is the component's
own coroutine, so they stay background work.

**Test code** is exempt: a test body or async fixture runs in a task rooted in
the test framework or ``tests/``, so a test that queries on its own loop is not
flagged.

The write-transaction guard (``tests/txn_guard_support.py``) rides on the same
hook: each request's coroutine runs through it, and a request that suspends
with an uncommitted write, outside a thread hop, is recorded here as a
*write-txn* violation, allowed only through ``KNOWN_OPEN_WRITE_TXN``.

The autouse fixture in ``tests/conftest.py`` fails a ``tests/web``,
``tests/api`` or ``tests/e2e`` test on any request, lifespan or write-txn
violation that is not allow-listed, and a test in *any* directory on a
background violation whose origin is not in :data:`KNOWN_BACKGROUND_LOOP_DB`
(the render queue's cancel path is exercised mostly by ``tests/integration``).
The e2e live server runs in another process: it installs the guard with a file sink
(``_serve.py --loop-guard-log``) and the fixture reads that file after the
server has stopped.

Set ``TLM_LOOP_GUARD_RECORD=<dir>`` to run in record-only mode: nothing fails,
and every violation in any test directory is appended as JSON lines to
``<dir>/<worker>.jsonl`` for review.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import os
import sys
import threading
import traceback
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from types import FrameType
from typing import Any

from fastapi import FastAPI
from sqlalchemy import Engine, event
from sqlalchemy.pool import Pool
from starlette.types import Receive, Scope, Send

from tests import txn_guard_support
from tests.txn_guard_support import KNOWN_OPEN_WRITE_TXN

# Routes that query on the event loop today, as "METHOD /route/template" (the
# route's path exactly as declared, converters included) -> the issue that moves
# their database work off the loop. Populated from a record-only run of the full
# suite, so it holds exactly the routes that trip. Each fix PR deletes its
# entries; once this is empty, the allow-list goes and any new loop-DB route
# fails CI.
KNOWN_LOOP_DB: dict[str, int] = {}

# Background work that queries on the event loop, as the innermost application
# coroutine on the stack ("module:qualname") -> the issue that moves it off the
# loop. Empty since #63 moved the last capture reads (the frame-cap guard and the
# listener's default-credentials lookup) off the loop; it must stay empty. A new
# background loop-DB origin is fixed, not listed: route the read through the
# capture executor seam (``CaptureSupervisor._run_blocking``) or a thread hop.
# tests/unit/capture/test_supervisor_loop_db.py asserts this list and
# KNOWN_LOOP_DB stay empty.
KNOWN_BACKGROUND_LOOP_DB: dict[str, int] = {}

# Startup and shutdown work that queries on the event loop, as the innermost
# repository coroutine on the stack ("module:qualname") -> the issue that moves
# it off the loop. Empty since #66 L4 moved the lifespan's SSRF subnet merge
# into a thread; it must stay empty. Hop to a thread (``asyncio.to_thread``)
# instead of listing a new origin.
KNOWN_LIFESPAN_LOOP_DB: dict[str, int] = {}

# Values of ``LoopDbViolation.guard``.
GUARD_LOOP = "loop"
GUARD_LIFESPAN = "lifespan"
GUARD_WRITE_TXN = "write-txn"

# Directories whose tests fail on an unexpected violation.
GATED_TEST_DIRS = ("web", "api", "e2e")

RECORD_ENV = "TLM_LOOP_GUARD_RECORD"

# Frames kept from the repository's own code (src and tests), innermost last.
_STACK_DEPTH = 8
_REPO_ROOT = str(Path(__file__).resolve().parents[1]) + os.sep
_VENV_MARKER = f"{os.sep}site-packages{os.sep}"

# Where application code lives: a task whose root coroutine is defined here is
# background work, and the innermost coroutine frame here names its origin. A
# tuple so the guard's own tests can add a test module.
_APP_ROOTS: tuple[str, ...] = (
    str(Path(__file__).resolve().parents[1] / "src" / "timelapse_manager") + os.sep,
)
# The repository's own code (not its virtualenv or scripts): the outermost frame
# from here decides whether a loop callback is application or test work.
_REPO_CODE_ROOTS = (_REPO_ROOT + "src" + os.sep, _REPO_ROOT + "tests" + os.sep)
# The session plumbing is never the origin worth naming.
_APP_DB_PACKAGE = "timelapse_manager.db"
_ASYNC_FLAGS = inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR
# The frame from which the event loop runs a callback or a task step.
_HANDLE_RUN_CODE = asyncio.events.Handle._run.__code__

_request_scope: contextvars.ContextVar[Scope | None] = contextvars.ContextVar(
    "loop_guard_request_scope", default=None
)
_in_lifespan: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "loop_guard_in_lifespan", default=False
)
# The ASGI scope types that are requests (marked and watched per request).
_REQUEST_SCOPE_TYPES = frozenset({"http", "websocket"})
# Guard modules never name the origin.
_GUARD_FILES = frozenset({__file__, txn_guard_support.__file__})

_lock = threading.Lock()
_records: list[LoopDbViolation] = []
_file_sink: Path | None = None
_server_logs: list[Path] = []
_installed = False


@dataclass(frozen=True)
class LoopDbViolation:
    """One guard violation: a request, background work or the lifespan.

    ``guard`` says which rule broke: ``"loop"`` (database work on the event
    loop in a request or in background work), ``"lifespan"`` (the same in
    startup or shutdown) or ``"write-txn"`` (a request suspended holding an
    uncommitted write; ``statement`` is that write).

    ``origin`` is set for background work and the lifespan (no request being
    served): the innermost application (lifespan: repository) coroutine as
    ``module:qualname``. A request or write-txn violation has ``origin``
    ``None`` and names its method, path and route instead.
    """

    method: str
    path: str
    route: str | None
    kind: str
    statement: str
    stack: tuple[str, ...]
    origin: str | None = None
    guard: str = GUARD_LOOP

    @property
    def background(self) -> bool:
        """Whether this happened in background work rather than a request."""
        return self.origin is not None and self.guard == GUARD_LOOP

    @property
    def lifespan(self) -> bool:
        """Whether this happened in startup or shutdown work."""
        return self.guard == GUARD_LIFESPAN

    @property
    def key(self) -> str:
        """Return the allow-list key.

        ``"METHOD /route/template"`` for a request (:data:`KNOWN_LOOP_DB`, or
        ``KNOWN_OPEN_WRITE_TXN`` for a write-txn violation), the
        ``module:qualname`` origin for background work
        (:data:`KNOWN_BACKGROUND_LOOP_DB`) and the lifespan
        (:data:`KNOWN_LIFESPAN_LOOP_DB`). A request violation before routing
        (middleware) or on an unmatched path has no route template; its key names
        the raw path and can never be allow-listed.
        """
        if self.origin is not None:
            return self.origin
        if self.route is None:
            return f"{self.method} {self.path} (no route: middleware or 404)"
        return f"{self.method} {self.route}"

    def describe(self) -> str:
        """Return a multi-line, human-readable account of the violation."""
        if self.background:
            label = f"background {self.key}"
        elif self.guard == GUARD_LOOP:
            label = self.key
        else:
            label = f"{self.guard} {self.key}"
        lines = [f"{label}  [{self.kind}] {self.statement}"]
        lines.extend(f"    {frame}" for frame in self.stack)
        return "\n".join(lines)


def _on_event_loop() -> bool:
    """Return whether the calling thread is running an asyncio event loop."""
    return asyncio._get_running_loop() is not None  # noqa: SLF001 - non-raising probe


def _app_stack() -> tuple[str, ...]:
    """Return the innermost frames of the repo's own code, as ``file:line func``.

    Library and stdlib frames are dropped so the handler and any helper that
    queries (``base_context``, ``_get_project_or_404``...) stand out.
    """
    frames = [
        f"{Path(f.filename).name}:{f.lineno} {f.name}"
        for f in traceback.extract_stack()
        if f.filename.startswith(_REPO_ROOT)
        and _VENV_MARKER not in f.filename
        and f.filename != __file__
    ]
    return tuple(frames[-_STACK_DEPTH:])


def _is_app_file(filename: str) -> bool:
    """Return whether ``filename`` is application code (see ``_APP_ROOTS``)."""
    return filename.startswith(_APP_ROOTS)


def _background_origin() -> str | None:
    """Name the background work querying on this loop, or ``None`` if exempt.

    Only called on an event-loop thread with no request marked. The current
    task's root coroutine decides: application code means background work; the
    lifespan task, a test body and anything else rooted outside the application
    are exempt (see the module docstring). Outside any task -- a loop callback,
    or a sync test fixture on a thread that also runs a loop (Playwright's sync
    API) -- the outermost repository frame (``src/`` or ``tests/``) inside the
    callback decides the same way. The origin is
    the innermost application coroutine on the stack, else the innermost
    application function outside the session plumbing.
    """
    task = asyncio.current_task()
    if task is not None:
        root = getattr(task.get_coro(), "cr_code", None)
        if root is None or not _is_app_file(root.co_filename):
            return None
    coroutine: FrameType | None = None
    function: FrameType | None = None
    outermost_repo_file: str | None = None
    frame: FrameType | None = sys._getframe(1)  # noqa: SLF001 - cheap stack walk
    while frame is not None:
        filename = frame.f_code.co_filename
        if frame.f_code is _HANDLE_RUN_CODE:
            break  # the loop's own frames (and whatever started it) don't count
        if filename.startswith(_REPO_CODE_ROOTS):
            outermost_repo_file = filename
        if _is_app_file(filename):
            if coroutine is None and frame.f_code.co_flags & _ASYNC_FLAGS:
                coroutine = frame
            elif function is None and not str(
                frame.f_globals.get("__name__", "")
            ).startswith(_APP_DB_PACKAGE):
                function = frame
        frame = frame.f_back
    if task is None and (
        outermost_repo_file is None or not _is_app_file(outermost_repo_file)
    ):
        return None
    named = coroutine or function
    return "?" if named is None else _frame_name(named)


def _lifespan_origin() -> str:
    """Name the startup or shutdown code querying on the loop.

    The innermost repository coroutine on the stack (``src/`` or ``tests/``,
    so a test's own lifespan is named too), else the innermost repository
    function outside the session plumbing and the guards themselves.
    """
    coroutine: FrameType | None = None
    function: FrameType | None = None
    frame: FrameType | None = sys._getframe(1)  # noqa: SLF001 - cheap stack walk
    while frame is not None and coroutine is None:
        code = frame.f_code
        if code is _HANDLE_RUN_CODE:
            break
        if code.co_filename.startswith(_REPO_CODE_ROOTS) and (
            code.co_filename not in _GUARD_FILES
        ):
            if code.co_flags & _ASYNC_FLAGS:
                coroutine = frame
            elif function is None and not str(
                frame.f_globals.get("__name__", "")
            ).startswith(_APP_DB_PACKAGE):
                function = frame
        frame = frame.f_back
    named = coroutine or function
    return "?" if named is None else _frame_name(named)


def _frame_name(frame: FrameType | None) -> str:
    """Return ``module:qualname`` for a frame (``?`` when it is gone)."""
    if frame is None:
        return "?"
    return f"{frame.f_globals.get('__name__', '?')}:{frame.f_code.co_qualname}"


def _record(kind: str, statement: str) -> None:
    """Record a violation if the event loop runs a request, background work or
    the lifespan."""
    if not _on_event_loop():
        return
    scope = _request_scope.get()
    if scope is None:
        guard = GUARD_LOOP
        origin = _background_origin()
        if origin is None:
            if not _in_lifespan.get():
                return
            guard = GUARD_LIFESPAN
            origin = _lifespan_origin()
        violation = LoopDbViolation(
            method="",
            path="",
            route=None,
            kind=kind,
            statement=" ".join(statement.split())[:120],
            stack=_app_stack(),
            origin=origin,
            guard=guard,
        )
    else:
        route = scope.get("route")
        violation = LoopDbViolation(
            method=_scope_method(scope),
            path=str(scope.get("path", "")),
            route=getattr(route, "path", None),
            kind=kind,
            statement=" ".join(statement.split())[:120],
            stack=_app_stack(),
        )
    _store(violation)


def _scope_method(scope: Scope) -> str:
    """The request's HTTP method, or ``WEBSOCKET`` for a websocket scope."""
    return str(scope.get("method") or str(scope.get("type", "")).upper())


def _store(violation: LoopDbViolation) -> None:
    """Keep a violation for :func:`collect` (and the e2e file sink)."""
    with _lock:
        _records.append(violation)
        if _file_sink is not None:
            with _file_sink.open("a", encoding="utf-8") as sink:
                sink.write(json.dumps(asdict(violation)) + "\n")


def _record_open_write_txn(
    scope: Scope, statement: str, stack: tuple[str, ...]
) -> None:
    """Sink for the write-transaction guard (``tests/txn_guard_support.py``)."""
    route = scope.get("route")
    _store(
        LoopDbViolation(
            method=_scope_method(scope),
            path=str(scope.get("path", "")),
            route=getattr(route, "path", None),
            kind="open-write-txn",
            statement=statement,
            stack=stack,
            guard=GUARD_WRITE_TXN,
        )
    )


def _before_cursor_execute(
    conn: Any,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
) -> None:
    _record("execute", statement)


def _on_checkout(dbapi_connection: Any, record: Any, proxy: Any) -> None:
    _record("checkout", "pool checkout")


def install(file_sink: Path | None = None) -> None:
    """Install the request marker, the SQLAlchemy listeners and the
    write-transaction guard (idempotent).

    :param file_sink: also append each violation to this JSON-lines file; used
        by the e2e server subprocess, whose records the test process cannot see.
    """
    global _installed, _file_sink
    if file_sink is not None:
        _file_sink = file_sink
    if _installed:
        return
    original_call = FastAPI.__call__

    async def marked_call(
        self: FastAPI, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] == "lifespan":
            lifespan_token = _in_lifespan.set(True)
            try:
                await original_call(self, scope, receive, send)
            finally:
                _in_lifespan.reset(lifespan_token)
            return
        if scope["type"] not in _REQUEST_SCOPE_TYPES:
            await original_call(self, scope, receive, send)
            return
        token = _request_scope.set(scope)
        try:
            await txn_guard_support.watch(
                original_call(self, scope, receive, send), scope
            )
        finally:
            _request_scope.reset(token)

    FastAPI.__call__ = marked_call  # type: ignore[method-assign]
    if not event.contains(Engine, "before_cursor_execute", _before_cursor_execute):
        event.listen(Engine, "before_cursor_execute", _before_cursor_execute)
    if not event.contains(Pool, "checkout", _on_checkout):
        event.listen(Pool, "checkout", _on_checkout)
    txn_guard_support.install(_record_open_write_txn)
    _installed = True


def watch_server_log(path: Path) -> None:
    """Register an e2e server's violation log for the current test to check."""
    with _lock:
        _server_logs.append(path)


def reset() -> None:
    """Forget every recorded violation and registered server log."""
    with _lock:
        _records.clear()
        _server_logs.clear()


def collect() -> list[LoopDbViolation]:
    """Return this process's violations plus those in registered server logs."""
    with _lock:
        found = list(_records)
        logs = list(_server_logs)
    for log in logs:
        if not log.exists():
            continue
        for line in log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                data = json.loads(line)
                data["stack"] = tuple(data["stack"])
                found.append(LoopDbViolation(**data))
    return found


def unexpected(
    violations: Iterable[LoopDbViolation],
    allowed: dict[str, int] = KNOWN_LOOP_DB,
    allowed_background: dict[str, int] = KNOWN_BACKGROUND_LOOP_DB,
    allowed_lifespan: dict[str, int] = KNOWN_LIFESPAN_LOOP_DB,
    allowed_write_txn: dict[str, int] = KNOWN_OPEN_WRITE_TXN,
) -> list[LoopDbViolation]:
    """Return the first violation per guard and key that is not allow-listed.

    A request violation is checked against ``allowed`` by route, and a
    write-txn violation against ``allowed_write_txn``; one with no route (a
    middleware, or a 404) is always unexpected. A background violation is
    checked against ``allowed_background`` and a lifespan violation against
    ``allowed_lifespan``, both by origin.
    """
    seen: set[tuple[str, str]] = set()
    out: list[LoopDbViolation] = []
    for violation in violations:
        if violation.background:
            listed = violation.key in allowed_background
        elif violation.guard == GUARD_LIFESPAN:
            listed = violation.key in allowed_lifespan
        elif violation.guard == GUARD_WRITE_TXN:
            listed = violation.route is not None and violation.key in allowed_write_txn
        else:
            listed = violation.route is not None and violation.key in allowed
        seen_key = (violation.guard, violation.key)
        if listed or seen_key in seen:
            continue
        seen.add(seen_key)
        out.append(violation)
    return out


def failure_message(violations: list[LoopDbViolation]) -> str:
    """Explain unexpected violations and how to resolve them."""
    body = "\n".join(v.describe() for v in violations)
    return (
        "Database work ran on the event loop, or a request held a write open"
        " across an await.\n"
        "In a request: make the route a plain `def`, or move its Session work into"
        " a sync helper called with `await run_in_threadpool(...)` (see #46-#50).\n"
        "In background work: call the synchronous Session helper with"
        " `await asyncio.to_thread(...)` (see #62).\n"
        "In startup or shutdown (lifespan ...): the same, `await"
        " asyncio.to_thread(...)` (see #66 L4).\n"
        "Holding a write (write-txn ...): commit in the thread hop that wrote,"
        " before awaiting anything else (see #66 C1).\n"
        f"{body}"
    )


def record_to(directory: Path, nodeid: str, violations: list[LoopDbViolation]) -> None:
    """Append ``violations`` for test ``nodeid`` to this worker's record file."""
    if not violations:
        return
    directory.mkdir(parents=True, exist_ok=True)
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    with (directory / f"{worker}.jsonl").open("a", encoding="utf-8") as out:
        for violation in violations:
            entry = asdict(violation)
            entry["key"] = violation.key
            entry["nodeid"] = nodeid
            out.write(json.dumps(entry) + "\n")
