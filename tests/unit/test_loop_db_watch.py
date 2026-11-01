"""The production loop-DB signal (#46-#50, #66; debug builds only).

``LoopDbWatch`` logs one WARNING per offending function when a request queries
the database on the event loop, and one per application coroutine when
background work does (a pool checkout is enough); never for thread hops or
for code outside the application. The lifespan installs it only when
``observability.debug_endpoints_enabled``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event, text

import timelapse_manager.app as app_module
from timelapse_manager.app import create_app
from timelapse_manager.config import Settings
from timelapse_manager.config.settings import ObservabilitySettings
from timelapse_manager.logging import request_id_ctx
from timelapse_manager.monitoring.diagnostics import LoopDbWatch, _offending_function

_LOGGER = "timelapse_manager.monitoring.diagnostics"


@pytest.fixture()
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = create_engine(f"sqlite:///{tmp_path / 'watch.db'}")
    try:
        yield eng
    finally:
        eng.dispose()


RunOnLoop = Callable[[Coroutine[Any, Any, None]], None]


@pytest.fixture()
def run_on_loop() -> Iterator[RunOnLoop]:
    """Run coroutines on a private event loop, leaving global asyncio state alone.

    ``asyncio.run`` ends with ``set_event_loop(None)``, which leaves the policy
    with no current loop: a later test in the same worker that calls
    ``asyncio.get_event_loop()`` then fails. This loop is never installed as
    the current one, and teardown closes it (with its default executor).
    """
    loop = asyncio.new_event_loop()
    try:
        yield loop.run_until_complete
    finally:
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()


def _query(engine: Engine) -> None:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _LOGGER and r.levelno == logging.WARNING
    ]


def test_request_query_on_the_loop_warns_once(
    engine: Engine, caplog: pytest.LogCaptureFixture, run_on_loop: RunOnLoop
) -> None:
    watch = LoopDbWatch(engine)
    watch.install()

    async def request() -> None:
        token = request_id_ctx.set("req-1")
        try:
            _query(engine)
            _query(engine)
        finally:
            request_id_ctx.reset(token)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        run_on_loop(request())

    [message] = _warnings(caplog)
    assert "event loop" in message and "req-1" in message


def test_threadpool_and_non_request_work_is_silent(
    engine: Engine, caplog: pytest.LogCaptureFixture, run_on_loop: RunOnLoop
) -> None:
    watch = LoopDbWatch(engine)
    watch.install()

    async def request_in_threadpool() -> None:
        token = request_id_ctx.set("req-2")
        try:
            await asyncio.to_thread(_query, engine)
        finally:
            request_id_ctx.reset(token)

    async def background_on_loop() -> None:
        _query(engine)  # no request, and no application frame: not attributable

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        run_on_loop(request_in_threadpool())
        run_on_loop(background_on_loop())
        token = request_id_ctx.set("req-3")
        try:
            _query(engine)  # a request id but no running loop
        finally:
            request_id_ctx.reset(token)

    assert _warnings(caplog) == []


def test_remove_stops_watching(engine: Engine) -> None:
    watch = LoopDbWatch(engine)
    watch.install()
    watch.install()
    assert event.contains(engine, "before_cursor_execute", watch._check)
    watch.remove()
    watch.remove()
    assert not event.contains(engine, "before_cursor_execute", watch._check)


def test_offending_function_prefers_the_route_handler() -> None:
    """The outermost router/API frame names the route; else the outermost app one."""
    namespace: dict[str, Any] = {"__name__": "timelapse_manager.web.routers.fake"}
    # Frames whose globals claim a router module and a helper module.
    exec("def handler(helper):\n    return helper()\n", namespace)
    helpers: dict[str, Any] = {"__name__": "timelapse_manager.web.dependencies"}
    exec("import sys\ndef base_context():\n    return sys._getframe(0)\n", helpers)

    frame = namespace["handler"](helpers["base_context"])
    assert _offending_function(frame) == "timelapse_manager.web.routers.fake.handler"
    assert _offending_function(None) == "unknown"


@pytest.mark.parametrize("enabled", [True, False])
def test_lifespan_installs_the_watch_only_in_debug_builds(
    web_settings: Settings,
    alembic_cfg: Config,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> None:
    created: list[LoopDbWatch] = []

    class RecordingWatch(LoopDbWatch):
        def __init__(self, engine: Engine) -> None:
            super().__init__(engine)
            created.append(self)

    monkeypatch.setattr(app_module, "LoopDbWatch", RecordingWatch)
    settings = web_settings.model_copy(
        update={"observability": ObservabilitySettings(debug_endpoints_enabled=enabled)}
    )
    alembic_cfg.set_main_option("sqlalchemy.url", settings.database.url)
    alembic_command.upgrade(alembic_cfg, "head")

    with TestClient(create_app(settings)):
        if enabled:
            [watch] = created
            assert event.contains(watch._engine, "before_cursor_execute", watch._check)
        else:
            assert created == []
    if enabled:
        assert not event.contains(watch._engine, "before_cursor_execute", watch._check)


def _app_coroutine(engine: Engine, name: str, body: str) -> Any:
    """Define ``async def drain()`` in a module that stands in for application
    code (``timelapse_manager.render.fake``)."""
    namespace: dict[str, Any] = {
        "__name__": name,
        "engine": engine,
        "asyncio": asyncio,
        "_connect": _connect,
    }
    exec(body, namespace)  # noqa: S102 - a fixed test snippet
    return namespace["drain"]


def _connect(engine: Engine) -> None:
    with engine.connect():
        pass  # a checkout only: no statement reaches the cursor


def test_background_checkout_on_the_loop_warns_once(
    engine: Engine, caplog: pytest.LogCaptureFixture, run_on_loop: RunOnLoop
) -> None:
    """A background task that only checks a connection out of the pool on the
    loop is named by its application coroutine and its task (#66)."""
    watch = LoopDbWatch(engine)
    watch.install()
    drain = _app_coroutine(
        engine,
        "timelapse_manager.render.fake",
        "async def drain():\n    _connect(engine)\n    _connect(engine)\n",
    )

    async def run_twice() -> None:
        await asyncio.create_task(drain(), name="render-drain")
        await asyncio.create_task(drain(), name="render-drain")

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        run_on_loop(run_twice())

    [message] = _warnings(caplog)
    assert "timelapse_manager.render.fake:drain" in message
    assert "render-drain" in message and "event loop" in message


def test_background_work_through_a_thread_is_silent(
    engine: Engine, caplog: pytest.LogCaptureFixture, run_on_loop: RunOnLoop
) -> None:
    watch = LoopDbWatch(engine)
    watch.install()
    drain = _app_coroutine(
        engine,
        "timelapse_manager.render.fake",
        "async def drain():\n    await asyncio.to_thread(_connect, engine)\n",
    )

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        run_on_loop(drain())

    assert _warnings(caplog) == []


def test_session_plumbing_is_never_the_origin(
    engine: Engine, caplog: pytest.LogCaptureFixture, run_on_loop: RunOnLoop
) -> None:
    """A query reached through ``timelapse_manager.db`` names its caller."""
    watch = LoopDbWatch(engine)
    watch.install()
    namespace: dict[str, Any] = {"__name__": "timelapse_manager.db.fake"}
    exec("def helper(engine, connect):\n    connect(engine)\n", namespace)  # noqa: S102
    drain = _app_coroutine(
        engine,
        "timelapse_manager.monitoring.fake",
        "async def drain():\n    helper(engine, _connect)\n",
    )
    drain.__globals__["helper"] = namespace["helper"]

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        run_on_loop(drain())

    [message] = _warnings(caplog)
    assert "timelapse_manager.monitoring.fake:drain" in message


def test_install_watches_the_pool_checkout_too(engine: Engine) -> None:
    watch = LoopDbWatch(engine)
    watch.install()
    watch.install()
    assert event.contains(engine, "checkout", watch._check)
    watch.remove()
    watch.remove()
    assert not event.contains(engine, "checkout", watch._check)
