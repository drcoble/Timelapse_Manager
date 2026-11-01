"""The project settings form warns about a camera other projects point elsewhere.

#69 CAM-R4 (WP-69-F, merged into WP-69-D): a PTZ camera shared by projects with
different positions moves between their captures, and captures on it run one at
a time. The settings form says so, naming the other projects.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from timelapse_manager.db.models import Camera, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context


def _seed(projects: list[dict[str, object]]) -> list[int]:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(name="dome", address="127.0.0.1", protocol="vapix")
        db.add(cam)
        db.flush()
        ids: list[int] = []
        for fields in projects:
            project = Project(camera_id=cam.id, capture_interval_seconds=60, **fields)
            db.add(project)
            db.flush()
            ids.append(project.id)
        return ids


def test_a_different_position_on_the_same_camera_is_warned(
    admin_client: TestClient,
) -> None:
    mine, _, _, _ = _seed(
        [
            {"name": "gate view", "ptz_preset": "gate"},
            {"name": "yard view", "ptz_preset": "yard"},
            {"name": "same view", "ptz_preset": "gate"},
            {"name": "old view", "ptz_preset": "pool", "lifecycle_state": "archived"},
        ]
    )
    body = admin_client.get(f"/projects/{mine}/settings").text
    assert 'id="ptz-conflict-warning"' in body
    assert "yard view (preset &#39;yard&#39;)" in body
    assert "same view" not in body  # same position: no conflict
    assert "old view" not in body  # archived projects do not capture


def test_a_fixed_view_project_is_warned_about_a_ptz_neighbour(
    admin_client: TestClient,
) -> None:
    mine, _ = _seed(
        [
            {"name": "fixed"},
            {"name": "raw", "ptz_pan": 10.0, "ptz_tilt": -5.0, "ptz_zoom": 1.0},
        ]
    )
    body = admin_client.get(f"/projects/{mine}/settings").text
    assert "raw (pan 10.0, tilt -5.0, zoom 1.0)" in body


def test_no_ptz_anywhere_is_not_warned(admin_client: TestClient) -> None:
    mine, _ = _seed([{"name": "plain a"}, {"name": "plain b"}])
    body = admin_client.get(f"/projects/{mine}/settings").text
    assert "ptz-conflict-warning" not in body


def test_names_are_escaped(admin_client: TestClient) -> None:
    mine, _ = _seed(
        [
            {"name": "mine", "ptz_preset": "a"},
            {"name": "<b>x</b>", "ptz_preset": "<i>p</i>"},
        ]
    )
    body = admin_client.get(f"/projects/{mine}/settings").text
    assert "<b>x</b>" not in body
    assert "&lt;b&gt;x&lt;/b&gt;" in body
