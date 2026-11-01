"""CSRF hardening over HTTP: masking, fetch metadata, rotation (#70 F-S4, F-R3).

* Rendered pages carry a masked token, never the session's raw secret, and the
  raw secret is still accepted (tabs from before the upgrade).
* ``Sec-Fetch-Site: cross-site`` / ``same-site`` is refused even with a valid
  token; ``same-origin``, ``none`` and no header fall through to the token.
* A failed secret lookup denies outright: a token forged to "match" the public
  deny sentinel must not pass.
* ``/alerts/summary`` reports a stale ``X-CSRF-Token`` with a fresh token in
  ``HX-Trigger`` (``tlm:csrf``) that verifies against the live session, and says
  nothing for a current token, masked or raw.
"""

from __future__ import annotations

import datetime
import json
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import csrf_of, login, seed_admin
from timelapse_manager.db.models import Session as SessionRow
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.security.csrf import mask_secret, verify_csrf
from timelapse_manager.security.sessions import hash_token
from timelapse_manager.web import middleware as middleware_module

_FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def _secret_of(raw_token: str) -> str:
    with session_scope(get_context().session_factory) as db:
        row = db.query(SessionRow).filter_by(token_hash=hash_token(raw_token)).one()
        assert row.csrf_secret
        return row.csrf_secret


def _last_active(raw_token: str) -> datetime.datetime | None:
    with session_scope(get_context().session_factory) as db:
        row = db.query(SessionRow).filter_by(token_hash=hash_token(raw_token)).one()
        return row.last_active


def _set_last_active(raw_token: str, value: datetime.datetime) -> None:
    with session_scope(get_context().session_factory) as db:
        row = db.query(SessionRow).filter_by(token_hash=hash_token(raw_token)).one()
        row.last_active = value


@pytest.fixture()
def signed_in(web_client: TestClient) -> tuple[TestClient, str]:
    """An admin client and its raw session cookie value."""
    seed_admin(web_client)
    return web_client, login(web_client)


def _sign_in_again(client: TestClient) -> str:
    """Start a new session on ``client``, as another tab signing in would."""
    client.cookies.clear()
    return login(client)


def _rotated_token(resp: Any) -> str | None:
    header = resp.headers.get("HX-Trigger")
    if header is None:
        return None
    payload = json.loads(header)
    token = payload["tlm:csrf"]["token"]
    assert isinstance(token, str)
    return token


