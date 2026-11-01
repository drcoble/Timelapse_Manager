"""A change made while the body is being read still stops the request (#207).

The CSRF middleware resolves the session before it reads a multipart body, and
later layers reuse that answer (#67 R3). A slow upload gives a revoke, a disable
or a demotion time to land in between, so the gates re-check the facts that
decide access with primary-key reads instead of trusting the cached role.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from starlette.concurrency import run_in_threadpool

import timelapse_manager.web.middleware as middleware
from tests.conftest import csrf_of
from timelapse_manager.db.models import User
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.security.sessions import revoke_all_user_sessions

_ADMIN = "admin"


def _revoke(db, user: User) -> None:
    revoke_all_user_sessions(db, user.id)


def _disable(db, user: User) -> None:
    user.enabled = False


def _demote(db, user: User) -> None:
    user.role = "viewer"


def _land_during_body_read(
    monkeypatch: pytest.MonkeyPatch, change: Callable[[object, User], None]
) -> None:
    """Make the middleware's body read take long enough for ``change`` to land."""
    real = middleware._multipart_body

    async def slow(request):  # noqa: ANN001
        body = await real(request)

        def mutate() -> None:
            with session_scope(get_context().session_factory) as db:
                user = db.query(User).filter(User.username == _ADMIN).one()
                change(db, user)

        await run_in_threadpool(mutate)
        return body

    monkeypatch.setattr(middleware, "_multipart_body", slow)


def _post(client: TestClient, path: str, token: str):
    return client.post(
        path,
        data={"csrf_token": token},
        files={"f": ("x.txt", b"x", "text/plain")},
        follow_redirects=False,
    )


_VARIANTS = [
    pytest.param(_revoke, 401, id="revoked"),
    pytest.param(_disable, 401, id="disabled"),
    pytest.param(_demote, 403, id="demoted"),
]


@pytest.mark.parametrize(("change", "expected"), _VARIANTS)
def test_principal_gate_rechecks_after_a_slow_body(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    change: Callable[[object, User], None],
    expected: int,
) -> None:
    token = csrf_of(admin_client, "/")
    _land_during_body_read(monkeypatch, change)
    resp = _post(admin_client, "/alerts/clear-all", token)
    assert resp.status_code == expected


@pytest.mark.parametrize(("change", "expected"), _VARIANTS)
def test_admin_user_gate_rechecks_after_a_slow_body(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    change: Callable[[object, User], None],
    expected: int,
) -> None:
    token = csrf_of(admin_client, "/")
    _land_during_body_read(monkeypatch, change)
    resp = _post(admin_client, "/users/999999/revoke-sessions", token)
    assert resp.status_code == expected


def test_without_a_change_the_same_requests_succeed(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = csrf_of(admin_client, "/")
    _land_during_body_read(monkeypatch, lambda db, user: None)
    assert _post(admin_client, "/alerts/clear-all", token).status_code == 200
    # An admin passes the gate; the unknown target is then a 404.
    assert (
        _post(admin_client, "/users/999999/revoke-sessions", token).status_code == 404
    )
