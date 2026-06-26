"""Web tests for the PTZ preset/position picker (F3).

Covers:
- GET /cameras/ptz-presets — three outcomes (presets present, reachable-but-no-
  PTZ, unreachable).  All camera-side failures render at HTTP 200; only the role
  gate returns 403.
- POST /projects (create) persists ptz_preset / ptz_pan / ptz_tilt / ptz_zoom.
- POST /projects/{id}/settings (edit) updates PTZ fields.
- GET /projects/{id}/settings (edit form) renders the saved preset preselected
  in the <select> when the adapter returns matching presets.

Adapter stubbing mirrors test_stream_profile_select.py: patch
``timelapse_manager.cameras.resolve_camera_host`` (pass-through) and
``timelapse_manager.cameras.build_adapter`` to return a mock adapter.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.cameras.base import PTZPreset, PTZPresetsResult
from timelapse_manager.db.models import Camera, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context

_ALLOWED_ADDRESS = "192.168.1.60"

# Two presets returned by a capable camera.
_TWO_PRESETS = PTZPresetsResult(
    presets=[
        PTZPreset(id="home", label="Home"),
        PTZPreset(id="door", label="Front Door"),
    ],
    ptz_supported=True,
    ok=True,
)

_REACHABLE_NO_PTZ = PTZPresetsResult(
    presets=[],
    ptz_supported=False,
    ok=True,
)

_UNREACHABLE = PTZPresetsResult(
    presets=[],
    ptz_supported=False,
    ok=False,
    message="camera unreachable",
)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_camera(*, name: str) -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(
            name=name,
            address=_ALLOWED_ADDRESS,
            protocol="vapix",
            snapshot_uri=f"http://{_ALLOWED_ADDRESS}/snap",
        )
        db.add(cam)
        db.flush()
        return cam.id


def _seed_project(
    *,
    name: str,
    camera_id: int,
    ptz_preset: str | None = None,
    ptz_pan: float | None = None,
    ptz_tilt: float | None = None,
    ptz_zoom: float | None = None,
) -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        proj = Project(
            camera_id=camera_id,
            name=name,
            capture_interval_seconds=60,
            lifecycle_state="active",
            ptz_preset=ptz_preset,
            ptz_pan=ptz_pan,
            ptz_tilt=ptz_tilt,
            ptz_zoom=ptz_zoom,
        )
        db.add(proj)
        db.flush()
        return proj.id


def _project_ptz(
    project_id: int,
) -> tuple[str | None, float | None, float | None, float | None]:
    """Return (ptz_preset, ptz_pan, ptz_tilt, ptz_zoom) for a stored project."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        proj = db.get(Project, project_id)
        assert proj is not None
        return proj.ptz_preset, proj.ptz_pan, proj.ptz_tilt, proj.ptz_zoom


# ---------------------------------------------------------------------------
# Adapter stub helpers
# ---------------------------------------------------------------------------


def _ptz_adapter(result: PTZPresetsResult) -> MagicMock:
    adapter = MagicMock()
    adapter.list_ptz_presets = AsyncMock(return_value=result)
    adapter.close = AsyncMock()
    return adapter


def _ptz_patches(result: PTZPresetsResult):
    return (
        patch(
            "timelapse_manager.cameras.resolve_camera_host",
            side_effect=lambda a: a,
        ),
        patch(
            "timelapse_manager.cameras.build_adapter",
            return_value=_ptz_adapter(result),
        ),
    )


# ---------------------------------------------------------------------------
# F3 — GET /cameras/ptz-presets
# ---------------------------------------------------------------------------


