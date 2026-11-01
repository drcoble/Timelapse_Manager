"""Login CSRF: the pages with no session yet carry their own token (#67 S21).

``GET /login`` and ``GET /first-run`` set a random pre-session cookie and render a
token for it; the POST must present that token.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient
from httpx import Response

from tests.conftest import seed_admin
from tests.presession_support import presession_cookie, session_cookie
from tests.web.test_first_run import (
    _ADMIN_FORM,
    _FORM_HEADERS,
    _real_admin_count,
    _setup_code,
)
from timelapse_manager.runtime import get_context
from timelapse_manager.security.csrf import verify_csrf
from timelapse_manager.web.presession import PRESESSION_RETRY_MESSAGE

PREFIXED = "__Host-tlm_presession"
PLAIN = "tlm_presession"
_FORM = {"Content-Type": "application/x-www-form-urlencoded"}
_PASSWORD = "AdminP@ssw0rd1234"
_TOKEN = re.compile(r'<input type="hidden" name="csrf_token" value="([^"]*)">')


def _token_in(html: str) -> str:
    match = _TOKEN.search(html)
    assert match is not None, "no csrf_token field on the page"
    return match.group(1)


def _post_login(
    client: TestClient, csrf_token: str, password: str = _PASSWORD
) -> Response:
    return client.post(
        "/login",
        data={"username": "admin", "password": password, "csrf_token": csrf_token},
        headers=_FORM,
        follow_redirects=False,
    )


def _set_cookies(resp: Response) -> list[str]:
    return list(resp.headers.get_list("set-cookie"))


class TestTheLoginPage:
    def test_get_sets_the_cookie_and_renders_a_matching_token(
        self, anon_client: TestClient
    ) -> None:
        resp = anon_client.get("/login")
        assert resp.status_code == 200
        [header] = [h for h in _set_cookies(resp) if h.startswith(f"{PREFIXED}=")]
        assert "HttpOnly" in header
        assert "Secure" in header
        assert "SameSite=strict" in header
        assert "Path=/" in header
        assert "Domain" not in header
        secret = anon_client.cookies.get(PREFIXED)
        assert secret
        token = _token_in(resp.text)
        assert token and token != secret  # masked, not the bare secret
        assert verify_csrf(secret, token)

    def test_the_token_differs_on_every_render(self, anon_client: TestClient) -> None:
        first = _token_in(anon_client.get("/login").text)
        second = _token_in(anon_client.get("/login").text)
        assert first != second

    def test_a_second_visit_reuses_the_cookie(self, anon_client: TestClient) -> None:
        """A second tab must not invalidate the first tab's token."""
        first = anon_client.get("/login")
        token_one = _token_in(first.text)
        second = anon_client.get("/login")
        assert not [h for h in _set_cookies(second) if h.startswith(f"{PREFIXED}=")]
        assert _token_in(second.text) != token_one
        # Both verify against the one cookie.
        assert _post_login(anon_client, token_one).status_code == 303

    def test_a_malformed_cookie_is_replaced(self, anon_client: TestClient) -> None:
        anon_client.cookies.set(PREFIXED, "short")
        resp = anon_client.get("/login")
        [header] = [h for h in _set_cookies(resp) if h.startswith(f"{PREFIXED}=")]
        assert not header.startswith(f"{PREFIXED}=short;")

    def test_the_cookie_is_plain_named_over_http(
        self, web_client_no_redirect: TestClient
    ) -> None:
        seed_admin(web_client_no_redirect)
        resp = web_client_no_redirect.get("/login")
        [header] = [h for h in _set_cookies(resp) if h.startswith(f"{PLAIN}=")]
        assert "Secure" not in header
        assert not any(h.startswith("__Host-") for h in _set_cookies(resp))
        assert (
            _post_login(web_client_no_redirect, _token_in(resp.text)).status_code == 303
        )


