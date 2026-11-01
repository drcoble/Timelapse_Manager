"""The PTZ preset picker warns about a camera other projects point elsewhere.

#151: #150 (WP-69-D) warned on the project *settings* form only. The
new-project form loads its PTZ picker from ``GET /cameras/ptz-presets`` when a
camera is chosen, so the picker response now carries the same warning: every
other active project on that camera with a PTZ position, since the new
project's position starts at "none".
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from timelapse_manager.cameras.base import PTZPreset, PTZPresetsResult
from timelapse_manager.db.models import Camera, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context

_NO_PTZ = PTZPresetsResult(presets=[], ptz_supported=False, ok=True, message="")
_PRESETS = PTZPresetsResult(
    presets=[PTZPreset(id="gate", label="Gate")],
    ptz_supported=True,
    ok=True,
    message="",
)


def _seed(projects: list[dict[str, object]]) -> int:
    """Insert a camera and the given projects on it; return the camera id."""
    with session_scope(get_context().session_factory) as db:
        camera = Camera(name="dome", address="192.168.1.60", protocol="vapix")
        db.add(camera)
        db.flush()
        for fields in projects:
            db.add(
                Project(
                    camera_id=camera.id,
                    capture_interval_seconds=60,
                    **{"lifecycle_state": "active", **fields},
                )
            )
        return camera.id


def _picker(
    client: TestClient,
    camera_id: int,
    result: PTZPresetsResult,
    **extra_params: object,
) -> str:
    adapter = MagicMock()
    adapter.list_ptz_presets = AsyncMock(return_value=result)
    adapter.close = AsyncMock()
    with (
        patch(
            "timelapse_manager.web.routers._shared._resolve_camera_host_async",
            new=AsyncMock(return_value=None),
        ),
        patch("timelapse_manager.cameras.build_adapter", return_value=adapter),
    ):
        resp = client.get(
            "/cameras/ptz-presets", params={"camera_id": camera_id, **extra_params}
        )
    assert resp.status_code == 200
    return resp.text


def test_a_positioned_project_on_the_camera_is_warned(
    admin_client: TestClient,
) -> None:
    camera_id = _seed(
        [
            {"name": "yard view", "ptz_preset": "yard"},
            {"name": "raw view", "ptz_pan": 10.0, "ptz_tilt": -5.0, "ptz_zoom": 1.0},
            {"name": "plain view"},
            {"name": "old view", "ptz_preset": "pool", "lifecycle_state": "archived"},
        ]
    )
    body = _picker(admin_client, camera_id, _PRESETS)
    assert 'id="ptz-conflict-warning"' in body
    assert "yard view (preset &#39;yard&#39;)" in body
    assert "raw view (pan 10.0, tilt -5.0, zoom 1.0)" in body
    assert "plain view" not in body  # no position: nothing to conflict with
    assert "old view" not in body  # archived projects do not capture
    # The picker itself still renders below the warning.
    assert 'name="ptz_preset_id"' in body


def test_the_warning_shows_even_when_the_camera_is_unreachable(
    admin_client: TestClient,
) -> None:
    camera_id = _seed([{"name": "yard view", "ptz_preset": "yard"}])
    unreachable = PTZPresetsResult(
        presets=[], ptz_supported=False, ok=False, message="unreachable"
    )
    body = _picker(admin_client, camera_id, unreachable)
    assert 'id="ptz-conflict-warning"' in body
    assert "camera unreachable" in body


def test_no_positioned_project_is_not_warned(admin_client: TestClient) -> None:
    camera_id = _seed([{"name": "plain a"}, {"name": "plain b"}])
    body = _picker(admin_client, camera_id, _NO_PTZ)
    assert "ptz-conflict-warning" not in body


def test_an_unknown_camera_is_not_warned(admin_client: TestClient) -> None:
    _seed([{"name": "yard view", "ptz_preset": "yard"}])
    resp = admin_client.get("/cameras/ptz-presets", params={"camera_id": "nope"})
    assert resp.status_code == 200
    assert "ptz-conflict-warning" not in resp.text


def test_names_are_escaped(admin_client: TestClient) -> None:
    camera_id = _seed([{"name": "<b>x</b>", "ptz_preset": "<i>p</i>"}])
    body = _picker(admin_client, camera_id, _NO_PTZ)
    assert "<b>x</b>" not in body
    assert "&lt;b&gt;x&lt;/b&gt;" in body
    assert "<i>p</i>" not in body


def _project_id(name: str) -> int:
    with session_scope(get_context().session_factory) as db:
        return db.query(Project).filter(Project.name == name).one().id


def test_the_edited_project_is_not_its_own_conflict(admin_client: TestClient) -> None:
    """#167: the settings form sends its own ``project_id``; it is left out."""
    camera_id = _seed(
        [
            {"name": "mine", "ptz_preset": "gate"},
            {"name": "yard view", "ptz_preset": "yard"},
        ]
    )
    mine = _project_id("mine")
    without = _picker(admin_client, camera_id, _PRESETS)
    assert "mine (preset" in without  # the new-project form lists every project
    body = _picker(admin_client, camera_id, _PRESETS, project_id=mine)
    assert "mine (preset" not in body
    assert "yard view (preset &#39;yard&#39;)" in body


def test_a_lone_edited_project_gets_no_warning(admin_client: TestClient) -> None:
    camera_id = _seed([{"name": "mine", "ptz_preset": "gate"}])
    body = _picker(admin_client, camera_id, _PRESETS, project_id=_project_id("mine"))
    assert "ptz-conflict-warning" not in body


def test_a_junk_project_id_is_ignored(admin_client: TestClient) -> None:
    camera_id = _seed([{"name": "yard view", "ptz_preset": "yard"}])
    body = _picker(admin_client, camera_id, _PRESETS, project_id="nope")
    assert "yard view" in body


def test_the_settings_form_requests_the_picker_with_its_project_id(
    admin_client: TestClient,
) -> None:
    """#167: the settings form names itself and renders the shared partial."""
    camera_id = _seed([{"name": "mine", "ptz_preset": "gate"}, {"name": "other"}])
    mine = _project_id("mine")
    assert camera_id
    body = admin_client.get(f"/projects/{mine}/settings").text
    assert f'hx-get="/cameras/ptz-presets?project_id={mine}"' in body
