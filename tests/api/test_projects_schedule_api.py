"""API tests for capture-schedule exposure on the /api/v1/projects endpoints.

Covers:
  - Create with a valid schedule: stored in DB (ProjectOut omits the field,
    so DB is the only way to verify)
  - Create with an invalid schedule: 422
  - PATCH update with a valid schedule: persisted
  - PATCH update with an invalid schedule: 422
  - Schedule is not cleared by PATCH requests that omit it

The ``ProjectOut`` response schema does NOT include the capture ``schedule``
column, so all persistence assertions go through a direct DB read.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from timelapse_manager.config.settings import Settings
from timelapse_manager.db.engine import create_db_engine
from timelapse_manager.db.models import Camera, Project
from timelapse_manager.db.session import create_session_factory, session_scope

API = "/api/v1/projects"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_camera(settings: Settings, *, name: str = "sched-api-cam") -> int:
    """Insert a camera row directly and return its id."""
    engine = create_db_engine(settings.database.url)
    factory = create_session_factory(engine)
    try:
        with session_scope(factory) as session:
            cam = Camera(
                name=name,
                address="127.0.0.1",
                protocol="vapix",
                snapshot_uri="http://127.0.0.1/snap",
            )
            session.add(cam)
            session.flush()
            return cam.id
    finally:
        engine.dispose()


def _read_project_schedule(settings: Settings, project_id: int) -> dict | None:
    """Read the stored capture schedule for ``project_id`` directly from the DB."""
    engine = create_db_engine(settings.database.url)
    factory = create_session_factory(engine)
    try:
        with session_scope(factory) as session:
            project = session.get(Project, project_id)
            return project.schedule if project is not None else None
    finally:
        engine.dispose()


# Valid capture schedules: each must parse without error.
_VALID_SCHEDULES = [
    {"enabled": True, "timezone": "UTC"},
    {
        "enabled": True,
        "timezone": "America/New_York",
        "windows": [{"start_time": "09:00", "end_time": "17:00"}],
        "day_of_week_mask": 31,
    },
    {
        "enabled": True,
        "timezone": "UTC",
        "sun_window": [
            {"anchor": "sunrise", "offset_minutes": -15},
            {"anchor": "sunset", "offset_minutes": 30},
        ],
    },
]

# Schedules that parse_schedule must reject.
_INVALID_SCHEDULES = [
    {"enabled": True, "timezone": "Not/AReal/Zone"},
    {
        "enabled": True,
        "timezone": "UTC",
        "windows": [{"start_time": "25:00", "end_time": "10:00"}],
    },
    {"enabled": True, "timezone": "UTC", "day_of_week_mask": 200},  # > 127
]


class TestCreateWithSchedule:
    def test_valid_schedule_accepted_and_persisted(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        cam_id = _seed_camera(settings_no_autostart, name="sched-create-cam-a")
        schedule = {"enabled": True, "timezone": "UTC"}
        resp = migrated_client.post(
            API,
            json={
                "name": "API Sched Create A",
                "camera_id": cam_id,
                "capture_interval_seconds": 60,
                "schedule": schedule,
            },
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 201, resp.text
        project_id = resp.json()["id"]
        stored = _read_project_schedule(settings_no_autostart, project_id)
        assert stored is not None
        assert stored.get("enabled") is True
        assert stored.get("timezone") == "UTC"

    def test_business_schedule_mask_persisted(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        cam_id = _seed_camera(settings_no_autostart, name="sched-create-cam-b")
        schedule = {
            "enabled": True,
            "timezone": "UTC",
            "windows": [{"start_time": "09:00", "end_time": "17:00"}],
            "day_of_week_mask": 31,
        }
        resp = migrated_client.post(
            API,
            json={
                "name": "API Sched Create B",
                "camera_id": cam_id,
                "capture_interval_seconds": 60,
                "schedule": schedule,
            },
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 201, resp.text
        project_id = resp.json()["id"]
        stored = _read_project_schedule(settings_no_autostart, project_id)
        assert stored is not None
        assert stored.get("day_of_week_mask") == 31
        windows = stored.get("windows", [])
        assert windows[0]["start_time"] == "09:00"

    def test_sun_window_schedule_persisted(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        cam_id = _seed_camera(settings_no_autostart, name="sched-create-cam-c")
        schedule = {
            "enabled": True,
            "timezone": "UTC",
            "sun_window": [
                {"anchor": "sunrise", "offset_minutes": -15},
                {"anchor": "sunset", "offset_minutes": 30},
            ],
        }
        resp = migrated_client.post(
            API,
            json={
                "name": "API Sched Create C",
                "camera_id": cam_id,
                "capture_interval_seconds": 60,
                "schedule": schedule,
            },
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 201, resp.text
        project_id = resp.json()["id"]
        stored = _read_project_schedule(settings_no_autostart, project_id)
        assert stored is not None
        sw = stored.get("sun_window", [])
        assert sw[0]["anchor"] == "sunrise"
        assert sw[0]["offset_minutes"] == -15
        assert sw[1]["offset_minutes"] == 30

    def test_null_schedule_accepted_and_stored_as_none(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        cam_id = _seed_camera(settings_no_autostart, name="sched-create-cam-null")
        resp = migrated_client.post(
            API,
            json={
                "name": "API Sched Null",
                "camera_id": cam_id,
                "capture_interval_seconds": 60,
                "schedule": None,
            },
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 201, resp.text
        project_id = resp.json()["id"]
        stored = _read_project_schedule(settings_no_autostart, project_id)
        assert stored is None

    def test_bad_timezone_returns_422(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        cam_id = _seed_camera(settings_no_autostart, name="sched-create-cam-bad-tz")
        resp = migrated_client.post(
            API,
            json={
                "name": "API Sched Bad TZ",
                "camera_id": cam_id,
                "capture_interval_seconds": 60,
                "schedule": {"enabled": True, "timezone": "Not/AReal/Zone"},
            },
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 422, resp.text

    def test_bad_window_time_returns_422(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        cam_id = _seed_camera(settings_no_autostart, name="sched-create-cam-bad-win")
        resp = migrated_client.post(
            API,
            json={
                "name": "API Sched Bad Win",
                "camera_id": cam_id,
                "capture_interval_seconds": 60,
                "schedule": {
                    "enabled": True,
                    "timezone": "UTC",
                    "windows": [{"start_time": "25:00", "end_time": "10:00"}],
                },
            },
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 422, resp.text

    def test_bad_day_mask_returns_422(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        cam_id = _seed_camera(settings_no_autostart, name="sched-create-cam-bad-mask")
        resp = migrated_client.post(
            API,
            json={
                "name": "API Sched Bad Mask",
                "camera_id": cam_id,
                "capture_interval_seconds": 60,
                "schedule": {
                    "enabled": True,
                    "timezone": "UTC",
                    "day_of_week_mask": 200,  # > 127 (max valid is 0b1111111)
                },
            },
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 422, resp.text


class TestUpdateWithSchedule:
    def _create_project(
        self,
        client: TestClient,
        token: str,
        settings: Settings,
        cam_id: int,
        name: str,
    ) -> int:
        resp = client.post(
            API,
            json={
                "name": name,
                "camera_id": cam_id,
                "capture_interval_seconds": 60,
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    def test_patch_with_valid_schedule_persists(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        cam_id = _seed_camera(settings_no_autostart, name="sched-patch-cam-a")
        proj_id = self._create_project(
            migrated_client,
            cam_auth_token,
            settings_no_autostart,
            cam_id,
            "API Patch Sched A",
        )
        schedule = {
            "enabled": True,
            "timezone": "America/Chicago",
            "windows": [{"start_time": "12:00", "end_time": "12:30"}],
            "day_of_week_mask": 127,
        }
        resp = migrated_client.patch(
            f"{API}/{proj_id}",
            json={"schedule": schedule},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 200, resp.text
        stored = _read_project_schedule(settings_no_autostart, proj_id)
        assert stored is not None
        assert stored.get("timezone") == "America/Chicago"
        assert stored.get("day_of_week_mask") == 127

    def test_patch_with_invalid_schedule_returns_422(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        cam_id = _seed_camera(settings_no_autostart, name="sched-patch-cam-bad")
        proj_id = self._create_project(
            migrated_client,
            cam_auth_token,
            settings_no_autostart,
            cam_id,
            "API Patch Bad Sched",
        )
        resp = migrated_client.patch(
            f"{API}/{proj_id}",
            json={"schedule": {"enabled": True, "timezone": "Not/Real"}},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 422, resp.text

    def test_patch_without_schedule_key_does_not_clear_it(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        """A PATCH that omits the 'schedule' key must leave the stored
        schedule intact."""
        cam_id = _seed_camera(settings_no_autostart, name="sched-patch-cam-preserve")
        # Create with a business schedule.
        resp = migrated_client.post(
            API,
            json={
                "name": "API Preserve Sched",
                "camera_id": cam_id,
                "capture_interval_seconds": 60,
                "schedule": {
                    "enabled": True,
                    "timezone": "UTC",
                    "windows": [{"start_time": "09:00", "end_time": "17:00"}],
                    "day_of_week_mask": 31,
                },
            },
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 201
        proj_id = resp.json()["id"]

        # PATCH with only capture_interval_seconds -- no schedule key.
        resp2 = migrated_client.patch(
            f"{API}/{proj_id}",
            json={"capture_interval_seconds": 90},
            headers=_auth(cam_auth_token),
        )
        assert resp2.status_code == 200, resp2.text

        stored = _read_project_schedule(settings_no_autostart, proj_id)
        assert stored is not None
        assert stored.get("day_of_week_mask") == 31
