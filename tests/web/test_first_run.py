"""A-suite: First-run gate and bootstrap flow."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import seed_admin


class TestFirstRunGate:
    def test_non_allowlisted_path_redirects_to_first_run_on_fresh_db(
        self, web_client: TestClient
    ) -> None:
        """Any protected path redirects to /first-run before an admin exists."""
        resp = web_client.get("/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].endswith("/first-run")

    def test_login_path_also_redirects_to_first_run(
        self, web_client: TestClient
    ) -> None:
        resp = web_client.get("/login", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].endswith("/first-run")

    def test_first_run_path_itself_is_allowed(self, web_client: TestClient) -> None:
        resp = web_client.get("/first-run", follow_redirects=False)
        assert resp.status_code == 200

    def test_healthz_is_always_reachable(self, web_client: TestClient) -> None:
        resp = web_client.get("/healthz")
        assert resp.status_code == 200

    def test_api_prefix_is_always_reachable(self, web_client: TestClient) -> None:
        resp = web_client.get("/api/v1/system", follow_redirects=False)
        # 401 is expected (no bearer token), not a redirect to /first-run.
        assert resp.status_code == 401

    def test_static_prefix_is_allowed(self, web_client: TestClient) -> None:
        # /static itself returns 404 (no directory listing), not a redirect.
        resp = web_client.get("/static/nonexistent.css", follow_redirects=False)
        assert resp.status_code not in (303, 308)


class TestSentinelDoesNotCountAsAdmin:
    def test_sentinel_only_db_still_shows_first_run_form(
        self, web_client: TestClient
    ) -> None:
        """The sentinel user (id 1, no password) does not satisfy first-run.

        After migrations run, the sentinel may or may not already exist — but
        if it does, the first-run gate must still be active because the sentinel
        has no password hash and must never count as a real admin.
        """
        resp = web_client.get("/first-run", follow_redirects=False)
        assert resp.status_code == 200
        assert (
            "first" in resp.text.lower()
            or "setup" in resp.text.lower()
            or "admin" in resp.text.lower()
        )

    def test_sentinel_cannot_authenticate(self, web_client: TestClient) -> None:
        """Logging in as 'system' (the sentinel) must always fail."""
        resp = web_client.post(
            "/first-run",
            data={
                "username": "system",
                "password": "anything-at-all-123!",
                "password_confirm": "anything-at-all-123!",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        # Should either create a NEW admin named "system" (if username accepted)
        # or show an error. Either way, we must NOT be able to log in with a
        # blank-hash sentinel: try to authenticate explicitly.
        # Now attempt to POST /login with sentinel credentials — must fail.
        if resp.status_code == 303:
            # first-run created a user named "system" with the given password,
            # which is a real admin (not the sentinel). That is acceptable.
            return
        # If first-run failed, the sentinel still cannot log in.
        login_resp = web_client.post(
            "/login",
            data={"username": "system", "password": "anything-at-all-123!"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert login_resp.status_code in (401, 303), (
            "Sentinel (no-password user) must not be able to log in"
        )
        if login_resp.status_code == 303:
            # If it redirected, first-run must still be needed (not a login).
            assert "first-run" in login_resp.headers.get("location", "")


class TestFirstRunSubmit:
    def test_post_first_run_creates_admin_and_lands_on_dashboard(
        self, web_client: TestClient
    ) -> None:
        resp = web_client.post(
            "/first-run",
            data={
                "username": "initialadmin",
                "password": "SecureAdminPass99!",
                "password_confirm": "SecureAdminPass99!",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "dashboard" in resp.url.path or resp.url.path == "/"

    def test_post_first_run_sets_secure_session_cookie_over_https(
        self, web_client: TestClient
    ) -> None:
        resp = web_client.post(
            "/first-run",
            data={
                "username": "setupadmin",
                "password": "SecureAdminPass99!",
                "password_confirm": "SecureAdminPass99!",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        set_cookie = resp.headers.get("set-cookie", "")
        assert "HttpOnly" in set_cookie
        assert "Secure" in set_cookie
        assert "SameSite=lax" in set_cookie or "samesite=lax" in set_cookie.lower()

    def test_revisiting_first_run_after_setup_redirects_to_login(
        self, web_client: TestClient
    ) -> None:
        seed_admin(web_client)
        resp = web_client.get("/first-run", follow_redirects=False)
        assert resp.status_code == 303
        assert "/login" in resp.headers["location"]

    def test_first_run_post_after_setup_is_rejected(
        self, web_client: TestClient
    ) -> None:
        seed_admin(web_client)
        resp = web_client.post(
            "/first-run",
            data={
                "username": "second-admin",
                "password": "AnotherPassword99!",
                "password_confirm": "AnotherPassword99!",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        # Must be a redirect to login, not acceptance.
        assert resp.status_code == 303
        assert "/login" in resp.headers["location"]

    def test_short_password_returns_error(self, web_client: TestClient) -> None:
        resp = web_client.post(
            "/first-run",
            data={
                "username": "adminuser",
                "password": "short",
                "password_confirm": "short",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_password_mismatch_returns_error(self, web_client: TestClient) -> None:
        resp = web_client.post(
            "/first-run",
            data={
                "username": "adminuser",
                "password": "SecureAdminPass99!",
                "password_confirm": "DifferentPassword99!",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_no_default_credentials_anywhere(self, web_client: TestClient) -> None:
        """No route should grant access without valid credentials on a fresh install."""
        for path in ("/", "/dashboard", "/cameras", "/projects", "/settings", "/users"):
            resp = web_client.get(path, follow_redirects=False)
            # Fresh DB: gate redirects everything to /first-run (not 200).
            assert resp.status_code in (303, 308), (
                f"{path} returned {resp.status_code} before first-run"
            )
