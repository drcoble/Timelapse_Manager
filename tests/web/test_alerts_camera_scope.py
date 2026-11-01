"""The alerts panel names the camera of a camera-scoped alert (#69, CAM-R6).

Camera offline alerts are camera-scoped since 0.2.0, so the panel shows the
camera's name with a link to its row on the Cameras page, and a project alert
names and links its project. An entity that no longer exists, and a system
alert, keep the plain ``<scope> #<id>`` text.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from timelapse_manager.db.models import Camera, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.monitoring import log_event
from timelapse_manager.runtime import get_context


def _seed_camera_and_project(camera_name: str, project_name: str) -> tuple[int, int]:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(name=camera_name, address="10.0.16.71", protocol="vapix")
        db.add(cam)
        db.flush()
        proj = Project(
            camera_id=cam.id,
            name=project_name,
            lifecycle_state="active",
            operational_status="idle",
        )
        db.add(proj)
        db.flush()
        return cam.id, proj.id


def _alert(scope: str, scope_id: int | None, message: str) -> None:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        log_event(db, scope=scope, scope_id=scope_id, level="warning", message=message)


class TestAlertScopeLabels:
    def test_camera_alert_names_and_links_the_camera(
        self, admin_client: TestClient
    ) -> None:
        camera_id, _ = _seed_camera_and_project("Pano<1>", "noon")
        _alert("camera", camera_id, "camera 'Pano<1>' is offline (projects: noon)")

        resp = admin_client.get("/alerts/summary", follow_redirects=False)

        assert resp.status_code == 200
        assert (
            f'<a href="/cameras#camera-row-{camera_id}">camera Pano&lt;1&gt;</a>'
            in resp.text
        )
        assert f"camera #{camera_id}" not in resp.text

    def test_project_alert_names_and_links_the_project(
        self, admin_client: TestClient
    ) -> None:
        _, project_id = _seed_camera_and_project("Porch", "door-noon")
        _alert("project", project_id, "exact-time anchor failed")

        resp = admin_client.get("/alerts/summary", follow_redirects=False)

        assert f'<a href="/projects/{project_id}">project door-noon</a>' in resp.text

    def test_missing_entities_and_system_alerts_fall_back_to_plain_text(
        self, admin_client: TestClient
    ) -> None:
        _alert("camera", 98765, "camera gone")
        _alert("system", None, "capture on camera failed for a local reason")

        resp = admin_client.get("/alerts/summary", follow_redirects=False)

        assert "camera #98765" in resp.text
        assert 'href="/cameras#camera-row-98765"' not in resp.text
        assert re.search(r'class="alert-desc">\s*system\s*&middot;', resp.text)

    def test_names_are_read_in_at_most_two_queries(
        self, admin_client: TestClient
    ) -> None:
        from sqlalchemy import event

        ids = [_seed_camera_and_project(f"cam{i}", f"proj{i}") for i in range(4)]
        for camera_id, project_id in ids:
            _alert("camera", camera_id, f"cam {camera_id} offline")
            _alert("project", project_id, f"proj {project_id} alert")

        statements: list[str] = []

        def record(conn, cursor, statement, *_args) -> None:  # noqa: ANN001
            statements.append(statement)

        engine = get_context().session_factory.kw["bind"]
        event.listen(engine, "before_cursor_execute", record)
        try:
            resp = admin_client.get("/alerts/summary", follow_redirects=False)
        finally:
            event.remove(engine, "before_cursor_execute", record)

        assert resp.status_code == 200
        name_reads = [
            s
            for s in statements
            if s.lstrip().upper().startswith("SELECT")
            and ("camera.name" in s or "project.name" in s)
            and "event" not in s
        ]
        assert len(name_reads) == 2
