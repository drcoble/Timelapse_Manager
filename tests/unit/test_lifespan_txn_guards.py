"""The lifespan, websocket and write-transaction guards fire where they should (#66).

Three additions to the loop-DB guard (``tests/loop_guard_support.py``), each
checked on a minimal app with its own SQLite engine:

* **Lifespan (L4).** Startup or shutdown work that queries on the event loop is
  a *lifespan* violation, keyed by the lifespan coroutine, and fails unless
  allow-listed in ``KNOWN_LIFESPAN_LOOP_DB``. The application's own lifespan
  is clean.
* **Websocket scopes** are marked like HTTP requests.
* **Write transactions (C1).** A request that suspends holding an uncommitted
  write, outside a thread hop, is a *write-txn* violation, allowed only through
  ``KNOWN_OPEN_WRITE_TXN``. Committing (or rolling back) in a hop first, or
  waiting only on thread hops, is not recorded. The hop count works on uvloop,
  which the e2e server runs on, as on asyncio.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from fastapi import Depends, FastAPI, WebSocket
from fastapi.concurrency import run_in_threadpool
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from tests import loop_guard_support as guard
from tests import txn_guard_support
from timelapse_manager.app import create_app
from timelapse_manager.config import Settings


def _query(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("SELECT 1")).scalar_one())


def _flush_update(session: Session) -> None:
    """Write without committing: the connection holds SQLite's write lock."""
    session.execute(text("UPDATE item SET value = value + 1 WHERE id = 1"))


@pytest.fixture()
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = create_engine(f"sqlite:///{tmp_path / 'guards.db'}")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE item (id INTEGER PRIMARY KEY, value INTEGER)"))
        conn.execute(text("INSERT INTO item (id, value) VALUES (1, 0)"))
    guard.reset()
    try:
        yield eng
    finally:
        # These tests violate on purpose; keep them out of the autouse guard and
        # out of a record-only run.
        guard.reset()
        eng.dispose()


def _by_guard(kind: str) -> list[guard.LoopDbViolation]:
    return [v for v in guard.collect() if v.guard == kind]


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


def test_lifespan_querying_on_the_loop_is_recorded_and_fails(engine: Engine) -> None:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        _query(engine)
        yield

    with TestClient(FastAPI(lifespan=lifespan)):
        pass

    violations = guard.collect()
    key = f"{__name__}:{lifespan.__qualname__}"
    assert {(v.guard, v.key) for v in violations} == {("lifespan", key)}
    assert {v.kind for v in violations} == {"checkout", "execute"}
    assert all(v.lifespan and not v.background for v in violations)
    assert violations[0].describe().startswith(f"lifespan {key}")
    assert guard.unexpected(violations, allowed_lifespan={}) == [violations[0]]
    assert guard.unexpected(violations, allowed_lifespan={key: 66}) == []
    # A lifespan origin is not a background one: listing it there does not help.
    assert guard.unexpected(
        violations, allowed_background={key: 66}, allowed_lifespan={}
    ) == [violations[0]]


def test_lifespan_shutdown_on_the_loop_is_recorded(engine: Engine) -> None:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        _query(engine)

    with TestClient(FastAPI(lifespan=lifespan)):
        assert guard.collect() == []

    assert {v.guard for v in guard.collect()} == {"lifespan"}


def test_lifespan_hopping_to_a_thread_is_not_recorded(engine: Engine) -> None:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await asyncio.to_thread(_query, engine)
        yield
        await asyncio.to_thread(_query, engine)

    with TestClient(FastAPI(lifespan=lifespan)):
        pass

    assert guard.collect() == []


def test_application_lifespan_does_no_database_work_on_the_loop(
    web_settings: Settings, alembic_cfg: Config
) -> None:
    """Startup and shutdown of the real app are clean, so the allow-list is
    empty (#66 L4). Also covers a stored SSRF row being merged at startup."""
    alembic_cfg.set_main_option("sqlalchemy.url", web_settings.database.url)
    alembic_command.upgrade(alembic_cfg, "head")
    guard.reset()

    with TestClient(create_app(web_settings)):
        pass

    lifespan = [v for v in guard.collect() if v.lifespan]
    assert lifespan == [], guard.failure_message(lifespan)


def test_lifespan_and_write_txn_allow_lists_stay_empty() -> None:
    """From wave 8 on, a new origin is fixed, not listed (plan §8)."""
    assert guard.KNOWN_LIFESPAN_LOOP_DB == {}
    assert txn_guard_support.KNOWN_OPEN_WRITE_TXN == {}
    assert guard.KNOWN_LOOP_DB == {}
    assert guard.KNOWN_BACKGROUND_LOOP_DB == {}


# ---------------------------------------------------------------------------
# Websocket scopes
# ---------------------------------------------------------------------------


def test_websocket_route_querying_on_the_loop_is_recorded(engine: Engine) -> None:
    app = FastAPI()

    @app.websocket("/ws/{item_id:int}")
    async def socket(websocket: WebSocket, item_id: int) -> None:
        await websocket.accept()
        await websocket.send_text(str(_query(engine)))
        await websocket.close()

    with TestClient(app) as client, client.websocket_connect("/ws/3") as ws:
        assert ws.receive_text() == "1"

    violations = guard.collect()
    assert {v.key for v in violations} == {"WEBSOCKET /ws/{item_id:int}"}
    assert {v.guard for v in violations} == {"loop"}
    assert guard.unexpected(violations, allowed={}) == [violations[0]]


