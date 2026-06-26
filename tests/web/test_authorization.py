"""E-suite: Authorization — deny-by-default, role enforcement."""

from __future__ import annotations

from urllib.parse import quote

from fastapi.testclient import TestClient

from tests.conftest import csrf_of, seed_admin
from timelapse_manager.db.models import Camera, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context

# A real browser navigation sends an Accept that asks for HTML; that is the
# signal the login redirect keys on (a sub-resource/API fetch does not).
_NAV = {"Accept": "text/html,application/xhtml+xml"}


def _seed_camera_and_project() -> tuple[int, int]:
    """Insert a protocol-configured camera and a project; return their ids.

    Used by the operator-access tests so a mutation that loads a real row
    exercises the authorization gate against a live resource rather than 404ing
    before the handler body runs.
    """
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(name="op-cam", address="127.0.0.1", protocol="vapix")
        db.add(cam)
        db.flush()
        camera_id = cam.id
        proj = Project(
            camera_id=camera_id,
            name="op-project",
            capture_interval_seconds=60,
            lifecycle_state="active",
            operational_status="idle",
            storage_path="/tmp/op-project",
        )
        db.add(proj)
        db.flush()
        project_id = proj.id
    return camera_id, project_id


class TestAnonymousAccess:
    """An unauthenticated browser is redirected to /login with a return-to.

    The redirect (not a bare 401) is the expected behavior: these assert the
    concrete 303 → /login?next=<path> so a regression back to a bare 401 fails.
    """

    def test_anon_dashboard_redirects_to_login(self, anon_client: TestClient) -> None:
        resp = anon_client.get("/", headers=_NAV, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/login?next={quote('/', safe='')}"

    def test_anon_cameras_page_redirects_to_login(
        self, anon_client: TestClient
    ) -> None:
        resp = anon_client.get("/cameras", headers=_NAV, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/login?next={quote('/cameras', safe='')}"

    def test_anon_users_page_redirects_to_login(self, anon_client: TestClient) -> None:
        resp = anon_client.get("/users", headers=_NAV, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/login?next={quote('/users', safe='')}"

    def test_anon_settings_page_redirects_to_login(
        self, anon_client: TestClient
    ) -> None:
        resp = anon_client.get("/settings", headers=_NAV, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/login?next={quote('/settings', safe='')}"

    def test_query_string_preserved_in_next(self, anon_client: TestClient) -> None:
        resp = anon_client.get("/projects?page=2", headers=_NAV, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == (
            f"/login?next={quote('/projects?page=2', safe='')}"
        )


class TestLoginRedirectScope:
    """The login redirect is browser-only; machine callers keep the bare 401."""

    def test_api_path_keeps_bare_401_json(self, anon_client: TestClient) -> None:
        # An API path (token-gated) returns a bare 401, never a login redirect.
        resp = anon_client.get("/api/v1/system", follow_redirects=False)
        assert resp.status_code == 401
        assert "location" not in resp.headers

    def test_explicit_json_client_keeps_bare_401(self, anon_client: TestClient) -> None:
        resp = anon_client.get(
            "/", headers={"Accept": "application/json"}, follow_redirects=False
        )
        assert resp.status_code == 401

    def test_htmx_boosted_nav_preserves_destination(
        self, anon_client: TestClient
    ) -> None:
        # An hx-boosted request is a genuine navigation: its own path is the
        # destination and is preserved as next.
        resp = anon_client.get(
            "/cameras",
            headers={"HX-Request": "true", "HX-Boosted": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 204
        assert (
            resp.headers["HX-Redirect"] == f"/login?next={quote('/cameras', safe='')}"
        )

    def test_htmx_background_poll_returns_to_current_page(
        self, anon_client: TestClient
    ) -> None:
        # The regression: a background poll (the alerts panel refresh) whose
        # session has expired must send the user back to the PAGE they are on
        # (HX-Current-URL), not to the polled fragment endpoint /alerts/summary.
        resp = anon_client.get(
            "/alerts/summary",
            headers={
                "HX-Request": "true",
                "HX-Current-URL": "https://testserver/projects",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 204
        assert (
            resp.headers["HX-Redirect"] == f"/login?next={quote('/projects', safe='')}"
        )
        # And never the fragment endpoint itself.
        assert "alerts/summary" not in resp.headers["HX-Redirect"]

    def test_htmx_poll_without_current_url_falls_back_to_bare_login(
        self, anon_client: TestClient
    ) -> None:
        # No HX-Current-URL to recover the page from: a bare /login, which the
        # login route resolves to the dashboard -- never the fragment endpoint.
        resp = anon_client.get(
            "/alerts/summary",
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 204
        assert resp.headers["HX-Redirect"] == "/login"


class TestLoginReturnTo:
    """After signing in, the user lands back on the originally-requested path."""

    def test_post_login_follows_safe_next(self, web_client: TestClient) -> None:
        seed_admin(web_client)
        resp = web_client.post(
            "/login",
            data={
                "username": "admin",
                "password": "AdminP@ssw0rd1234",
                "next": "/cameras",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/cameras"

    def test_post_login_rejects_open_redirect(self, web_client: TestClient) -> None:
        seed_admin(web_client)
        for hostile in ("//evil.example", "https://evil.example", "/\\evil"):
            resp = web_client.post(
                "/login",
                data={
                    "username": "admin",
                    "password": "AdminP@ssw0rd1234",
                    "next": hostile,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert resp.headers["location"] == "/", f"open redirect via {hostile!r}"
            # Log out so the next iteration starts anonymous again.
            csrf = csrf_of(web_client, "/")
            web_client.post(
                "/logout",
                data={"csrf_token": csrf},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )

    def test_login_page_carries_next_into_form(self, anon_client: TestClient) -> None:
        resp = anon_client.get("/login?next=/cameras", follow_redirects=False)
        assert resp.status_code == 200
        assert '<input type="hidden" name="next" value="/cameras">' in resp.text

    def test_post_login_rejects_fragment_next(self, web_client: TestClient) -> None:
        # A stale or crafted next pointing at a server-rendered fragment endpoint
        # must not land the user on an unstyled partial -- it falls back to "/".
        seed_admin(web_client)
        for fragment in ("/alerts/summary", "/partials/status", "/partials/projects"):
            resp = web_client.post(
                "/login",
                data={
                    "username": "admin",
                    "password": "AdminP@ssw0rd1234",
                    "next": fragment,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert resp.headers["location"] == "/", f"fragment leaked via {fragment!r}"
            csrf = csrf_of(web_client, "/")
            web_client.post(
                "/logout",
                data={"csrf_token": csrf},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )


class TestViewerAccess:
    def test_viewer_can_access_dashboard(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get("/", follow_redirects=False)
        assert resp.status_code == 200

    def test_viewer_can_access_cameras_page(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get("/cameras", follow_redirects=False)
        assert resp.status_code == 200

    def test_viewer_can_access_projects_page(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get("/projects", follow_redirects=False)
        assert resp.status_code == 200

    def test_viewer_can_access_renders_page(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get("/renders", follow_redirects=False)
        assert resp.status_code == 200

    def test_viewer_post_cameras_is_403(self, viewer_client: TestClient) -> None:
        """Viewer hitting an admin-only mutation is rejected server-side."""
        csrf = csrf_of(viewer_client, "/cameras")
        resp = viewer_client.post(
            "/cameras",
            data={
                "name": "test-cam",
                "protocol": "http",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_viewer_get_users_page_is_403(self, viewer_client: TestClient) -> None:
        """GET /users is admin-only; viewer gets 403."""
        resp = viewer_client.get("/users", follow_redirects=False)
        assert resp.status_code == 403

    def test_viewer_post_trigger_render_is_403(self, viewer_client: TestClient) -> None:
        """POST /projects/1/renders is admin-only."""
        csrf = csrf_of(viewer_client, "/projects")
        resp = viewer_client.post(
            "/projects/1/renders",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_viewer_get_settings_is_403(self, viewer_client: TestClient) -> None:
        """GET /settings is admin-only."""
        resp = viewer_client.get("/settings", follow_redirects=False)
        assert resp.status_code == 403


class TestAdminAccess:
    def test_admin_can_access_dashboard(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/", follow_redirects=False)
        assert resp.status_code == 200

    def test_admin_can_access_users_page(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/users", follow_redirects=False)
        assert resp.status_code == 200

    def test_admin_can_access_settings_page(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/settings", follow_redirects=False)
        assert resp.status_code == 200


class TestServerEnforcesAuthZ:
    def test_viewer_post_cameras_fails_even_with_correct_csrf(
        self, viewer_client: TestClient
    ) -> None:
        """Server-side enforcement is independent of template-level hiding."""
        csrf = csrf_of(viewer_client, "/cameras")
        # Viewer has a valid CSRF token but still must not be allowed.
        resp = viewer_client.post(
            "/cameras",
            data={
                "name": "injected-cam",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_viewer_delete_camera_is_403(self, viewer_client: TestClient) -> None:
        """DELETE /cameras/{id} requires admin; viewer is denied by the server."""
        csrf = csrf_of(viewer_client, "/cameras")
        resp = viewer_client.delete(
            "/cameras/99999",
            headers={"X-CSRF-Token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 403


class TestOperatorCanMutateOperationalSurface:
    """Operator may mutate cameras, projects, renders, and frames.

    Each assertion isolates the *authorization* outcome: the role gate runs
    before the handler body, so an admitted operator never gets a 401/403. Where
    a request needs no pre-existing row (camera/project create) the concrete 303
    success is asserted; the rest assert the operator got past the gate.
    """

    def _not_denied(self, status_code: int) -> bool:
        # The gate admitting the operator means anything but 401/403. A missing
        # resource (404) or a validation re-render (400) still proves admission.
        return status_code not in (401, 403)

    def test_operator_can_create_camera(self, operator_client: TestClient) -> None:
        csrf = csrf_of(operator_client, "/cameras")
        resp = operator_client.post(
            "/cameras",
            data={"name": "op-created-cam", "protocol": "http", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_operator_can_edit_camera(self, operator_client: TestClient) -> None:
        camera_id, _ = _seed_camera_and_project()
        csrf = csrf_of(operator_client, "/cameras")
        resp = operator_client.post(
            f"/cameras/{camera_id}/edit",
            data={"name": "op-renamed-cam", "protocol": "vapix", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 200

    def test_operator_can_delete_camera(self, operator_client: TestClient) -> None:
        # A standalone camera (no dependent project) so the delete reflects the
        # authorization outcome, not a foreign-key constraint.
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            cam = Camera(name="op-del-cam", address="127.0.0.1", protocol="vapix")
            db.add(cam)
            db.flush()
            camera_id = cam.id
        csrf = csrf_of(operator_client, "/cameras")
        resp = operator_client.delete(
            f"/cameras/{camera_id}",
            headers={"X-CSRF-Token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 200

    def test_operator_can_load_camera_forms(self, operator_client: TestClient) -> None:
        camera_id, _ = _seed_camera_and_project()
        assert operator_client.get("/cameras/add-form").status_code == 200
        assert operator_client.get(f"/cameras/{camera_id}/edit-form").status_code == 200

    def test_operator_can_create_project(self, operator_client: TestClient) -> None:
        camera_id, _ = _seed_camera_and_project()
        csrf = csrf_of(operator_client, "/projects")
        resp = operator_client.post(
            "/projects",
            data={
                "name": "op-new-project",
                "camera_id": str(camera_id),
                "capture_interval_value": "60",
                "capture_interval_unit": "seconds",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_operator_can_edit_project(self, operator_client: TestClient) -> None:
        camera_id, project_id = _seed_camera_and_project()
        csrf = csrf_of(operator_client, f"/projects/{project_id}/settings")
        resp = operator_client.post(
            f"/projects/{project_id}/settings",
            data={
                "name": "op-edited-project",
                "camera_id": str(camera_id),
                "capture_interval_value": "120",
                "capture_interval_unit": "seconds",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code in (200, 303)

    def test_operator_can_clone_project(self, operator_client: TestClient) -> None:
        _, project_id = _seed_camera_and_project()
        csrf = csrf_of(operator_client, f"/projects/{project_id}/clone")
        resp = operator_client.post(
            f"/projects/{project_id}/clone",
            data={"name": "op-clone", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code in (200, 303)

    def test_operator_can_pause_and_resume_project(
        self, operator_client: TestClient
    ) -> None:
        _, project_id = _seed_camera_and_project()
        csrf = csrf_of(operator_client, "/projects")
        for action in ("pause", "resume"):
            resp = operator_client.post(
                f"/projects/{project_id}/{action}",
                data={"csrf_token": csrf},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
            assert self._not_denied(resp.status_code)

    def test_operator_can_delete_project(self, operator_client: TestClient) -> None:
        _, project_id = _seed_camera_and_project()
        csrf = csrf_of(operator_client, "/projects")
        resp = operator_client.post(
            f"/projects/{project_id}/delete",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert self._not_denied(resp.status_code)

    def test_operator_can_trigger_render(self, operator_client: TestClient) -> None:
        _, project_id = _seed_camera_and_project()
        csrf = csrf_of(operator_client, "/projects")
        resp = operator_client.post(
            f"/projects/{project_id}/renders",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert self._not_denied(resp.status_code)

    def test_operator_frame_mutations_pass_the_gate(
        self, operator_client: TestClient
    ) -> None:
        # No frame exists, so these 404 in the handler body -- but a denied
        # operator would 403 before that, so 404 proves the gate admitted them.
        _, project_id = _seed_camera_and_project()
        csrf = csrf_of(operator_client, "/projects")
        soft = operator_client.post(
            f"/projects/{project_id}/frames/1/soft-delete",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert self._not_denied(soft.status_code)
        restore = operator_client.post(
            f"/projects/{project_id}/frames/1/restore",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert self._not_denied(restore.status_code)


class TestOperatorCannotTouchAccountsOrSettings:
    """Operator is denied every account-management and system-settings surface."""

    def test_operator_get_users_page_is_403(self, operator_client: TestClient) -> None:
        resp = operator_client.get("/users", follow_redirects=False)
        assert resp.status_code == 403

    def test_operator_get_users_add_form_is_403(
        self, operator_client: TestClient
    ) -> None:
        resp = operator_client.get("/users/add-form", follow_redirects=False)
        assert resp.status_code == 403

    def test_operator_create_user_is_403(self, operator_client: TestClient) -> None:
        csrf = csrf_of(operator_client, "/cameras")
        resp = operator_client.post(
            "/users",
            data={
                "username": "sneaky",
                "password": "SneakyPass12345!",
                "role": "viewer",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_operator_edit_user_role_is_403(self, operator_client: TestClient) -> None:
        csrf = csrf_of(operator_client, "/cameras")
        resp = operator_client.post(
            "/users/1/edit",
            data={"role": "admin", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_operator_get_settings_is_403(self, operator_client: TestClient) -> None:
        resp = operator_client.get("/settings", follow_redirects=False)
        assert resp.status_code == 403

    def test_operator_post_settings_is_403(self, operator_client: TestClient) -> None:
        csrf = csrf_of(operator_client, "/cameras")
        resp = operator_client.post(
            "/settings",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_operator_get_notification_settings_is_403(
        self, operator_client: TestClient
    ) -> None:
        resp = operator_client.get("/notification-settings", follow_redirects=False)
        assert resp.status_code == 403
