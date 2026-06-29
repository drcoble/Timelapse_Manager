"""Web create-project flow: GET form, POST validation, and the success path.

These exercise the admin-gated, CSRF-protected create routes end to end through
the running app (a real session cookie + form token), which inspection alone
cannot verify. The capture supervisor is constructed but not started in the web
test settings (``capture.autostart=False``), so the create handler's
``notify_reconcile()`` is a safe no-op here; the reconcile loop's runtime
behaviour is covered by the supervisor unit tests.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.models import Camera, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context


def _seed_camera(*, name: str, protocol: str | None) -> int:
    """Insert a camera into the running app's database and return its id."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(
            name=name,
            address="127.0.0.1",
            protocol=protocol,
            snapshot_uri="http://127.0.0.1/snap",
        )
        db.add(cam)
        db.flush()
        return cam.id


def _project_named(name: str) -> Project | None:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        return db.query(Project).filter(Project.name == name).one_or_none()


class TestCreateProjectForm:
    def test_get_new_project_form_renders(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/projects/new")
        assert resp.status_code == 200

    def test_get_new_project_not_shadowed_by_detail_route(
        self, admin_client: TestClient
    ) -> None:
        # ``/projects/new`` must resolve to the form, not the int-typed
        # ``/projects/{project_id}`` detail route (which would 422 on "new").
        resp = admin_client.get("/projects/new")
        assert resp.status_code != 422


class TestCreateProjectSubmit:
    def test_valid_create_redirects_and_persists(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(name="create-cam", protocol="vapix")
        csrf = csrf_of(admin_client, "/projects/new")
        resp = admin_client.post(
            "/projects",
            data={
                "name": "Sunrise Lapse",
                "camera_id": str(camera_id),
                "capture_interval_value": "60",
                "capture_interval_unit": "seconds",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        project = _project_named("Sunrise Lapse")
        assert project is not None
        assert project.camera_id == camera_id
        assert project.capture_interval_seconds == 60
        assert project.lifecycle_state == "active"
        # Redirect targets the new project's detail page.
        assert resp.headers["location"] == f"/projects/{project.id}"

    def test_duplicate_name_is_rejected_with_400_not_500(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(name="dup-cam", protocol="vapix")
        csrf = csrf_of(admin_client, "/projects/new")
        first = admin_client.post(
            "/projects",
            data={
                "name": "Only One",
                "camera_id": str(camera_id),
                "capture_interval_value": "60",
                "capture_interval_unit": "seconds",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert first.status_code == 303

        csrf = csrf_of(admin_client, "/projects/new")
        dup = admin_client.post(
            "/projects",
            data={
                "name": "Only One",
                "camera_id": str(camera_id),
                "capture_interval_value": "60",
                "capture_interval_unit": "seconds",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert dup.status_code == 400

    def test_camera_without_protocol_is_rejected(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(name="no-proto-cam", protocol=None)
        csrf = csrf_of(admin_client, "/projects/new")
        resp = admin_client.post(
            "/projects",
            data={
                "name": "Needs Protocol",
                "camera_id": str(camera_id),
                "capture_interval_value": "60",
                "capture_interval_unit": "seconds",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert _project_named("Needs Protocol") is None

    def test_unit_converts_to_seconds(self, admin_client: TestClient) -> None:
        # A value+unit pair persists the canonical seconds. Months uses the
        # 30-day approximation (2_592_000s).
        camera_id = _seed_camera(name="months-cam", protocol="vapix")
        csrf = csrf_of(admin_client, "/projects/new")
        resp = admin_client.post(
            "/projects",
            data={
                "name": "Monthly Lapse",
                "camera_id": str(camera_id),
                "capture_interval_value": "3",
                "capture_interval_unit": "months",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        project = _project_named("Monthly Lapse")
        assert project is not None
        assert project.capture_interval_seconds == 3 * 2592000

    def test_minutes_unit_converts_to_seconds(self, admin_client: TestClient) -> None:
        camera_id = _seed_camera(name="minutes-cam", protocol="vapix")
        csrf = csrf_of(admin_client, "/projects/new")
        resp = admin_client.post(
            "/projects",
            data={
                "name": "Five Minute Lapse",
                "camera_id": str(camera_id),
                "capture_interval_value": "5",
                "capture_interval_unit": "minutes",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        project = _project_named("Five Minute Lapse")
        assert project is not None
        assert project.capture_interval_seconds == 300

    def test_bad_unit_is_rejected_with_400(self, admin_client: TestClient) -> None:
        camera_id = _seed_camera(name="bad-unit-cam", protocol="vapix")
        csrf = csrf_of(admin_client, "/projects/new")
        resp = admin_client.post(
            "/projects",
            data={
                "name": "Bad Unit",
                "camera_id": str(camera_id),
                "capture_interval_value": "5",
                "capture_interval_unit": "fortnights",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert _project_named("Bad Unit") is None

    def test_non_integer_value_is_rejected_with_400(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(name="frac-cam", protocol="vapix")
        csrf = csrf_of(admin_client, "/projects/new")
        resp = admin_client.post(
            "/projects",
            data={
                "name": "Fractional",
                "camera_id": str(camera_id),
                "capture_interval_value": "1.5",
                "capture_interval_unit": "minutes",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert _project_named("Fractional") is None

    def test_missing_value_is_rejected_with_400(self, admin_client: TestClient) -> None:
        camera_id = _seed_camera(name="missing-val-cam", protocol="vapix")
        csrf = csrf_of(admin_client, "/projects/new")
        resp = admin_client.post(
            "/projects",
            data={
                "name": "Missing Value",
                "camera_id": str(camera_id),
                "capture_interval_unit": "minutes",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert _project_named("Missing Value") is None

    def test_non_positive_interval_is_rejected(self, admin_client: TestClient) -> None:
        camera_id = _seed_camera(name="bad-interval-cam", protocol="vapix")
        csrf = csrf_of(admin_client, "/projects/new")
        resp = admin_client.post(
            "/projects",
            data={
                "name": "Bad Interval",
                "camera_id": str(camera_id),
                "capture_interval_value": "0",
                "capture_interval_unit": "seconds",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert _project_named("Bad Interval") is None

    def test_missing_csrf_token_is_forbidden(self, admin_client: TestClient) -> None:
        camera_id = _seed_camera(name="csrf-cam", protocol="vapix")
        resp = admin_client.post(
            "/projects",
            data={
                "name": "No CSRF",
                "camera_id": str(camera_id),
                "capture_interval_value": "60",
                "capture_interval_unit": "seconds",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert _project_named("No CSRF") is None
