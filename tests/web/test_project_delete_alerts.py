"""Deleting a project clears its open project-scoped alerts (#282).

Mirrors the camera delete (#132): the alerts would otherwise keep counting in the
"Active alerts" badge while pointing at a project that no longer exists. The
events are kept, cleared with ``alert_clear_reason="project_deleted"``.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.models import Camera, Event, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.monitoring.alerts import get_active_alerts
from timelapse_manager.runtime import get_context
from timelapse_manager.security.token import ensure_local_token


def _seed() -> tuple[int, int, int]:
    """A camera and two projects, each with open alerts; return the ids."""
    with session_scope(get_context().session_factory) as db:
        cam = Camera(name="cam", address="127.0.0.1", protocol="vapix")
        db.add(cam)
        db.flush()
        doomed = Project(camera_id=cam.id, name="doomed", capture_interval_seconds=60)
        other = Project(camera_id=cam.id, name="other", capture_interval_seconds=60)
        db.add_all([doomed, other])
        db.flush()
        for project, reason in ((doomed, "anchor_failed"), (other, "anchor_failed")):
            db.add(
                Event(
                    scope="project",
                    scope_id=project.id,
                    level="warning",
                    message=f"alert for {project.name}",
                    event_metadata={"reason": reason},
                )
            )
        db.add(
            Event(
                scope="project",
                scope_id=doomed.id,
                level="warning",
                message="second alert",
                event_metadata={"reason": "anchor_skipped"},
            )
        )
        db.add(
            Event(
                scope="camera",
                scope_id=cam.id,
                level="warning",
                message="camera alert",
                event_metadata={"reason": "camera_offline"},
            )
        )
        db.flush()
        return cam.id, doomed.id, other.id


def _active_count() -> int:
    with session_scope(get_context().session_factory) as db:
        return get_active_alerts(db)[1]


def _events_of(project_id: int) -> list[Event]:
    with session_scope(get_context().session_factory) as db:
        rows = (
            db.query(Event)
            .filter(
                Event.scope == "project",
                Event.scope_id == project_id,
                Event.level == "warning",
            )
            .all()
        )
        db.expunge_all()
        return rows


def _assert_cleared(project_id: int, other_id: int, camera_id: int) -> None:
    rows = _events_of(project_id)
    assert len(rows) == 2  # kept as history
    for row in rows:
        assert row.alert_cleared_at is not None
        assert row.alert_clear_reason == "project_deleted"
        assert row.alert_cleared_by is None
    # Another project's and the camera's alerts are untouched.
    assert all(e.alert_cleared_at is None for e in _events_of(other_id))
    with session_scope(get_context().session_factory) as db:
        cam_alert = db.query(Event).filter_by(scope="camera", scope_id=camera_id).one()
        assert cam_alert.alert_cleared_at is None
    assert _active_count() == 2  # other project's + the camera's


def test_api_delete_clears_the_projects_alerts(admin_client: TestClient) -> None:
    camera_id, project_id, other_id = _seed()
    assert _active_count() == 4
    token = ensure_local_token(get_context().settings)
    resp = admin_client.request(
        "DELETE",
        f"/api/v1/projects/{project_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "X-CSRF-Token": csrf_of(admin_client, "/"),
        },
    )
    assert resp.status_code == 204
    _assert_cleared(project_id, other_id, camera_id)


def test_web_delete_clears_the_projects_alerts(admin_client: TestClient) -> None:
    camera_id, project_id, other_id = _seed()
    resp = admin_client.post(
        f"/projects/{project_id}/delete",
        data={"csrf_token": csrf_of(admin_client, "/projects")},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert resp.status_code in (200, 204, 303)
    with session_scope(get_context().session_factory) as db:
        assert db.get(Project, project_id) is None
    _assert_cleared(project_id, other_id, camera_id)
