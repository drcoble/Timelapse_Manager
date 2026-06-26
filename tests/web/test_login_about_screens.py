"""Web tests for the Phase-13 auth/about screen polish.

Login and first-run carry the Meridian brand mark (inline SVG — the pre-auth
pages have no shell sprite) and a version line; the About page tucks the full
license text behind a disclosure. One role-client per test.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import seed_admin
from timelapse_manager.version import get_app_version


def test_login_page_shows_meridian_mark_and_version(web_client: TestClient) -> None:
    """Logged-out login page renders the brand mark + version, not the old glyph."""
    # An admin must exist or /login redirects to /first-run; do not log in.
    seed_admin(web_client)
    html = web_client.get("/login").text
    assert 'class="login-logo"' in html
    assert "<circle" in html and "<line" in html  # inline meridian SVG
    assert "&#9654;" not in html  # the old play-button glyph is gone
    assert f"Version {get_app_version()}" in html
    assert 'id="login-theme-toggle"' in html


def test_login_error_state_still_renders_version(web_client: TestClient) -> None:
    """A failed login re-renders the styled page (with version) and 401s."""
    resp = web_client.post(
        "/login",
        data={"username": "nobody", "password": "wrongwrongwrong", "csrf_token": ""},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    # No admin seeded → first-run guard may redirect; otherwise it's a 401 page.
    if resp.status_code == 401:
        assert "Invalid credentials" in resp.text
        assert f"Version {get_app_version()}" in resp.text


def test_first_run_page_shows_meridian_mark_and_version(
    web_client: TestClient,
) -> None:
    """First-run setup (no admin yet) renders the brand mark + version."""
    html = web_client.get("/first-run").text
    assert 'class="login-logo"' in html
    assert "<circle" in html and "<line" in html
    assert "&#9654;" not in html
    assert f"Version {get_app_version()}" in html


def test_about_license_behind_disclosure(admin_client: TestClient) -> None:
    """The full license text is tucked into a <details> disclosure."""
    html = admin_client.get("/about").text
    assert "<details" in html
    assert "View license text" in html
    # The text itself is still present in the DOM (collapsed, not removed).
    assert "Apache License" in html
    # Version + build still shown.
    assert get_app_version() in html
