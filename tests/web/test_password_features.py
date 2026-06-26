"""Integration tests for the two password-management features.

Feature 1: POST /account/password — self-service password change for any
authenticated user.

Feature 2: POST /users/{user_id}/reset-password — admin-only forced reset
with HTMX row-fragment response.

Each test class documents the contract it exercises. DB state is verified
through the session factory (not by re-logging in) so the assertions survive
future UI changes.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import csrf_of, login, seed_admin
from timelapse_manager.db.models import Session as SessionRow
from timelapse_manager.db.models import User
from timelapse_manager.db.models.event import Event
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.security import create_session, verify_password

# Password min-length in web_settings fixture (conftest.py AuthSettings): 12.
# A too-short password must be below this; a valid one must meet or exceed it.
_VALID_PW = "ValidPass12345!"
_TOO_SHORT_PW = "Short1!"  # 7 characters — below the 12-char minimum


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _get_user(username: str) -> User:
    """Return the User row for the given username via the running context."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        user = db.query(User).filter(User.username == username).one()
        db.expunge(user)
        return user


def _get_password_hash(username: str) -> str | None:
    """Return the stored password_hash for ``username``."""
    return _get_user(username).password_hash


def _create_local_user(
    client: TestClient,
    *,
    username: str,
    role: str,
    password: str = _VALID_PW,
) -> int:
    """Create a local user via the admin UI and return the new user id."""
    csrf = csrf_of(client, "/users")
    resp = client.post(
        "/users",
        data={
            "username": username,
            "password": password,
            "password_confirm": password,
            "role": role,
            "csrf_token": csrf,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"create_user failed: {resp.text[:200]}"
    return _get_user(username).id


def _create_ldap_user(username: str, role: str = "viewer") -> int:
    """Insert a directory (LDAP) user directly into the DB and return its id."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        user = User(
            username=username,
            auth_source="ldap",
            password_hash=None,
            role=role,
            enabled=True,
        )
        db.add(user)
        db.flush()
        uid = user.id
    return uid


def _post_account_password(
    client: TestClient,
    *,
    current_password: str,
    new_password: str,
    confirm_password: str,
) -> object:
    """POST /account/password and return the response (no redirect following)."""
    csrf = csrf_of(client, "/account/preferences")
    return client.post(
        "/account/password",
        data={
            "current_password": current_password,
            "new_password": new_password,
            "confirm_password": confirm_password,
            "csrf_token": csrf,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )


def _post_reset_password(
    client: TestClient,
    user_id: int,
    *,
    password: str,
    password_confirm: str,
) -> object:
    """POST /users/{user_id}/reset-password and return the response."""
    csrf = csrf_of(client, "/users")
    return client.post(
        f"/users/{user_id}/reset-password",
        data={
            "password": password,
            "password_confirm": password_confirm,
            "csrf_token": csrf,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )


def _count_live_sessions(user_id: int) -> int:
    """Return the number of non-revoked sessions for ``user_id``."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        return (
            db.query(SessionRow)
            .filter(
                SessionRow.user_id == user_id,
                SessionRow.revoked.is_(False),
            )
            .count()
        )


def _make_second_client(web_client: TestClient) -> TestClient:
    """Return a second TestClient that shares the app (and therefore DB)."""
    return TestClient(web_client.app, base_url="https://testserver")


# ---------------------------------------------------------------------------
# Feature 1: POST /account/password — self-service password change
# ---------------------------------------------------------------------------


class TestAccountPasswordHappyPath:
    """Happy-path: successful password change for a local non-admin user."""

    def test_password_changed_redirects_with_success_param(
        self, web_client: TestClient
    ) -> None:
        """A correct submission redirects to /account/preferences?password_changed=1."""
        seed_admin(web_client)
        login(web_client)
        _create_local_user(web_client, username="pw-happy", role="viewer")
        # Log out admin and log in as the viewer.
        csrf = csrf_of(web_client, "/")
        web_client.post(
            "/logout",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        login(web_client, username="pw-happy", password=_VALID_PW)

        resp = _post_account_password(
            web_client,
            current_password=_VALID_PW,
            new_password="NewValidPass99!",
            confirm_password="NewValidPass99!",
        )
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "/account/preferences" in location
        assert "password_changed=1" in location

    def test_password_hash_changes_and_new_password_verifies(
        self, web_client: TestClient
    ) -> None:
        """After a successful change the stored hash accepts the new password."""
        seed_admin(web_client)
        login(web_client)
        _create_local_user(web_client, username="pw-hashcheck", role="viewer")
        csrf = csrf_of(web_client, "/")
        web_client.post(
            "/logout",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        login(web_client, username="pw-hashcheck", password=_VALID_PW)

        old_hash = _get_password_hash("pw-hashcheck")
        _post_account_password(
            web_client,
            current_password=_VALID_PW,
            new_password="BrandNewPwd99!",
            confirm_password="BrandNewPwd99!",
        )

        new_hash = _get_password_hash("pw-hashcheck")
        ctx = get_context()
        assert new_hash != old_hash, "Hash must change after password change"
        assert verify_password("BrandNewPwd99!", new_hash, ctx.settings.auth), (
            "New password must verify against new hash"
        )
        assert not verify_password(_VALID_PW, new_hash, ctx.settings.auth), (
            "Old password must be rejected by new hash"
        )


class TestAccountPasswordSessionBehavior:
    """Session management: current session survives; others are revoked."""

    def test_current_session_remains_valid_after_change(
        self, web_client: TestClient
    ) -> None:
        """The client performing the change is not logged out."""
        seed_admin(web_client)
        login(web_client)
        _create_local_user(web_client, username="pw-stay", role="viewer")
        csrf = csrf_of(web_client, "/")
        web_client.post(
            "/logout",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        login(web_client, username="pw-stay", password=_VALID_PW)

        _post_account_password(
            web_client,
            current_password=_VALID_PW,
            new_password="StayLoggedIn99!",
            confirm_password="StayLoggedIn99!",
        )
        # The same client should still be able to GET an authenticated page.
        resp = web_client.get(
            "/account/preferences",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert resp.status_code == 200, (
            "Current session should remain valid after password change"
        )

    def test_other_sessions_revoked_after_change(self, web_client: TestClient) -> None:
        """Sessions belonging to other clients for the same user are revoked."""
        seed_admin(web_client)
        login(web_client)
        _create_local_user(web_client, username="pw-revoke-other", role="viewer")
        csrf = csrf_of(web_client, "/")
        web_client.post(
            "/logout",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        login(web_client, username="pw-revoke-other", password=_VALID_PW)

        # Establish a second session for the same user on a different client.
        other = _make_second_client(web_client)
        login(other, username="pw-revoke-other", password=_VALID_PW)
        # Confirm the second session is live before we make any change.
        assert (
            other.get(
                "/", headers={"Accept": "text/html"}, follow_redirects=False
            ).status_code
            == 200
        )

        # Change password from the first client.
        _post_account_password(
            web_client,
            current_password=_VALID_PW,
            new_password="RevokeOthers99!",
            confirm_password="RevokeOthers99!",
        )

        # The second client's session should now be dead.
        resp = other.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/login")


class TestAccountPasswordValidation:
    """Error paths: wrong current pw, mismatch, too-short, LDAP account."""

    def _setup_viewer(self, web_client: TestClient, username: str) -> None:
        """Seed admin, create a viewer, log admin out, log viewer in."""
        seed_admin(web_client)
        login(web_client)
        _create_local_user(web_client, username=username, role="viewer")
        csrf = csrf_of(web_client, "/")
        web_client.post(
            "/logout",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        login(web_client, username=username, password=_VALID_PW)

    def test_wrong_current_password_returns_error_current(
        self, web_client: TestClient
    ) -> None:
        """A wrong current password redirects with ?password_error=current."""
        self._setup_viewer(web_client, "pw-wrong-current")
        old_hash = _get_password_hash("pw-wrong-current")

        resp = _post_account_password(
            web_client,
            current_password="ThisIsNotMyPassword!",
            new_password="SomethingNew99!",
            confirm_password="SomethingNew99!",
        )
        assert resp.status_code == 303
        assert "password_error=current" in resp.headers["location"]
        assert _get_password_hash("pw-wrong-current") == old_hash

    def test_new_confirm_mismatch_returns_error_mismatch(
        self, web_client: TestClient
    ) -> None:
        """Mismatched new/confirm redirects with ?password_error=mismatch."""
        self._setup_viewer(web_client, "pw-mismatch")
        old_hash = _get_password_hash("pw-mismatch")

        # Supply correct current password so we reach the mismatch check.
        resp = _post_account_password(
            web_client,
            current_password=_VALID_PW,
            new_password="Mismatch99!One",
            confirm_password="Mismatch99!Two",
        )
        assert resp.status_code == 303
        assert "password_error=mismatch" in resp.headers["location"]
        assert _get_password_hash("pw-mismatch") == old_hash

    def test_too_short_new_password_returns_error_policy(
        self, web_client: TestClient
    ) -> None:
        """A too-short new password redirects with ?password_error=policy."""
        self._setup_viewer(web_client, "pw-policy")
        old_hash = _get_password_hash("pw-policy")

        # Supply correct current password AND matching confirm to reach the
        # policy check (both new_password and confirm_password are too short).
        resp = _post_account_password(
            web_client,
            current_password=_VALID_PW,
            new_password=_TOO_SHORT_PW,
            confirm_password=_TOO_SHORT_PW,
        )
        assert resp.status_code == 303
        assert "password_error=policy" in resp.headers["location"]
        assert _get_password_hash("pw-policy") == old_hash

    def test_ldap_user_cannot_change_password(
        self, web_client: TestClient, monkeypatch: object
    ) -> None:
        """An LDAP-backed user gets ?password_error=ldap; hash stays None."""
        seed_admin(web_client)
        uid = _create_ldap_user("ldap-pw-changer", role="viewer")

        # Mint a session manually — authenticate_user refuses LDAP accounts.
        # The freshly-minted session has last_revalidated_at=None (only the
        # login path via rotate_session seeds it), so the session is immediately
        # "due" for revalidation. Patch _revalidate on the sessions module to
        # short-circuit the directory round-trip entirely; this is deterministic
        # regardless of whether LDAP revalidation is enabled in the test env.
        import timelapse_manager.security.sessions as _sessions_mod

        monkeypatch.setattr(_sessions_mod, "_revalidate", lambda *a, **kw: True)

        ctx = get_context()
        cookie_name = ctx.settings.session.cookie_name
        with session_scope(ctx.session_factory) as db:
            ldap_user = db.get(User, uid)
            _session_row, raw_token = create_session(
                db, ldap_user, remember_me=False, settings=ctx.settings.session
            )

        # Set cookie without a domain — specifying 'testserver' as domain causes
        # the httpx cookie jar to not send the cookie on requests to testserver.
        web_client.cookies.set(cookie_name, raw_token)

        # GET the preferences page to obtain a valid CSRF token.
        resp = web_client.get("/account/preferences", follow_redirects=False)
        assert resp.status_code == 200, (
            f"LDAP session not resolved — check monkeypatch. Status: {resp.status_code}"
        )

        import re

        match = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]*)"', resp.text)
        assert match is not None
        csrf = match.group(1)

        resp = web_client.post(
            "/account/password",
            data={
                "current_password": "anything",
                "new_password": "SomethingNew99!",
                "confirm_password": "SomethingNew99!",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "password_error=ldap" in resp.headers["location"]
        # LDAP user's password_hash must remain None.
        assert _get_user("ldap-pw-changer").password_hash is None


class TestAccountPasswordCsrf:
    """Missing or invalid CSRF token is rejected before the route runs."""

    def test_missing_csrf_is_rejected(self, admin_client: TestClient) -> None:
        """A POST without a csrf_token field is refused by CSRF middleware."""
        old_hash = _get_password_hash("admin")
        resp = admin_client.post(
            "/account/password",
            data={
                "current_password": "AdminP@ssw0rd1234",
                "new_password": "NewAdminPass99!",
                "confirm_password": "NewAdminPass99!",
                # csrf_token intentionally omitted
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert _get_password_hash("admin") == old_hash

    def test_invalid_csrf_is_rejected(self, admin_client: TestClient) -> None:
        """A POST with a garbage CSRF token is refused by CSRF middleware."""
        old_hash = _get_password_hash("admin")
        resp = admin_client.post(
            "/account/password",
            data={
                "current_password": "AdminP@ssw0rd1234",
                "new_password": "NewAdminPass99!",
                "confirm_password": "NewAdminPass99!",
                "csrf_token": "not-a-real-token",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert _get_password_hash("admin") == old_hash


class TestAccountPasswordAuditEvent:
    """A successful change writes an audit Event row attributed to the actor."""

    def test_audit_event_written_on_success(self, web_client: TestClient) -> None:
        """An Event row with the actor's user id is created on password change."""
        seed_admin(web_client)
        login(web_client)
        _create_local_user(web_client, username="pw-audit", role="viewer")
        csrf = csrf_of(web_client, "/")
        web_client.post(
            "/logout",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        login(web_client, username="pw-audit", password=_VALID_PW)

        actor_id = _get_user("pw-audit").id
        _post_account_password(
            web_client,
            current_password=_VALID_PW,
            new_password="AuditedChange99!",
            confirm_password="AuditedChange99!",
        )

        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            event = (
                db.query(Event)
                .filter(
                    Event.actor_user_id == actor_id,
                    Event.message.contains("password changed"),
                )
                .first()
            )
        assert event is not None, "Expected an audit event for the password change"
        assert event.actor_user_id == actor_id


class TestAccountPasswordAllRoles:
    """Every role can change their own password (endpoint is not admin-gated)."""

    def test_viewer_can_change_own_password(self, viewer_client: TestClient) -> None:
        """A viewer-role user reaches the change-password endpoint successfully."""
        resp = _post_account_password(
            viewer_client,
            current_password="ViewerPass12345!",
            new_password="ViewerNewPass99!",
            confirm_password="ViewerNewPass99!",
        )
        assert resp.status_code == 303
        assert "password_changed=1" in resp.headers["location"]

    def test_operator_can_change_own_password(
        self, operator_client: TestClient
    ) -> None:
        """An operator-role user reaches the change-password endpoint successfully."""
        resp = _post_account_password(
            operator_client,
            current_password="OperatorPass12345!",
            new_password="OperatorNewPass99!",
            confirm_password="OperatorNewPass99!",
        )
        assert resp.status_code == 303
        assert "password_changed=1" in resp.headers["location"]

    def test_admin_can_change_own_password(self, admin_client: TestClient) -> None:
        """An admin-role user can also change their own password."""
        resp = _post_account_password(
            admin_client,
            current_password="AdminP@ssw0rd1234",
            new_password="AdminNewPass99!",
            confirm_password="AdminNewPass99!",
        )
        assert resp.status_code == 303
        assert "password_changed=1" in resp.headers["location"]


# ---------------------------------------------------------------------------
# Feature 2: POST /users/{user_id}/reset-password — admin-only forced reset
# ---------------------------------------------------------------------------


class TestResetUserPasswordHappyPath:
    """Admin successfully sets a new password for a local user."""

    def test_reset_changes_hash_and_verifies_new_password(
        self, admin_client: TestClient
    ) -> None:
        """Hash is updated and the new password verifies after admin reset."""
        uid = _create_local_user(admin_client, username="reset-target", role="viewer")
        old_hash = _get_password_hash("reset-target")

        resp = _post_reset_password(
            admin_client,
            uid,
            password="AdminResetPwd99!",
            password_confirm="AdminResetPwd99!",
        )
        assert resp.status_code == 200

        new_hash = _get_password_hash("reset-target")
        ctx = get_context()
        assert new_hash != old_hash, "Hash must change after admin reset"
        assert verify_password("AdminResetPwd99!", new_hash, ctx.settings.auth)
        assert not verify_password(_VALID_PW, new_hash, ctx.settings.auth)

    def test_reset_revokes_target_sessions(self, web_client: TestClient) -> None:
        """All target-user sessions are revoked after an admin password reset."""
        seed_admin(web_client)
        login(web_client)
        uid = _create_local_user(web_client, username="reset-sessions", role="viewer")
        # Create a second client to establish a session for the target user.
        victim = _make_second_client(web_client)
        login(victim, username="reset-sessions", password=_VALID_PW)
        assert (
            victim.get(
                "/", headers={"Accept": "text/html"}, follow_redirects=False
            ).status_code
            == 200
        )

        _post_reset_password(
            web_client,
            uid,
            password="ResetRevoke99!",
            password_confirm="ResetRevoke99!",
        )

        # The victim's session should now redirect to login.
        resp = victim.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/login")

    def test_response_contains_user_row_fragment(
        self, admin_client: TestClient
    ) -> None:
        """A successful reset returns a 200 HTMX user-row fragment."""
        uid = _create_local_user(admin_client, username="reset-fragment", role="viewer")
        resp = _post_reset_password(
            admin_client,
            uid,
            password="FragmentPwd99!",
            password_confirm="FragmentPwd99!",
        )
        assert resp.status_code == 200
        assert f'id="user-row-{uid}"' in resp.text


class TestResetUserPasswordValidation:
    """Error paths: mismatch, too-short, LDAP account."""

    def test_password_mismatch_returns_200_fragment_with_error(
        self, admin_client: TestClient
    ) -> None:
        """A password/confirm mismatch returns a 200 fragment with error text."""
        uid = _create_local_user(admin_client, username="reset-mismatch", role="viewer")
        old_hash = _get_password_hash("reset-mismatch")

        resp = _post_reset_password(
            admin_client,
            uid,
            password="MismatchOne99!",
            password_confirm="MismatchTwo99!",
        )
        assert resp.status_code == 200
        assert "do not match" in resp.text.lower() or "mismatch" in resp.text.lower()
        assert _get_password_hash("reset-mismatch") == old_hash

    def test_too_short_password_returns_200_fragment_with_error(
        self, admin_client: TestClient
    ) -> None:
        """A too-short password returns a 200 fragment with a policy error."""
        uid = _create_local_user(admin_client, username="reset-short", role="viewer")
        old_hash = _get_password_hash("reset-short")

        resp = _post_reset_password(
            admin_client,
            uid,
            password=_TOO_SHORT_PW,
            password_confirm=_TOO_SHORT_PW,
        )
        assert resp.status_code == 200
        # The error message quotes the minimum length (12 characters).
        assert "12" in resp.text or "least" in resp.text
        assert _get_password_hash("reset-short") == old_hash

    def test_ldap_target_returns_200_fragment_refusal(
        self, admin_client: TestClient
    ) -> None:
        """Attempting to reset an LDAP account returns a 200 fragment refusal.

        No password is set and the target's sessions are not revoked.
        """
        uid = _create_ldap_user("ldap-reset-target", role="viewer")
        # Create a session for the LDAP user so we can verify it is NOT revoked.
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            ldap_user = db.get(User, uid)
            _session_row, _raw = create_session(
                db, ldap_user, remember_me=False, settings=ctx.settings.session
            )

        live_before = _count_live_sessions(uid)
        assert live_before == 1

        resp = _post_reset_password(
            admin_client,
            uid,
            password="SomePassword99!",
            password_confirm="SomePassword99!",
        )
        assert resp.status_code == 200
        # Must contain a refusal message about directory/LDAP.
        assert "directory" in resp.text.lower() or "ldap" in resp.text.lower()
        # Hash still None — no password was set.
        assert _get_user("ldap-reset-target").password_hash is None
        # Sessions were NOT revoked (the route returns before revoke_all_user_sessions
        # when the target is an LDAP account).
        live_after = _count_live_sessions(uid)
        assert live_after == live_before


class TestResetUserPasswordAuthorization:
    """Only admins may call the reset-password endpoint."""

    def test_viewer_gets_403(self, viewer_client: TestClient) -> None:
        """A viewer is denied the reset-password route with a 403."""
        csrf = csrf_of(viewer_client, "/")
        resp = viewer_client.post(
            "/users/1/reset-password",
            data={
                "password": "Victim99!Pass",
                "password_confirm": "Victim99!Pass",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_operator_gets_403(self, operator_client: TestClient) -> None:
        """An operator is denied the reset-password route with a 403."""
        csrf = csrf_of(operator_client, "/")
        resp = operator_client.post(
            "/users/1/reset-password",
            data={
                "password": "Victim99!Pass",
                "password_confirm": "Victim99!Pass",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403


class TestResetUserPasswordAuditEvent:
    """A successful admin reset writes an audit Event row with the admin's id."""

    def test_audit_event_actor_is_admin(self, admin_client: TestClient) -> None:
        """The audit Event names the acting admin, not the target user."""
        admin_id = _get_user("admin").id
        uid = _create_local_user(
            admin_client, username="reset-audit-target", role="viewer"
        )

        _post_reset_password(
            admin_client,
            uid,
            password="AuditPwd99!Reset",
            password_confirm="AuditPwd99!Reset",
        )

        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            event = (
                db.query(Event)
                .filter(
                    Event.actor_user_id == admin_id,
                    Event.message.contains("reset-audit-target"),
                )
                .first()
            )
        assert event is not None, "Expected audit event for admin password reset"
        assert event.actor_user_id == admin_id


class TestResetUserPasswordEmptyPasswordPath:
    """Empty-password POST revokes sessions without changing the hash.

    The router's ``if password:`` guard skips the hash update when the field is
    absent or blank, but still calls ``revoke_all_user_sessions`` and writes an
    audit event. Verify that contract is intact.
    """

    def test_empty_password_revokes_sessions_without_changing_hash(
        self, web_client: TestClient
    ) -> None:
        """POST with no password field revokes sessions; hash is unchanged."""
        seed_admin(web_client)
        login(web_client)
        uid = _create_local_user(web_client, username="reset-empty-pw", role="viewer")
        old_hash = _get_password_hash("reset-empty-pw")

        # Establish a live session for the target.
        victim = _make_second_client(web_client)
        login(victim, username="reset-empty-pw", password=_VALID_PW)
        assert (
            victim.get(
                "/", headers={"Accept": "text/html"}, follow_redirects=False
            ).status_code
            == 200
        )

        # POST without a password field — submit only csrf and an empty string.
        csrf = csrf_of(web_client, "/users")
        resp = web_client.post(
            f"/users/{uid}/reset-password",
            data={"password": "", "password_confirm": "", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        # Hash unchanged.
        assert _get_password_hash("reset-empty-pw") == old_hash
        # Sessions revoked.
        revoked_resp = victim.get(
            "/", headers={"Accept": "text/html"}, follow_redirects=False
        )
        assert revoked_resp.status_code == 303
        assert revoked_resp.headers["location"].startswith("/login")