# ---------------------------------------------------------------------------
# Write transactions held across an await
# ---------------------------------------------------------------------------


def _write_txn_app(engine: Engine) -> FastAPI:
    factory = sessionmaker(bind=engine)
    app = FastAPI()

    def get_session() -> Iterator[Session]:
        session = factory()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    @app.post("/hold/{item_id:int}")
    async def hold(item_id: int) -> dict[str, bool]:
        session = factory()
        try:
            await run_in_threadpool(_flush_update, session)
            await asyncio.sleep(0.01)  # e.g. a camera probe: the lock is held
            await run_in_threadpool(session.commit)
        finally:
            await run_in_threadpool(session.close)
        return {"ok": True}

    @app.post("/commit-first")
    async def commit_first() -> dict[str, bool]:
        session = factory()
        try:

            def write_and_commit() -> None:
                _flush_update(session)
                session.commit()

            await run_in_threadpool(write_and_commit)
            await asyncio.sleep(0.01)
        finally:
            await run_in_threadpool(session.close)
        return {"ok": True}

    @app.post("/rollback-first")
    async def rollback_first() -> dict[str, bool]:
        session = factory()
        try:

            def write_and_roll_back() -> None:
                _flush_update(session)
                session.rollback()

            await run_in_threadpool(write_and_roll_back)
            await asyncio.sleep(0.01)
        finally:
            await run_in_threadpool(session.close)
        return {"ok": True}

    @app.post("/hops-only")
    async def hops_only() -> dict[str, bool]:
        """A write, then only thread hops (anyio's and the executor's) until
        the commit: no wait on the loop holds the lock."""
        session = factory()
        try:
            await run_in_threadpool(_flush_update, session)
            await asyncio.to_thread(time.sleep, 0.01)
            await run_in_threadpool(session.commit)
        finally:
            await run_in_threadpool(session.close)
        return {"ok": True}

    @app.post("/sync")
    def sync_write(
        session: Session = Depends(get_session),  # noqa: B008 - a local dependency
    ) -> dict[str, bool]:
        _flush_update(session)
        return {"ok": True}

    return app


def test_write_held_across_an_await_is_recorded_and_fails(engine: Engine) -> None:
    with TestClient(_write_txn_app(engine)) as client:
        assert client.post("/hold/1").status_code == 200

    [violation] = _by_guard("write-txn")
    assert violation.key == "POST /hold/{item_id:int}"
    assert violation.kind == "open-write-txn"
    assert violation.statement.startswith("UPDATE item SET value")
    assert any("hold" in frame for frame in violation.stack), violation.stack
    assert violation.describe().startswith("write-txn POST /hold/{item_id:int}")
    assert guard.unexpected([violation], allowed_write_txn={}) == [violation]
    assert guard.unexpected([violation], allowed_write_txn={violation.key: 66}) == []
    # The write-txn list is separate from the loop-DB one.
    assert guard.unexpected([violation], allowed={violation.key: 66}) == [violation]


def test_committing_or_rolling_back_first_is_not_recorded(engine: Engine) -> None:
    with TestClient(_write_txn_app(engine)) as client:
        assert client.post("/commit-first").status_code == 200
        assert client.post("/rollback-first").status_code == 200

    assert guard.collect() == []


def test_waiting_only_on_thread_hops_is_not_recorded(engine: Engine) -> None:
    with TestClient(_write_txn_app(engine)) as client:
        assert client.post("/hops-only").status_code == 200
        assert client.post("/sync").status_code == 200

    assert guard.collect() == []


def test_one_record_per_request(engine: Engine) -> None:
    with TestClient(_write_txn_app(engine)) as client:
        assert client.post("/hold/1").status_code == 200
        assert client.post("/hold/2").status_code == 200

    assert len(_by_guard("write-txn")) == 2  # one per request, not per await


def test_hop_count_holds_on_uvloop(engine: Engine) -> None:
    """The e2e server runs on uvloop: an executor hop is counted there too."""
    uvloop = pytest.importorskip("uvloop")
    factory = sessionmaker(bind=engine)
    scope = {"type": "http", "method": "POST", "path": "/uv"}

    async def write_then(wait_in_thread: bool) -> None:
        session = factory()
        try:
            await asyncio.to_thread(_flush_update, session)
            if wait_in_thread:
                await asyncio.to_thread(time.sleep, 0.01)
            else:
                await asyncio.sleep(0.01)
            await asyncio.to_thread(session.commit)
        finally:
            await asyncio.to_thread(session.close)

    loop = uvloop.new_event_loop()
    try:
        loop.run_until_complete(txn_guard_support.watch(write_then(True), scope))
        assert guard.collect() == []
        loop.run_until_complete(txn_guard_support.watch(write_then(False), scope))
    finally:
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()

    [violation] = _by_guard("write-txn")
    assert violation.key == "POST /uv (no route: middleware or 404)"
