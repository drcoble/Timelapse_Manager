"""Web tests for the render-trigger panel + inline-override trigger."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.models import Camera, Project, RenderJob
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context


def _seed_project() -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(name="rt-cam", address="10.0.0.8", protocol="vapix")
        db.add(cam)
        db.flush()
        proj = Project(
            camera_id=cam.id,
            name="RT Project",
            capture_interval_seconds=300,
            lifecycle_state="active",
        )
        db.add(proj)
        db.flush()
        return proj.id


def _latest_job_settings(project_id: int) -> dict:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        job = (
            db.query(RenderJob)
            .filter(RenderJob.project_id == project_id)
            .order_by(RenderJob.id.desc())
            .first()
        )
        assert job is not None
        return dict(job.output_settings or {})


def test_detail_has_render_trigger_panel(admin_client: TestClient) -> None:
    pid = _seed_project()
    html = admin_client.get(f"/projects/{pid}").text
    assert "render-trigger-panel" in html
    assert 'name="render_encoder"' in html
    assert 'name="render_container"' in html
    assert 'name="render_fps"' in html
    assert 'id="render-combo-warning"' in html
    assert 'data-chip-target="#render_fps"' in html


def test_trigger_applies_inline_overrides(admin_client: TestClient) -> None:
    pid = _seed_project()
    csrf = csrf_of(admin_client, f"/projects/{pid}")
    resp = admin_client.post(
        f"/projects/{pid}/renders",
        data={
            "csrf_token": csrf,
            "render_encoder": "libx265",
            "render_container": "mkv",
            "render_fps": "30",
            "render_resolution": "1280x720",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    settings = _latest_job_settings(pid)
    assert settings["codec"] == "libx265"
    assert settings["container"] == "mkv"
    assert settings["fps"] == 30
    assert settings["width"] == 1280 and settings["height"] == 720


def test_trigger_without_overrides_uses_default(admin_client: TestClient) -> None:
    pid = _seed_project()
    csrf = csrf_of(admin_client, f"/projects/{pid}")
    resp = admin_client.post(
        f"/projects/{pid}/renders",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    settings = _latest_job_settings(pid)
    # A never-configured project falls back to a usable default codec/container.
    assert settings.get("codec") and settings.get("container")