class TestTheLoginPost:
    def test_a_good_token_signs_in_and_clears_the_cookie(
        self, anon_client: TestClient
    ) -> None:
        token = _token_in(anon_client.get("/login").text)
        resp = _post_login(anon_client, token)
        assert resp.status_code == 303
        assert session_cookie(anon_client)
        assert any(
            h.startswith(f"{PREFIXED}=") and "Max-Age=0" in h
            for h in _set_cookies(resp)
        )
        assert presession_cookie(anon_client) in (None, "")

    def test_no_token_is_refused_even_with_the_right_password(
        self, anon_client: TestClient
    ) -> None:
        anon_client.get("/login")
        resp = _post_login(anon_client, "")
        assert resp.status_code == 403
        assert PRESESSION_RETRY_MESSAGE in resp.text
        assert session_cookie(anon_client) is None

    def test_no_cookie_and_no_token_is_refused(self, anon_client: TestClient) -> None:
        """An empty token must not "match" an absent secret."""
        resp = _post_login(anon_client, "")
        assert resp.status_code == 403
        assert session_cookie(anon_client) is None

    def test_a_token_without_the_cookie_is_refused(
        self, anon_client: TestClient
    ) -> None:
        """What a cross-site form has: a token it knows, but not the victim's cookie."""
        token = _token_in(anon_client.get("/login").text)
        anon_client.cookies.clear()
        resp = _post_login(anon_client, token)
        assert resp.status_code == 403

    def test_a_token_for_another_secret_is_refused(
        self, anon_client: TestClient
    ) -> None:
        anon_client.get("/login")
        other = "A" * 43
        resp = _post_login(anon_client, other)
        assert resp.status_code == 403

    def test_a_non_ascii_token_is_a_mismatch_not_an_error(
        self, anon_client: TestClient
    ) -> None:
        anon_client.get("/login")
        resp = _post_login(anon_client, "é😀token")
        assert resp.status_code == 403

    def test_a_planted_plain_cookie_is_not_read_over_https(
        self, anon_client: TestClient
    ) -> None:
        """A sibling subdomain can set ``tlm_presession`` but not ``__Host-``."""
        secret = "B" * 43
        anon_client.cookies.set(PLAIN, secret)
        resp = _post_login(anon_client, secret)
        assert resp.status_code == 403

    def test_the_session_csrf_token_does_not_stand_in(
        self, anon_client: TestClient
    ) -> None:
        anon_client.get("/login")
        resp = _post_login(anon_client, "x")
        assert resp.status_code == 403

    def test_a_refusal_renders_a_working_form(self, anon_client: TestClient) -> None:
        """The re-rendered page carries a token the retry can use."""
        refused = _post_login(anon_client, "")
        assert refused.status_code == 403
        token = _token_in(refused.text)
        assert token
        assert _post_login(anon_client, token).status_code == 303

    def test_a_wrong_password_renders_a_form_that_still_works(
        self, anon_client: TestClient
    ) -> None:
        token = _token_in(anon_client.get("/login").text)
        wrong = _post_login(anon_client, token, password="nope")
        assert wrong.status_code == 401
        retry = _token_in(wrong.text)
        assert retry
        assert _post_login(anon_client, retry).status_code == 303

    def test_a_forged_post_spends_none_of_the_throttle_budget(
        self, anon_client: TestClient
    ) -> None:
        budget = get_context().settings.auth.throttle_max_failures
        for _ in range(budget + 2):
            assert _post_login(anon_client, "").status_code == 403
        token = _token_in(anon_client.get("/login").text)
        assert _post_login(anon_client, token).status_code == 303


class TestFirstRun:
    def test_get_sets_the_cookie_and_renders_a_token(
        self, web_client: TestClient
    ) -> None:
        resp = web_client.get("/first-run")
        assert resp.status_code == 200
        secret = web_client.cookies.get(PREFIXED)
        assert secret
        assert verify_csrf(secret, _token_in(resp.text))

    def test_post_without_the_token_is_refused_and_creates_nothing(
        self, web_client: TestClient
    ) -> None:
        code = _setup_code(web_client)
        resp = web_client.post(
            "/first-run",
            data={**_ADMIN_FORM, "setup_code": code, "csrf_token": ""},
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert PRESESSION_RETRY_MESSAGE in resp.text
        assert _real_admin_count() == 0

    def test_post_with_the_token_creates_the_admin(
        self, web_client: TestClient
    ) -> None:
        code = _setup_code(web_client)
        token = _token_in(web_client.get("/first-run").text)
        resp = web_client.post(
            "/first-run",
            data={**_ADMIN_FORM, "setup_code": code, "csrf_token": token},
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _real_admin_count() == 1
        assert session_cookie(web_client)
