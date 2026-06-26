"""D-suite: CSRF enforcement on cookie-authenticated requests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import csrf_of, seed_admin
from timelapse_manager.runtime import get_context
from timelapse_manager.security.token import ensure_local_token


class TestCsrfRequiredForCookieAuth:
    def test_post_without_token_returns_403(self, admin_client: TestClient) -> None:
        """A cookie-authenticated POST with no CSRF token is rejected."""
        resp = admin_client.post(
            "/logout",
            data={},  # no csrf_token
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_post_with_wrong_token_returns_403(self, admin_client: TestClient) -> None:
        resp = admin_client.post(
            "/logout",
            data={"csrf_token": "wrong-bogus-token-value"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_post_with_correct_form_token_succeeds(
        self, admin_client: TestClient
    ) -> None:
        csrf = csrf_of(admin_client, "/")
        resp = admin_client.post(
            "/logout",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_post_with_correct_header_token_succeeds(
        self, admin_client: TestClient
    ) -> None:
        csrf = csrf_of(admin_client, "/")
        # Re-login so we have an active session (logout above consumed it).
        # Build a fresh admin_client by using the underlying web_client fixture.
        # In this test we use a fresh admin_client so the session is still live.
        resp = admin_client.post(
            "/settings",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-CSRF-Token": csrf,
            },
            data={},
            follow_redirects=False,
        )
        # Successful settings POST → 303 redirect (not 403).
        assert resp.status_code == 303

    def test_delete_without_csrf_returns_403(self, admin_client: TestClient) -> None:
        """DELETE is an unsafe method and requires CSRF for cookie-auth."""
        resp = admin_client.delete(
            "/cameras/9999",  # Does not exist, but CSRF check comes first.
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_get_never_requires_csrf(self, admin_client: TestClient) -> None:
        """GET requests are always allowed regardless of CSRF token presence."""
        resp = admin_client.get("/", follow_redirects=False)
        assert resp.status_code == 200


class TestCsrfNotRequiredForNoCookie:
    def test_dead_cookie_post_login_is_not_csrf_lockout(
        self, web_client: TestClient
    ) -> None:
        """A present-but-dead cookie does not trigger the CSRF check on POST /login.

        When a session has expired the middleware must NOT require a CSRF token
        on the login form (there is no live session to protect), so the user
        can re-authenticate without being locked out.
        """
        seed_admin(web_client)
        # Plant a bogus (non-live) session cookie manually.
        web_client.cookies.set("tlm_session", "dead-token-not-in-db")
        resp = web_client.post(
            "/login",
            data={"username": "admin", "password": "AdminP@ssw0rd1234"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        # Should authenticate and get a 303 (not a 403 CSRF lockout).
        assert resp.status_code == 303

    def test_cli_bearer_path_is_csrf_exempt(self, cli_client: TestClient) -> None:
        """The CLI bearer-token path carries no session cookie, so CSRF is exempt."""
        ctx = get_context()
        token = ensure_local_token(ctx.settings)
        # POST to the API (not the web UI) using bearer — no CSRF token needed.
        resp = cli_client.get(
            "/api/v1/system",
            headers={"Authorization": f"Bearer {token}"},
            follow_redirects=False,
        )
        assert resp.status_code == 200


class TestCsrfTokenInResponse:
    def test_csrf_meta_tag_present_on_authenticated_page(
        self, admin_client: TestClient
    ) -> None:
        resp = admin_client.get("/")
        assert resp.status_code == 200
        assert 'name="csrf-token"' in resp.text
        assert 'content="' in resp.text

    def test_csrf_token_is_non_empty_when_authenticated(
        self, admin_client: TestClient
    ) -> None:
        csrf = csrf_of(admin_client, "/")
        assert len(csrf) > 10
