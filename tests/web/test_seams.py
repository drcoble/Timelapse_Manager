"""I/J-suite: Role acceptance at the user-create seam, plus inert LDAP source."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import csrf_of, login, seed_admin


class TestUserCreateRoleAcceptance:
    def test_create_user_with_operator_role_is_accepted(
        self, admin_client: TestClient
    ) -> None:
        """POST /users with role='operator' is accepted at the route layer."""
        csrf = csrf_of(admin_client, "/users")
        resp = admin_client.post(
            "/users",
            data={
                "username": "operator-user",
                "password": "OperatorPass12345!",
                "password_confirm": "OperatorPass12345!",
                "role": "operator",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303, (
            "Operator role must be accepted at the route layer"
        )

    def test_create_user_with_admin_role_is_accepted(
        self, admin_client: TestClient
    ) -> None:
        csrf = csrf_of(admin_client, "/users")
        resp = admin_client.post(
            "/users",
            data={
                "username": "another-admin",
                "password": "AnotherAdminPass99!",
                "password_confirm": "AnotherAdminPass99!",
                "role": "admin",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_create_user_with_viewer_role_is_accepted(
        self, admin_client: TestClient
    ) -> None:
        csrf = csrf_of(admin_client, "/users")
        resp = admin_client.post(
            "/users",
            data={
                "username": "viewer-seam-test",
                "password": "ViewerSeamPass99!",
                "password_confirm": "ViewerSeamPass99!",
                "role": "viewer",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303


class TestLdapAuthSourceInert:
    def test_local_login_succeeds_for_local_account(
        self, web_client: TestClient
    ) -> None:
        seed_admin(web_client)
        resp = web_client.post(
            "/login",
            data={"username": "admin", "password": "AdminP@ssw0rd1234"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_ldap_account_cannot_authenticate_via_local_login(
        self, web_client: TestClient
    ) -> None:
        """An account with auth_source='ldap' is rejected by authenticate_user.

        The local login path explicitly filters on auth_source='local'.
        """
        from timelapse_manager.db.models import User
        from timelapse_manager.db.session import session_scope
        from timelapse_manager.runtime import get_context

        seed_admin(web_client)
        login(web_client)
        # Seed an ldap account directly.
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            ldap_user = User(
                username="ldap-user-test",
                auth_source="ldap",
                password_hash=None,
                role="viewer",
                enabled=True,
            )
            db.add(ldap_user)
        # Log out admin.
        from tests.conftest import csrf_of as _csrf_of

        csrf = _csrf_of(web_client, "/")
        web_client.post(
            "/logout",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        # Try to log in as the ldap user — must fail.
        resp = web_client.post(
            "/login",
            data={"username": "ldap-user-test", "password": "anything-123!"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 401, (
            "LDAP-sourced account must not authenticate via the local login path"
        )
