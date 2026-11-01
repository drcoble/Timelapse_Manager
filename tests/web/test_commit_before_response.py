"""Regression for issue #3: the request's DB commit must land before the response.

``TestClient`` runs a request *and* its dependency teardown to completion before
returning, so it cannot see the bug. The ASGI tests below inspect the database
(through an independent connection) at the instant ``http.response.start`` is
sent -- the moment a browser could follow the redirect. On a fast host the old
teardown often finished first anyway (it reproduced on the Linux deployment, not
on a laptop), so the deterministic guard is the dependency-tree walk at the end:
``get_session`` must be function-scoped everywhere.
"""

from __future__ import annotations

import sqlite3
from http.cookies import SimpleCookie
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import seed_admin
from timelapse_manager.runtime import get_context
from timelapse_manager.security.authz import prefixed_cookie_name
from timelapse_manager.security.csrf import mask_secret
from timelapse_manager.security.sessions import hash_token
from timelapse_manager.web.presession import PRESESSION_COOKIE

_PRESESSION = "p" * 43


def _login_scope() -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/login",
        "raw_path": b"/login",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/x-www-form-urlencoded"),
            # The sign-in page's pre-session cookie (the body below carries a
            # masked token for it).
            (
                b"cookie",
                f"{prefixed_cookie_name(PRESESSION_COOKIE)}={_PRESESSION}".encode(),
            ),
        ],
        "server": ("testserver", 443),
        "client": ("127.0.0.1", 50000),
    }


def _session_row_visible_at_response_start(
    client: TestClient, body: bytes
) -> tuple[int, bool | None]:
    """POST /login over raw ASGI; report status and whether the row existed at start."""
    ctx = get_context()
    cookie_name = prefixed_cookie_name(ctx.settings.session.cookie_name)
    db_path = ctx.settings.database.url.removeprefix("sqlite:///")
    seen: dict[str, Any] = {"status": 0, "row": None}

    async def receive() -> dict[str, Any]:
        full = body + f"&csrf_token={mask_secret(_PRESESSION)}".encode()
        return {"type": "http.request", "body": full, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] != "http.response.start":
            return
        seen["status"] = message["status"]
        for name, value in message.get("headers", []):
            if name.lower() != b"set-cookie":
                continue
            cookie: SimpleCookie = SimpleCookie()
            cookie.load(value.decode())
            if cookie_name in cookie:
                raw = cookie[cookie_name].value
                # An independent connection (not the app's pool) sees only
                # committed data -- exactly what the browser's next request sees.
                conn = sqlite3.connect(db_path)
                try:
                    found = conn.execute(
                        "SELECT 1 FROM session WHERE token_hash = ?",
                        (hash_token(raw),),
                    ).fetchone()
                finally:
                    conn.close()
                seen["row"] = found is not None

    async def call() -> None:
        await client.app(_login_scope(), receive, send)  # type: ignore[arg-type]

    client.portal.call(call)  # type: ignore[union-attr]
    return seen["status"], seen["row"]


class TestCommitBeforeResponse:
    def test_login_session_row_is_committed_when_response_starts(
        self, web_client: TestClient
    ) -> None:
        seed_admin(web_client)
        status, row_visible = _session_row_visible_at_response_start(
            web_client, b"username=admin&password=AdminP%40ssw0rd1234"
        )
        assert status == 303
        assert row_visible is True, (
            "session row was not committed when the 303 was sent (issue #3)"
        )

    def test_failed_login_sets_no_session(self, web_client: TestClient) -> None:
        seed_admin(web_client)
        status, row_visible = _session_row_visible_at_response_start(
            web_client, b"username=admin&password=wrong-password-123"
        )
        assert status != 303
        assert row_visible in (None, False)


def _get_session_scopes(dependant: Any, found: list[tuple[str, str | None]]) -> None:
    from timelapse_manager.db.session import get_session

    for sub in dependant.dependencies:
        if sub.call is get_session:
            found.append((dependant.name or "?", sub.scope))
        _get_session_scopes(sub, found)


class TestEverySessionDependencyIsFunctionScoped:
    def test_no_route_uses_a_request_scoped_session(
        self, web_client: TestClient
    ) -> None:
        """Every route's DB session must commit before its response is sent.

        Deterministic guard for issue #3: walks every route's dependency tree and
        requires ``scope="function"`` wherever ``get_session`` appears. A single
        default-scoped site would also split the request into two sessions.
        """
        found: list[tuple[str, str | None]] = []
        for route in web_client.app.routes:  # type: ignore[attr-defined]
            dependant = getattr(route, "dependant", None)
            if dependant is not None:
                _get_session_scopes(dependant, found)
        assert found, "expected routes to depend on get_session"
        wrong = sorted({name for name, scope in found if scope != "function"})
        assert not wrong, f"request-scoped get_session in: {wrong}"
