"""Project create/edit routes keep their database work off the event loop (#46).

The loop guard (``tests/loop_guard_support.py``) already fails any of these
requests that queries on the loop; these tests pin the other half of the rule:
the post-commit effects (the capture supervisor's reconcile wake-up) run back
*on* the event loop, never inside the threadpool helper that committed.

A running asyncio loop on the calling thread is the signal: FastAPI's threadpool
workers have none, while the TestClient's portal thread runs the app's loop.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.models import Camera, Event, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.services import _base as services_base


def _on_event_loop() -> bool:
    """Return whether the calling thread is running an asyncio event loop."""
    return asyncio._get_running_loop() is not None  # noqa: SLF001 - non-raising probe


def _seed_camera(*, name: str, address: str | None = "127.0.0.1") -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(
            name=name,
            address=address,
            protocol="vapix",
            snapshot_uri="http://127.0.0.1/snap",
        )
        db.add(cam)
        db.flush()
        return cam.id


def _seed_project(*, name: str, camera_id: int) -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        proj = Project(
            camera_id=camera_id,
            name=name,
            capture_interval_seconds=60,
            lifecycle_state="active",
        )
        db.add(proj)
        db.flush()
        return proj.id


def _form(name: str, camera_id: int, csrf: str) -> dict[str, str]:
    return {
        "name": name,
        "camera_id": str(camera_id),
        "capture_interval_value": "90",
        "capture_interval_unit": "seconds",
        "csrf_token": csrf,
    }


_FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}


class TestEditProjectEffectsOnLoop:
    def test_run_effects_runs_on_the_event_loop(self, admin_client: TestClient) -> None:
        cam = _seed_camera(name="offloop-edit-cam")
        pid = _seed_project(name="Off Loop Edit", camera_id=cam)
        csrf = csrf_of(admin_client, "/renders")
        calls: list[bool] = []

        def recording_run_effects(effects: Any, ctx: Any) -> None:
            calls.append(_on_event_loop())
            services_base.run_effects(effects, ctx)

        with patch(
            "timelapse_manager.web.routers.projects.run_effects",
            side_effect=recording_run_effects,
        ):
            resp = admin_client.post(
                f"/projects/{pid}/settings",
                data=_form("Off Loop Edit 2", cam, csrf),
                headers=_FORM_HEADERS,
                follow_redirects=False,
            )

        assert resp.status_code == 303
        assert resp.headers["location"] == f"/projects/{pid}"
        assert calls == [True]

    def test_duplicate_name_rerenders_after_rollback(
        self, admin_client: TestClient
    ) -> None:
        # The service rejects the rename inside the threadpool helper; the 400
        # re-render that follows runs in its own threadpool call.
        cam = _seed_camera(name="offloop-dup-cam")
        _seed_project(name="Off Loop Taken", camera_id=cam)
        pid = _seed_project(name="Off Loop Mine", camera_id=cam)
        csrf = csrf_of(admin_client, "/renders")
        resp = admin_client.post(
            f"/projects/{pid}/settings",
            data=_form("Off Loop Taken", cam, csrf),
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "already exists" in resp.text


class TestCreateProjectNotifyOnLoop:
    def test_supervisor_is_notified_on_the_event_loop(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera(name="offloop-create-cam")
        csrf = csrf_of(admin_client, "/renders")
        ctx = get_context()
        previous = ctx.capture_supervisor
        calls: list[bool] = []
        mock_supervisor = MagicMock()
        mock_supervisor.notify_reconcile.side_effect = lambda: calls.append(
            _on_event_loop()
        )
        ctx.capture_supervisor = mock_supervisor
        try:
            resp = admin_client.post(
                "/projects",
                data=_form("Off Loop Create", cam, csrf),
                headers=_FORM_HEADERS,
                follow_redirects=False,
            )
        finally:
            ctx.capture_supervisor = previous

        assert resp.status_code == 303
        assert calls == [True]
        with session_scope(ctx.session_factory) as db:
            project = db.query(Project).filter(Project.name == "Off Loop Create").one()
            assert resp.headers["location"] == f"/projects/{project.id}"
            audit = (
                db.query(Event)
                .filter(Event.scope == "project", Event.scope_id == project.id)
                .all()
            )
            assert [e.message for e in audit] == [f"project {project.id} created"]


class TestSettingsFormWithSupervisor:
    def test_camera_probes_resolve_credentials_off_loop(
        self, admin_client: TestClient
    ) -> None:
        # With a supervisor present the stream/PTZ/event probes get as far as the
        # default-credential lookup (an address-less camera skips the SSRF
        # resolve), which must run in the threadpool -- the loop guard fails the
        # request otherwise. The mock HTTP client makes each probe fail soft.
        cam = _seed_camera(name="offloop-probe-cam", address=None)
        pid = _seed_project(name="Off Loop Probe", camera_id=cam)
        ctx = get_context()
        previous = ctx.capture_supervisor
        ctx.capture_supervisor = MagicMock()
        try:
            page = admin_client.get(f"/projects/{pid}/settings")
            fragment = admin_client.get(f"/projects/{pid}/settings/form")
        finally:
            ctx.capture_supervisor = previous

        assert page.status_code == 200
        assert fragment.status_code == 200
        assert "Off Loop Probe" in page.text
        assert "Off Loop Probe" in fragment.text

    def test_missing_project_is_404(self, admin_client: TestClient) -> None:
        assert admin_client.get("/projects/999999/settings").status_code == 404
        assert admin_client.get("/projects/999999/settings/form").status_code == 404
