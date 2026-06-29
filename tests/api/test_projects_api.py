"""Integration tests for the /api/v1/projects create endpoint.

Uses the migrated_client fixture (fully-migrated DB, autostart=False) and
cam_auth_token for bearer authentication. Covers the happy path, validation
errors, and the supervisor-notify seam that makes a created project start
capturing without a restart.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from timelapse_manager.config.settings import Settings
from timelapse_manager.db.engine import create_db_engine
from timelapse_manager.db.models import Camera, Project
from timelapse_manager.db.session import create_session_factory, session_scope
from timelapse_manager.runtime import get_context

API = "/api/v1/projects"
CAMERAS = "/api/v1/cameras"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_camera(settings: Settings, *, protocol: str | None) -> int:
    """Directly insert a camera row and return its id."""
    engine = create_db_engine(settings.database.url)
    factory = create_session_factory(engine)
    try:
        with session_scope(factory) as session:
            cam = Camera(
                name=f"api-proj-cam-{protocol}",
                address="127.0.0.1",
                protocol=protocol,
                snapshot_uri="http://127.0.0.1/snap",
            )
            session.add(cam)
            session.flush()
            return cam.id
    finally:
        engine.dispose()


class TestCreateProjectAuth:
    def test_create_requires_auth(self, migrated_client: TestClient) -> None:
        resp = migrated_client.post(API, json={"name": "x", "camera_id": 1})
        assert resp.status_code == 401


class TestCreateProject:
    def test_valid_create_returns_201(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        resp = migrated_client.post(
            API,
            json={
                "name": "API Lapse",
                "camera_id": camera_id,
                "capture_interval_seconds": 90,
            },
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["name"] == "API Lapse"
        assert body["camera_id"] == camera_id
        assert body["capture_interval_seconds"] == 90
        assert body["lifecycle_state"] == "active"
        assert isinstance(body["id"], int)

    def test_create_notifies_supervisor(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        # The notify seam is what makes create -> capture-without-restart work.
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        ctx = get_context()
        previous = ctx.capture_supervisor
        mock_supervisor = MagicMock()
        ctx.capture_supervisor = mock_supervisor
        try:
            resp = migrated_client.post(
                API,
                json={
                    "name": "Notify Lapse",
                    "camera_id": camera_id,
                    "capture_interval_seconds": 60,
                },
                headers=_auth(cam_auth_token),
            )
            assert resp.status_code == 201, resp.text
            mock_supervisor.notify_reconcile.assert_called_once_with()
        finally:
            ctx.capture_supervisor = previous

    def test_duplicate_name_returns_409(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        payload = {
            "name": "Dupe API",
            "camera_id": camera_id,
            "capture_interval_seconds": 60,
        }
        first = migrated_client.post(API, json=payload, headers=_auth(cam_auth_token))
        assert first.status_code == 201, first.text
        second = migrated_client.post(API, json=payload, headers=_auth(cam_auth_token))
        assert second.status_code == 409

    def test_missing_camera_returns_404(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        resp = migrated_client.post(
            API,
            json={
                "name": "Ghost Camera",
                "camera_id": 999999,
                "capture_interval_seconds": 60,
            },
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 404

    def test_camera_without_protocol_returns_422(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart, protocol=None)
        resp = migrated_client.post(
            API,
            json={
                "name": "No Protocol API",
                "camera_id": camera_id,
                "capture_interval_seconds": 60,
            },
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 422

    def test_non_positive_interval_returns_422(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        resp = migrated_client.post(
            API,
            json={
                "name": "Zero Interval API",
                "camera_id": camera_id,
                "capture_interval_seconds": 0,
            },
            headers=_auth(cam_auth_token),
        )
        # Pydantic Field(ge=1) rejects this before the handler runs.
        assert resp.status_code == 422


def _create_project(
    client: TestClient, token: str, *, name: str, camera_id: int, interval: int = 60
) -> int:
    """Create a project via the API and return its id."""
    resp = client.post(
        API,
        json={
            "name": name,
            "camera_id": camera_id,
            "capture_interval_seconds": interval,
        },
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


class TestUpdateProject:
    def test_patch_updates_fields_and_persists(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        project_id = _create_project(
            migrated_client, cam_auth_token, name="Patch Me", camera_id=camera_id
        )
        resp = migrated_client.patch(
            f"{API}/{project_id}",
            json={"capture_interval_seconds": 300, "name": "Patched"},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["name"] == "Patched"
        assert body["capture_interval_seconds"] == 300

    def test_patch_storage_path_persists_and_omission_preserves(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        # A PATCH that sets storage_path persists it; a later PATCH that omits
        # storage_path must leave it intact (exclude_unset semantics).
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        project_id = _create_project(
            migrated_client, cam_auth_token, name="Patch Storage", camera_id=camera_id
        )
        engine = create_db_engine(settings_no_autostart.database.url)
        factory = create_session_factory(engine)
        try:
            set_resp = migrated_client.patch(
                f"{API}/{project_id}",
                json={"storage_path": "/data/custom-frames"},
                headers=_auth(cam_auth_token),
            )
            assert set_resp.status_code == 200, set_resp.text
            with session_scope(factory) as db:
                proj = db.get(Project, project_id)
                assert proj is not None
                assert proj.storage_path == "/data/custom-frames"

            # Omit storage_path on the next PATCH; it must survive.
            omit_resp = migrated_client.patch(
                f"{API}/{project_id}",
                json={"capture_interval_seconds": 200},
                headers=_auth(cam_auth_token),
            )
            assert omit_resp.status_code == 200, omit_resp.text
            with session_scope(factory) as db:
                proj = db.get(Project, project_id)
                assert proj is not None
                assert proj.storage_path == "/data/custom-frames"
                assert proj.capture_interval_seconds == 200
        finally:
            engine.dispose()

    def test_patch_sets_and_returns_schedules(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        # A PATCH that sets render_schedule and archive_schedule persists both and
        # ProjectOut reflects them back so a client can read its configuration.
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        project_id = _create_project(
            migrated_client, cam_auth_token, name="Sched Set", camera_id=camera_id
        )
        render_schedule = {"enabled": True, "interval_seconds": 3600}
        archive_schedule = {"enabled": True, "interval_seconds": 86400}
        resp = migrated_client.patch(
            f"{API}/{project_id}",
            json={
                "render_schedule": render_schedule,
                "archive_schedule": archive_schedule,
            },
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["render_schedule"] == render_schedule
        assert body["archive_schedule"] == archive_schedule

    def test_patch_enabled_schedule_without_interval_returns_422(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        project_id = _create_project(
            migrated_client, cam_auth_token, name="Sched No Int", camera_id=camera_id
        )
        resp = migrated_client.patch(
            f"{API}/{project_id}",
            json={"render_schedule": {"enabled": True}},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 422, resp.text

    def test_patch_enabled_schedule_with_invalid_interval_returns_422(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        project_id = _create_project(
            migrated_client, cam_auth_token, name="Sched Bad Int", camera_id=camera_id
        )
        resp = migrated_client.patch(
            f"{API}/{project_id}",
            json={"archive_schedule": {"enabled": True, "interval_seconds": 0}},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 422, resp.text

    def test_patch_disabled_schedule_is_accepted(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        # A disabled schedule (off) needs no interval and is accepted as-is.
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        project_id = _create_project(
            migrated_client, cam_auth_token, name="Sched Off", camera_id=camera_id
        )
        schedule = {"enabled": False}
        resp = migrated_client.patch(
            f"{API}/{project_id}",
            json={"render_schedule": schedule},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["render_schedule"] == schedule

    def test_patch_schedule_omission_preserves_and_null_clears(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        # Setting a schedule, then PATCHing without it must preserve it
        # (exclude_unset); an explicit null clears it back to off.
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        project_id = _create_project(
            migrated_client, cam_auth_token, name="Sched Lifecycle", camera_id=camera_id
        )
        schedule = {"enabled": True, "interval_seconds": 3600}
        set_resp = migrated_client.patch(
            f"{API}/{project_id}",
            json={"render_schedule": schedule},
            headers=_auth(cam_auth_token),
        )
        assert set_resp.status_code == 200, set_resp.text

        # Omit render_schedule: it must survive.
        omit_resp = migrated_client.patch(
            f"{API}/{project_id}",
            json={"capture_interval_seconds": 200},
            headers=_auth(cam_auth_token),
        )
        assert omit_resp.status_code == 200, omit_resp.text
        assert omit_resp.json()["render_schedule"] == schedule

        # Explicit null clears it.
        clear_resp = migrated_client.patch(
            f"{API}/{project_id}",
            json={"render_schedule": None},
            headers=_auth(cam_auth_token),
        )
        assert clear_resp.status_code == 200, clear_resp.text
        assert clear_resp.json()["render_schedule"] is None

    def test_patch_sets_and_returns_post_render_actions(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        # A PATCH that sets post_render_actions persists the list and ProjectOut
        # reflects it back so a client can read its configuration.
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        project_id = _create_project(
            migrated_client, cam_auth_token, name="PRA Set", camera_id=camera_id
        )
        actions = [
            {"type": "export", "destination": "/exports"},
            {"type": "prune", "keep": 5},
        ]
        resp = migrated_client.patch(
            f"{API}/{project_id}",
            json={"post_render_actions": actions},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["post_render_actions"] == actions

    def test_patch_post_render_actions_non_list_returns_422(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        # The value must be a list of action objects; a dict or string is 422.
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        project_id = _create_project(
            migrated_client, cam_auth_token, name="PRA NonList", camera_id=camera_id
        )
        for bad in ({"type": "export"}, "export"):
            resp = migrated_client.patch(
                f"{API}/{project_id}",
                json={"post_render_actions": bad},
                headers=_auth(cam_auth_token),
            )
            assert resp.status_code == 422, resp.text

    def test_patch_post_render_action_missing_type_returns_422(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        # Each element must carry a non-empty string type.
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        project_id = _create_project(
            migrated_client, cam_auth_token, name="PRA NoType", camera_id=camera_id
        )
        resp = migrated_client.patch(
            f"{API}/{project_id}",
            json={"post_render_actions": [{"destination": "/exports"}]},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 422, resp.text

    def test_patch_post_render_actions_omission_preserves_and_null_clears(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        # Setting actions, then PATCHing without them must preserve them
        # (exclude_unset); an explicit null clears them back to none.
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        project_id = _create_project(
            migrated_client, cam_auth_token, name="PRA Lifecycle", camera_id=camera_id
        )
        actions = [{"type": "export", "destination": "/exports"}]
        set_resp = migrated_client.patch(
            f"{API}/{project_id}",
            json={"post_render_actions": actions},
            headers=_auth(cam_auth_token),
        )
        assert set_resp.status_code == 200, set_resp.text

        # Omit post_render_actions: it must survive.
        omit_resp = migrated_client.patch(
            f"{API}/{project_id}",
            json={"capture_interval_seconds": 200},
            headers=_auth(cam_auth_token),
        )
        assert omit_resp.status_code == 200, omit_resp.text
        assert omit_resp.json()["post_render_actions"] == actions

        # Explicit null clears it.
        clear_resp = migrated_client.patch(
            f"{API}/{project_id}",
            json={"post_render_actions": None},
            headers=_auth(cam_auth_token),
        )
        assert clear_resp.status_code == 200, clear_resp.text
        assert clear_resp.json()["post_render_actions"] is None

    def test_patch_notifies_supervisor(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        project_id = _create_project(
            migrated_client, cam_auth_token, name="Patch Notify", camera_id=camera_id
        )
        ctx = get_context()
        previous = ctx.capture_supervisor
        mock_supervisor = MagicMock()
        ctx.capture_supervisor = mock_supervisor
        try:
            resp = migrated_client.patch(
                f"{API}/{project_id}",
                json={"capture_interval_seconds": 120},
                headers=_auth(cam_auth_token),
            )
            assert resp.status_code == 200, resp.text
            mock_supervisor.notify_reconcile.assert_called_once_with()
        finally:
            ctx.capture_supervisor = previous

    def test_patch_duplicate_name_returns_409(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        _create_project(
            migrated_client, cam_auth_token, name="Taken Name", camera_id=camera_id
        )
        other = _create_project(
            migrated_client, cam_auth_token, name="Other Name", camera_id=camera_id
        )
        resp = migrated_client.patch(
            f"{API}/{other}",
            json={"name": "Taken Name"},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 409

    def test_patch_same_name_is_allowed(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        # Saving the project's own unchanged name must not false-positive as a dup.
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        project_id = _create_project(
            migrated_client, cam_auth_token, name="Keep Name", camera_id=camera_id
        )
        resp = migrated_client.patch(
            f"{API}/{project_id}",
            json={"name": "Keep Name", "capture_interval_seconds": 45},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["capture_interval_seconds"] == 45

    def test_patch_missing_camera_returns_404(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        project_id = _create_project(
            migrated_client, cam_auth_token, name="Patch Cam", camera_id=camera_id
        )
        resp = migrated_client.patch(
            f"{API}/{project_id}",
            json={"camera_id": 999999},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 404

    def test_patch_unknown_project_returns_404(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        resp = migrated_client.patch(
            f"{API}/999999",
            json={"capture_interval_seconds": 60},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 404


class TestDeleteProject:
    def test_delete_returns_204_and_removes_project(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        project_id = _create_project(
            migrated_client, cam_auth_token, name="Delete Me", camera_id=camera_id
        )
        resp = migrated_client.delete(
            f"{API}/{project_id}", headers=_auth(cam_auth_token)
        )
        assert resp.status_code == 204
        follow = migrated_client.patch(
            f"{API}/{project_id}",
            json={"capture_interval_seconds": 60},
            headers=_auth(cam_auth_token),
        )
        assert follow.status_code == 404

    def test_delete_notifies_supervisor(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        project_id = _create_project(
            migrated_client, cam_auth_token, name="Delete Notify", camera_id=camera_id
        )
        ctx = get_context()
        previous = ctx.capture_supervisor
        mock_supervisor = MagicMock()
        ctx.capture_supervisor = mock_supervisor
        try:
            resp = migrated_client.delete(
                f"{API}/{project_id}", headers=_auth(cam_auth_token)
            )
            assert resp.status_code == 204
            mock_supervisor.notify_reconcile.assert_called_once_with()
        finally:
            ctx.capture_supervisor = previous

    def test_delete_unknown_project_returns_404(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        resp = migrated_client.delete(f"{API}/999999", headers=_auth(cam_auth_token))
        assert resp.status_code == 404


class TestCloneProject:
    def test_clone_copies_config_with_zero_frames(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        source = _create_project(
            migrated_client,
            cam_auth_token,
            name="Source Lapse",
            camera_id=camera_id,
            interval=120,
        )
        resp = migrated_client.post(
            f"{API}/{source}/clone",
            json={"name": "Cloned Lapse"},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["name"] == "Cloned Lapse"
        assert body["camera_id"] == camera_id
        assert body["capture_interval_seconds"] == 120
        assert body["lifecycle_state"] == "active"
        assert body["frame_count"] == 0
        assert body["id"] != source

    def test_clone_duplicate_name_returns_409(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        source = _create_project(
            migrated_client, cam_auth_token, name="Clone Src", camera_id=camera_id
        )
        _create_project(
            migrated_client, cam_auth_token, name="Clone Dest", camera_id=camera_id
        )
        resp = migrated_client.post(
            f"{API}/{source}/clone",
            json={"name": "Clone Dest"},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 409

    def test_clone_unknown_source_returns_404(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        resp = migrated_client.post(
            f"{API}/999999/clone",
            json={"name": "Orphan Clone"},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 404

    def test_clone_notifies_supervisor(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        source = _create_project(
            migrated_client,
            cam_auth_token,
            name="Clone Notify Src",
            camera_id=camera_id,
        )
        ctx = get_context()
        previous = ctx.capture_supervisor
        mock_supervisor = MagicMock()
        ctx.capture_supervisor = mock_supervisor
        try:
            resp = migrated_client.post(
                f"{API}/{source}/clone",
                json={"name": "Clone Notify Dest"},
                headers=_auth(cam_auth_token),
            )
            assert resp.status_code == 201, resp.text
            mock_supervisor.notify_reconcile.assert_called_once_with()
        finally:
            ctx.capture_supervisor = previous


def _lifecycle_state(settings: Settings, project_id: int) -> str:
    """Read a project's persisted lifecycle_state directly from the DB."""
    engine = create_db_engine(settings.database.url)
    factory = create_session_factory(engine)
    try:
        with session_scope(factory) as session:
            return session.get(Project, project_id).lifecycle_state
    finally:
        engine.dispose()


