"""Web add/edit user flows: form fragments and the role-edit apply path.

These exercise the admin-gated, CSRF-protected user form routes end to end
through the running app. The edit path enforces the admin-lockout guard: the
last remaining real administrator cannot be demoted (which also covers an admin
demoting themselves into lockout).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.models import User
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context


def _create_user(
    client: TestClient, *, username: str, role: str, password: str = "UserPass12345!"
) -> int:
    """Create a user via the admin UI and return its id."""
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
    assert resp.status_code == 303, resp.text[:200]
    return _user_id(username)


def _user_id(username: str) -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        row = db.query(User).filter(User.username == username).one()
        return row.id


def _user_role(username: str) -> str:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        return db.query(User).filter(User.username == username).one().role


def _user_enabled(username: str) -> bool:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        return db.query(User).filter(User.username == username).one().enabled


def _user_exists(username: str) -> bool:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        return (
            db.query(User).filter(User.username == username).one_or_none() is not None
        )


class TestUserDisableEnable:
    def test_disable_then_enable_toggles_flag(self, admin_client: TestClient) -> None:
        uid = _create_user(admin_client, username="toggle-me", role="viewer")
        csrf = csrf_of(admin_client, "/users")

        resp = admin_client.post(
            f"/users/{uid}/disable",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert _user_enabled("toggle-me") is False

        resp = admin_client.post(
            f"/users/{uid}/enable",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert _user_enabled("toggle-me") is True

    def test_disable_revokes_target_sessions(self, web_client: TestClient) -> None:
        from tests.conftest import login, seed_admin

        seed_admin(web_client)
        login(web_client)
        uid = _create_user(web_client, username="victim", role="viewer")
        # Sign the victim in on a separate client sharing the same app/db.
        victim = TestClient(web_client.app, base_url="https://testserver")
        login(victim, username="victim", password="UserPass12345!")
        assert victim.get("/", follow_redirects=False).status_code == 200

        csrf = csrf_of(web_client, "/users")
        web_client.post(
            f"/users/{uid}/disable",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        # The victim's live session is killed by the disable.
        follow = victim.get(
            "/", headers={"Accept": "text/html"}, follow_redirects=False
        )
        assert follow.status_code == 303
        assert follow.headers["location"].startswith("/login")

    def test_viewer_cannot_disable(self, viewer_client: TestClient) -> None:
        # A viewer is denied the disable route server-side (admin-only).
        csrf = csrf_of(viewer_client, "/")
        resp = viewer_client.post(
            "/users/1/disable",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_disable_missing_csrf_is_forbidden(self, admin_client: TestClient) -> None:
        uid = _create_user(admin_client, username="csrf-disable", role="viewer")
        resp = admin_client.post(
            f"/users/{uid}/disable",
            data={},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert _user_enabled("csrf-disable") is True


class TestUserDelete:
    def test_delete_removes_account(self, admin_client: TestClient) -> None:
        uid = _create_user(admin_client, username="goner", role="viewer")
        csrf = csrf_of(admin_client, "/users")
        resp = admin_client.post(
            f"/users/{uid}/delete",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/users"
        assert _user_exists("goner") is False

    def test_viewer_cannot_delete(self, viewer_client: TestClient) -> None:
        csrf = csrf_of(viewer_client, "/")
        resp = viewer_client.post(
            "/users/1/delete",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403


class TestLastAdminGuard:
    def test_cannot_disable_last_admin(self, admin_client: TestClient) -> None:
        # The seeded admin is the only enabled real admin; disabling it is refused.
        admin_id = _user_id("admin")
        csrf = csrf_of(admin_client, "/users")
        resp = admin_client.post(
            f"/users/{admin_id}/disable",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        # The row is returned (200) but the account stays enabled.
        assert resp.status_code == 200
        assert "last remaining administrator" in resp.text
        assert _user_enabled("admin") is True

    def test_cannot_delete_last_admin(self, admin_client: TestClient) -> None:
        admin_id = _user_id("admin")
        csrf = csrf_of(admin_client, "/users")
        resp = admin_client.post(
            f"/users/{admin_id}/delete",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "last remaining administrator" in resp.text
        assert _user_exists("admin") is True

    def test_can_delete_admin_when_another_enabled_admin_exists(
        self, admin_client: TestClient
    ) -> None:
        # Promote a second admin, then the first becomes deletable.
        second = _create_user(admin_client, username="admin2", role="admin")
        csrf = csrf_of(admin_client, "/users")
        resp = admin_client.post(
            f"/users/{second}/delete",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _user_exists("admin2") is False


class TestUserAddForm:
    def test_add_form_returns_200(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/users/add-form")
        assert resp.status_code == 200
        assert 'action="/users"' in resp.text
        assert 'name="csrf_token"' in resp.text

    def test_add_form_offers_admin_operator_and_viewer(
        self, admin_client: TestClient
    ) -> None:
        resp = admin_client.get("/users/add-form")
        assert resp.status_code == 200
        assert 'value="viewer"' in resp.text
        assert 'value="operator"' in resp.text
        assert 'value="admin"' in resp.text

    def test_add_form_forbidden_for_viewer(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get("/users/add-form", follow_redirects=False)
        assert resp.status_code == 403

    def test_create_operator_account(self, admin_client: TestClient) -> None:
        _create_user(admin_client, username="new-operator", role="operator")
        assert _user_role("new-operator") == "operator"


class TestUserEditForm:
    def test_edit_form_prefills_role(self, admin_client: TestClient) -> None:
        user_id = _create_user(admin_client, username="viewer-to-edit", role="viewer")
        resp = admin_client.get(f"/users/{user_id}/edit-form")
        assert resp.status_code == 200
        # The fragment IS the row (same id), so the apply's hx-target survives
        # the edit-form swap rather than being orphaned by a card-shaped reply.
        assert f'id="user-row-{user_id}"' in resp.text
        assert "viewer-to-edit" in resp.text
        # The current role (viewer) is the selected option.
        assert "selected" in resp.text
        # The selected attribute is on the viewer option, not the admin option.
        viewer_idx = resp.text.index('value="viewer"')
        operator_idx = resp.text.index('value="operator"')
        # The selected attribute is on the viewer option, which precedes the
        # operator and admin options in the select.
        assert "selected" in resp.text[viewer_idx:operator_idx]
        assert 'value="operator"' in resp.text

    def test_edit_form_username_readonly(self, admin_client: TestClient) -> None:
        user_id = _create_user(admin_client, username="ro-user", role="viewer")
        resp = admin_client.get(f"/users/{user_id}/edit-form")
        assert resp.status_code == 200
        assert "readonly" in resp.text

    def test_edit_form_unknown_user_is_404(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/users/999999/edit-form")
        assert resp.status_code == 404

    def test_edit_form_forbidden_for_viewer(self, viewer_client: TestClient) -> None:
        # viewer_client itself is the "viewer" account; use its own id.
        user_id = _user_id("viewer")
        resp = viewer_client.get(f"/users/{user_id}/edit-form", follow_redirects=False)
        assert resp.status_code == 403


class TestUserEditApply:
    def test_viewer_promoted_to_admin(self, admin_client: TestClient) -> None:
        user_id = _create_user(admin_client, username="promote-me", role="viewer")
        csrf = csrf_of(admin_client, "/users")
        resp = admin_client.post(
            f"/users/{user_id}/edit",
            data={"role": "admin", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert f'id="user-row-{user_id}"' in resp.text
        assert _user_role("promote-me") == "admin"

    def test_admin_demoted_to_viewer_when_others_remain(
        self, admin_client: TestClient
    ) -> None:
        # Two real admins exist (the seeded "admin" plus this one); demoting one
        # is allowed because another real admin remains.
        user_id = _create_user(admin_client, username="spare-admin", role="admin")
        csrf = csrf_of(admin_client, "/users")
        resp = admin_client.post(
            f"/users/{user_id}/edit",
            data={"role": "viewer", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert _user_role("spare-admin") == "viewer"

    def test_last_admin_cannot_be_demoted(self, admin_client: TestClient) -> None:
        # The seeded "admin" is the only real admin; demoting it must be refused.
        user_id = _user_id("admin")
        csrf = csrf_of(admin_client, "/users")
        resp = admin_client.post(
            f"/users/{user_id}/edit",
            data={"role": "viewer", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        # Inline error at 200; the account stays an admin.
        assert resp.status_code == 200
        assert "last remaining administrator" in resp.text.lower()
        assert _user_role("admin") == "admin"

    def test_self_demotion_into_lockout_blocked(self, admin_client: TestClient) -> None:
        # The acting admin is the sole real admin and tries to demote itself --
        # the same last-admin guard refuses it.
        user_id = _user_id("admin")
        csrf = csrf_of(admin_client, "/users")
        resp = admin_client.post(
            f"/users/{user_id}/edit",
            data={"role": "viewer", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert _user_role("admin") == "admin"

    def test_invalid_role_rejected(self, admin_client: TestClient) -> None:
        user_id = _create_user(admin_client, username="role-victim", role="viewer")
        csrf = csrf_of(admin_client, "/users")
        resp = admin_client.post(
            f"/users/{user_id}/edit",
            data={"role": "superuser", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert "invalid role" in resp.text.lower()
        assert _user_role("role-victim") == "viewer"

    def test_viewer_promoted_to_operator(self, admin_client: TestClient) -> None:
        user_id = _create_user(admin_client, username="to-operator", role="viewer")
        csrf = csrf_of(admin_client, "/users")
        resp = admin_client.post(
            f"/users/{user_id}/edit",
            data={"role": "operator", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert _user_role("to-operator") == "operator"

    def test_last_admin_cannot_be_demoted_to_operator(
        self, admin_client: TestClient
    ) -> None:
        # Demoting the sole real admin to operator must be refused too: an
        # operator is not a real admin, so this would leave zero administrators.
        user_id = _user_id("admin")
        csrf = csrf_of(admin_client, "/users")
        resp = admin_client.post(
            f"/users/{user_id}/edit",
            data={"role": "operator", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert "last remaining administrator" in resp.text.lower()
        assert _user_role("admin") == "admin"

    def test_viewer_cannot_edit(self, viewer_client: TestClient) -> None:
        target_id = _user_id("admin")
        csrf = csrf_of(viewer_client, "/")
        resp = viewer_client.post(
            f"/users/{target_id}/edit",
            data={"role": "viewer", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_missing_csrf_is_forbidden(self, admin_client: TestClient) -> None:
        user_id = _create_user(admin_client, username="csrf-user", role="viewer")
        resp = admin_client.post(
            f"/users/{user_id}/edit",
            data={"role": "admin"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert _user_role("csrf-user") == "viewer"
