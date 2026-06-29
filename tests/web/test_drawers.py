"""Tests for the right-side drawer system: fragment routes, access control,
sole-camera auto-select, post-camera nudge, and opener markup.

Route map:
  GET /drawers/new-project  — OperatorUser (admin + operator); dual-serve
  GET /drawers/new-user     — AdminUser only; dual-serve

Dual-serve: when HX-Request header is present, return a bare HTML fragment
(no <html>); otherwise return the full page wrapping the same form.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from timelapse_manager.db.models import Camera
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context

# ---------------------------------------------------------------------------
# Helper: seed a camera directly into the test DB (same pattern as
# test_project_create.py — works after the TestClient lifespan has run).
# ---------------------------------------------------------------------------


def _seed_camera(*, name: str, protocol: str | None) -> int:
    """Insert a Camera and return its id."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(name=name, address="10.0.0.1", protocol=protocol)
        db.add(cam)
        db.flush()
        return cam.id


# ===========================================================================
# /drawers/new-project — dual-serve (OperatorUser)
# ===========================================================================


class TestNewProjectDrawerFragment:
    """GET /drawers/new-project with HX-Request returns a bare fragment."""

    def test_new_project_drawer_returns_fragment_for_htmx(
        self, admin_client: TestClient
    ) -> None:
        # Seed one camera so the form body renders (it guards on cameras list).
        _seed_camera(name="cam-a", protocol="vapix")

        resp = admin_client.get(
            "/drawers/new-project",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 200
        assert 'data-drawer-title="New Project"' in resp.text
        assert 'name="capture_interval_value"' in resp.text
        assert "<html" not in resp.text

    def test_new_project_drawer_returns_full_page_without_htmx_header(
        self, admin_client: TestClient
    ) -> None:
        _seed_camera(name="cam-b", protocol="vapix")

        resp = admin_client.get("/drawers/new-project")

        assert resp.status_code == 200
        assert "<html" in resp.text
        # Full page still contains the form body.
        assert 'name="capture_interval_value"' in resp.text


# ===========================================================================
# /drawers/new-user — dual-serve (AdminUser)
# ===========================================================================


class TestNewUserDrawerFragment:
    """GET /drawers/new-user: fragment for HTMX, full page otherwise."""

    def test_new_user_drawer_returns_fragment_for_htmx(
        self, admin_client: TestClient
    ) -> None:
        resp = admin_client.get(
            "/drawers/new-user",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 200
        assert 'data-drawer-title="New User"' in resp.text
        assert "<html" not in resp.text

    def test_new_user_drawer_returns_full_page_without_htmx_header(
        self, admin_client: TestClient
    ) -> None:
        resp = admin_client.get("/drawers/new-user")

        assert resp.status_code == 200
        assert "<html" in resp.text


# ===========================================================================
# /drawers/new-user — access control (AdminUser dependency)
# ===========================================================================


class TestNewUserDrawerAccessControl:
    """Non-admin roles are forbidden from the new-user drawer route.

    ``require_role`` raises HTTP 403 for authenticated-but-wrong-role and
    HTTP 401 for unauthenticated requests.
    """

    def test_new_user_drawer_denies_viewer(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get(
            "/drawers/new-user",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 403

    def test_new_user_drawer_denies_operator(self, operator_client: TestClient) -> None:
        resp = operator_client.get(
            "/drawers/new-user",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 403


# ===========================================================================
# Sole-camera auto-select in the new-project form
# ===========================================================================


class TestSoleCameraAutoSelect:
    """When exactly one camera has a configured protocol, that option is
    pre-selected and the placeholder option is not.  With two or more protocol-
    configured cameras the placeholder remains selected.
    """

    def test_sole_protocol_camera_is_preselected(
        self, admin_client: TestClient
    ) -> None:
        cam_id = _seed_camera(name="sole-cam", protocol="vapix")

        resp = admin_client.get(
            "/drawers/new-project",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 200
        # The camera option must carry the 'selected' attribute.
        assert f'value="{cam_id}" selected' in resp.text
        # The placeholder option must NOT be selected.
        assert 'value="" disabled selected' not in resp.text

    def test_placeholder_stays_selected_with_two_protocol_cameras(
        self, admin_client: TestClient
    ) -> None:
        cam_id_1 = _seed_camera(name="cam-one", protocol="vapix")
        cam_id_2 = _seed_camera(name="cam-two", protocol="onvif")

        resp = admin_client.get(
            "/drawers/new-project",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 200
        # Placeholder selected when more than one camera exists.
        assert 'value="" disabled selected' in resp.text
        # Neither individual camera is pre-selected.
        assert f'value="{cam_id_1}" selected' not in resp.text
        assert f'value="{cam_id_2}" selected' not in resp.text

    def test_unconfigured_camera_does_not_trigger_auto_select(
        self, admin_client: TestClient
    ) -> None:
        """A camera with protocol=None should not count toward sole-camera logic."""
        _seed_camera(name="unconfigured", protocol=None)

        resp = admin_client.get(
            "/drawers/new-project",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 200
        # No camera has a protocol, so no auto-select; placeholder stays.
        assert 'value="" disabled selected' in resp.text


# ===========================================================================
# Post-camera-add nudge banner on /cameras
# ===========================================================================


class TestCameraCreatedNudge:
    """GET /cameras?created=1 shows a 'Create a project' nudge for operators;
    the same page without the query param does not.
    """

    def test_nudge_shown_with_created_param_for_operator(
        self, operator_client: TestClient
    ) -> None:
        resp = operator_client.get("/cameras?created=1")

        assert resp.status_code == 200
        assert "Create a project" in resp.text

    def test_nudge_not_shown_without_created_param(
        self, operator_client: TestClient
    ) -> None:
        resp = operator_client.get("/cameras")

        assert resp.status_code == 200
        assert "Create a project" not in resp.text

    def test_nudge_not_shown_to_viewer_even_with_created_param(
        self, viewer_client: TestClient
    ) -> None:
        """Viewers lack can_operate so the nudge must not appear for them."""
        resp = viewer_client.get("/cameras?created=1")

        assert resp.status_code == 200
        assert "Create a project" not in resp.text


# ===========================================================================
# Opener markup — drawer trigger attributes on page templates
# ===========================================================================


class TestDrawerOpenerMarkup:
    """Key pages must contain the HTMX/drawer opener attributes that JS relies on
    to intercept navigation and open the drawer instead.
    """

    def test_dashboard_contains_new_project_drawer_opener(
        self, admin_client: TestClient
    ) -> None:
        resp = admin_client.get("/")
        assert resp.status_code == 200
        assert 'hx-get="/drawers/new-project"' in resp.text

    def test_projects_page_contains_new_project_drawer_opener(
        self, admin_client: TestClient
    ) -> None:
        resp = admin_client.get("/projects")
        assert resp.status_code == 200
        assert 'hx-get="/drawers/new-project"' in resp.text

    def test_users_page_contains_new_user_drawer_opener(
        self, admin_client: TestClient
    ) -> None:
        resp = admin_client.get("/users")
        assert resp.status_code == 200
        assert 'hx-get="/drawers/new-user"' in resp.text
