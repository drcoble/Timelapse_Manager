"""An admin acting on their own account drops the request's cached session (#208)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import timelapse_manager.web.routers.users as users_router
from tests.conftest import csrf_of
from timelapse_manager.db.models import User
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context

_FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def _admin_id() -> int:
    with session_scope(get_context().session_factory) as db:
        return db.query(User).filter(User.username == "admin").one().id


def _spy_invalidations(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    real = users_router.invalidate_request_session
    monkeypatch.setattr(
        users_router,
        "invalidate_request_session",
        lambda request: (calls.append(1), real(request))[1],
    )
    return calls


def test_revoking_your_own_sessions_invalidates_the_cached_session(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = csrf_of(admin_client, "/")
    calls = _spy_invalidations(monkeypatch)
    resp = admin_client.post(
        f"/users/{_admin_id()}/revoke-sessions",
        data={"csrf_token": token},
        headers=_FORM,
    )
    assert resp.status_code == 200
    assert calls == [1]
    # The page rendered after the revoke minted no CSRF token for the dead session.
    assert token not in resp.text


def test_resetting_your_own_password_invalidates_the_cached_session(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = csrf_of(admin_client, "/")
    calls = _spy_invalidations(monkeypatch)
    resp = admin_client.post(
        f"/users/{_admin_id()}/reset-password",
        data={"csrf_token": token},
        headers=_FORM,
    )
    assert resp.status_code == 200
    assert calls == [1]


def test_acting_on_someone_else_leaves_the_cache_alone(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = csrf_of(admin_client, "/")
    with session_scope(get_context().session_factory) as db:
        other = User(
            username="other", auth_source="local", password_hash="x", role="viewer"
        )
        db.add(other)
        db.flush()
        other_id = other.id
    calls = _spy_invalidations(monkeypatch)
    resp = admin_client.post(
        f"/users/{other_id}/revoke-sessions",
        data={"csrf_token": token},
        headers=_FORM,
    )
    assert resp.status_code == 200
    assert calls == []