class TestPauseResume:
    def test_pause_returns_paused_state(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        pid = _create_project(
            migrated_client, cam_auth_token, name="Pause Me", camera_id=camera_id
        )
        resp = migrated_client.post(f"{API}/{pid}/pause", headers=_auth(cam_auth_token))
        assert resp.status_code == 200, resp.text
        assert resp.json()["lifecycle_state"] == "paused"
        assert _lifecycle_state(settings_no_autostart, pid) == "paused"

    def test_pause_is_idempotent(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        # Pausing an already-paused project is a no-op success, not a conflict.
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        pid = _create_project(
            migrated_client, cam_auth_token, name="Pause Twice", camera_id=camera_id
        )
        first = migrated_client.post(
            f"{API}/{pid}/pause", headers=_auth(cam_auth_token)
        )
        assert first.status_code == 200, first.text
        second = migrated_client.post(
            f"{API}/{pid}/pause", headers=_auth(cam_auth_token)
        )
        assert second.status_code == 200, second.text
        assert second.json()["lifecycle_state"] == "paused"

    def test_pause_notifies_supervisor(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        pid = _create_project(
            migrated_client, cam_auth_token, name="Pause Notify", camera_id=camera_id
        )
        ctx = get_context()
        previous = ctx.capture_supervisor
        mock_supervisor = MagicMock()
        ctx.capture_supervisor = mock_supervisor
        try:
            resp = migrated_client.post(
                f"{API}/{pid}/pause", headers=_auth(cam_auth_token)
            )
            assert resp.status_code == 200, resp.text
            mock_supervisor.notify_reconcile.assert_called_once_with()
        finally:
            ctx.capture_supervisor = previous

    def test_resume_from_paused_returns_active(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        pid = _create_project(
            migrated_client, cam_auth_token, name="Resume Me", camera_id=camera_id
        )
        migrated_client.post(f"{API}/{pid}/pause", headers=_auth(cam_auth_token))
        resp = migrated_client.post(
            f"{API}/{pid}/resume", headers=_auth(cam_auth_token)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["lifecycle_state"] == "active"
        assert _lifecycle_state(settings_no_autostart, pid) == "active"

    def test_resume_notifies_supervisor(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        pid = _create_project(
            migrated_client, cam_auth_token, name="Resume Notify", camera_id=camera_id
        )
        migrated_client.post(f"{API}/{pid}/pause", headers=_auth(cam_auth_token))
        ctx = get_context()
        previous = ctx.capture_supervisor
        mock_supervisor = MagicMock()
        ctx.capture_supervisor = mock_supervisor
        try:
            resp = migrated_client.post(
                f"{API}/{pid}/resume", headers=_auth(cam_auth_token)
            )
            assert resp.status_code == 200, resp.text
            mock_supervisor.notify_reconcile.assert_called_once_with()
        finally:
            ctx.capture_supervisor = previous

    def test_resume_on_active_project_returns_409(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        # Resume is only valid from paused: an active project is a 409 so a client
        # cannot mistake "already running" for a successful resume.
        camera_id = _seed_camera(settings_no_autostart, protocol="vapix")
        pid = _create_project(
            migrated_client, cam_auth_token, name="Resume Active", camera_id=camera_id
        )
        resp = migrated_client.post(
            f"{API}/{pid}/resume", headers=_auth(cam_auth_token)
        )
        assert resp.status_code == 409
        assert _lifecycle_state(settings_no_autostart, pid) == "active"

    def test_pause_unknown_project_returns_404(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        resp = migrated_client.post(
            f"{API}/999999/pause", headers=_auth(cam_auth_token)
        )
        assert resp.status_code == 404

    def test_pause_requires_auth(self, migrated_client: TestClient) -> None:
        resp = migrated_client.post(f"{API}/1/pause")
        assert resp.status_code == 401
