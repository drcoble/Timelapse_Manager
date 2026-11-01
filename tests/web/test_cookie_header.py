"""The security cookies are read from the raw header, strictly.

A name must match exactly (only space and tab are trimmed); a name that matches
only after ``str.strip()`` and a duplicated name both fail closed. httpx
re-encodes non-ASCII header bytes, so the requests here are raw ASGI scopes with
the bytes unchanged.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.requests import Request

from tests.conftest import seed_admin
from tests.presession_support import session_cookie
from timelapse_manager.db.models import Session as SessionRow
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.security.cookie_header import cookie_value
from timelapse_manager.security.sessions import hash_token
from timelapse_manager.web.presession import PRESESSION_RETRY_MESSAGE

PREFIXED = "__Host-tlm_session"
PRESESSION = "__Host-tlm_presession"


def _request(cookie: bytes) -> Request:
    return Request(
        {"type": "http", "headers": [(b"cookie", cookie)], "method": "GET", "path": "/"}
    )


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (b"a=1", "1"),
        (b"x=9;  a = 1 ;y=2", "1"),  # surrounding space and tab are trimmed
        (b"x=9; a=1; y=2", "1"),
        (b"a=1; a=2", None),  # duplicate: ambiguous
        (b"\x85a=1", None),  # non-ASCII byte before the name
        (b"\xa0a=1", None),  # a different non-ASCII byte before the name
        (b"a\x85=1", None),
        (b"\x85a=1; a=2", None),  # an inexact name beside the real one: fail closed
        (b"a=1; \xa0a=2", None),
        (b"b=1", None),
        (b"a", None),
        (b"a=", ""),
        (b" \ta=1", "1"),  # ASCII space and tab are trimmed
    ],
)
def test_strict_parsing(header: bytes, expected: str | None) -> None:
    assert cookie_value(_request(header), "a") == expected


def _call(
    client: TestClient, method: str, path: str, cookie: bytes
) -> tuple[int, bytes]:
    """Drive the app over a raw ASGI scope; return (status, body)."""
    seen: dict[str, Any] = {"status": 0, "body": b""}
    body = b"" if method == "GET" else b"username=admin&password=x&csrf_token=zz"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"cookie", cookie),
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"content-length", str(len(body)).encode()),
        ],
        "server": ("testserver", 443),
        "client": ("127.0.0.1", 50000),
        "state": dict(client.app_state),
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            seen["status"] = message["status"]
        elif message["type"] == "http.response.body":
            seen["body"] += message.get("body", b"")

    async def run() -> None:
        await client.app(scope, receive, send)  # type: ignore[arg-type]

    client.portal.call(run)  # type: ignore[union-attr]
    return seen["status"], seen["body"]


@pytest.fixture()
def token(admin_client: TestClient) -> str:
    value = session_cookie(admin_client)
    assert value
    return value


def test_one_normal_cookie_authenticates(admin_client: TestClient, token: str) -> None:
    status, _ = _call(admin_client, "GET", "/", f"{PREFIXED}={token}".encode())
    assert status == 200


def test_a_session_cookie_name_that_only_matches_after_unicode_strip_is_rejected(
    admin_client: TestClient, token: str
) -> None:
    cookie = b"\x85" + f"{PREFIXED}={token}".encode()
    status, _ = _call(admin_client, "GET", "/alerts/summary", cookie)
    assert status == 401


def test_an_inexact_name_beside_the_real_cookie_makes_it_ambiguous(
    admin_client: TestClient, token: str
) -> None:
    """A later cookie whose name only matches after Unicode strip must not win."""
    cookie = f"{PREFIXED}={token}; ".encode() + b"\xa0" + f"{PREFIXED}=x".encode()
    status, _ = _call(admin_client, "GET", "/alerts/summary", cookie)
    assert status == 401  # ambiguous, so absent: never somebody else's identity


def test_a_duplicate_session_cookie_is_absent(
    admin_client: TestClient, token: str
) -> None:
    cookie = f"{PREFIXED}={token}; {PREFIXED}={token}".encode()
    status, _ = _call(admin_client, "GET", "/alerts/summary", cookie)
    assert status == 401


def test_a_presession_name_that_only_matches_after_unicode_strip_is_rejected(
    web_client: TestClient,
) -> None:
    seed_admin(web_client)
    secret = "Q" * 43
    cookie = b"\xa0" + f"{PRESESSION}={secret}".encode()
    status, _ = _call(web_client, "POST", "/login", cookie)
    assert status == 403


def test_a_duplicate_presession_cookie_is_absent(web_client: TestClient) -> None:
    seed_admin(web_client)
    secret = "Q" * 43
    cookie = f"{PRESESSION}={secret}; {PRESESSION}={secret}".encode()
    status, body = _call(web_client, "POST", "/login", cookie)
    assert status == 403
    assert PRESESSION_RETRY_MESSAGE.encode() in body


LEGACY = "tlm_session"


@pytest.fixture()
def pre_upgrade_token(admin_client: TestClient) -> str:
    """A live session that predates the upgrade (a plain, un-marked token)."""
    value = session_cookie(admin_client)
    assert value
    old_style = secrets.token_urlsafe(32)
    with session_scope(get_context().session_factory) as db:
        row = db.execute(
            select(SessionRow).where(SessionRow.token_hash == hash_token(value))
        ).scalar_one()
        row.token_hash = hash_token(old_style)
    return old_style


def test_a_valid_legacy_cookie_alone_is_refused_over_https(
    admin_client: TestClient, pre_upgrade_token: str
) -> None:
    """The old name is not read over HTTPS any more (#238)."""
    status, _ = _call(
        admin_client, "GET", "/alerts/summary", f"{LEGACY}={pre_upgrade_token}".encode()
    )
    assert status == 401


@pytest.mark.parametrize(
    "extra",
    [
        b"\x85" + f"{PREFIXED}=x".encode(),  # non-ASCII byte before the prefixed name
        b"\xa0"
        + f"{PREFIXED}=x".encode(),  # a different non-ASCII byte before the name
        f"{PREFIXED}=x; {PREFIXED}=y".encode(),  # duplicate
    ],
)
def test_an_inexact_prefixed_name_cannot_push_the_request_to_the_legacy_one(
    admin_client: TestClient, pre_upgrade_token: str, extra: bytes
) -> None:
    cookie = extra + f"; {LEGACY}={pre_upgrade_token}".encode()
    status, _ = _call(admin_client, "GET", "/alerts/summary", cookie)
    assert status == 401


@pytest.mark.parametrize(
    "extra",
    [b"\x85" + f"{LEGACY}=x".encode(), f"{LEGACY}=x; {LEGACY}=y".encode()],
)
def test_an_ambiguous_legacy_name_is_ignored_beside_the_real_prefixed_cookie(
    admin_client: TestClient, token: str, extra: bytes
) -> None:
    """The old name is never looked at over HTTPS, so adding it changes nothing."""
    cookie = f"{PREFIXED}={token}; ".encode() + extra
    status, _ = _call(admin_client, "GET", "/alerts/summary", cookie)
    assert status == 200