class TestPtzPresetsRoute:
    def test_presets_present_renders_select(self, admin_client: TestClient) -> None:
        """A PTZ-capable camera renders the <select> with its presets."""
        camera_id = _seed_camera(name="ptz-success")
        guard, builder = _ptz_patches(_TWO_PRESETS)
        with guard, builder:
            resp = admin_client.get(
                "/cameras/ptz-presets", params={"camera_id": camera_id}
            )
        assert resp.status_code == 200
        html = resp.text
        assert 'name="ptz_preset_id"' in html
        assert 'value="home"' in html
        assert "Home" in html
        assert 'value="door"' in html
        assert "Front Door" in html

    def test_reachable_no_ptz_renders_nothing(self, admin_client: TestClient) -> None:
        """A reachable non-PTZ camera renders nothing (no select, no hint)."""
        camera_id = _seed_camera(name="ptz-no-ptz")
        guard, builder = _ptz_patches(_REACHABLE_NO_PTZ)
        with guard, builder:
            resp = admin_client.get(
                "/cameras/ptz-presets", params={"camera_id": camera_id}
            )
        assert resp.status_code == 200
        html = resp.text
        assert 'name="ptz_preset_id"' not in html
        # No unreachable hint either.
        assert "Could not load PTZ presets" not in html

    def test_unreachable_renders_hint_at_200(self, admin_client: TestClient) -> None:
        """An unreachable camera renders the informational hint at HTTP 200."""
        camera_id = _seed_camera(name="ptz-unreachable")
        guard, builder = _ptz_patches(_UNREACHABLE)
        with guard, builder:
            resp = admin_client.get(
                "/cameras/ptz-presets", params={"camera_id": camera_id}
            )
        assert resp.status_code == 200
        html = resp.text
        assert "Could not load PTZ presets" in html
        assert "camera unreachable" in html.lower() or "unreachable" in html
        assert 'name="ptz_preset_id"' not in html

    def test_missing_camera_id_returns_200(self, admin_client: TestClient) -> None:
        """Missing camera_id renders the partial's fallback state (never crashes)."""
        resp = admin_client.get("/cameras/ptz-presets")
        assert resp.status_code == 200

    def test_unknown_camera_id_returns_200(self, admin_client: TestClient) -> None:
        """An unknown camera_id renders the partial's fallback state at HTTP 200."""
        resp = admin_client.get("/cameras/ptz-presets", params={"camera_id": 999999})
        assert resp.status_code == 200

    def test_forbidden_for_viewer(self, viewer_client: TestClient) -> None:
        """The role gate returns 403 for viewers."""
        resp = viewer_client.get(
            "/cameras/ptz-presets",
            params={"camera_id": 1},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_allowed_for_operator(self, operator_client: TestClient) -> None:
        """Operators can fetch PTZ presets."""
        camera_id = _seed_camera(name="ptz-operator")
        guard, builder = _ptz_patches(_TWO_PRESETS)
        with guard, builder:
            resp = operator_client.get(
                "/cameras/ptz-presets", params={"camera_id": camera_id}
            )
        assert resp.status_code == 200
        assert 'name="ptz_preset_id"' in resp.text


# ---------------------------------------------------------------------------
# F3 — POST /projects (create) persists PTZ fields
# ---------------------------------------------------------------------------


class TestCreateProjectPersistsPtz:
    def test_create_with_preset_persists_preset_id(
        self, admin_client: TestClient
    ) -> None:
        """Creating a project with a preset_id stores it in ptz_preset."""
        camera_id = _seed_camera(name="ptz-create-preset")
        csrf = csrf_of(admin_client, "/projects/new")
        guard, builder = _ptz_patches(_TWO_PRESETS)
        with guard, builder:
            resp = admin_client.post(
                "/projects",
                data={
                    "name": "PTZ Create Preset",
                    "camera_id": str(camera_id),
                    "capture_interval_value": "60",
                    "capture_interval_unit": "seconds",
                    "ptz_preset_id": "home",
                    "csrf_token": csrf,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
        assert resp.status_code == 303, resp.text
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            proj = (
                db.query(Project)
                .filter(Project.name == "PTZ Create Preset")
                .one_or_none()
            )
            assert proj is not None
            # Form field is ptz_preset_id but the column is ptz_preset.
            assert proj.ptz_preset == "home"
            assert proj.ptz_pan is None
            assert proj.ptz_tilt is None
            assert proj.ptz_zoom is None

    def test_create_with_pan_tilt_zoom_persists_floats(
        self, admin_client: TestClient
    ) -> None:
        """Creating a project with raw pan/tilt/zoom persists them as floats."""
        camera_id = _seed_camera(name="ptz-create-raw")
        csrf = csrf_of(admin_client, "/projects/new")
        resp = admin_client.post(
            "/projects",
            data={
                "name": "PTZ Create Raw",
                "camera_id": str(camera_id),
                "capture_interval_value": "60",
                "capture_interval_unit": "seconds",
                "ptz_pan": "45.0",
                "ptz_tilt": "-10.5",
                "ptz_zoom": "2.0",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            proj = (
                db.query(Project).filter(Project.name == "PTZ Create Raw").one_or_none()
            )
            assert proj is not None
            assert proj.ptz_preset is None
            assert proj.ptz_pan == pytest.approx(45.0)
            assert proj.ptz_tilt == pytest.approx(-10.5)
            assert proj.ptz_zoom == pytest.approx(2.0)

    def test_create_without_ptz_stores_nulls(self, admin_client: TestClient) -> None:
        """Creating a project without PTZ fields stores nulls for all four columns."""
        camera_id = _seed_camera(name="ptz-create-none")
        csrf = csrf_of(admin_client, "/projects/new")
        resp = admin_client.post(
            "/projects",
            data={
                "name": "PTZ Create None",
                "camera_id": str(camera_id),
                "capture_interval_value": "60",
                "capture_interval_unit": "seconds",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            proj = (
                db.query(Project)
                .filter(Project.name == "PTZ Create None")
                .one_or_none()
            )
            assert proj is not None
            assert proj.ptz_preset is None
            assert proj.ptz_pan is None
            assert proj.ptz_tilt is None
            assert proj.ptz_zoom is None


# ---------------------------------------------------------------------------
# F3 — POST /projects/{id}/settings (edit) updates PTZ fields
# ---------------------------------------------------------------------------


class TestEditProjectPtz:
    def test_edit_sets_preset(self, admin_client: TestClient) -> None:
        """Editing a project with ptz_preset_id updates the stored ptz_preset."""
        camera_id = _seed_camera(name="ptz-edit-set")
        project_id = _seed_project(name="PTZ Edit Set", camera_id=camera_id)
        csrf = csrf_of(admin_client, f"/projects/{project_id}/settings")
        guard, builder = _ptz_patches(_TWO_PRESETS)
        with guard, builder:
            resp = admin_client.post(
                f"/projects/{project_id}/settings",
                data={
                    "name": "PTZ Edit Set",
                    "camera_id": str(camera_id),
                    "capture_interval_value": "60",
                    "capture_interval_unit": "seconds",
                    "ptz_preset_id": "door",
                    "csrf_token": csrf,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
        assert resp.status_code == 303, resp.text
        preset, pan, tilt, zoom = _project_ptz(project_id)
        assert preset == "door"
        assert pan is None
        assert tilt is None
        assert zoom is None

    def test_edit_clears_preset_when_blank(self, admin_client: TestClient) -> None:
        """Submitting a blank ptz_preset_id clears the saved preset."""
        camera_id = _seed_camera(name="ptz-edit-clear")
        project_id = _seed_project(
            name="PTZ Edit Clear", camera_id=camera_id, ptz_preset="home"
        )
        csrf = csrf_of(admin_client, f"/projects/{project_id}/settings")
        guard, builder = _ptz_patches(_TWO_PRESETS)
        with guard, builder:
            resp = admin_client.post(
                f"/projects/{project_id}/settings",
                data={
                    "name": "PTZ Edit Clear",
                    "camera_id": str(camera_id),
                    "capture_interval_value": "60",
                    "capture_interval_unit": "seconds",
                    "ptz_preset_id": "",
                    "csrf_token": csrf,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
        assert resp.status_code == 303, resp.text
        preset, _pan, _tilt, _zoom = _project_ptz(project_id)
        assert preset is None


# ---------------------------------------------------------------------------
# F3 — GET /projects/{id}/settings (edit form) renders preselected preset
# ---------------------------------------------------------------------------


class TestEditProjectFormPtzPreselect:
    def test_saved_preset_renders_selected_option(
        self, admin_client: TestClient
    ) -> None:
        """The edit form marks the saved preset as selected in the <select>."""
        camera_id = _seed_camera(name="ptz-form-preselect")
        project_id = _seed_project(
            name="PTZ Form Preselect",
            camera_id=camera_id,
            ptz_preset="door",
        )
        guard, builder = _ptz_patches(_TWO_PRESETS)
        with guard, builder:
            resp = admin_client.get(f"/projects/{project_id}/settings")
        assert resp.status_code == 200
        html = resp.text
        # The saved preset "door" must appear selected.
        assert 'value="door"' in html
        assert "Front Door" in html
        # The select must be present.
        assert 'name="ptz_preset_id"' in html

    def test_no_preset_saved_selects_none_option(
        self, admin_client: TestClient
    ) -> None:
        """The edit form selects the 'none' option when no preset is saved."""
        camera_id = _seed_camera(name="ptz-form-none")
        project_id = _seed_project(
            name="PTZ Form None", camera_id=camera_id, ptz_preset=None
        )
        guard, builder = _ptz_patches(_TWO_PRESETS)
        with guard, builder:
            resp = admin_client.get(f"/projects/{project_id}/settings")
        assert resp.status_code == 200
        html = resp.text
        assert 'name="ptz_preset_id"' in html
        # The empty "none" option must be selected.
        assert "— none / don&#39;t move —" in html or "none" in html.lower()

    def test_unreachable_camera_renders_hint_on_edit_form(
        self, admin_client: TestClient
    ) -> None:
        """When the camera cannot be reached, the edit form shows the PTZ hint."""
        camera_id = _seed_camera(name="ptz-form-unreachable")
        project_id = _seed_project(name="PTZ Form Unreachable", camera_id=camera_id)
        guard, builder = _ptz_patches(_UNREACHABLE)
        with guard, builder:
            resp = admin_client.get(f"/projects/{project_id}/settings")
        assert resp.status_code == 200
        html = resp.text
        assert "Could not load PTZ presets" in html
        assert 'name="ptz_preset_id"' not in html
