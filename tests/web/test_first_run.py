"""A-suite: First-run gate and bootstrap flow."""

from __future__ import annotations

import logging
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.conftest import LOOPBACK_PEER, seed_admin
from tests.presession_support import WebTestClient
from timelapse_manager.db.models import User
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.security.login import read_setup_code, setup_code_path
from timelapse_manager.web.routers import auth as auth_routes


def _setup_code(client: TestClient) -> str:
    """Open the setup page (which creates the code file) and return the code."""
    assert client.get("/first-run", follow_redirects=False).status_code == 200
    code = read_setup_code(get_context().settings.paths.data_dir)
    assert code is not None
    return code


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
                "setup_code": _setup_code(web_client),
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
                "setup_code": _setup_code(web_client),
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
                "setup_code": _setup_code(web_client),
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
                "setup_code": "any-code-at-all",
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
                "setup_code": _setup_code(web_client),
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
                "setup_code": _setup_code(web_client),
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


# ---------------------------------------------------------------------------
# One-time setup code and serialised creation (#67 S11)
# ---------------------------------------------------------------------------

_ADMIN_FORM = {
    "username": "initialadmin",
    "password": "SecureAdminPass99!",
    "password_confirm": "SecureAdminPass99!",
}
_FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}


def _code_file() -> Path:
    return setup_code_path(get_context().settings.paths.data_dir)


def _real_admin_count() -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        return len(
            db.execute(
                select(User.id).where(
                    User.role == "admin", User.password_hash.is_not(None)
                )
            ).all()
        )


class TestSetupCode:
    def test_setup_page_creates_owner_only_code_file(
        self, web_client: TestClient
    ) -> None:
        assert not _code_file().exists()
        resp = web_client.get("/first-run")
        assert resp.status_code == 200
        assert 'name="setup_code"' in resp.text
        info = _code_file().stat()
        assert stat.S_IMODE(info.st_mode) == 0o600
        code = read_setup_code(get_context().settings.paths.data_dir)
        assert code is not None and len(code) == 12
        # The page never shows the code itself.
        assert code not in resp.text

    def test_code_is_stable_across_page_loads(self, web_client: TestClient) -> None:
        first = _setup_code(web_client)
        assert _setup_code(web_client) == first

    def test_creation_is_logged_without_the_code(
        self, web_client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            code = _setup_code(web_client)
        messages = [record.getMessage() for record in caplog.records]
        assert any("first-run setup code written to" in m for m in messages)
        assert all(code not in m for m in messages)

    @pytest.mark.parametrize("presented", [None, "", "wrong-code-12", "x" * 64])
    def test_missing_or_wrong_code_is_403_and_creates_nothing(
        self, web_client: TestClient, presented: str | None
    ) -> None:
        _setup_code(web_client)
        data = dict(_ADMIN_FORM)
        if presented is not None:
            data["setup_code"] = presented
        resp = web_client.post(
            "/first-run", data=data, headers=_FORM_HEADERS, follow_redirects=False
        )
        assert resp.status_code == 403
        assert "setup code is missing or incorrect" in resp.text
        # The username survives the re-render; the rejected code does not.
        assert 'value="initialadmin"' in resp.text
        if presented:
            assert presented not in resp.text
        assert _real_admin_count() == 0
        assert _code_file().exists()

    def test_no_code_file_means_403_even_for_an_empty_code(
        self, web_client: TestClient
    ) -> None:
        """A submit before any code exists fails closed, and creates no code."""
        resp = web_client.post(
            "/first-run",
            data={**_ADMIN_FORM, "setup_code": ""},
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert not _code_file().exists()
        assert _real_admin_count() == 0

    def test_non_ascii_code_is_403_not_500(self, web_client: TestClient) -> None:
        _setup_code(web_client)
        resp = web_client.post(
            "/first-run",
            data={**_ADMIN_FORM, "setup_code": "é" * 12},
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_code_is_checked_before_the_other_fields(
        self, web_client: TestClient
    ) -> None:
        """A wrong code with a bad password is a 403, not a 400 field error."""
        _setup_code(web_client)
        resp = web_client.post(
            "/first-run",
            data={
                "setup_code": "wrong-code-12",
                "username": "adminuser",
                "password": "short",
                "password_confirm": "short",
            },
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_correct_code_creates_admin_and_deletes_the_file(
        self, web_client: TestClient
    ) -> None:
        code = _setup_code(web_client)
        resp = web_client.post(
            "/first-run",
            data={**_ADMIN_FORM, "setup_code": f"  {code}\n"},
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"
        assert _real_admin_count() == 1
        assert not _code_file().exists()
        # The used code is now worthless: setup is complete. (Drop the new
        # admin's session cookie, or the CSRF layer answers first.)
        web_client.cookies.clear()
        again = web_client.post(
            "/first-run",
            data={**_ADMIN_FORM, "username": "second", "setup_code": code},
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        assert again.status_code == 303
        assert "/login" in again.headers["location"]
        assert _real_admin_count() == 1

    def test_existing_install_never_gets_a_code(self, web_client: TestClient) -> None:
        """An install past first-run (both deployed VMs) is unaffected."""
        seed_admin(web_client)
        resp = web_client.get("/first-run", follow_redirects=False)
        assert resp.status_code == 303
        assert not _code_file().exists()

    def test_symlinked_code_file_is_not_followed(
        self, web_client: TestClient, tmp_path: Path
    ) -> None:
        """A link planted at the code path is never read through."""
        outside = tmp_path / "outside-code"
        outside.write_text("abcdefghijkl\n")
        _code_file().symlink_to(outside)
        assert read_setup_code(get_context().settings.paths.data_dir) is None
        resp = web_client.post(
            "/first-run",
            data={**_ADMIN_FORM, "setup_code": "abcdefghijkl"},
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_unwritable_data_dir_shows_an_error_and_stays_closed(
        self, web_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(_data_dir: Path) -> Path:
            raise PermissionError("read-only")

        monkeypatch.setattr(auth_routes, "ensure_setup_code", _fail)
        resp = web_client.get("/first-run")
        assert resp.status_code == 200
        assert "could not be created" in resp.text


class TestConcurrentFirstRun:
    def test_parallel_submits_create_exactly_one_admin(
        self, web_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two threads racing the submit: one admin, no 500, the other refused."""
        code = _setup_code(web_client)
        barrier = threading.Barrier(2)
        real_create = auth_routes.create_initial_admin

        def slow_create(*args: Any, **kwargs: Any) -> Any:
            # Hold the check-then-create window open, so without the lock both
            # racers would pass the re-check before either commits.
            time.sleep(0.3)
            return real_create(*args, **kwargs)

        monkeypatch.setattr(auth_routes, "create_initial_admin", slow_create)

        def submit(name: str) -> int:
            # A separate client (own cookie jar) per racer, without its own
            # lifespan: the app context stays the one ``web_client`` started.
            client = WebTestClient(
                web_client.app, base_url="https://testserver", client=LOOPBACK_PEER
            )
            barrier.wait()
            resp = client.post(
                "/first-run",
                data={**_ADMIN_FORM, "username": name, "setup_code": code},
                headers=_FORM_HEADERS,
                follow_redirects=False,
            )
            return resp.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(submit, ["racer-one", "racer-two"]))

        # The winner lands on ``/``; the loser re-checks under the lock, sees the
        # admin and is sent to ``/login``. Neither is a 500.
        assert statuses == [303, 303], statuses
        assert _real_admin_count() == 1
