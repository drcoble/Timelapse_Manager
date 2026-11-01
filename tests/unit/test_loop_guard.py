"""The loop-DB guard itself fires, and only where it should (#46-#50, #62).

A minimal app with its own SQLite engine: an ``async def`` route that queries is
recorded under its route template; a ``def`` route, an ``async`` route that hands
the query to ``run_in_threadpool``, and lifespan work done in a thread are not.
A query in a middleware has no route and so can never be allow-listed.

Background work (#62): a task whose root coroutine is application code and that
queries on the loop is recorded under its innermost application coroutine; the
same query through ``asyncio.to_thread`` and a test body are not, and the
lifespan task's own query is a *lifespan* violation (#66 L4,
``tests/unit/test_lifespan_txn_guards.py``), not a background one. This module
stands in for application code by adding itself to the guard's application
roots.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from starlette.middleware.base import BaseHTTPMiddleware

from tests import loop_guard_support as guard


def _query(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("SELECT 1")).scalar_one())


def _build_app(engine: Engine, *, query_in_middleware: bool = False) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Startup and shutdown work hops to a thread (#66 L4).
        await asyncio.to_thread(_query, engine)
        yield
        await asyncio.to_thread(_query, engine)

    app = FastAPI(lifespan=lifespan)

    @app.get("/on-loop/{item_id:int}")
    async def on_loop(item_id: int) -> dict[str, int]:
        return {"value": _query(engine)}

    @app.get("/sync")
    def sync_route() -> dict[str, int]:
        return {"value": _query(engine)}

    @app.get("/threadpool")
    async def via_threadpool() -> dict[str, int]:
        return {"value": await run_in_threadpool(_query, engine)}

    if query_in_middleware:

        async def dispatch(
            request: Request, call_next: Callable[[Request], Awaitable[Response]]
        ) -> Response:
            _query(engine)
            return await call_next(request)

        app.add_middleware(BaseHTTPMiddleware, dispatch=dispatch)
    return app


@pytest.fixture()
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = create_engine(f"sqlite:///{tmp_path / 'guard.db'}")
    guard.reset()
    try:
        yield eng
    finally:
        # These tests violate on purpose; keep them out of the autouse guard and
        # out of a record-only run.
        guard.reset()
        eng.dispose()


def _keys() -> set[str]:
    return {v.key for v in guard.collect()}


def test_async_route_querying_on_the_loop_is_recorded(engine: Engine) -> None:
    with TestClient(_build_app(engine)) as client:
        assert client.get("/on-loop/7").json() == {"value": 1}

    violations = guard.collect()
    assert {v.key for v in violations} == {"GET /on-loop/{item_id:int}"}
    assert {v.kind for v in violations} == {"checkout", "execute"}
    executed = next(v for v in violations if v.kind == "execute")
    assert executed.statement == "SELECT 1"
    assert executed.stack, "the record carries a stack"
    assert guard.unexpected(violations, allowed={}) == [violations[0]]


def test_sync_route_and_threadpool_are_not_recorded(engine: Engine) -> None:
    with TestClient(_build_app(engine)) as client:
        assert client.get("/sync").json() == {"value": 1}
        assert client.get("/threadpool").json() == {"value": 1}

    assert _keys() == set()


def test_lifespan_work_in_a_thread_is_not_recorded(engine: Engine) -> None:
    with TestClient(_build_app(engine)):
        pass

    assert _keys() == set()


def test_allow_listed_route_is_not_unexpected(engine: Engine) -> None:
    with TestClient(_build_app(engine)) as client:
        client.get("/on-loop/1")

    allowed = {"GET /on-loop/{item_id:int}": 0}
    assert guard.unexpected(guard.collect(), allowed=allowed) == []


def test_middleware_query_has_no_route_and_cannot_be_allow_listed(
    engine: Engine,
) -> None:
    with TestClient(_build_app(engine, query_in_middleware=True)) as client:
        client.get("/sync")

    violations = guard.collect()
    assert [v.route for v in violations] == [None, None]  # checkout + execute
    key = violations[0].key
    assert key == "GET /sync (no route: middleware or 404)"
    assert guard.unexpected(violations, allowed={key: 0}) == [violations[0]]


def test_server_log_round_trips(engine: Engine, tmp_path: Path) -> None:
    """The e2e server's file sink is read back as the same violations."""
    with TestClient(_build_app(engine)) as client:
        client.get("/on-loop/2")
    in_process = guard.collect()
    log = tmp_path / "loop-guard.jsonl"
    log.write_text("".join(f"{_as_json(v)}\n" for v in in_process))
    guard.reset()

    guard.watch_server_log(log)
    assert guard.collect() == in_process


