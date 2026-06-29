"""Tests for the System About page.

Covers: the page renders for any authenticated user (not admin-gated), shows the
app version / build info / license name, the version is single-sourced from
``get_app_version()`` (no divergent hardcode in the template), and an
unauthenticated browser is redirected to login like every other authed page.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi.testclient import TestClient

from timelapse_manager.version import get_app_version

# Browser-style Accept so the auth layer issues the login *redirect* (a machine
# client gets a bare 401 instead).
_NAV = {"Accept": "text/html,application/xhtml+xml"}


class TestAboutPageRenders:
    def test_renders_200_for_admin(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/about", follow_redirects=False)
        assert resp.status_code == 200

    def test_renders_200_for_non_admin_viewer(self, viewer_client: TestClient) -> None:
        """The About page is not admin-gated: a viewer can reach it."""
        resp = viewer_client.get("/about", follow_redirects=False)
        assert resp.status_code == 200

    def test_shows_app_version(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/about")
        assert get_app_version() in resp.text

    def test_shows_build_info(self, admin_client: TestClient) -> None:
        """In a dev checkout the build info is the 'unknown' fallback."""
        resp = admin_client.get("/about")
        # The route calls get_build_info(); with no generated module both fields
        # render as "unknown".
        assert "unknown" in resp.text

    def test_shows_license_name(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/about")
        assert "Apache-2.0" in resp.text

    def test_shows_license_text_block(self, admin_client: TestClient) -> None:
        # The full Apache 2.0 text is rendered (a stable phrase from the body).
        resp = admin_client.get("/about")
        assert "Apache License" in resp.text
        assert "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in (
            resp.text
        )


class TestAboutVersionSingleSourced:
    def test_displayed_version_equals_get_app_version(
        self, admin_client: TestClient
    ) -> None:
        """The version on the page is exactly get_app_version() -- no hardcode."""
        resp = admin_client.get("/about")
        assert get_app_version() in resp.text


class TestAboutPageAuth:
    def test_anon_about_redirects_to_login(self, anon_client: TestClient) -> None:
        resp = anon_client.get("/about", headers=_NAV, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/login?next={quote('/about', safe='')}"
