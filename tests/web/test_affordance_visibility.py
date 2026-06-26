"""E-suite: role-based visibility of mutation affordances in rendered HTML.

These complement the route-level authorization tests in
``test_authorization.py``. Those prove the *server* admits or denies a role;
these prove the *templates* show or hide the matching buttons/links so the UI
an operator sees lines up with what the routes let them do. The class of bug
guarded against here is a mutation route opened to the operator role while the
template still hides its button behind an admin-only check (or vice versa).

Affordances are asserted on stable anchors -- the ``hx-get``/``href``/``action``
URL or the button text -- not on incidental markup, so cosmetic template edits
do not make these brittle.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context


def _seed_project_with_frame() -> tuple[int, int]:
    """Insert a camera, an active project, and one active frame.

    Returns the project and frame ids. With no live supervisor state the view
    derives a ``stopped`` operational status, so the status card renders its
    Start affordance; the frame lets the frame browser render its per-tile
    mutation controls. ``operational_status`` is the stored enum (``idle``),
    distinct from the derived display word.
    """
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(name="vis-cam", address="127.0.0.1", protocol="vapix")
        db.add(cam)
        db.flush()
        proj = Project(
            camera_id=cam.id,
            name="vis-project",
            capture_interval_seconds=60,
            lifecycle_state="active",
            operational_status="idle",
            storage_path="/tmp/vis-project",
        )
        db.add(proj)
        db.flush()
        project_id = proj.id
        frame = Frame(
            project_id=project_id,
            sequence_index=1,
            lifecycle_state="active",
            file_path="0001.jpg",
        )
        db.add(frame)
        db.flush()
        frame_id = frame.id
    return project_id, frame_id


# ---------------------------------------------------------------------------
# Operational affordances: visible to operator, hidden from viewer.
# ---------------------------------------------------------------------------


class TestOperatorSeesOperationalAffordances:
    """An operator sees the same mutation controls an admin would."""

    def test_operator_sees_add_camera_button(self, operator_client: TestClient) -> None:
        resp = operator_client.get("/cameras")
        assert resp.status_code == 200
        assert 'hx-get="/cameras/add-form"' in resp.text

    def test_operator_sees_new_project_button_on_projects(
        self, operator_client: TestClient
    ) -> None:
        resp = operator_client.get("/projects")
        assert resp.status_code == 200
        assert 'href="/projects/new"' in resp.text

    def test_operator_sees_new_project_button_on_dashboard(
        self, operator_client: TestClient
    ) -> None:
        resp = operator_client.get("/")
        assert resp.status_code == 200
        assert 'href="/projects/new"' in resp.text

    def test_operator_sees_render_empty_state_action(
        self, operator_client: TestClient
    ) -> None:
        # With no renders the page shows the operational "Go to Projects" link;
        # that link is the only role-gated affordance on the renders page.
        resp = operator_client.get("/renders")
        assert resp.status_code == 200
        assert "Go to Projects" in resp.text

    def test_operator_sees_project_actions_and_start(
        self, operator_client: TestClient
    ) -> None:
        project_id, _ = _seed_project_with_frame()
        resp = operator_client.get(f"/projects/{project_id}")
        assert resp.status_code == 200
        # The sidebar "Project Actions" card and the per-status Start control are
        # both operational. Delete is a two-step inline confirmation, so the
        # detail page shows its trigger (the real POST lives in the confirm row).
        assert f"/projects/{project_id}/delete-confirm" in resp.text
        assert f'action="/projects/{project_id}/start"' in resp.text

    def test_operator_sees_frame_browser_show_deleted_toggle(
        self, operator_client: TestClient
    ) -> None:
        project_id, _ = _seed_project_with_frame()
        resp = operator_client.get(f"/frames?project_id={project_id}")
        assert resp.status_code == 200
        assert "show_deleted=1" in resp.text

    def test_operator_sees_frame_tile_remove_control(
        self, operator_client: TestClient
    ) -> None:
        project_id, frame_id = _seed_project_with_frame()
        resp = operator_client.get(f"/frames?project_id={project_id}")
        assert resp.status_code == 200
        # The per-frame soft-delete control is operational.
        assert f"/projects/{project_id}/frames/{frame_id}/soft-delete" in resp.text

    def test_operator_sees_camera_row_edit_control(
        self, operator_client: TestClient
    ) -> None:
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            cam = Camera(name="row-cam", address="127.0.0.1", protocol="vapix")
            db.add(cam)
            db.flush()
            camera_id = cam.id
        resp = operator_client.get("/cameras")
        assert resp.status_code == 200
        assert f"/cameras/{camera_id}/edit-form" in resp.text


class TestViewerDoesNotSeeOperationalAffordances:
    """A viewer sees none of the mutation controls (read-only UI)."""

    def test_viewer_no_add_camera_button(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get("/cameras")
        assert resp.status_code == 200
        assert 'hx-get="/cameras/add-form"' not in resp.text

    def test_viewer_no_new_project_button_on_projects(
        self, viewer_client: TestClient
    ) -> None:
        resp = viewer_client.get("/projects")
        assert resp.status_code == 200
        assert 'href="/projects/new"' not in resp.text

    def test_viewer_no_new_project_button_on_dashboard(
        self, viewer_client: TestClient
    ) -> None:
        resp = viewer_client.get("/")
        assert resp.status_code == 200
        assert 'href="/projects/new"' not in resp.text

    def test_viewer_no_render_empty_state_action(
        self, viewer_client: TestClient
    ) -> None:
        resp = viewer_client.get("/renders")
        assert resp.status_code == 200
        assert "Go to Projects" not in resp.text

    def test_viewer_no_project_actions_or_start(
        self, viewer_client: TestClient
    ) -> None:
        project_id, _ = _seed_project_with_frame()
        resp = viewer_client.get(f"/projects/{project_id}")
        assert resp.status_code == 200
        assert f'action="/projects/{project_id}/delete"' not in resp.text
        assert f'action="/projects/{project_id}/start"' not in resp.text

    def test_viewer_no_frame_browser_show_deleted_toggle(
        self, viewer_client: TestClient
    ) -> None:
        project_id, _ = _seed_project_with_frame()
        resp = viewer_client.get(f"/frames?project_id={project_id}")
        assert resp.status_code == 200
        assert "show_deleted=1" not in resp.text

    def test_viewer_no_frame_tile_remove_control(
        self, viewer_client: TestClient
    ) -> None:
        project_id, frame_id = _seed_project_with_frame()
        resp = viewer_client.get(f"/frames?project_id={project_id}")
        assert resp.status_code == 200
        assert f"/projects/{project_id}/frames/{frame_id}/soft-delete" not in resp.text

    def test_viewer_no_camera_row_edit_control(self, viewer_client: TestClient) -> None:
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            cam = Camera(name="row-cam", address="127.0.0.1", protocol="vapix")
            db.add(cam)
            db.flush()
            camera_id = cam.id
        resp = viewer_client.get("/cameras")
        assert resp.status_code == 200
        assert f"/cameras/{camera_id}/edit-form" not in resp.text


# ---------------------------------------------------------------------------
# HTMX partial re-renders must carry the affordance for an operator too.
# These are the standalone fragment endpoints that polling/swaps return; a
# context flag that fails to thread through them would silently drop the
# operator's controls on the first swap.
# ---------------------------------------------------------------------------


class TestOperatorSeesAffordancesInPartials:
    def test_operator_project_card_partial_has_controls(
        self, operator_client: TestClient
    ) -> None:
        project_id, _ = _seed_project_with_frame()
        resp = operator_client.get("/partials/projects")
        assert resp.status_code == 200
        assert f'action="/projects/{project_id}/start"' in resp.text

    def test_operator_camera_edit_form_partial_is_served(
        self, operator_client: TestClient
    ) -> None:
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            cam = Camera(name="frag-cam", address="127.0.0.1", protocol="vapix")
            db.add(cam)
            db.flush()
            camera_id = cam.id
        resp = operator_client.get(f"/cameras/{camera_id}/edit-form")
        assert resp.status_code == 200
        assert f'action="/cameras/{camera_id}/edit"' in resp.text

    def test_operator_camera_row_after_edit_keeps_controls(
        self, operator_client: TestClient
    ) -> None:
        # The post-edit response re-renders the camera row fragment standalone.
        # Its controls must survive the swap for an operator -- the bug class is
        # a context flag that fails to thread into this re-render path.
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            cam = Camera(name="swap-cam", address="127.0.0.1", protocol="vapix")
            db.add(cam)
            db.flush()
            camera_id = cam.id
        csrf = csrf_of(operator_client, "/cameras")
        resp = operator_client.post(
            f"/cameras/{camera_id}/edit",
            data={"name": "swap-renamed", "protocol": "vapix", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert f"/cameras/{camera_id}/edit-form" in resp.text

    def test_operator_frame_tile_after_soft_delete_keeps_controls(
        self, operator_client: TestClient
    ) -> None:
        # The post-soft-delete response re-renders the frame tile standalone; the
        # restore control must be present for an operator after the swap.
        project_id, frame_id = _seed_project_with_frame()
        csrf = csrf_of(operator_client, "/projects")
        resp = operator_client.post(
            f"/projects/{project_id}/frames/{frame_id}/soft-delete",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert f"/projects/{project_id}/frames/{frame_id}/restore" in resp.text


class TestViewerDoesNotSeeAffordancesInPartials:
    def test_viewer_project_card_partial_has_no_controls(
        self, viewer_client: TestClient
    ) -> None:
        project_id, _ = _seed_project_with_frame()
        resp = viewer_client.get("/partials/projects")
        assert resp.status_code == 200
        assert f'action="/projects/{project_id}/start"' not in resp.text


# ---------------------------------------------------------------------------
# Account / system administration: visible to admin, hidden from operator.
# ---------------------------------------------------------------------------


class TestAdminSeesAccountAndSettingsNav:
    def test_admin_sees_users_settings_nav(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/")
        assert resp.status_code == 200
        assert 'href="/users"' in resp.text
        assert 'href="/settings"' in resp.text
        # Notification settings live on the Settings page now, so there is no
        # standalone nav link for them.
        assert 'href="/notification-settings"' not in resp.text


class TestOperatorDoesNotSeeAccountAndSettingsNav:
    def test_operator_no_users_settings_nav(self, operator_client: TestClient) -> None:
        resp = operator_client.get("/")
        assert resp.status_code == 200
        assert 'href="/users"' not in resp.text
        assert 'href="/settings"' not in resp.text
        assert 'href="/notification-settings"' not in resp.text
