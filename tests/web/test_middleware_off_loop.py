"""The middleware's database work stays off the event loop (#37).

The first-run gate used to run its query synchronously on the event loop thread.
Under a burst that exhausted the pool, that checkout froze the whole loop for the
pool timeout -- including the loop work that would have committed the request
holding SQLite's write lock -- so every request stalled for 30 s.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import anyio.to_thread
import pytest
from alembic import command as alembic_command
from alembic.config import Config
from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.app import create_app
from timelapse_manager.config import Settings


def _on_event_loop() -> bool:
    """Return whether the calling thread is running an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _recording(target: Callable[..., Any], calls: list[bool]) -> Callable[..., Any]:
    """Wrap ``target`` to record, per call, whether it ran on the event loop."""

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        calls.append(_on_event_loop())
        return target(*args, **kwargs)

    return wrapper


def test_first_run_gate_queries_off_the_event_loop(
    web_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import timelapse_manager.web.middleware as middleware

    calls: list[bool] = []
    monkeypatch.setattr(
        middleware,
        "first_run_needed",
        _recording(middleware.first_run_needed, calls),
    )

    resp = web_client.get("/", follow_redirects=False)

    assert resp.status_code == 303  # fresh DB: the gate still redirects
    assert calls == [False]


def test_csrf_session_lookup_runs_off_the_event_loop(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import timelapse_manager.web.middleware as middleware

    csrf = csrf_of(admin_client, "/")
    calls: list[bool] = []
    monkeypatch.setattr(
        middleware,
        "resolve_request_session",
        _recording(middleware.resolve_request_session, calls),
    )

    resp = admin_client.post(
        "/logout",
        data={"csrf_token": csrf},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )

    assert resp.status_code == 303  # the token was accepted
    assert calls == [False]


def test_lifespan_pins_the_request_threadpool(
    web_settings: Settings,
    alembic_cfg: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pool is sized to the threadpool, so startup pins the threadpool."""
    import timelapse_manager.app as app_module

    # A value anyio would never pick by itself, so the assertion proves the pin.
    monkeypatch.setattr(app_module, "REQUEST_THREADPOOL_TOKENS", 17)
    alembic_cfg.set_main_option("sqlalchemy.url", web_settings.database.url)
    alembic_command.upgrade(alembic_cfg, "head")

    async def tokens() -> float:
        return anyio.to_thread.current_default_thread_limiter().total_tokens

    with TestClient(create_app(web_settings)) as client:
        assert client.portal is not None
        assert client.portal.call(tokens) == 17


# ---------------------------------------------------------------------------
# First-run gate: failure handling and the setup-complete latch (#67 R3, #66 C8)
# ---------------------------------------------------------------------------


def _break_gate_query(monkeypatch: pytest.MonkeyPatch) -> None:
    import timelapse_manager.web.middleware as middleware

    def boom(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(middleware, "first_run_needed", boom)


def test_a_gate_database_error_lets_a_get_through_and_refuses_a_post(
    web_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break_gate_query(monkeypatch)

    read = web_client.get("/", follow_redirects=False)
    assert read.status_code != 503
    assert read.headers.get("location") != "/first-run"  # not wedged on setup

    write = web_client.post("/cameras/query", data={"address": ""})
    assert write.status_code == 503
    assert write.text == "setup state unavailable"

    # The setup routes themselves are allow-listed, so setup is never locked out.
    assert web_client.post("/first-run", data={}).status_code != 503


def test_a_head_request_passes_when_the_state_is_unknown(
    web_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break_gate_query(monkeypatch)
    assert web_client.head("/", follow_redirects=False).status_code != 503


def test_the_gate_keeps_asking_until_an_admin_exists(
    web_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import timelapse_manager.web.middleware as middleware

    calls: list[bool] = []
    monkeypatch.setattr(
        middleware,
        "first_run_needed",
        _recording(middleware.first_run_needed, calls),
    )
    for _ in range(3):
        assert web_client.get("/", follow_redirects=False).status_code == 303
    assert len(calls) == 3  # "needed" is never latched


def test_once_an_admin_exists_the_gate_never_asks_again(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import timelapse_manager.web.middleware as middleware

    assert admin_client.get("/").status_code == 200  # the first look latches
    calls: list[bool] = []
    monkeypatch.setattr(
        middleware,
        "first_run_needed",
        _recording(middleware.first_run_needed, calls),
    )
    for _ in range(3):
        assert admin_client.get("/").status_code == 200
    assert calls == []

    # And a database error afterwards is not a refusal: the answer is latched.
    _break_gate_query(monkeypatch)
    token = csrf_of(admin_client, "/")
    resp = admin_client.post(
        "/account/theme",
        data={"theme": "dark", "csrf_token": token},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 204
