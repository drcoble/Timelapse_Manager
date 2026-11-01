"""Web tests for the render-trigger panel + inline-override trigger."""

from __future__ import annotations

import re

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
    assert 'id="trigger-render-combo-warning"' in html
    assert 'data-chip-target="#trigger-render-fps"' in html
    # The panel's ids must not collide with the Settings tab's render form,
    # which is loaded into the same page (#34).
    assert 'hx-include="#trigger-render-encoder,#trigger-render-container"' in html
    assert 'hx-target="#trigger-render-combo-warning"' in html


def test_trigger_panel_ids_are_unique_with_the_settings_form(
    admin_client: TestClient,
) -> None:
    """The panel and the lazily-loaded settings form share no element id."""
    pid = _seed_project()
    page = admin_client.get(f"/projects/{pid}").text
    form = admin_client.get(f"/projects/{pid}/settings/form").text
    panel_ids = set(re.findall(r'\bid="([^"]+)"', page))
    form_ids = set(re.findall(r'\bid="([^"]+)"', form))
    for name in ("render_encoder", "render_container", "render_fps"):
        assert name in form_ids
    assert {"render_encoder", "render_container", "render_fps"} & panel_ids == set()
    assert "render-combo-warning" not in panel_ids
    # The settings form's combo-check reads only its own two selects, not every
    # render_* field on the page (which included the trigger panel's).
    assert form.count('hx-include="#render_encoder,#render_container"') == 2
    assert "[name^='render_']" not in form


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


def test_download_blocks_unauthenticated(anon_client: TestClient) -> None:
    """An unauthenticated caller cannot pull a render artifact.

    The download route is gated to any authenticated (viewer-or-above) session:
    a machine-style request (no ``text/html`` Accept) resolves to a bare 401 from
    the auth dependency, so the produced video is never served anonymously -- and
    the auth check runs before the render lookup, so it fires even for an id that
    would otherwise 404.
    """
    resp = anon_client.get(
        "/renders/1/download", headers={"Accept": "application/json"}
    )
    assert resp.status_code == 401


def test_stream_blocks_unauthenticated(anon_client: TestClient) -> None:
    """The inline-playback stream route is gated the same as download."""
    resp = anon_client.get("/renders/1/stream", headers={"Accept": "application/json"})
    assert resp.status_code == 401


def _manual_job_ids(project_id: int) -> list[int]:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        return [
            job.id
            for job in db.query(RenderJob)
            .filter(RenderJob.project_id == project_id, RenderJob.kind == "manual")
            .order_by(RenderJob.id)
        ]


def test_double_submit_queues_one_render(admin_client: TestClient) -> None:
    """#67 R14: the same submit twice queues one job; the second says so."""
    pid = _seed_project()
    csrf = csrf_of(admin_client, f"/projects/{pid}")
    form = {"csrf_token": csrf, "render_fps": "30"}

    first = admin_client.post(
        f"/projects/{pid}/renders", data=form, follow_redirects=False
    )
    second = admin_client.post(
        f"/projects/{pid}/renders", data=form, follow_redirects=False
    )

    assert first.status_code == 303
    jobs = _manual_job_ids(pid)
    assert len(jobs) == 1
    # The repeat shows the queued render's card with an "already queued" toast.
    assert second.status_code == 200
    assert f"Render #{jobs[0]} with these settings is already queued." in second.text
    assert 'class="toast info"' in second.text
    assert f"Render #{jobs[0]}" in second.text


def test_submit_with_other_settings_queues_another(admin_client: TestClient) -> None:
    pid = _seed_project()
    csrf = csrf_of(admin_client, f"/projects/{pid}")

    for fps in ("30", "24"):
        resp = admin_client.post(
            f"/projects/{pid}/renders",
            data={"csrf_token": csrf, "render_fps": fps},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    assert len(_manual_job_ids(pid)) == 2
