"""L-suite: Log redaction — no secrets in log output during auth flows.

The web request handlers and security layer do not call logger.xxx() in normal
operation (password hashing, session creation, CSRF verification all happen
silently).  These tests therefore verify two things:

  1. Positive control: caplog IS capturing records during the test (proven by
     an explicit sentinel log line emitted at the start of each test).
  2. Negative assertion: the secret substring does NOT appear anywhere in the
     captured records, so if the source code is ever changed to log something
     that contains a secret it will be caught immediately.

The positive control uses the test's own logger (``logging.getLogger(__name__)``
at DEBUG level) so it is always captured without depending on application code
emitting anything.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from tests.conftest import csrf_of, login, seed_admin
from timelapse_manager.runtime import get_context
from timelapse_manager.security.token import ensure_local_token

_LOG = logging.getLogger(__name__)

# A sentinel string that is always logged before each redaction check so that
# caplog can be proven to be capturing records.  Must not overlap with any
# secret value used in the tests.
_SENTINEL_LOG_MARKER = "redaction-test-caplog-sentinel"


def _emit_sentinel(caplog: pytest.LogCaptureFixture) -> None:
    """Emit the sentinel string so the test can confirm caplog is active.

    Propagates through the root logger so that caplog (which captures root)
    receives it regardless of which logger hierarchy the test module sits in.
    """
    logging.getLogger().debug(_SENTINEL_LOG_MARKER)


def _assert_caplog_active(caplog: pytest.LogCaptureFixture) -> None:
    """Confirm that caplog captured the sentinel record (positive control)."""
    combined = "\n".join(r.getMessage() for r in caplog.records)
    assert _SENTINEL_LOG_MARKER in combined, (
        "caplog did not capture the sentinel record — positive control failed"
    )


def _assert_no_secret_in_logs(
    caplog: pytest.LogCaptureFixture,
    secret_substrings: list[str],
    *,
    description: str = "secret",
) -> None:
    """Assert that none of the given secret substrings appear in any log record."""
    combined = "\n".join(r.getMessage() for r in caplog.records)
    for secret in secret_substrings:
        if not secret:
            continue
        assert secret not in combined, (
            f"{description!r} appeared in log output: {combined[:200]!r}"
        )


class TestPasswordNotLogged:
    def test_login_password_not_in_logs(
        self, web_client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        seed_admin(web_client)
        password = "AdminP@ssw0rd1234"
        with caplog.at_level(logging.DEBUG):
            _emit_sentinel(caplog)
            web_client.post(
                "/login",
                data={"username": "admin", "password": password},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
        _assert_caplog_active(caplog)
        _assert_no_secret_in_logs(caplog, [password], description="password")

    def test_failed_login_password_not_in_logs(
        self, web_client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        seed_admin(web_client)
        wrong_password = "WrongPasswordXYZ99!"
        with caplog.at_level(logging.DEBUG):
            _emit_sentinel(caplog)
            web_client.post(
                "/login",
                data={"username": "admin", "password": wrong_password},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
        _assert_caplog_active(caplog)
        _assert_no_secret_in_logs(
            caplog, [wrong_password], description="failed-login password"
        )

    def test_first_run_password_not_in_logs(
        self, web_client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        password = "SetupPassword99!!"
        with caplog.at_level(logging.DEBUG):
            _emit_sentinel(caplog)
            web_client.post(
                "/first-run",
                data={
                    "username": "setupadmin",
                    "password": password,
                    "password_confirm": password,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
        _assert_caplog_active(caplog)
        _assert_no_secret_in_logs(caplog, [password], description="first-run password")


class TestSessionTokenNotLogged:
    def test_session_token_not_in_logs_after_login(
        self, web_client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        seed_admin(web_client)
        with caplog.at_level(logging.DEBUG):
            _emit_sentinel(caplog)
            login(web_client)
        cookie_name = get_context().settings.session.cookie_name
        raw_token = web_client.cookies.get(cookie_name)
        assert raw_token  # sanity check
        _assert_caplog_active(caplog)
        _assert_no_secret_in_logs(caplog, [raw_token], description="session token")

    def test_session_token_not_in_logs_after_logout(
        self, admin_client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        cookie_name = get_context().settings.session.cookie_name
        raw_token = admin_client.cookies.get(cookie_name)
        csrf = csrf_of(admin_client, "/")
        with caplog.at_level(logging.DEBUG):
            _emit_sentinel(caplog)
            admin_client.post(
                "/logout",
                data={"csrf_token": csrf},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
        _assert_caplog_active(caplog)
        _assert_no_secret_in_logs(
            caplog, [raw_token], description="session token (logout)"
        )


class TestCliTokenNotLogged:
    def test_cli_bearer_token_not_in_logs(
        self, cli_client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        ctx = get_context()
        token = ensure_local_token(ctx.settings)
        with caplog.at_level(logging.DEBUG):
            _emit_sentinel(caplog)
            cli_client.get(
                "/api/v1/system",
                headers={"Authorization": f"Bearer {token}"},
            )
        _assert_caplog_active(caplog)
        _assert_no_secret_in_logs(caplog, [token], description="CLI bearer token")


class TestPasswordHashNotLogged:
    def test_password_hash_not_in_logs_during_user_admin(
        self, admin_client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The Argon2 hash must not be emitted during user management actions."""
        csrf = csrf_of(admin_client, "/users")
        new_password = "NewUserPassword99!"
        with caplog.at_level(logging.DEBUG):
            _emit_sentinel(caplog)
            admin_client.post(
                "/users",
                data={
                    "username": "hash-test-user",
                    "password": new_password,
                    "password_confirm": new_password,
                    "role": "viewer",
                    "csrf_token": csrf,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
        _assert_caplog_active(caplog)
        _assert_no_secret_in_logs(
            caplog, [new_password], description="user admin password"
        )
        # Also assert no argon2 hash format appears.
        combined = "\n".join(r.getMessage() for r in caplog.records)
        assert "$argon2" not in combined, "Argon2 hash must not appear in logs"
