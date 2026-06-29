"""Tests for the capture-schedule form helpers and the web create/edit flows.

Covers:
  - ``_build_schedule_from_form``: all five presets, mask values, sun offsets
  - ``_schedule_to_form``: round-trip and shape-collision edge cases
  - Web create POST: schedule field persisted (read back from DB)
  - Web settings POST: schedule update persisted
  - RBAC: operator admitted, viewer blocked

DB assertions always read the ``project.schedule`` column directly -- the
API's ``ProjectOut`` schema does not expose the capture schedule.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.models import Camera, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.web.routers._shared import (
    _build_schedule_from_form,
    _schedule_to_form,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_camera(*, name: str, protocol: str = "vapix") -> int:
    """Insert a camera row using the running app's DB and return its id."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(
            name=name,
            address="127.0.0.1",
            protocol=protocol,
            snapshot_uri="http://127.0.0.1/snap",
        )
        db.add(cam)
        db.flush()
        return cam.id


def _project_named(name: str) -> Project | None:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        return db.query(Project).filter(Project.name == name).one_or_none()


def _create_project_via_web(
    client: TestClient,
    *,
    name: str,
    camera_id: int,
    extra_fields: dict | None = None,
) -> int:
    """POST to /projects with a schedule fieldset and return the new project id."""
    csrf = csrf_of(client, "/projects/new")
    data = {
        "name": name,
        "camera_id": str(camera_id),
        "capture_interval_value": "30",
        "capture_interval_unit": "seconds",
        "csrf_token": csrf,
        **(extra_fields or {}),
    }
    resp = client.post(
        "/projects",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"create failed: {resp.status_code} {resp.text}"
    project = _project_named(name)
    assert project is not None
    return project.id


# ---------------------------------------------------------------------------
# _build_schedule_from_form — pure helper, no client needed
# ---------------------------------------------------------------------------


class TestBuildScheduleFromFormAlways:
    def test_always_preset_has_no_windows(self) -> None:
        form = {"capture_schedule_preset": "always", "capture_schedule_timezone": "UTC"}
        schedule, err = _build_schedule_from_form(form, [])
        assert err is None
        assert schedule is not None
        assert "windows" not in schedule
        assert "sun_window" not in schedule
        assert "day_of_week_mask" not in schedule

    def test_always_preset_carries_timezone(self) -> None:
        form = {
            "capture_schedule_preset": "always",
            "capture_schedule_timezone": "America/New_York",
        }
        schedule, err = _build_schedule_from_form(form, [])
        assert err is None
        assert schedule is not None
        assert schedule["timezone"] == "America/New_York"

    def test_always_is_enabled(self) -> None:
        form = {"capture_schedule_preset": "always", "capture_schedule_timezone": "UTC"}
        schedule, err = _build_schedule_from_form(form, [])
        assert err is None
        assert schedule is not None
        assert schedule["enabled"] is True

    def test_missing_preset_defaults_to_always(self) -> None:
        # A form that omits capture_schedule_preset should default to "always".
        form = {"capture_schedule_timezone": "UTC"}
        schedule, err = _build_schedule_from_form(form, [])
        assert err is None
        assert schedule is not None
        assert "windows" not in schedule


class TestBuildScheduleFromFormBusiness:
    def test_business_preset_window(self) -> None:
        form = {
            "capture_schedule_preset": "business",
            "capture_schedule_timezone": "UTC",
        }
        schedule, err = _build_schedule_from_form(form, [])
        assert err is None
        assert schedule is not None
        windows = schedule.get("windows", [])
        assert len(windows) == 1
        assert windows[0]["start_time"] == "09:00"
        assert windows[0]["end_time"] == "17:00"

    def test_business_preset_mask_is_31(self) -> None:
        # 0b0011111 == 31 == Monday through Friday.
        form = {
            "capture_schedule_preset": "business",
            "capture_schedule_timezone": "UTC",
        }
        schedule, err = _build_schedule_from_form(form, [])
        assert err is None
        assert schedule is not None
        assert schedule.get("day_of_week_mask") == 31


class TestBuildScheduleFromFormNoon:
    def test_noon_preset_window(self) -> None:
        form = {
            "capture_schedule_preset": "noon",
            "capture_schedule_timezone": "UTC",
        }
        schedule, err = _build_schedule_from_form(form, [])
        assert err is None
        assert schedule is not None
        windows = schedule.get("windows", [])
        assert len(windows) == 1
        assert windows[0]["start_time"] == "12:00"
        assert windows[0]["end_time"] == "12:30"

    def test_noon_preset_mask_is_127(self) -> None:
        # 0b1111111 == 127 == all seven days.
        form = {
            "capture_schedule_preset": "noon",
            "capture_schedule_timezone": "UTC",
        }
        schedule, err = _build_schedule_from_form(form, [])
        assert err is None
        assert schedule is not None
        assert schedule.get("day_of_week_mask") == 127


class TestBuildScheduleFromFormSun:
    def test_sun_preset_anchors(self) -> None:
        form = {
            "capture_schedule_preset": "sun",
            "capture_schedule_timezone": "UTC",
            "sun_offset_start_min": "0",
            "sun_offset_end_min": "0",
        }
        schedule, err = _build_schedule_from_form(form, [])
        assert err is None
        assert schedule is not None
        sw = schedule.get("sun_window", [])
        assert len(sw) == 2
        assert sw[0]["anchor"] == "sunrise"
        assert sw[1]["anchor"] == "sunset"

    def test_sun_preset_positive_offsets(self) -> None:
        form = {
            "capture_schedule_preset": "sun",
            "capture_schedule_timezone": "UTC",
            "sun_offset_start_min": "30",
            "sun_offset_end_min": "60",
        }
        schedule, err = _build_schedule_from_form(form, [])
        assert err is None
        assert schedule is not None
        sw = schedule["sun_window"]
        assert sw[0]["offset_minutes"] == 30
        assert sw[1]["offset_minutes"] == 60

    def test_sun_preset_negative_offsets(self) -> None:
        # Negative offsets mean "before sunrise/sunset".
        form = {
            "capture_schedule_preset": "sun",
            "capture_schedule_timezone": "UTC",
            "sun_offset_start_min": "-15",
            "sun_offset_end_min": "-30",
        }
        schedule, err = _build_schedule_from_form(form, [])
        assert err is None
        assert schedule is not None
        sw = schedule["sun_window"]
        assert sw[0]["offset_minutes"] == -15
        assert sw[1]["offset_minutes"] == -30

    def test_sun_preset_zero_offsets_when_fields_absent(self) -> None:
        form = {
            "capture_schedule_preset": "sun",
            "capture_schedule_timezone": "UTC",
        }
        schedule, err = _build_schedule_from_form(form, [])
        assert err is None
        assert schedule is not None
        sw = schedule["sun_window"]
        assert sw[0]["offset_minutes"] == 0
        assert sw[1]["offset_minutes"] == 0

    def test_sun_preset_bad_offset_returns_error(self) -> None:
        form = {
            "capture_schedule_preset": "sun",
            "capture_schedule_timezone": "UTC",
            "sun_offset_start_min": "not_a_number",
        }
        _schedule, err = _build_schedule_from_form(form, [])
        assert err is not None
        assert "offset" in err.lower() or "number" in err.lower()


class TestBuildScheduleFromFormCustom:
    def test_custom_preset_window_times(self) -> None:
        form = {
            "capture_schedule_preset": "custom",
            "capture_schedule_timezone": "UTC",
            "capture_window_start": "08:00",
            "capture_window_end": "10:00",
        }
        schedule, err = _build_schedule_from_form(form, ["mon", "wed"])
        assert err is None
        assert schedule is not None
        windows = schedule.get("windows", [])
        assert windows[0]["start_time"] == "08:00"
        assert windows[0]["end_time"] == "10:00"

    def test_custom_preset_days_to_mask(self) -> None:
        # mon=bit0=1, wed=bit2=4 -> mask=5
        form = {
            "capture_schedule_preset": "custom",
            "capture_schedule_timezone": "UTC",
            "capture_window_start": "08:00",
            "capture_window_end": "10:00",
        }
        schedule, err = _build_schedule_from_form(form, ["mon", "wed"])
        assert err is None
        assert schedule is not None
        assert schedule.get("day_of_week_mask") == 5

    def test_custom_preset_no_days_defaults_to_127(self) -> None:
        # Empty day list -> full week (127) rather than "no day allowed".
        form = {
            "capture_schedule_preset": "custom",
            "capture_schedule_timezone": "UTC",
            "capture_window_start": "06:00",
            "capture_window_end": "18:00",
        }
        schedule, err = _build_schedule_from_form(form, [])
        assert err is None
        assert schedule is not None
        assert schedule.get("day_of_week_mask") == 127

    def test_custom_preset_missing_window_returns_error(self) -> None:
        form = {
            "capture_schedule_preset": "custom",
            "capture_schedule_timezone": "UTC",
            # window fields absent
        }
        _schedule, err = _build_schedule_from_form(form, [])
        assert err is not None

    def test_custom_preset_all_seven_days(self) -> None:
        form = {
            "capture_schedule_preset": "custom",
            "capture_schedule_timezone": "UTC",
            "capture_window_start": "08:00",
            "capture_window_end": "20:00",
        }
        all_days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        schedule, err = _build_schedule_from_form(form, all_days)
        assert err is None
        assert schedule is not None
        assert schedule.get("day_of_week_mask") == 127


class TestBuildScheduleFromFormErrors:
    def test_unknown_preset_returns_error(self) -> None:
        form = {
            "capture_schedule_preset": "bogus",
            "capture_schedule_timezone": "UTC",
        }
        _schedule, err = _build_schedule_from_form(form, [])
        assert err is not None

    def test_invalid_timezone_returns_error(self) -> None:
        form = {
            "capture_schedule_preset": "always",
            "capture_schedule_timezone": "Not/AReal/Zone",
        }
        _schedule, err = _build_schedule_from_form(form, [])
        assert err is not None


# ---------------------------------------------------------------------------
# _schedule_to_form — round-trip
# ---------------------------------------------------------------------------


class TestScheduleToFormRoundTrip:
    """Each preset built by _build_schedule_from_form must survive a round-trip
    through _schedule_to_form and come back with the same preset label.
    """

    def _round_trip(self, form: dict, days: list[str]) -> dict:
        schedule, err = _build_schedule_from_form(form, days)
        assert err is None, f"build failed: {err}"
        assert schedule is not None
        return _schedule_to_form(schedule)

    def test_always_round_trip(self) -> None:
        form = {"capture_schedule_preset": "always", "capture_schedule_timezone": "UTC"}
        result = self._round_trip(form, [])
        assert result["preset"] == "always"

    def test_business_round_trip(self) -> None:
        form = {
            "capture_schedule_preset": "business",
            "capture_schedule_timezone": "UTC",
        }
        result = self._round_trip(form, [])
        assert result["preset"] == "business"

    def test_noon_round_trip(self) -> None:
        form = {
            "capture_schedule_preset": "noon",
            "capture_schedule_timezone": "UTC",
        }
        result = self._round_trip(form, [])
        assert result["preset"] == "noon"

    def test_sun_round_trip(self) -> None:
        form = {
            "capture_schedule_preset": "sun",
            "capture_schedule_timezone": "UTC",
            "sun_offset_start_min": "-15",
            "sun_offset_end_min": "30",
        }
        result = self._round_trip(form, [])
        assert result["preset"] == "sun"
        assert result["sun_offset_start_min"] == -15
        assert result["sun_offset_end_min"] == 30

    def test_custom_round_trip(self) -> None:
        # Window 08:00-10:00, mask 5 (mon+wed) -- won't collide with business/noon.
        form = {
            "capture_schedule_preset": "custom",
            "capture_schedule_timezone": "UTC",
            "capture_window_start": "08:00",
            "capture_window_end": "10:00",
        }
        result = self._round_trip(form, ["mon", "wed"])
        assert result["preset"] == "custom"
        assert result["window_start"] == "08:00"
        assert result["window_end"] == "10:00"

    def test_none_schedule_returns_always_preset(self) -> None:
        result = _schedule_to_form(None)
        assert result["preset"] == "always"

    def test_timezone_preserved(self) -> None:
        form = {
            "capture_schedule_preset": "always",
            "capture_schedule_timezone": "America/Chicago",
        }
        result = self._round_trip(form, [])
        assert result["timezone"] == "America/Chicago"


class TestScheduleToFormShapeInference:
    """_schedule_to_form infers the preset from the stored shape, not a tag."""

    def test_business_shape_identified_correctly(self) -> None:
        stored = {
            "enabled": True,
            "timezone": "UTC",
            "windows": [{"start_time": "09:00", "end_time": "17:00"}],
            "day_of_week_mask": 31,
        }
        result = _schedule_to_form(stored)
        assert result["preset"] == "business"

    def test_noon_shape_identified_correctly(self) -> None:
        stored = {
            "enabled": True,
            "timezone": "UTC",
            "windows": [{"start_time": "12:00", "end_time": "12:30"}],
            "day_of_week_mask": 127,
        }
        result = _schedule_to_form(stored)
        assert result["preset"] == "noon"

    def test_almost_business_but_wrong_mask_falls_to_custom(self) -> None:
        # 09:00-17:00 but mask 127 (not 31) must not match business.
        stored = {
            "enabled": True,
            "timezone": "UTC",
            "windows": [{"start_time": "09:00", "end_time": "17:00"}],
            "day_of_week_mask": 127,
        }
        result = _schedule_to_form(stored)
        assert result["preset"] == "custom"

    def test_sun_window_takes_precedence(self) -> None:
        stored = {
            "enabled": True,
            "timezone": "UTC",
            "sun_window": [
                {"anchor": "sunrise", "offset_minutes": 0},
                {"anchor": "sunset", "offset_minutes": 0},
            ],
        }
        result = _schedule_to_form(stored)
        assert result["preset"] == "sun"


# ---------------------------------------------------------------------------
# Web create POST — schedule persistence
# ---------------------------------------------------------------------------


class TestWebCreateSchedulePersistence:
    def test_always_schedule_persisted_on_create(
        self, admin_client: TestClient
    ) -> None:
        cam_id = _seed_camera(name="sched-create-always")
        _create_project_via_web(
            admin_client,
            name="Sched Always Proj",
            camera_id=cam_id,
            extra_fields={
                "capture_schedule_preset": "always",
                "capture_schedule_timezone": "UTC",
            },
        )
        project = _project_named("Sched Always Proj")
        assert project is not None
        schedule = project.schedule
        assert schedule is not None
        assert schedule.get("enabled") is True
        assert "windows" not in schedule

    def test_business_schedule_persisted_on_create(
        self, admin_client: TestClient
    ) -> None:
        cam_id = _seed_camera(name="sched-create-business")
        _create_project_via_web(
            admin_client,
            name="Sched Business Proj",
            camera_id=cam_id,
            extra_fields={
                "capture_schedule_preset": "business",
                "capture_schedule_timezone": "UTC",
            },
        )
        project = _project_named("Sched Business Proj")
        assert project is not None
        schedule = project.schedule
        assert schedule is not None
        assert schedule.get("day_of_week_mask") == 31
        windows = schedule.get("windows", [])
        assert windows[0]["start_time"] == "09:00"

    def test_no_schedule_fieldset_stores_none(self, admin_client: TestClient) -> None:
        # A form with no capture_schedule_preset stores schedule=None.
        cam_id = _seed_camera(name="sched-create-none")
        _create_project_via_web(
            admin_client,
            name="No Sched Proj",
            camera_id=cam_id,
        )
        project = _project_named("No Sched Proj")
        assert project is not None
        assert project.schedule is None


# ---------------------------------------------------------------------------
# Web settings POST — schedule update persistence
# ---------------------------------------------------------------------------


class TestWebSettingsSchedulePersistence:
    def _create_bare_project(self, client: TestClient, cam_id: int, name: str) -> int:
        return _create_project_via_web(client, name=name, camera_id=cam_id)

    def test_settings_post_updates_schedule(self, admin_client: TestClient) -> None:
        cam_id = _seed_camera(name="sched-settings-cam")
        project_id = self._create_bare_project(admin_client, cam_id, "Sched Edit Proj")

        # Now POST to the settings form with a noon schedule.
        csrf = csrf_of(admin_client, f"/projects/{project_id}/settings")
        resp = admin_client.post(
            f"/projects/{project_id}/settings",
            data={
                "name": "Sched Edit Proj",
                "camera_id": str(cam_id),
                "capture_interval_value": "60",
                "capture_interval_unit": "seconds",
                "capture_schedule_preset": "noon",
                "capture_schedule_timezone": "UTC",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code in (200, 303), (
            f"settings post failed: {resp.status_code}"
        )

        project = _project_named("Sched Edit Proj")
        assert project is not None
        schedule = project.schedule
        assert schedule is not None
        assert schedule.get("day_of_week_mask") == 127
        windows = schedule.get("windows", [])
        assert windows[0]["start_time"] == "12:00"
        assert windows[0]["end_time"] == "12:30"

    def test_settings_post_without_schedule_fieldset_leaves_schedule_untouched(
        self, admin_client: TestClient
    ) -> None:
        cam_id = _seed_camera(name="sched-settings-preserve-cam")
        # Create with a business schedule.
        _create_project_via_web(
            admin_client,
            name="Preserve Sched Proj",
            camera_id=cam_id,
            extra_fields={
                "capture_schedule_preset": "business",
                "capture_schedule_timezone": "UTC",
            },
        )
        project = _project_named("Preserve Sched Proj")
        assert project is not None
        project_id = project.id

        # POST settings without the schedule fieldset (no capture_schedule_preset).
        csrf = csrf_of(admin_client, f"/projects/{project_id}/settings")
        resp = admin_client.post(
            f"/projects/{project_id}/settings",
            data={
                "name": "Preserve Sched Proj",
                "camera_id": str(cam_id),
                "capture_interval_value": "60",
                "capture_interval_unit": "seconds",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code in (200, 303)

        project = _project_named("Preserve Sched Proj")
        assert project is not None
        schedule = project.schedule
        # Business schedule must be intact -- the settings POST without the
        # fieldset must not overwrite it.
        assert schedule is not None
        assert schedule.get("day_of_week_mask") == 31


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


class TestScheduleRbac:
    def test_viewer_cannot_create_project_with_schedule(
        self, viewer_client: TestClient
    ) -> None:
        # A viewer POSTing to /projects must be denied (403).
        # viewer_client is pre-authenticated as a viewer.
        # The viewer can GET /cameras (read-only) to obtain a CSRF token --
        # /projects/new is operator-only (403 on GET) so we use a
        # viewer-accessible page.
        cam_id = _seed_camera(name="rbac-sched-cam-viewer")
        csrf = csrf_of(viewer_client, "/cameras")
        resp = viewer_client.post(
            "/projects",
            data={
                "name": "Viewer Sched Proj",
                "camera_id": str(cam_id),
                "capture_interval_value": "60",
                "capture_interval_unit": "seconds",
                "capture_schedule_preset": "always",
                "capture_schedule_timezone": "UTC",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_operator_can_create_project_with_schedule(
        self, operator_client: TestClient
    ) -> None:
        cam_id = _seed_camera(name="rbac-sched-cam-operator")
        csrf = csrf_of(operator_client, "/projects/new")
        resp = operator_client.post(
            "/projects",
            data={
                "name": "Operator Sched Proj",
                "camera_id": str(cam_id),
                "capture_interval_value": "60",
                "capture_interval_unit": "seconds",
                "capture_schedule_preset": "always",
                "capture_schedule_timezone": "UTC",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code not in (401, 403), (
            f"operator was denied: {resp.status_code}"
        )

    def test_viewer_cannot_update_schedule_via_settings(
        self, viewer_client: TestClient
    ) -> None:
        # Seed a project inside the viewer's own DB (via get_context).
        cam_id = _seed_camera(name="rbac-settings-cam-v")
        # A viewer can't access /projects/new -- seed the project via the DB helper
        # and POST directly to /projects/{id}/settings with the viewer session.
        # We need an existing project; create it by inserting directly.
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            proj = Project(
                camera_id=cam_id,
                name="Viewer Settings Proj",
                capture_interval_seconds=60,
                lifecycle_state="active",
            )
            db.add(proj)
            db.flush()
            project_id = proj.id

        # Viewer can see the project detail page but not the
        # operator-only settings form.
        # Use /cameras for CSRF (viewer-accessible).
        csrf = csrf_of(viewer_client, "/cameras")
        resp = viewer_client.post(
            f"/projects/{project_id}/settings",
            data={
                "name": "Viewer Settings Proj",
                "camera_id": str(cam_id),
                "capture_interval_value": "60",
                "capture_interval_unit": "seconds",
                "capture_schedule_preset": "noon",
                "capture_schedule_timezone": "UTC",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_operator_can_update_schedule_via_settings(
        self, operator_client: TestClient
    ) -> None:
        cam_id = _seed_camera(name="rbac-settings-op-cam")
        project_id = _create_project_via_web(
            operator_client, name="Rbac Op Settings Proj", camera_id=cam_id
        )

        csrf = csrf_of(operator_client, f"/projects/{project_id}/settings")
        resp = operator_client.post(
            f"/projects/{project_id}/settings",
            data={
                "name": "Rbac Op Settings Proj",
                "camera_id": str(cam_id),
                "capture_interval_value": "60",
                "capture_interval_unit": "seconds",
                "capture_schedule_preset": "business",
                "capture_schedule_timezone": "UTC",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code not in (401, 403), (
            f"operator was denied: {resp.status_code}"
        )
