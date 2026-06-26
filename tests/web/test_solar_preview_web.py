"""Web tests for the solar-capture preview: coordinate verification and the
upcoming capture time rendered in the camera's own local timezone.

Covers the settings-page render and the live HTMX preview endpoint, plus RBAC.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from timelapse_manager.db.models import Camera, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context

# Chicago: a clean named zone (America/Chicago) for assertions.
_CHI_LAT, _CHI_LON = 41.85, -87.65


def _seed_camera(*, name: str, lat: float | None, lon: float | None) -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(
            name=name,
            address="127.0.0.1",
            protocol="vapix",
            snapshot_uri="http://127.0.0.1/snap",
            geolocation_latitude=lat,
            geolocation_longitude=lon,
        )
        db.add(cam)
        db.flush()
        return cam.id


def _seed_project(*, name: str, camera_id: int, anchors: list | None) -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        proj = Project(
            name=name,
            camera_id=camera_id,
            capture_interval_seconds=30,
            exact_time_anchors=anchors,
        )
        db.add(proj)
        db.flush()
        return proj.id


def test_settings_page_shows_camera_zone_and_upcoming_time(
    admin_client: TestClient,
) -> None:
    cam_id = _seed_camera(name="solar-chi", lat=_CHI_LAT, lon=_CHI_LON)
    pid = _seed_project(
        name="Solar Preview Proj",
        camera_id=cam_id,
        anchors=[{"kind": "solar_noon", "offset_minutes": 0, "enabled": True}],
    )
    resp = admin_client.get(f"/projects/{pid}/settings")
    assert resp.status_code == 200
    body = resp.text
    # Coordinate verification surfaces the camera's resolved IANA zone...
    assert "America/Chicago" in body
    assert "camera" in body.lower() and "local timezone" in body.lower()
    # ...and the upcoming capture time is listed.
    assert "Upcoming solar capture" in body
    # Clause-4 proof: the time is rendered in the CAMERA's zone, not the viewer's.
    # The test viewer's timezone is UTC, so a Chicago abbreviation appearing can
    # only come from formatting the instant in the camera's coordinate-derived
    # zone. (%Z yields CDT in summer / CST in winter -- assert either.)
    assert "CDT" in body or "CST" in body


def test_live_preview_endpoint_renders_upcoming_time(
    admin_client: TestClient,
) -> None:
    cam_id = _seed_camera(name="solar-chi-live", lat=_CHI_LAT, lon=_CHI_LON)
    pid = _seed_project(name="Solar Live Proj", camera_id=cam_id, anchors=None)
    # Simulate the HTMX query string for one enabled solar row with a +30m offset.
    resp = admin_client.get(
        f"/projects/{pid}/solar-preview",
        params=[
            ("anchor_kind", "solar_noon"),
            ("anchor_offset", "30"),
            ("anchor_id", ""),
            ("anchor_enabled", "new:0"),
        ],
    )
    assert resp.status_code == 200
    body = resp.text
    assert 'id="solar-preview"' in body
    assert "America/Chicago" in body
    assert "Solar noon +30 min" in body
    assert "Upcoming solar capture" in body


def test_settings_page_flags_invalid_coordinates(admin_client: TestClient) -> None:
    cam_id = _seed_camera(name="solar-bad", lat=200.0, lon=10.0)
    pid = _seed_project(
        name="Solar Bad Proj",
        camera_id=cam_id,
        anchors=[{"kind": "solar_noon", "offset_minutes": 0, "enabled": True}],
    )
    resp = admin_client.get(f"/projects/{pid}/settings")
    assert resp.status_code == 200
    assert "Invalid camera location" in resp.text


def test_live_preview_blocked_for_viewer(viewer_client: TestClient) -> None:
    cam_id = _seed_camera(name="solar-rbac", lat=_CHI_LAT, lon=_CHI_LON)
    pid = _seed_project(name="Solar RBAC Proj", camera_id=cam_id, anchors=None)
    resp = viewer_client.get(
        f"/projects/{pid}/solar-preview",
        params=[("anchor_kind", "solar_noon"), ("anchor_enabled", "new:0")],
    )
    assert resp.status_code == 403
