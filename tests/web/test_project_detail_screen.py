"""Web tests for the Meridian project-detail tabs + ribbon."""

from __future__ import annotations

from fastapi.testclient import TestClient

from timelapse_manager.db.models import Camera, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context


def _seed_project() -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(name="pd-cam", address="10.0.0.7", protocol="vapix")
        db.add(cam)
        db.flush()
        proj = Project(
            camera_id=cam.id,
            name="PD Project",
            capture_interval_seconds=300,
            lifecycle_state="active",
        )
        db.add(proj)
        db.flush()
        return proj.id


def test_detail_has_tabs_and_ribbon(admin_client: TestClient) -> None:
    pid = _seed_project()
    html = admin_client.get(f"/projects/{pid}").text
    assert 'class="tabs"' in html
    assert 'data-tab-target="#tab-status"' in html
    assert 'data-tab-target="#tab-renders"' in html
    assert 'id="tab-status"' in html
    assert 'id="tab-renders"' in html
    assert "time-ribbon-slot" in html
    assert "/ribbon?h=36" in html


def test_detail_loads_tabs_script(admin_client: TestClient) -> None:
    pid = _seed_project()
    assert "/static/js/tabs.js" in admin_client.get(f"/projects/{pid}").text


def test_detail_lifecycle_actions_preserved(admin_client: TestClient) -> None:
    pid = _seed_project()
    html = admin_client.get(f"/projects/{pid}").text
    # The working lifecycle/clone/delete affordances must survive the rebuild.
    assert f"/projects/{pid}/clone" in html
    assert f"/projects/{pid}/delete" in html
