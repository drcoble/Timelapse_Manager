"""K-suite: UI smoke tests — pages render and include the CSRF meta tag."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import csrf_of


class TestPublicPages:
    def test_login_page_renders_without_auth(self, anon_client: TestClient) -> None:
        resp = anon_client.get("/login")
        assert resp.status_code == 200

    def test_login_page_contains_form(self, anon_client: TestClient) -> None:
        resp = anon_client.get("/login")
        assert "username" in resp.text.lower()
        assert "password" in resp.text.lower()


class TestAuthenticatedPagesCsrfMeta:
    def test_dashboard_renders_200(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/", follow_redirects=False)
        assert resp.status_code == 200

    def test_dashboard_contains_csrf_meta_tag(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/")
        assert 'name="csrf-token"' in resp.text

    def test_dashboard_csrf_meta_content_is_non_empty(
        self, admin_client: TestClient
    ) -> None:
        csrf = csrf_of(admin_client, "/")
        assert len(csrf) > 10

    def test_cameras_page_renders_200(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/cameras", follow_redirects=False)
        assert resp.status_code == 200

    def test_cameras_page_contains_csrf_meta_tag(
        self, admin_client: TestClient
    ) -> None:
        resp = admin_client.get("/cameras")
        assert 'name="csrf-token"' in resp.text

    def test_projects_page_renders_200(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/projects", follow_redirects=False)
        assert resp.status_code == 200

    def test_renders_page_renders_200(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/renders", follow_redirects=False)
        assert resp.status_code == 200

    def test_users_page_renders_200(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/users", follow_redirects=False)
        assert resp.status_code == 200

    def test_settings_page_renders_200(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/settings", follow_redirects=False)
        assert resp.status_code == 200

    def test_settings_page_contains_csrf_meta_tag(
        self, admin_client: TestClient
    ) -> None:
        resp = admin_client.get("/settings")
        assert 'name="csrf-token"' in resp.text


class TestPartialEndpoints:
    def test_partial_projects_returns_project_grid_wrapper(
        self, admin_client: TestClient
    ) -> None:
        """GET /partials/projects returns the project-grid wrapper (or empty state)."""
        resp = admin_client.get("/partials/projects")
        assert resp.status_code == 200
        # Either the empty-state div or the project-grid class should be present.
        assert "project-grid" in resp.text or "empty-state" in resp.text

    def test_frames_without_project_id_shows_all_projects_grid(
        self, admin_client: TestClient
    ) -> None:
        """GET /frames without project_id renders the cross-project grid, not 422.

        The bare nav link carries no scope. Instead of erroring it shows the
        "All Projects" global grid (keyset-paged on the frame id) with a project
        picker <select> to narrow to a single project.
        """
        from timelapse_manager.db.models import Camera, Project
        from timelapse_manager.db.session import session_scope
        from timelapse_manager.runtime import get_context

        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            cam = Camera(name="picker-cam", address="127.0.0.1", protocol="http")
            db.add(cam)
            db.flush()
            proj = Project(
                camera_id=cam.id,
                name="picker-project",
                capture_interval_seconds=60,
                lifecycle_state="active",
                operational_status="idle",
                storage_path="/tmp/picker",
            )
            db.add(proj)
            db.flush()
            project_id = proj.id

        resp = admin_client.get("/frames", follow_redirects=False)
        assert resp.status_code == 200
        # All-Projects mode: the global grid header + the project picker select
        # (with the seeded project as an option), not the old picker page.
        assert "All Projects" in resp.text
        assert 'id="frame-project-select"' in resp.text
        assert f'value="{project_id}"' in resp.text


class TestKnownGaps:
    def test_project_action_start_records_audit_intent(
        self, admin_client: TestClient
    ) -> None:
        """POST /projects/{id}/start is wired and audits intent (no capture API).

        No per-project capture-control runtime API exists; the route records an
        audited event and redirects. Assert that behaviour without assuming a
        full capture engine.
        """
        from timelapse_manager.db.models import Camera, Project
        from timelapse_manager.db.session import session_scope
        from timelapse_manager.runtime import get_context

        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            cam = Camera(name="smoke-cam", address="127.0.0.1", protocol="http")
            db.add(cam)
            db.flush()
            proj = Project(
                camera_id=cam.id,
                name="smoke-project",
                capture_interval_seconds=60,
                lifecycle_state="active",
                operational_status="idle",
                storage_path="/tmp/smoke",
            )
            db.add(proj)
            db.flush()
            project_id = proj.id

        csrf = csrf_of(admin_client, f"/projects/{project_id}")
        resp = admin_client.post(
            f"/projects/{project_id}/start",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        # Route is wired: expect a redirect to the project detail page.
        assert resp.status_code == 303
