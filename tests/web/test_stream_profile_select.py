"""Web per-project stream/profile selection: the picker route, persistence, and
the edit-form preselect.

These drive the operator/admin-gated routes through the running app (real session
cookie + CSRF token), with the camera adapter mocked so no real device is
contacted. The picker route is deliberately fragment-only: every camera problem
renders an inline notice at HTTP 200 so HTMX swaps it, and only the role gate
returns a real status code.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.cameras.base import StreamProfile, StreamProfileResult
from timelapse_manager.db.models import Camera, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context

_ALLOWED_ADDRESS = "192.168.1.50"


def _seed_camera(*, name: str, protocol: str | None = "vapix") -> int:
    """Insert a camera into the running app's database and return its id."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(
            name=name,
            address=_ALLOWED_ADDRESS,
            protocol=protocol,
            snapshot_uri=f"http://{_ALLOWED_ADDRESS}/snap",
        )
        db.add(cam)
        db.flush()
        return cam.id


def _seed_project(*, name: str, camera_id: int, stream_id: str | None = None) -> int:
    """Insert a project and return its id."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        project = Project(
            camera_id=camera_id,
            name=name,
            capture_interval_seconds=60,
            stream_id=stream_id,
            stream_label=stream_id,
            lifecycle_state="active",
        )
        db.add(project)
        db.flush()
        return project.id


def _project_named(name: str) -> Project | None:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        return db.query(Project).filter(Project.name == name).one_or_none()


def _stub_adapter(result: StreamProfileResult) -> MagicMock:
    """Build a mock adapter whose list_stream_profiles returns ``result``."""
    adapter = MagicMock()
    adapter.list_stream_profiles = AsyncMock(return_value=result)
    adapter.close = AsyncMock()
    return adapter


def _patches(result: StreamProfileResult):
    """Patch the SSRF guard (allow) and build_adapter to return a stub.

    ``build_adapter``/``resolve_camera_host`` are imported inside the route at
    call time, so they are patched at their source module.
    """
    return (
        patch(
            "timelapse_manager.cameras.resolve_camera_host",
            side_effect=lambda a: a,
        ),
        patch(
            "timelapse_manager.cameras.build_adapter",
            return_value=_stub_adapter(result),
        ),
    )


_TWO_PROFILES = StreamProfileResult(
    profiles=[
        StreamProfile(id="profile-hd", label="HD 1080p"),
        StreamProfile(id="profile-sd", label="SD 480p"),
    ],
    ok=True,
)


class TestStreamProfilesRoute:
    def test_success_renders_select_with_default_first_option(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(name="sp-success")
        guard, builder = _patches(_TWO_PROFILES)
        with guard, builder:
            resp = admin_client.get(
                "/cameras/stream-profiles", params={"camera_id": camera_id}
            )
        assert resp.status_code == 200
        text = resp.text
        assert 'name="stream_profile"' in text
        # The "use camera default" option is first and carries an empty value.
        assert '<option value=""' in text
        assert "Use camera default" in text
        # Each enumerated profile is an option with its id as the value.
        assert 'value="profile-hd"' in text
        assert "HD 1080p" in text
        assert 'value="profile-sd"' in text
        assert "SD 480p" in text

    def test_unreachable_returns_error_fragment_at_200(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(name="sp-unreachable")
        failure = StreamProfileResult(profiles=[], ok=False, message="boom")
        guard, builder = _patches(failure)
        with guard, builder:
            resp = admin_client.get(
                "/cameras/stream-profiles", params={"camera_id": camera_id}
            )
        # Fragment swaps on 200; no 4xx for a camera-side problem.
        assert resp.status_code == 200
        assert "alert error" in resp.text
        assert "Could not load stream profiles" in resp.text
        assert 'name="stream_profile"' not in resp.text

    def test_zero_profiles_returns_error_fragment(
        self, admin_client: TestClient
    ) -> None:
        # Reached cleanly but no selectable streams: still the inline notice, no
        # empty select with only the default option.
        camera_id = _seed_camera(name="sp-empty")
        empty = StreamProfileResult(profiles=[], ok=True)
        guard, builder = _patches(empty)
        with guard, builder:
            resp = admin_client.get(
                "/cameras/stream-profiles", params={"camera_id": camera_id}
            )
        assert resp.status_code == 200
        assert "alert error" in resp.text
        assert 'name="stream_profile"' not in resp.text

    def test_missing_camera_id_returns_error_fragment_not_crash(
        self, admin_client: TestClient
    ) -> None:
        resp = admin_client.get("/cameras/stream-profiles")
        assert resp.status_code == 200
        assert "alert error" in resp.text

    def test_invalid_camera_id_returns_error_fragment(
        self, admin_client: TestClient
    ) -> None:
        resp = admin_client.get(
            "/cameras/stream-profiles", params={"camera_id": "not-an-int"}
        )
        assert resp.status_code == 200
        assert "alert error" in resp.text

    def test_unknown_camera_id_returns_error_fragment(
        self, admin_client: TestClient
    ) -> None:
        resp = admin_client.get(
            "/cameras/stream-profiles", params={"camera_id": 999999}
        )
        assert resp.status_code == 200
        assert "alert error" in resp.text

    def test_forbidden_for_viewer(self, viewer_client: TestClient) -> None:
        # The role gate is the one place this route returns a real status code.
        resp = viewer_client.get(
            "/cameras/stream-profiles",
            params={"camera_id": 1},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_allowed_for_operator(self, operator_client: TestClient) -> None:
        camera_id = _seed_camera(name="sp-operator")
        guard, builder = _patches(_TWO_PROFILES)
        with guard, builder:
            resp = operator_client.get(
                "/cameras/stream-profiles", params={"camera_id": camera_id}
            )
        assert resp.status_code == 200
        assert 'name="stream_profile"' in resp.text


class TestCreateProjectPersistsStream:
    def test_chosen_profile_persists_id_and_label(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(name="sp-create-chosen")
        csrf = csrf_of(admin_client, "/projects/new")
        guard, builder = _patches(_TWO_PROFILES)
        with guard, builder:
            resp = admin_client.post(
                "/projects",
                data={
                    "name": "Stream Pick",
                    "camera_id": str(camera_id),
                    "capture_interval_value": "60",
                    "capture_interval_unit": "seconds",
                    "stream_profile": "profile-hd",
                    "csrf_token": csrf,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
        assert resp.status_code == 303
        project = _project_named("Stream Pick")
        assert project is not None
        assert project.stream_id == "profile-hd"
        # The label is resolved from the enumerated profiles, not just echoed.
        assert project.stream_label == "HD 1080p"

    def test_blank_profile_stores_null_null(self, admin_client: TestClient) -> None:
        camera_id = _seed_camera(name="sp-create-blank")
        csrf = csrf_of(admin_client, "/projects/new")
        resp = admin_client.post(
            "/projects",
            data={
                "name": "No Stream Pick",
                "camera_id": str(camera_id),
                "capture_interval_value": "60",
                "capture_interval_unit": "seconds",
                "stream_profile": "",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        project = _project_named("No Stream Pick")
        assert project is not None
        assert project.stream_id is None
        assert project.stream_label is None

    def test_chosen_profile_unreachable_falls_back_to_id_as_label(
        self, admin_client: TestClient
    ) -> None:
        # Label lookup fails at save time -> the id is stored as its own label;
        # the save must still succeed.
        camera_id = _seed_camera(name="sp-create-unreachable")
        csrf = csrf_of(admin_client, "/projects/new")
        failure = StreamProfileResult(profiles=[], ok=False, message="down")
        guard, builder = _patches(failure)
        with guard, builder:
            resp = admin_client.post(
                "/projects",
                data={
                    "name": "Unreachable Label",
                    "camera_id": str(camera_id),
                    "capture_interval_value": "60",
                    "capture_interval_unit": "seconds",
                    "stream_profile": "profile-xyz",
                    "csrf_token": csrf,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
        assert resp.status_code == 303
        project = _project_named("Unreachable Label")
        assert project is not None
        assert project.stream_id == "profile-xyz"
        assert project.stream_label == "profile-xyz"


class TestEditProjectStream:
    def test_edit_updates_selection(self, admin_client: TestClient) -> None:
        camera_id = _seed_camera(name="sp-edit-update")
        project_id = _seed_project(name="Edit Update", camera_id=camera_id)
        csrf = csrf_of(admin_client, f"/projects/{project_id}/settings")
        guard, builder = _patches(_TWO_PROFILES)
        with guard, builder:
            resp = admin_client.post(
                f"/projects/{project_id}/settings",
                data={
                    "name": "Edit Update",
                    "camera_id": str(camera_id),
                    "capture_interval_value": "60",
                    "capture_interval_unit": "seconds",
                    "stream_profile": "profile-sd",
                    "csrf_token": csrf,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
        assert resp.status_code == 303
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            project = db.get(Project, project_id)
            assert project is not None
            assert project.stream_id == "profile-sd"
            assert project.stream_label == "SD 480p"

    def test_edit_clears_selection_when_blank(self, admin_client: TestClient) -> None:
        camera_id = _seed_camera(name="sp-edit-clear")
        project_id = _seed_project(
            name="Edit Clear", camera_id=camera_id, stream_id="profile-hd"
        )
        csrf = csrf_of(admin_client, f"/projects/{project_id}/settings")
        resp = admin_client.post(
            f"/projects/{project_id}/settings",
            data={
                "name": "Edit Clear",
                "camera_id": str(camera_id),
                "capture_interval_value": "60",
                "capture_interval_unit": "seconds",
                "stream_profile": "",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            project = db.get(Project, project_id)
            assert project is not None
            assert project.stream_id is None
            assert project.stream_label is None


class TestEditProjectFormPreselect:
    def test_saved_selection_renders_preselected(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(name="sp-form-preselect")
        project_id = _seed_project(
            name="Form Preselect", camera_id=camera_id, stream_id="profile-sd"
        )
        guard, builder = _patches(_TWO_PROFILES)
        with guard, builder:
            resp = admin_client.get(f"/projects/{project_id}/settings")
        assert resp.status_code == 200
        import re

        text = resp.text
        # The saved profile's option carries `selected`; the default does not.
        chosen = re.search(r'<option value="profile-sd"[^>]*>', text)
        assert chosen is not None
        assert "selected" in chosen.group(0)
        default_opt = re.search(r'<option value=""[^>]*>', text)
        assert default_opt is not None
        assert "selected" not in default_opt.group(0)

    def test_unreachable_camera_renders_error_state_not_crash(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(name="sp-form-unreachable")
        project_id = _seed_project(
            name="Form Unreachable", camera_id=camera_id, stream_id="profile-hd"
        )
        failure = StreamProfileResult(profiles=[], ok=False, message="down")
        guard, builder = _patches(failure)
        with guard, builder:
            resp = admin_client.get(f"/projects/{project_id}/settings")
        # Edit page still renders; the picker shows its inline notice.
        assert resp.status_code == 200
        assert "alert error" in resp.text
