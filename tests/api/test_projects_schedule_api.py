"""API tests for capture-schedule exposure on the /api/v1/projects endpoints.

Covers:
  - Create with a valid schedule: stored in DB (ProjectOut omits the field,
    so DB is the only way to verify)
  - Create with an invalid schedule: 422
  - PATCH update with a valid schedule: persisted
  - PATCH update with an invalid schedule: 422
  - Schedule is not cleared by PATCH requests that omit it
  - render_schedule overlay timestamp_format grammar, load-time fallback for
    stored rows, and clone sanitisation (#67 S4)

The ``ProjectOut`` response schema does NOT include the capture ``schedule``
column, so all persistence assertions go through a direct DB read.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from timelapse_manager.config.settings import Settings
from timelapse_manager.db.engine import create_db_engine
from timelapse_manager.db.models import Camera, Project, RenderJob
from timelapse_manager.db.session import create_session_factory, session_scope
from timelapse_manager.encode.overlay import DEFAULT_TIMESTAMP_FORMAT
from timelapse_manager.render import spec as spec_module
from timelapse_manager.render.scheduler import RenderScheduler

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


# ---------------------------------------------------------------------------
# render_schedule overlay timestamp_format (#67 S4)
# ---------------------------------------------------------------------------

_HOSTILE_FORMATS = [
    "'",
    "',null",
    "x',movie=/etc/passwd",
    "[a];movie=x",
    "%{eif:1:d}",
    "\\",
    "a" * 65,
]


def _store_render_schedule(
    settings: Settings, project_id: int, schedule: dict | None
) -> None:
    """Write ``render_schedule`` straight to the row, bypassing validation.

    Stands in for a row saved before the timestamp-format check existed.
    """
    engine = create_db_engine(settings.database.url)
    factory = create_session_factory(engine)
    try:
        with session_scope(factory) as session:
            project = session.get(Project, project_id)
            assert project is not None
            project.render_schedule = schedule
    finally:
        engine.dispose()


def _read_render_schedule(settings: Settings, project_id: int) -> dict | None:
    engine = create_db_engine(settings.database.url)
    factory = create_session_factory(engine)
    try:
        with session_scope(factory) as session:
            project = session.get(Project, project_id)
            assert project is not None
            return project.render_schedule
    finally:
        engine.dispose()


class TestRenderScheduleOverlayFormat:
    def _project(
        self, client: TestClient, token: str, settings: Settings, name: str
    ) -> int:
        cam_id = _seed_camera(settings, name=f"{name}-cam")
        resp = client.post(
            API,
            json={"name": name, "camera_id": cam_id, "capture_interval_seconds": 60},
            headers=_auth(token),
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    @pytest.mark.parametrize("enabled", [True, False])
    @pytest.mark.parametrize("fmt", _HOSTILE_FORMATS)
    def test_patch_hostile_format_returns_422_and_stores_nothing(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
        fmt: str,
        enabled: bool,
    ) -> None:
        # Checked even on a disabled schedule: it can be switched on later.
        proj_id = self._project(
            migrated_client, cam_auth_token, settings_no_autostart, "Overlay Bad"
        )
        schedule = {
            "enabled": enabled,
            "interval_seconds": 3600,
            "overlay_config": {"timestamp_enabled": True, "timestamp_format": fmt},
        }

        resp = migrated_client.patch(
            f"{API}/{proj_id}",
            json={"render_schedule": schedule},
            headers=_auth(cam_auth_token),
        )

        assert resp.status_code == 422, resp.text
        assert "timestamp_format" in resp.text
        assert _read_render_schedule(settings_no_autostart, proj_id) is None

    @pytest.mark.parametrize(
        "overlay",
        [
            "not an object",
            ["%Y"],
            {"timestamp_format": 5},
            {"timestamp_format": None},
        ],
    )
    def test_patch_malformed_overlay_returns_422(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
        overlay: object,
    ) -> None:
        proj_id = self._project(
            migrated_client, cam_auth_token, settings_no_autostart, "Overlay Shape"
        )

        resp = migrated_client.patch(
            f"{API}/{proj_id}",
            json={"render_schedule": {"enabled": False, "overlay_config": overlay}},
            headers=_auth(cam_auth_token),
        )

        assert resp.status_code == 422, resp.text

    def test_patch_valid_format_persists(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        proj_id = self._project(
            migrated_client, cam_auth_token, settings_no_autostart, "Overlay Good"
        )
        schedule = {
            "enabled": True,
            "interval_seconds": 3600,
            "overlay_config": {"timestamp_enabled": True, "timestamp_format": "%H:%M"},
        }

        resp = migrated_client.patch(
            f"{API}/{proj_id}",
            json={"render_schedule": schedule},
            headers=_auth(cam_auth_token),
        )

        assert resp.status_code == 200, resp.text
        assert _read_render_schedule(settings_no_autostart, proj_id) == schedule

    def test_patch_schedule_without_overlay_is_unaffected(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        proj_id = self._project(
            migrated_client, cam_auth_token, settings_no_autostart, "Overlay None"
        )
        for schedule in (
            {"enabled": True, "interval_seconds": 60},
            {"enabled": False, "overlay_config": None},
            {"enabled": False, "overlay_config": {"text_enabled": True}},
        ):
            resp = migrated_client.patch(
                f"{API}/{proj_id}",
                json={"render_schedule": schedule},
                headers=_auth(cam_auth_token),
            )
            assert resp.status_code == 200, resp.text

    @pytest.mark.parametrize("fmt", _HOSTILE_FORMATS)
    def test_stored_hostile_schedule_renders_with_the_default_format(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
        fmt: str,
    ) -> None:
        # The once-per-project warning (#140) is process state.
        monkeypatch.setattr(spec_module, "_WARNED_BAD_TIMESTAMP_FORMAT", set())
        proj_id = self._project(
            migrated_client, cam_auth_token, settings_no_autostart, "Overlay Stored"
        )
        _store_render_schedule(
            settings_no_autostart,
            proj_id,
            {
                "enabled": True,
                "interval_seconds": 60,
                "overlay_config": {"timestamp_enabled": True, "timestamp_format": fmt},
            },
        )
        engine = create_db_engine(settings_no_autostart.database.url)
        factory = create_session_factory(engine)
        try:
            with session_scope(factory) as session:
                project = session.get(Project, proj_id)
                assert project is not None
                # The scheduler copies the stored overlay into the job unchanged.
                scheduler = RenderScheduler(settings_no_autostart, factory, MagicMock())
                job_id = scheduler._create_job(
                    session, proj_id, "scheduled", project.render_schedule
                )
                job = session.get(RenderJob, job_id)
                assert job is not None
                assert job.overlay_config is not None
                assert job.overlay_config["timestamp_format"] == fmt

                with caplog.at_level(logging.WARNING, logger=spec_module.__name__):
                    overlay = spec_module._overlay_config(job)

                assert overlay.timestamp_enabled is True
                assert overlay.timestamp_format == DEFAULT_TIMESTAMP_FORMAT
                # The stored rows are not rewritten.
                assert job.overlay_config["timestamp_format"] == fmt
        finally:
            engine.dispose()
        warnings = [r for r in caplog.records if "timestamp_format" in r.getMessage()]
        assert len(warnings) == 1
        assert f"render job {job_id} (project {proj_id})" in warnings[0].getMessage()

    def test_clone_resets_a_stored_hostile_format_and_leaves_the_source(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        source_id = self._project(
            migrated_client, cam_auth_token, settings_no_autostart, "Overlay Source"
        )
        hostile = {
            "enabled": True,
            "interval_seconds": 60,
            "fps": 24,
            "overlay_config": {
                "timestamp_enabled": True,
                "timestamp_format": "x',movie=/etc/passwd",
            },
        }
        _store_render_schedule(settings_no_autostart, source_id, hostile)

        resp = migrated_client.post(
            f"{API}/{source_id}/clone",
            json={"name": "Overlay Clone"},
            headers=_auth(cam_auth_token),
        )

        assert resp.status_code == 201, resp.text
        cloned = _read_render_schedule(settings_no_autostart, resp.json()["id"])
        assert cloned is not None
        assert cloned["overlay_config"]["timestamp_format"] == DEFAULT_TIMESTAMP_FORMAT
        assert cloned["overlay_config"]["timestamp_enabled"] is True
        assert cloned["fps"] == 24
        assert _read_render_schedule(settings_no_autostart, source_id) == hostile

    def test_clone_copies_a_valid_overlay_unchanged(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        source_id = self._project(
            migrated_client, cam_auth_token, settings_no_autostart, "Overlay Src2"
        )
        schedule = {
            "enabled": False,
            "overlay_config": {"timestamp_enabled": True, "timestamp_format": "%H:%M"},
        }
        _store_render_schedule(settings_no_autostart, source_id, schedule)

        resp = migrated_client.post(
            f"{API}/{source_id}/clone",
            json={"name": "Overlay Clone2"},
            headers=_auth(cam_auth_token),
        )

        assert resp.status_code == 201, resp.text
        assert _read_render_schedule(settings_no_autostart, resp.json()["id"]) == (
            schedule
        )


class TestRenderScheduleCaptionLength:
    """A saved render schedule's overlay caption is capped too (#139)."""

    def _project(
        self, client: TestClient, token: str, settings: Settings, name: str
    ) -> int:
        return TestRenderScheduleOverlayFormat()._project(  # noqa: SLF001
            client, token, settings, name
        )

    @pytest.mark.parametrize("enabled", [True, False])
    def test_patch_oversized_caption_returns_422_and_stores_nothing(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
        enabled: bool,
    ) -> None:
        from timelapse_manager.encode.overlay import TEXT_CONTENT_MAX_LENGTH

        proj_id = self._project(
            migrated_client, cam_auth_token, settings_no_autostart, "Caption Long"
        )
        schedule = {
            "enabled": enabled,
            "interval_seconds": 3600,
            "overlay_config": {
                "text_enabled": True,
                "text_content": "x" * (TEXT_CONTENT_MAX_LENGTH + 1),
            },
        }

        resp = migrated_client.patch(
            f"{API}/{proj_id}",
            json={"render_schedule": schedule},
            headers=_auth(cam_auth_token),
        )

        assert resp.status_code == 422, resp.text
        assert "text_content" in resp.text
        assert _read_render_schedule(settings_no_autostart, proj_id) is None

    def test_patch_non_string_caption_returns_422(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        proj_id = self._project(
            migrated_client, cam_auth_token, settings_no_autostart, "Caption Type"
        )

        resp = migrated_client.patch(
            f"{API}/{proj_id}",
            json={
                "render_schedule": {
                    "enabled": False,
                    "interval_seconds": 3600,
                    "overlay_config": {"text_content": 5},
                }
            },
            headers=_auth(cam_auth_token),
        )

        assert resp.status_code == 422, resp.text

    def test_patch_caption_at_the_cap_is_accepted(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        from timelapse_manager.encode.overlay import TEXT_CONTENT_MAX_LENGTH

        proj_id = self._project(
            migrated_client, cam_auth_token, settings_no_autostart, "Caption Ok"
        )

        resp = migrated_client.patch(
            f"{API}/{proj_id}",
            json={
                "render_schedule": {
                    "enabled": False,
                    "interval_seconds": 3600,
                    "overlay_config": {"text_content": "x" * TEXT_CONTENT_MAX_LENGTH},
                }
            },
            headers=_auth(cam_auth_token),
        )

        assert resp.status_code == 200, resp.text
