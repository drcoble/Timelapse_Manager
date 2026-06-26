"""Web form tests for project campaign bounds (start/end date + frame cap).

Exercise the admin-gated, CSRF-protected create / edit / clone routes through the
running app (real session cookie + form token), matching the existing project
management web tests. Covers persistence + round-trip, form prefill, and the
end>start / positive-cap validation surfaced as inline 400 errors.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.models import Camera, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context


def _seed_camera(*, name: str) -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(
            name=name,
            address="127.0.0.1",
            protocol="vapix",
            snapshot_uri="http://127.0.0.1/snap",
        )
        db.add(cam)
        db.flush()
        return cam.id


def _seed_project(*, name: str, camera_id: int) -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        proj = Project(
            camera_id=camera_id,
            name=name,
            capture_interval_seconds=60,
            lifecycle_state="active",
        )
        db.add(proj)
        db.flush()
        return proj.id


def _project(project_id: int) -> Project | None:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        return db.get(Project, project_id)


def _project_named(name: str) -> Project | None:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        return db.query(Project).filter(Project.name == name).one_or_none()


class TestCreateProjectBounds:
    def test_create_persists_bounds(self, admin_client: TestClient) -> None:
        cam = _seed_camera(name="cb-create-cam")
        csrf = csrf_of(admin_client, "/projects/new")
        resp = admin_client.post(
            "/projects",
            data={
                "name": "Web Bounded",
                "camera_id": str(cam),
                "capture_interval_value": "60",
                "capture_interval_unit": "seconds",
                "start_date": "2026-07-01T08:00",
                "end_date": "2026-07-10T18:00",
                "max_frame_count": "250",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        project = _project_named("Web Bounded")
        assert project is not None
        assert project.max_frame_count == 250
        assert project.start_date is not None
        assert project.start_date.strftime("%Y-%m-%dT%H:%M") == "2026-07-01T08:00"
        assert project.end_date.strftime("%Y-%m-%dT%H:%M") == "2026-07-10T18:00"

    def test_create_rejects_end_before_start(self, admin_client: TestClient) -> None:
        cam = _seed_camera(name="cb-create-bad-cam")
        csrf = csrf_of(admin_client, "/projects/new")
        resp = admin_client.post(
            "/projects",
            data={
                "name": "Web BadDates",
                "camera_id": str(cam),
                "capture_interval_value": "60",
                "capture_interval_unit": "seconds",
                "start_date": "2026-07-10T08:00",
                "end_date": "2026-07-01T08:00",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "after the start date" in resp.text
        assert _project_named("Web BadDates") is None

    def test_create_rejects_zero_frame_cap(self, admin_client: TestClient) -> None:
        cam = _seed_camera(name="cb-create-zero-cam")
        csrf = csrf_of(admin_client, "/projects/new")
        resp = admin_client.post(
            "/projects",
            data={
                "name": "Web ZeroCap",
                "camera_id": str(cam),
                "capture_interval_value": "60",
                "capture_interval_unit": "seconds",
                "max_frame_count": "0",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "greater than zero" in resp.text
        assert _project_named("Web ZeroCap") is None


class TestEditProjectBounds:
    def _set_bounds(self, project_id: int) -> None:
        ctx = get_context()
        from datetime import datetime

        with session_scope(ctx.session_factory) as db:
            p = db.get(Project, project_id)
            p.start_date = datetime(2026, 7, 1, 8, 0)
            p.end_date = datetime(2026, 7, 10, 18, 0)
            p.max_frame_count = 333

    def test_edit_form_prefills_bounds(self, admin_client: TestClient) -> None:
        cam = _seed_camera(name="cb-edit-prefill-cam")
        pid = _seed_project(name="Prefill", camera_id=cam)
        self._set_bounds(pid)
        resp = admin_client.get(f"/projects/{pid}/settings")
        assert resp.status_code == 200
        # The datetime-local inputs and the number input must carry the stored
        # values (this is the frozen-dataclass / Jinja-undefined gotcha guard).
        assert 'value="2026-07-01T08:00"' in resp.text
        assert 'value="2026-07-10T18:00"' in resp.text
        assert 'value="333"' in resp.text

    def test_edit_updates_bounds(self, admin_client: TestClient) -> None:
        cam = _seed_camera(name="cb-edit-cam")
        pid = _seed_project(name="EditBounds", camera_id=cam)
        csrf = csrf_of(admin_client, f"/projects/{pid}/settings")
        resp = admin_client.post(
            f"/projects/{pid}/settings",
            data={
                "name": "EditBounds",
                "camera_id": str(cam),
                "capture_interval_value": "60",
                "capture_interval_unit": "seconds",
                "start_date": "2026-08-01T00:00",
                "end_date": "2026-08-05T00:00",
                "max_frame_count": "1000",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        project = _project(pid)
        assert project is not None
        assert project.max_frame_count == 1000
        assert project.start_date.strftime("%Y-%m-%dT%H:%M") == "2026-08-01T00:00"

    def test_edit_clearing_bounds_sets_null(self, admin_client: TestClient) -> None:
        cam = _seed_camera(name="cb-edit-clear-cam")
        pid = _seed_project(name="ClearBounds", camera_id=cam)
        self._set_bounds(pid)
        csrf = csrf_of(admin_client, f"/projects/{pid}/settings")
        resp = admin_client.post(
            f"/projects/{pid}/settings",
            data={
                "name": "ClearBounds",
                "camera_id": str(cam),
                "capture_interval_value": "60",
                "capture_interval_unit": "seconds",
                "start_date": "",
                "end_date": "",
                "max_frame_count": "",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        project = _project(pid)
        assert project is not None
        assert project.start_date is None
        assert project.end_date is None
        assert project.max_frame_count is None

    def test_edit_rejects_end_before_start(self, admin_client: TestClient) -> None:
        cam = _seed_camera(name="cb-edit-bad-cam")
        pid = _seed_project(name="EditBad", camera_id=cam)
        csrf = csrf_of(admin_client, f"/projects/{pid}/settings")
        resp = admin_client.post(
            f"/projects/{pid}/settings",
            data={
                "name": "EditBad",
                "camera_id": str(cam),
                "capture_interval_value": "60",
                "capture_interval_unit": "seconds",
                "start_date": "2026-08-05T00:00",
                "end_date": "2026-08-01T00:00",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "after the start date" in resp.text


class TestCloneProjectBounds:
    def test_clone_copies_bounds(self, admin_client: TestClient) -> None:
        from datetime import datetime

        cam = _seed_camera(name="cb-clone-cam")
        pid = _seed_project(name="CloneSrc", camera_id=cam)
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            p = db.get(Project, pid)
            p.start_date = datetime(2026, 9, 1, 0, 0)
            p.end_date = datetime(2026, 9, 10, 0, 0)
            p.max_frame_count = 77

        csrf = csrf_of(admin_client, f"/projects/{pid}/clone")
        resp = admin_client.post(
            f"/projects/{pid}/clone",
            data={"name": "CloneDst", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        clone = _project_named("CloneDst")
        assert clone is not None
        assert clone.max_frame_count == 77
        assert clone.start_date == datetime(2026, 9, 1, 0, 0)
        assert clone.end_date == datetime(2026, 9, 10, 0, 0)