def _as_json(violation: guard.LoopDbViolation) -> str:
    return json.dumps(asdict(violation))


@pytest.fixture()
def as_app_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat this test module as application code for the background guard."""
    monkeypatch.setattr(guard, "_APP_ROOTS", (*guard._APP_ROOTS, __file__))


async def _background_job(engine: Engine) -> int:
    """Background work that queries on the event loop."""
    return _query(engine)


async def _background_job_in_thread(engine: Engine) -> int:
    """Background work that hands its query to a worker thread."""
    return await asyncio.to_thread(_query, engine)


_BACKGROUND_KEY = f"{__name__}:_background_job"


def _background() -> list[guard.LoopDbViolation]:
    return [v for v in guard.collect() if v.background]


async def test_background_task_querying_on_the_loop_is_recorded(
    engine: Engine, as_app_code: None
) -> None:
    assert await asyncio.create_task(_background_job(engine)) == 1

    violations = _background()
    assert {v.key for v in violations} == {_BACKGROUND_KEY}
    assert {v.kind for v in violations} == {"checkout", "execute"}
    assert all(v.route is None and v.method == "" for v in violations)
    assert violations[0].describe().startswith(f"background {_BACKGROUND_KEY}")
    assert guard.unexpected(violations, allowed_background={}) == [violations[0]]
    assert guard.unexpected(violations, allowed_background={_BACKGROUND_KEY: 0}) == []


async def test_background_task_using_a_thread_is_not_recorded(
    engine: Engine, as_app_code: None
) -> None:
    assert await asyncio.create_task(_background_job_in_thread(engine)) == 1

    assert guard.collect() == []


async def test_test_body_querying_on_its_loop_is_not_recorded(engine: Engine) -> None:
    """A test's own setup on its loop is exempt: its task is rooted in tests/."""
    assert await _background_job(engine) == 1
    assert await asyncio.create_task(_background_job(engine)) == 1

    assert guard.collect() == []


async def test_loop_callback_running_app_code_is_recorded(
    engine: Engine, as_app_code: None
) -> None:
    done = asyncio.get_running_loop().create_future()

    def callback() -> None:
        done.set_result(_query(engine))

    asyncio.get_running_loop().call_soon(callback)
    assert await done == 1

    keys = {v.key for v in _background()}
    assert keys == {f"{__name__}:_query"}


def test_lifespan_work_and_tasks_it_starts_are_recorded_apart(
    engine: Engine, as_app_code: None
) -> None:
    """The lifespan's own query is a lifespan violation; a task it starts that
    queries is background work, keyed by its own coroutine."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        _query(engine)  # startup work on the loop: a lifespan violation
        task = asyncio.create_task(_background_job(engine))  # a background loop
        await task
        yield

    with TestClient(FastAPI(lifespan=lifespan)):
        pass

    by_guard = {(v.guard, v.key) for v in guard.collect()}
    assert by_guard == {
        ("loop", _BACKGROUND_KEY),
        ("lifespan", f"{__name__}:{lifespan.__qualname__}"),
    }


def test_background_server_log_round_trips(engine: Engine, tmp_path: Path) -> None:
    """Background violations survive the e2e file sink; old lines still parse."""
    violation = guard.LoopDbViolation(
        method="",
        path="",
        route=None,
        kind="execute",
        statement="SELECT 1",
        stack=("queue.py:1 _run_job",),
        origin=_BACKGROUND_KEY,
    )
    legacy = {k: v for k, v in asdict(violation).items() if k != "origin"}
    legacy.update(method="GET", path="/x", route="/x")
    log = tmp_path / "loop-guard.jsonl"
    log.write_text(f"{_as_json(violation)}\n{json.dumps(legacy)}\n")

    guard.watch_server_log(log)
    background, request = guard.collect()
    assert background == violation
    assert request.origin is None and request.key == "GET /x"


def test_background_allow_list_holds_only_capture_entries() -> None:
    """The render queue and dispatcher are off the loop (#62); only capture/,
    whose changes are an owner decision, may still be listed (#63)."""
    from tests.loop_guard_support import KNOWN_BACKGROUND_LOOP_DB

    for origin, issue in KNOWN_BACKGROUND_LOOP_DB.items():
        assert origin.startswith("timelapse_manager.capture."), origin
        assert issue == 63, origin


def test_known_loop_db_allow_list_stays_empty() -> None:
    """#46-#50 moved every request's database work off the event loop.

    The allow-list is now empty and must stay that way: a new route that queries
    on the loop has to be fixed, not listed.
    """
    from tests.loop_guard_support import KNOWN_LOOP_DB

    assert KNOWN_LOOP_DB == {}
