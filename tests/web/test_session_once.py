"""A request resolves its session once, whichever layers need it (#67 R3).

Before, one cookie-authenticated request looked its session up in the CSRF
middleware, again in the auth dependency, again in the principal gate and once
more for the CSRF token the page renders, each with its own SELECT, touch and
directory re-check. The spy counts the single function every one of them goes
through.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

import timelapse_manager.security.sessions as sessions_module
from tests.conftest import csrf_of, seed_admin
from tests.presession_support import ensure_presession, session_cookie
from timelapse_manager.db.models import Session as SessionRow
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.security.authz import prefixed_cookie_name
from timelapse_manager.security.sessions import hash_token
from timelapse_manager.web.presession import PRESESSION_COOKIE

_FORM = {"Content-Type": "application/x-www-form-urlencoded"}


@pytest.fixture()
def lookups(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Record every ``get_session_row`` call (the touch flag of each)."""
    calls: list[bool] = []
    real: Callable[..., Any] = sessions_module.get_session_row

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(bool(kwargs.get("touch", True)))
        return real(*args, **kwargs)

    monkeypatch.setattr(sessions_module, "get_session_row", spy)
    return calls


def _last_active(raw_token: str) -> datetime.datetime | None:
    with session_scope(get_context().session_factory) as db:
        row = db.query(SessionRow).filter_by(token_hash=hash_token(raw_token)).one()
        return row.last_active


def test_a_full_page_get_resolves_once(
    admin_client: TestClient, lookups: list[bool]
) -> None:
    resp = admin_client.get("/")  # auth dependency + the page's CSRF token
    assert resp.status_code == 200
    assert 'name="csrf-token"' in resp.text
    assert len(lookups) == 1


def test_a_form_post_resolves_once(
    admin_client: TestClient, lookups: list[bool]
) -> None:
    token = csrf_of(admin_client, "/")
    lookups.clear()
    resp = admin_client.post(
        "/account/theme", data={"theme": "dark", "csrf_token": token}, headers=_FORM
    )  # CSRF middleware + auth dependency
    assert resp.status_code == 204
    assert len(lookups) == 1


def test_a_principal_gated_post_resolves_once(
    admin_client: TestClient, lookups: list[bool]
) -> None:
    token = csrf_of(admin_client, "/")
    lookups.clear()
    resp = admin_client.post("/alerts/clear-all", headers={"X-CSRF-Token": token})
    assert resp.status_code == 200  # CSRF middleware + principal gate
    assert len(lookups) == 1


def test_a_dead_cookie_is_looked_up_once_and_refused(
    web_client: TestClient, lookups: list[bool]
) -> None:
    seed_admin(web_client)
    web_client.cookies.set(
        prefixed_cookie_name(get_context().settings.session.cookie_name),
        "not-a-session",
    )
    resp = web_client.get("/alerts/summary")
    assert resp.status_code == 401
    assert len(lookups) == 1  # the "no session" answer is cached too


def test_a_passive_request_stays_passive_through_the_principal_gate(
    admin_client: TestClient,
) -> None:
    """The one resolution honours the passive mark whichever layer runs first."""
    token = csrf_of(admin_client, "/")
    raw = session_cookie(admin_client)
    assert raw is not None
    stale = datetime.datetime.now(datetime.UTC).replace(
        tzinfo=None
    ) - datetime.timedelta(minutes=5)
    with session_scope(get_context().session_factory) as db:
        db.query(SessionRow).filter_by(
            token_hash=hash_token(raw)
        ).one().last_active = stale
    resp = admin_client.post(
        "/alerts/clear-all",
        headers={"X-CSRF-Token": token, "X-TLM-Passive": "1"},
    )
    assert resp.status_code == 200
    assert _last_active(raw) == stale


def test_a_rotation_inside_the_request_does_not_leave_a_stale_answer(
    admin_client: TestClient, lookups: list[bool]
) -> None:
    """Logout revokes the session; the old cookie is dead for the next request."""
    token = csrf_of(admin_client, "/")
    old_cookie = session_cookie(admin_client)
    resp = admin_client.post(
        "/logout", data={"csrf_token": token}, headers=_FORM, follow_redirects=False
    )
    assert resp.status_code == 303
    admin_client.cookies.set(
        get_context().settings.session.cookie_name, old_cookie or ""
    )
    assert admin_client.get("/alerts/summary").status_code == 401


def test_login_with_a_dead_cookie_gets_a_working_session(
    web_client: TestClient, lookups: list[bool]
) -> None:
    """The CSRF layer caches "no session" for the login POST; it must not leak."""
    seed_admin(web_client)
    name = get_context().settings.session.cookie_name
    presession = ensure_presession(web_client)
    resp = web_client.post(
        "/login",
        data={"username": "admin", "password": "AdminP@ssw0rd1234"},
        headers={
            **_FORM,
            # An explicit Cookie header replaces the jar's, so carry the
            # pre-session cookie by hand.
            "Cookie": (
                f"{name}=an-expired-cookie; "
                f"{prefixed_cookie_name(PRESESSION_COOKIE)}={presession}"
            ),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303  # passed the CSRF layer: the cookie is dead
    # The sign-in routes ignore a legacy-name cookie outright: no lookup at all.
    assert len(lookups) == 0
    assert web_client.get("/alerts/summary").status_code == 200
