"""Async project routes read plain values, not expired ORM rows (#66 L3).

Each test wraps the route's first threadpool hop so the session is expired right
after it returns. A route that still read an attribute of the camera or project
row on the event loop would then issue a refresh ``SELECT`` there, which the loop
guard records and fails the test on.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.models import Camera, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.web.routers import dashboard as dashboard_router
from timelapse_manager.web.routers import projects as projects_router

_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}


def _expiring(real: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a sync hop helper so its session is expired after it returns."""

    def wrapper(db: Any, *args: Any, **kwargs: Any) -> Any:
        result = real(db, *args, **kwargs)
        db.expire_all()
        return result

    return wrapper


def _seed(*, solar_geo: bool = False) -> tuple[int, int]:
    with session_scope(get_context().session_factory) as db:
        camera = Camera(
            name="pv-cam",
            address="127.0.0.1",
            protocol="vapix",
            snapshot_uri="http://127.0.0.1/snap",
            geolocation_latitude=40.0 if solar_geo else None,
            geolocation_longitude=-75.0 if solar_geo else None,
        )
        db.add(camera)
        db.flush()
        project = Project(
            camera_id=camera.id,
            name="pv-project",
            capture_interval_seconds=60,
            lifecycle_state="active",
        )
        db.add(project)
        db.flush()
        return camera.id, project.id


def _form(name: str, camera_id: int, csrf: str, **extra: str) -> dict[str, str]:
    return {
        "name": name,
        "camera_id": str(camera_id),
        "capture_interval_value": "90",
        "capture_interval_unit": "seconds",
        "csrf_token": csrf,
        **extra,
    }


def test_edit_project_survives_expiry_after_each_hop(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    camera_id, project_id = _seed(solar_geo=True)
    monkeypatch.setattr(
        projects_router,
        "load_camera_with_facts",
        _expiring(projects_router.load_camera_with_facts),
    )
    monkeypatch.setattr(
        projects_router,
        "_stored_event_triggers",
        _expiring(projects_router._stored_event_triggers),
    )
    csrf = csrf_of(admin_client, "/renders")
    resp = admin_client.post(
        f"/projects/{project_id}/settings",
        data=_form(
            "pv-renamed",
            camera_id,
            csrf,
            anchor_kind="solar_noon",
            anchor_time="",
            anchor_offset="0",
            anchor_id="",
            anchor_enabled="on",
            exact_time_marker="1",
        ),
        headers=_HEADERS,
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


def test_edit_project_missing_protocol_reads_plain_facts(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    camera_id, project_id = _seed()
    with session_scope(get_context().session_factory) as db:
        db.get(Camera, camera_id).protocol = None  # type: ignore[union-attr]
    monkeypatch.setattr(
        projects_router,
        "load_camera_with_facts",
        _expiring(projects_router.load_camera_with_facts),
    )
    csrf = csrf_of(admin_client, "/renders")
    resp = admin_client.post(
        f"/projects/{project_id}/settings",
        data=_form("pv-x", camera_id, csrf),
        headers=_HEADERS,
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "no protocol configured" in resp.text


def test_create_project_survives_expiry_after_the_camera_hop(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    camera_id, _ = _seed()
    monkeypatch.setattr(
        dashboard_router,
        "load_camera_with_facts",
        _expiring(dashboard_router.load_camera_with_facts),
    )
    csrf = csrf_of(admin_client, "/renders")
    resp = admin_client.post(
        "/projects",
        data=_form("pv-created", camera_id, csrf),
        headers=_HEADERS,
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
