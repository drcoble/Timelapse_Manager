"""A bearer-less unsafe ``/api/v1`` request is refused before its body is read (#102).

FastAPI reads and parses a JSON body before a route's dependencies run, so an
anonymous client used to make the service buffer up to
``server.max_default_body_bytes`` before getting its ``401``. These tests drive
raw ASGI (``_raw_post``), which counts the bytes the application actually pulled
from ``receive``: ``read == 0`` proves the body was never touched.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from tests.web.test_security_hardening import _MiB, _raw_post
from timelapse_manager.db.models import Session as SessionRow
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.security.authz import prefixed_cookie_name
from timelapse_manager.security.token import PEER_REFUSED_DETAIL, ensure_local_token
from timelapse_manager.web.body_limit import body_limit_for
from timelapse_manager.web.middleware import api_requires_bearer

_JSON = "application/json"
_BIG = 8 * _MiB  # under the 16 MiB default cap: only the gate can refuse it


class TestBearerlessRequestsAreRefusedUnread:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/cameras",
            "/api/v1/projects",
            "/api/v1/renders/1/cancel",
            "/api/v1/projects/1/frames/upload",
        ],
    )
    def test_no_credentials_at_all(self, web_client: TestClient, path: str) -> None:
        status, read, body = _raw_post(
            web_client, path, total_bytes=_BIG, content_type=_JSON
        )
        assert status == 401
        assert read == 0
        assert b"Missing or invalid local API token." in body

    def test_response_matches_the_dependencys_own_401(
        self, web_client: TestClient
    ) -> None:
        """Same status, body and ``WWW-Authenticate`` as ``require_local_token``."""
        gated = web_client.post("/api/v1/cameras", json={"x": 1})
        via_dependency = web_client.get("/api/v1/system")
        assert gated.status_code == via_dependency.status_code == 401
        assert gated.json() == via_dependency.json()
        assert gated.headers["www-authenticate"] == "Bearer"

    def test_a_session_cookie_alone_is_refused_like_the_router_does(
        self, viewer_client: TestClient
    ) -> None:
        """The router is bearer-only (#261): a live cookie without a bearer is 401,
        ahead of the CSRF and role layers, exactly as ``require_local_token`` is."""
        resp = viewer_client.post(
            "/api/v1/cameras/detect-protocol", json={"address": "10.0.0.5"}
        )
        assert resp.status_code == 401
        assert resp.json() == {"detail": "Missing or invalid local API token."}

    @pytest.mark.parametrize("header", [b"Basic abc", b"bearer lower", b"Bearer "])
    def test_a_non_bearer_or_empty_authorization_header(
        self, web_client: TestClient, header: bytes
    ) -> None:
        status, read, _ = _raw_post(
            web_client,
            "/api/v1/cameras",
            total_bytes=_BIG,
            content_type=_JSON,
            extra_headers=[(b"authorization", header)],
        )
        assert status == 401
        assert read == 0

    def test_a_refused_peer_still_gets_403_not_401(
        self, web_client: TestClient
    ) -> None:
        """The peer policy runs first, exactly as in the dependency."""
        get_context().settings.server.api_token_allowed_clients = ["203.0.113.9"]
        status, read, body = _raw_post(
            web_client, "/api/v1/cameras", total_bytes=_BIG, content_type=_JSON
        )
        assert status == 403
        assert read == 0
        assert PEER_REFUSED_DETAIL.encode() in body

    @pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
    def test_other_unsafe_methods(self, web_client: TestClient, method: str) -> None:
        assert api_requires_bearer(method, "/api/v1/cameras/1")
        resp = web_client.request(method, "/api/v1/cameras/1", content=b"{}")
        assert resp.status_code == 401


class TestRequestsWithABearerAreUnchanged:
    def test_a_wrong_bearer_is_refused_unread(self, web_client: TestClient) -> None:
        """A wrong bearer gets the dependency's own 401 before any body byte (#260)."""
        status, read, body = _raw_post(
            web_client,
            "/api/v1/cameras",
            total_bytes=_BIG,
            content_type=_JSON,
            extra_headers=[(b"authorization", b"Bearer wrong")],
        )
        assert status == 401
        assert read == 0
        assert b"Missing or invalid local API token." in body

    def test_a_valid_bearer_reaches_the_route(self, web_client: TestClient) -> None:
        token = ensure_local_token(get_context().settings)
        resp = web_client.post(
            "/api/v1/cameras",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422  # validated by the route, not refused

    def test_a_valid_bearer_body_is_read(self, web_client: TestClient) -> None:
        token = ensure_local_token(get_context().settings)
        _, read, _ = _raw_post(
            web_client,
            "/api/v1/cameras",
            total_bytes=64 * 1024,
            content_type=_JSON,
            extra_headers=[(b"authorization", f"Bearer {token}".encode())],
        )
        assert read > 0


class TestScope:
    @pytest.mark.parametrize(
        ("method", "path", "expected"),
        [
            ("POST", "/api/v1/cameras", True),
            ("DELETE", "/api/v1/projects/1", True),
            ("GET", "/api/v1/cameras", False),
            ("POST", "/login", False),
            ("POST", "/projects/1/frames/import", False),
            ("POST", "/api/v2/cameras", False),
            # The metrics and debug routers own their access control.
            ("POST", "/api/v1/system/debug/log-level", False),
            ("POST", "/api/v1/metrics", False),
        ],
    )
    def test_gate_applies_only_to_the_bearer_api(
        self, method: str, path: str, expected: bool
    ) -> None:
        assert api_requires_bearer(method, path) is expected

    def test_web_form_posts_are_untouched(self, web_client: TestClient) -> None:
        """A cookie-less web POST still reaches its own handling (login form)."""
        resp = web_client.post("/login", data={"username": "x", "password": "y"})
        assert resp.status_code != 401 or "text/html" in resp.headers["content-type"]


class TestDebugRoutesAreBounded:
    def test_debug_write_gets_the_small_cap(self, web_client: TestClient) -> None:
        settings = get_context().settings
        limit = body_limit_for(
            "POST", "/api/v1/system/debug/log-level", _JSON, settings
        )
        assert limit == 64 * 1024
        status, read, _ = _raw_post(
            web_client,
            "/api/v1/system/debug/log-level",
            total_bytes=_MiB,
            content_type=_JSON,
        )
        assert status == 413
        assert read <= limit + 64 * 1024


def _bearer(token: str) -> tuple[bytes, bytes]:
    return (b"authorization", f"Bearer {token}".encode())


def _cookie_header(client: TestClient) -> tuple[bytes, bytes]:
    jar = "; ".join(f"{c.name}={c.value}" for c in client.cookies.jar)
    return (b"cookie", jar.encode())


class TestCredentialsAreJudgedBeforeTheBody:
    """#260: a bad bearer or dead cookie never has its body buffered."""

    def test_a_refused_peer_with_a_bearer_is_403_unread(
        self, web_client: TestClient
    ) -> None:
        get_context().settings.server.api_token_allowed_clients = ["203.0.113.9"]
        status, read, body = _raw_post(
            web_client,
            "/api/v1/cameras",
            total_bytes=_BIG,
            content_type=_JSON,
            extra_headers=[_bearer("whatever")],
        )
        assert status == 403
        assert read == 0
        assert PEER_REFUSED_DETAIL.encode() in body

    def test_a_bogus_cookie_with_a_valid_bearer_is_401_unread(
        self, web_client: TestClient
    ) -> None:
        token = ensure_local_token(get_context().settings)
        cookie = prefixed_cookie_name(
            get_context().settings.session.cookie_name
        ).encode()
        status, read, body = _raw_post(
            web_client,
            "/api/v1/cameras",
            total_bytes=_BIG,
            content_type=_JSON,
            extra_headers=[_bearer(token), (b"cookie", cookie + b"=bogus")],
        )
        assert status == 401
        assert read == 0
        assert b"Authentication required." in body

    def test_a_bogus_cookie_alone_is_401_unread(self, web_client: TestClient) -> None:
        cookie = prefixed_cookie_name(
            get_context().settings.session.cookie_name
        ).encode()
        status, read, _ = _raw_post(
            web_client,
            "/api/v1/cameras",
            total_bytes=_BIG,
            content_type=_JSON,
            extra_headers=[(b"cookie", cookie + b"=bogus")],
        )
        assert status == 401
        assert read == 0

    def test_a_live_cookie_without_a_bearer_is_401_unread(
        self, admin_client: TestClient
    ) -> None:
        status, read, _ = _raw_post(
            admin_client,
            "/api/v1/cameras",
            total_bytes=_BIG,
            content_type=_JSON,
            extra_headers=[_cookie_header(admin_client)],
        )
        assert status == 401
        assert read == 0

    def test_a_revoked_session_with_a_valid_bearer_is_401_unread(
        self, admin_client: TestClient
    ) -> None:
        token = ensure_local_token(get_context().settings)
        cookie = _cookie_header(admin_client)
        with session_scope(get_context().session_factory) as db:
            db.query(SessionRow).update({"revoked": True})
        status, read, _ = _raw_post(
            admin_client,
            "/api/v1/cameras",
            total_bytes=_BIG,
            content_type=_JSON,
            extra_headers=[_bearer(token), cookie],
        )
        assert status == 401
        assert read == 0

    def test_a_valid_bearer_with_a_live_cookie_is_read(
        self, admin_client: TestClient
    ) -> None:
        """Both credentials good: the body is read (CSRF/route judge it next)."""
        token = ensure_local_token(get_context().settings)
        _, read, _ = _raw_post(
            admin_client,
            "/api/v1/cameras",
            total_bytes=64 * 1024,
            content_type=_JSON,
            extra_headers=[
                _bearer(token),
                _cookie_header(admin_client),
                (b"x-csrf-token", csrf_of(admin_client, "/").encode()),
            ],
        )
        assert read > 0

    def test_the_decision_runs_off_the_event_loop(
        self, web_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The token-file read and the compare are not done on the loop."""
        import asyncio

        import timelapse_manager.web.middleware as middleware

        on_loop: list[bool] = []
        real = middleware.require_local_token

        def spy(request: object) -> None:
            try:
                asyncio.get_running_loop()
                on_loop.append(True)
            except RuntimeError:
                on_loop.append(False)
            real(request)  # type: ignore[arg-type]

        monkeypatch.setattr(middleware, "require_local_token", spy)
        resp = web_client.post(
            "/api/v1/cameras", json={}, headers={"Authorization": "Bearer nope"}
        )
        assert resp.status_code == 401
        assert on_loop == [False]

    def test_the_session_is_resolved_once_across_gate_csrf_and_route(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate shares the #201 request cache: no second lookup anywhere."""
        import timelapse_manager.security.sessions as sessions_module

        csrf = csrf_of(admin_client, "/")
        token = ensure_local_token(get_context().settings)
        calls: list[int] = []
        real = sessions_module.get_session_row

        def spy(*args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            return real(*args, **kwargs)

        monkeypatch.setattr(sessions_module, "get_session_row", spy)
        resp = admin_client.post(
            "/api/v1/cameras",
            json={},
            headers={"Authorization": f"Bearer {token}", "X-CSRF-Token": csrf},
        )
        assert resp.status_code == 422  # reached the route's validation
        assert len(calls) == 1


class TestBearerApiCookieAndCsrf:
    """#261: where a cookie does count on /api/v1, CSRF is enforced."""

    def test_a_viewer_cookie_with_a_bearer_gets_the_roles_403(
        self, viewer_client: TestClient
    ) -> None:
        token = ensure_local_token(get_context().settings)
        resp = viewer_client.post(
            "/api/v1/cameras/detect-protocol",
            json={"address": "10.0.0.5"},
            headers={
                "Authorization": f"Bearer {token}",
                "X-CSRF-Token": csrf_of(viewer_client, "/"),
            },
        )
        assert resp.status_code == 403
        assert resp.json() == {"detail": "Insufficient privileges."}

    def test_every_unsafe_api_route_demands_csrf_when_a_cookie_rides_along(
        self, admin_client: TestClient
    ) -> None:
        """Bearer plus a live cookie but no CSRF token is refused on every unsafe
        route the gate covers, so no cookie-capable route lacks CSRF."""
        token = ensure_local_token(get_context().settings)
        checked = 0
        for route in admin_client.app.routes:  # type: ignore[attr-defined]
            path = getattr(route, "path", "")
            methods = getattr(route, "methods", None) or set()
            for method in methods - {"GET", "HEAD", "OPTIONS", "TRACE"}:
                url = re.sub(r"\{[^}]+\}", "1", path)
                if not api_requires_bearer(method, url):
                    continue
                resp = admin_client.request(
                    method,
                    url,
                    content=b"{}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": _JSON,
                    },
                )
                assert resp.status_code == 403, (method, path, resp.status_code)
                assert resp.text == "CSRF validation failed.", (method, path)
                checked += 1
        assert checked >= 15
