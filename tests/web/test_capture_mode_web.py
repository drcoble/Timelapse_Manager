"""Web tests for the capture-mode toggle (continuous interval vs solar /
scheduled-times only) and the sunrise/sunset solar anchors.

Solar mode stores a null capture interval -- the runner treats that as
anchor-only -- and requires at least one enabled capture time.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.models import Camera, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context

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


def _project_named(name: str) -> Project | None:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        return db.query(Project).filter(Project.name == name).one_or_none()


def _create(client: TestClient, *, name: str, camera_id: int, fields: dict) -> object:
    csrf = csrf_of(client, "/projects/new")
    data = {"name": name, "camera_id": str(camera_id), "csrf_token": csrf, **fields}
    return client.post(
        "/projects",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )


def test_solar_mode_creates_project_with_null_interval(
    admin_client: TestClient,
) -> None:
    cam_id = _seed_camera(name="cm-solar", lat=_CHI_LAT, lon=_CHI_LON)
    resp = _create(
        admin_client,
        name="Solar Mode Proj",
        camera_id=cam_id,
        fields={
            "capture_mode": "solar",
            # one enabled solar-noon anchor
            "exact_time_present": "1",
            "anchor_kind": "solar_noon",
            "anchor_id": "",
            "anchor_offset": "",
            "anchor_enabled": "new:0",
        },
    )
    assert resp.status_code == 303, resp.text
    proj = _project_named("Solar Mode Proj")
    assert proj is not None
    # Solar mode => no recurring interval (anchor-only).
    assert proj.capture_interval_seconds is None
    assert proj.exact_time_anchors
    assert proj.exact_time_anchors[0]["kind"] == "solar_noon"


def test_solar_mode_requires_an_anchor(admin_client: TestClient) -> None:
    cam_id = _seed_camera(name="cm-solar-empty", lat=_CHI_LAT, lon=_CHI_LON)
    resp = _create(
        admin_client,
        name="Solar No Anchor Proj",
        camera_id=cam_id,
        fields={
            "capture_mode": "solar",
            "exact_time_present": "1",  # fieldset present but no rows
        },
    )
    assert resp.status_code == 400
    assert "at least one enabled capture time" in resp.text
    assert _project_named("Solar No Anchor Proj") is None
    # The error re-render must KEEP the solar choice, not silently flip the form
    # back to interval mode (which would let a "fix + resubmit" create an
    # interval project the user never asked for).
    assert re.search(r'value="solar"[^>]*\bchecked\b', resp.text, re.DOTALL)


def test_interval_mode_still_requires_interval(admin_client: TestClient) -> None:
    cam_id = _seed_camera(name="cm-interval", lat=None, lon=None)
    # capture_mode interval (default) with no interval value -> validation error.
    resp = _create(
        admin_client,
        name="Interval No Value Proj",
        camera_id=cam_id,
        fields={"capture_mode": "interval"},
    )
    assert resp.status_code == 400
    assert _project_named("Interval No Value Proj") is None


def test_edit_switches_interval_project_to_solar(admin_client: TestClient) -> None:
    cam_id = _seed_camera(name="cm-switch", lat=_CHI_LAT, lon=_CHI_LON)
    # Start as an interval project.
    resp = _create(
        admin_client,
        name="Switch Proj",
        camera_id=cam_id,
        fields={"capture_interval_value": "30", "capture_interval_unit": "seconds"},
    )
    assert resp.status_code == 303, resp.text
    proj = _project_named("Switch Proj")
    assert proj is not None and proj.capture_interval_seconds == 30
    pid = proj.id

    # Edit to solar mode with a sunrise anchor.
    csrf = csrf_of(admin_client, f"/projects/{pid}/settings")
    resp = admin_client.post(
        f"/projects/{pid}/settings",
        data={
            "name": "Switch Proj",
            "camera_id": str(cam_id),
            "csrf_token": csrf,
            "capture_mode": "solar",
            "exact_time_present": "1",
            "anchor_kind": "sunrise",
            "anchor_id": "",
            "anchor_offset": "",
            "anchor_enabled": "new:0",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    updated = _project_named("Switch Proj")
    assert updated is not None
    assert updated.capture_interval_seconds is None  # now anchor-only
    assert updated.exact_time_anchors[0]["kind"] == "sunrise"


def test_settings_page_reflects_solar_mode_and_sun_events(
    admin_client: TestClient,
) -> None:
    cam_id = _seed_camera(name="cm-render", lat=_CHI_LAT, lon=_CHI_LON)
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        proj = Project(
            name="Sun Events Proj",
            camera_id=cam_id,
            capture_interval_seconds=None,  # solar mode
            exact_time_anchors=[
                {"kind": "sunrise", "offset_minutes": 0, "enabled": True},
                {"kind": "sunset", "offset_minutes": 0, "enabled": True},
            ],
        )
        db.add(proj)
        db.flush()
        pid = proj.id

    body = admin_client.get(f"/projects/{pid}/settings").text
    # Solar radio is selected.
    assert 'value="solar"' in body
    # Sunrise & sunset options exist and the preview lists their upcoming times.
    assert ">Sunrise<" in body and ">Sunset<" in body
    assert "Sunrise" in body and "Sunset" in body
    assert "America/Chicago" in body
    assert ("CDT" in body) or ("CST" in body)  # camera-zone abbreviation rendered
