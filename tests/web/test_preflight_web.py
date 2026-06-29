"""Web tests for the storage pre-flight route and create-form wiring."""

from __future__ import annotations

from fastapi.testclient import TestClient

from timelapse_manager.db.models import Camera
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context


def _seed_camera() -> None:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        db.add(Camera(name="pf-cam", address="10.0.0.9", protocol="vapix"))
        db.flush()


def test_preflight_returns_banner(admin_client: TestClient) -> None:
    r = admin_client.get(
        "/projects/preflight?capture_interval_value=5&capture_interval_unit=minutes"
    )
    assert r.status_code == 200
    assert "preflight-banner" in r.text
    assert "/day" in r.text


def test_preflight_empty_for_invalid_interval(admin_client: TestClient) -> None:
    r = admin_client.get(
        "/projects/preflight?capture_interval_value=&capture_interval_unit=minutes"
    )
    assert r.status_code == 200
    assert r.text.strip() == ""


def test_preflight_requires_operator(viewer_client: TestClient) -> None:
    r = viewer_client.get(
        "/projects/preflight?capture_interval_value=5&capture_interval_unit=minutes"
    )
    assert r.status_code in (401, 403)


def test_new_project_form_has_chips_and_preflight(admin_client: TestClient) -> None:
    _seed_camera()  # the create form only renders when a camera exists
    html = admin_client.get("/projects/new").text
    assert "chip-group" in html
    assert 'data-chip-target="#capture_interval_value"' in html
    assert 'id="preflight-container"' in html
