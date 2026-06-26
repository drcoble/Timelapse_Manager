"""B/C-suite: Login, logout, and session cookie behaviour."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import csrf_of, login, seed_admin
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.security.sessions import get_session_row


class TestLoginCookieFlags:
    def test_successful_login_sets_session_cookie(self, web_client: TestClient) -> None:
        seed_admin(web_client)
        resp = web_client.post(
            "/login",
            data={"username": "admin", "password": "AdminP@ssw0rd1234"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "tlm_session" in resp.headers.get("set-cookie", "")

    def test_session_cookie_is_httponly(self, web_client: TestClient) -> None:
        seed_admin(web_client)
        resp = web_client.post(
            "/login",
            data={"username": "admin", "password": "AdminP@ssw0rd1234"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        set_cookie = resp.headers.get("set-cookie", "")
        assert "HttpOnly" in set_cookie

    def test_session_cookie_is_secure_over_https(self, web_client: TestClient) -> None:
        """Cookie must carry Secure attribute when effective scheme is HTTPS."""
        seed_admin(web_client)
        resp = web_client.post(
            "/login",
            data={"username": "admin", "password": "AdminP@ssw0rd1234"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        set_cookie = resp.headers.get("set-cookie", "")
        assert "Secure" in set_cookie

    def test_session_cookie_has_samesite_lax(self, web_client: TestClient) -> None:
        seed_admin(web_client)
        resp = web_client.post(
            "/login",
            data={"username": "admin", "password": "AdminP@ssw0rd1234"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        set_cookie = resp.headers.get("set-cookie", "").lower()
        assert "samesite=lax" in set_cookie

    def test_session_cookie_not_secure_over_http(
        self, web_client_no_redirect: TestClient
    ) -> None:
        """Cookie must NOT carry Secure when effective scheme is HTTP."""
        # web_client_no_redirect uses http base_url; fixture already migrated.
        seed_admin(web_client_no_redirect)
        resp = web_client_no_redirect.post(
            "/login",
            data={"username": "admin", "password": "AdminP@ssw0rd1234"},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Forwarded-Proto": "http",
            },
            follow_redirects=False,
        )
        set_cookie = resp.headers.get("set-cookie", "")
        assert "Secure" not in set_cookie


class TestLoginFailure:
    def test_wrong_password_returns_generic_error(self, web_client: TestClient) -> None:
        seed_admin(web_client)
        resp = web_client.post(
            "/login",
            data={"username": "admin", "password": "completely-wrong-pw"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 401

    def test_unknown_username_returns_generic_error(
        self, web_client: TestClient
    ) -> None:
        seed_admin(web_client)
        resp = web_client.post(
            "/login",
            data={"username": "no-such-user", "password": "SomePassword123!"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 401

    def test_wrong_password_and_unknown_user_return_same_status(
        self, web_client: TestClient
    ) -> None:
        """Non-enumeration: both failure modes return identical HTTP status."""
        seed_admin(web_client)
        wrong_pw = web_client.post(
            "/login",
            data={"username": "admin", "password": "wrong-password-xx"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        unknown_user = web_client.post(
            "/login",
            data={"username": "ghost-user-xyz", "password": "SomePassword123!"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert wrong_pw.status_code == unknown_user.status_code

    def test_missing_username_returns_401(self, web_client: TestClient) -> None:
        seed_admin(web_client)
        resp = web_client.post(
            "/login",
            data={"username": "", "password": "SomePassword123!"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 401

    def test_missing_password_returns_401(self, web_client: TestClient) -> None:
        seed_admin(web_client)
        resp = web_client.post(
            "/login",
            data={"username": "admin", "password": ""},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 401


class TestSessionRotation:
    def test_session_token_changes_on_login(self, web_client: TestClient) -> None:
        """Session is rotated on login: the old token (if any) is revoked."""
        seed_admin(web_client)
        cookie_name = get_context().settings.session.cookie_name
        # First login.
        login(web_client)
        token_before = web_client.cookies.get(cookie_name)
        # Force a second login (log out then back in).
        csrf = csrf_of(web_client, "/")
        web_client.post(
            "/logout",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        login(web_client)
        token_after = web_client.cookies.get(cookie_name)
        assert token_before != token_after


class TestLogout:
    def test_logout_revokes_session_and_clears_cookie(
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
        assert "/login" in resp.headers["location"]
        # After logout the dashboard should require re-auth: a browser navigation
        # (Accept: text/html) is redirected to the login page, not a bare 401.
        get_resp = admin_client.get(
            "/", headers={"Accept": "text/html"}, follow_redirects=False
        )
        assert get_resp.status_code == 303
        assert get_resp.headers["location"].startswith("/login")

    def test_old_token_is_invalid_after_logout(self, admin_client: TestClient) -> None:
        cookie_name = get_context().settings.session.cookie_name
        old_token = admin_client.cookies.get(cookie_name)
        csrf = csrf_of(admin_client, "/")
        admin_client.post(
            "/logout",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        # Verify the token is no longer live in the DB.
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            row = get_session_row(db, old_token, settings=ctx.settings.session)
            assert row is None, "Session must be revoked after logout"


class TestRememberMe:
    def test_remember_me_does_not_set_max_age_when_false(
        self, web_client: TestClient
    ) -> None:
        seed_admin(web_client)
        resp = web_client.post(
            "/login",
            data={"username": "admin", "password": "AdminP@ssw0rd1234"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        set_cookie = resp.headers.get("set-cookie", "").lower()
        # Session cookie: no Max-Age or Expires.
        assert "max-age" not in set_cookie

    def test_remember_me_sets_max_age(self, web_client: TestClient) -> None:
        seed_admin(web_client)
        resp = web_client.post(
            "/login",
            data={
                "username": "admin",
                "password": "AdminP@ssw0rd1234",
                "remember_me": "1",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        set_cookie = resp.headers.get("set-cookie", "").lower()
        assert "max-age" in set_cookie


class TestXForwardedProto:
    def test_x_forwarded_proto_https_sets_secure_cookie(
        self, web_client_no_redirect: TestClient
    ) -> None:
        seed_admin(web_client_no_redirect)
        resp = web_client_no_redirect.post(
            "/login",
            data={"username": "admin", "password": "AdminP@ssw0rd1234"},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Forwarded-Proto": "https",
            },
            follow_redirects=False,
        )
        set_cookie = resp.headers.get("set-cookie", "")
        assert "Secure" in set_cookie


class TestLoginThrottleIntegration:
    """The brute-force throttle locks out the /login path after enough failures."""

    @staticmethod
    def _attempt(client: TestClient, password: str):
        return client.post(
            "/login",
            data={"username": "admin", "password": password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )

    def test_repeated_failures_lock_out_even_correct_password(
        self, web_client: TestClient
    ) -> None:
        seed_admin(web_client)
        ctx = get_context()
        cookie_name = ctx.settings.session.cookie_name
        max_failures = ctx.settings.auth.throttle_max_failures

        # Exhaust the failure budget from this source with wrong passwords.
        for _ in range(max_failures + 1):
            resp = self._attempt(web_client, "wrong-password")
            assert resp.status_code == 401

        # Now even the CORRECT password is rejected: the source is throttled, so
        # the lockout fires at the web layer before authentication is attempted.
        resp = self._attempt(web_client, "AdminP@ssw0rd1234")
        assert resp.status_code == 401
        assert web_client.cookies.get(cookie_name) is None  # no session established