class TestMaskedTokenInPages:
    def test_rendered_html_never_contains_the_raw_secret(
        self, signed_in: tuple[TestClient, str]
    ) -> None:
        client, raw = signed_in
        secret = _secret_of(raw)
        for path in ("/", "/alerts/summary", "/renders", "/about"):
            resp = client.get(path)
            assert resp.status_code == 200, path
            assert secret not in resp.text, path

    def test_each_render_carries_a_different_token_that_verifies(
        self, signed_in: tuple[TestClient, str]
    ) -> None:
        client, raw = signed_in
        secret = _secret_of(raw)
        first, second = csrf_of(client, "/"), csrf_of(client, "/")
        assert first != second
        assert verify_csrf(secret, first) and verify_csrf(secret, second)

    def test_body_has_no_frozen_hx_headers_token(
        self, signed_in: tuple[TestClient, str]
    ) -> None:
        client, _ = signed_in
        html = client.get("/").text
        body = re.search(r"<body[^>]*>", html)
        assert body is not None
        assert "hx-headers" not in body.group(0)
        assert "js/csrf.js" in html

    def test_masked_form_token_is_accepted(
        self, signed_in: tuple[TestClient, str]
    ) -> None:
        client, _ = signed_in
        resp = client.post(
            "/settings",
            data={"csrf_token": csrf_of(client, "/")},
            headers=_FORM,
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_raw_secret_is_refused(self, signed_in: tuple[TestClient, str]) -> None:
        client, raw = signed_in
        secret = _secret_of(raw)
        header = client.post(
            "/settings",
            data={},
            headers={**_FORM, "X-CSRF-Token": secret},
            follow_redirects=False,
        )
        assert header.status_code == 403
        assert header.text == "CSRF validation failed."
        form = client.post(
            "/settings",
            data={"csrf_token": secret},
            headers=_FORM,
            follow_redirects=False,
        )
        assert form.status_code == 403

    def test_token_from_the_previous_session_is_refused(
        self, signed_in: tuple[TestClient, str]
    ) -> None:
        client, _ = signed_in
        old = csrf_of(client, "/")
        _sign_in_again(client)
        resp = client.post(
            "/settings",
            data={"csrf_token": old},
            headers=_FORM,
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert resp.text == "CSRF validation failed."


class TestSecFetchSite:
    @pytest.mark.parametrize("site", ["cross-site", "same-site"])
    def test_other_site_is_refused_even_with_a_valid_token(
        self, signed_in: tuple[TestClient, str], site: str
    ) -> None:
        client, _ = signed_in
        token = csrf_of(client, "/")
        resp = client.post(
            "/settings",
            data={},
            headers={**_FORM, "X-CSRF-Token": token, "Sec-Fetch-Site": site},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert resp.text == "CSRF validation failed."

    def test_htmx_refusal_carries_the_csrf_failed_trigger(
        self, signed_in: tuple[TestClient, str]
    ) -> None:
        client, _ = signed_in
        resp = client.post(
            "/settings",
            data={},
            headers={
                **_FORM,
                "X-CSRF-Token": csrf_of(client, "/"),
                "Sec-Fetch-Site": "cross-site",
                "HX-Request": "true",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert "tlm:csrf-failed" in resp.headers["HX-Trigger"]

    @pytest.mark.parametrize("site", ["same-origin", "none", None])
    def test_same_origin_none_and_missing_pass(
        self, signed_in: tuple[TestClient, str], site: str | None
    ) -> None:
        client, _ = signed_in
        headers = {**_FORM, "X-CSRF-Token": csrf_of(client, "/")}
        if site is not None:
            headers["Sec-Fetch-Site"] = site
        resp = client.post(
            "/settings", data={}, headers=headers, follow_redirects=False
        )
        assert resp.status_code == 303

    def test_dead_cookie_login_is_not_blocked(self, web_client: TestClient) -> None:
        # The check sits behind the live-session test: re-login stays open.
        seed_admin(web_client)
        web_client.cookies.set("tlm_session", "dead-token-not-in-db")
        resp = web_client.post(
            "/login",
            data={"username": "admin", "password": "AdminP@ssw0rd1234"},
            headers={**_FORM, "Sec-Fetch-Site": "same-origin"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_safe_methods_are_not_checked(
        self, signed_in: tuple[TestClient, str]
    ) -> None:
        client, _ = signed_in
        resp = client.get("/", headers={"Sec-Fetch-Site": "cross-site"})
        assert resp.status_code == 200


class TestLookupFailureDenies:
    def _break_lookup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("database unavailable")

        # Only the middleware's lookup fails; the route's own auth still works,
        # which is exactly when a forged token would have reached it.
        monkeypatch.setattr(middleware_module, "resolve_request_session", boom)

    def test_masked_token_forged_for_the_deny_sentinel_is_refused(
        self, signed_in: tuple[TestClient, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = signed_in
        self._break_lookup(monkeypatch)
        forged = mask_secret(middleware_module._CSRF_DENY)
        resp = client.post(
            "/settings",
            data={},
            headers={**_FORM, "X-CSRF-Token": forged},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_raw_deny_sentinel_in_the_form_is_refused(
        self, signed_in: tuple[TestClient, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = signed_in
        self._break_lookup(monkeypatch)
        resp = client.post(
            "/settings",
            data={"csrf_token": middleware_module._CSRF_DENY},
            headers=_FORM,
            follow_redirects=False,
        )
        assert resp.status_code == 403


class TestRotationSignal:
    def test_stale_token_gets_a_fresh_one_for_the_live_session(
        self, signed_in: tuple[TestClient, str]
    ) -> None:
        client, _ = signed_in
        old = csrf_of(client, "/")
        new_raw = _sign_in_again(client)
        resp = client.get("/alerts/summary", headers={"X-CSRF-Token": old})
        assert resp.status_code == 200
        fresh = _rotated_token(resp)
        assert fresh is not None
        assert verify_csrf(_secret_of(new_raw), fresh)
        assert fresh != _secret_of(new_raw)
        # The fresh token works for a mutation in the new session.
        ok = client.post(
            "/settings",
            data={},
            headers={**_FORM, "X-CSRF-Token": fresh},
            follow_redirects=False,
        )
        assert ok.status_code == 303

    def test_current_masked_token_gets_no_trigger(
        self, signed_in: tuple[TestClient, str]
    ) -> None:
        client, _ = signed_in
        resp = client.get(
            "/alerts/summary", headers={"X-CSRF-Token": csrf_of(client, "/")}
        )
        assert resp.status_code == 200
        assert "HX-Trigger" not in resp.headers

    def test_bare_secret_gets_a_fresh_token(
        self, signed_in: tuple[TestClient, str]
    ) -> None:
        """A page still carrying the bare secret is told to take a masked one."""
        client, raw = signed_in
        resp = client.get("/alerts/summary", headers={"X-CSRF-Token": _secret_of(raw)})
        assert "tlm:csrf" in resp.headers.get("HX-Trigger", "")

    def test_no_header_gets_no_trigger(self, signed_in: tuple[TestClient, str]) -> None:
        client, _ = signed_in
        resp = client.get("/alerts/summary")
        assert resp.status_code == 200
        assert "HX-Trigger" not in resp.headers

    def test_garbage_header_gets_a_fresh_token(
        self, signed_in: tuple[TestClient, str]
    ) -> None:
        client, raw = signed_in
        resp = client.get("/alerts/summary", headers={"X-CSRF-Token": "garbage"})
        fresh = _rotated_token(resp)
        assert fresh is not None and verify_csrf(_secret_of(raw), fresh)

    def test_signed_out_poll_gets_no_token(self, web_client: TestClient) -> None:
        seed_admin(web_client)
        resp = web_client.get(
            "/alerts/summary",
            headers={"X-CSRF-Token": "stale", "HX-Request": "true"},
            follow_redirects=False,
        )
        # No session, so no route body runs: the auth layer answers (an htmx
        # sign-in redirect), and no token is handed out.
        assert resp.status_code != 200
        assert '"token"' not in resp.headers.get("HX-Trigger", "")

    def test_stale_passive_poll_does_not_touch_the_session(
        self, signed_in: tuple[TestClient, str]
    ) -> None:
        # The rotation lookup follows the passive-poll rule (#70 F-S1).
        client, _ = signed_in
        old = csrf_of(client, "/")
        new_raw = _sign_in_again(client)
        stale = datetime.datetime.now(datetime.UTC).replace(
            tzinfo=None
        ) - datetime.timedelta(minutes=5)
        _set_last_active(new_raw, stale)
        resp = client.get(
            "/alerts/summary",
            headers={"X-CSRF-Token": old, "X-TLM-Passive": "1"},
        )
        assert _rotated_token(resp) is not None
        assert _last_active(new_raw) == stale
