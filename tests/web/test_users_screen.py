"""Web tests for the Users admin screen affordances.

Covers the consolidated row-actions popover (Edit role / Reset password /
Revoke sessions / Disable-Enable / Delete) and the screen's iconography, plus
role-gating of the admin-only page. One role-client per test (separate clients
collide on the shared DB session).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import csrf_of


def _create_local_user(
    admin_client: TestClient,
    username: str = "rowtarget",
    role: str = "viewer",
) -> None:
    """Create a second (non-self) local user via the admin UI."""
    csrf = csrf_of(admin_client, "/users")
    resp = admin_client.post(
        "/users",
        data={
            "username": username,
            "password": "RowTargetPass12345!",
            "role": role,
            "csrf_token": csrf,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert resp.status_code in (200, 303), resp.text[:200]


def test_users_page_uses_icon_sprite(admin_client: TestClient) -> None:
    """The Add-User affordance and empty-state pull from the SVG sprite."""
    html = admin_client.get("/users").text
    assert 'href="#icon-users"' in html


def test_other_user_row_renders_actions_popover(admin_client: TestClient) -> None:
    """A non-self row consolidates its actions into the Ph-3 popover menu."""
    _create_local_user(admin_client)
    html = admin_client.get("/users").text
    assert "row-actions-menu" in html
    assert 'class="row-actions-popover" role="menu"' in html
    # Each action is a menuitem within the popover.
    assert "Edit role" in html
    # local user → the inline set-password form is offered in the popover
    assert "/reset-password" in html
    assert "Set password" in html
    assert "Revoke sessions" in html
    assert "Disable" in html
    assert "Delete" in html


def test_self_row_has_no_actions_popover(admin_client: TestClient) -> None:
    """The logged-in admin's own row shows '(you)', never the actions menu."""
    # Only the seeded admin exists → its row is the self row.
    html = admin_client.get("/users").text
    assert "(you)" in html
    assert "row-actions-menu" not in html


def test_users_page_is_admin_only(viewer_client: TestClient) -> None:
    """A viewer cannot reach the admin Users screen."""
    resp = viewer_client.get("/users", follow_redirects=False)
    assert resp.status_code in (302, 303, 403)
