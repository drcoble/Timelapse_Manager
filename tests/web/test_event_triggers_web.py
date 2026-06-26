"""Web-layer tests for the event-triggered capture feature.

Covers the project-settings screen: GET rendering with mocked discovery,
POST persistence (new triggers, edit, remove), cooldown validation, degraded
discovery, the presence-marker semantics, and RBAC.

No live camera calls are made; ``_enumerate_event_topics`` is patched at the
binding used by each code path.

Integration with the supervisor's ``_consume_event_source`` is already
covered at unit level in ``tests/unit/test_supervisor.py``
(``TestConsumeEventSource``); those tests are not duplicated here.
"""

from __future__ import annotations

import urllib.parse
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.models import Camera, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.web.routers._shared import EventTopicsResult

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# An address in a subnet that passes the SSRF guard when the test client's
# Settings uses the default (no private-subnet allow-list). Because we patch
# _enumerate_event_topics directly, the SSRF guard is bypassed for the event
# probe. However, the stream-profile and PTZ enumerators also run during
# GET and may reach their own SSRF checks. Using a public-looking address
# keeps those paths cleaner (they fail gracefully with ok=False anyway).
_CAMERA_ADDRESS = "192.0.2.50"  # TEST-NET-1, not routable

# Two representative event topic dicts as the real discovery path produces.
_TOPIC_VIRTUAL_INPUT = {
    "topic_id": "Device/IO/VirtualInput",
    "label": "Virtual Input",
    "category": "io",
    "stateful": True,
    "protocol": "vapix",
    "requires_app": False,
}
_TOPIC_MOTION = {
    "topic_id": "RuleEngine/MotionRegionDetector/Motion",
    "label": "Motion",
    "category": "motion",
    "stateful": True,
    "protocol": "vapix",
    "requires_app": False,
}

_TWO_EVENTS_RESULT = EventTopicsResult(
    events=[_TOPIC_VIRTUAL_INPUT, _TOPIC_MOTION],
    ok=True,
    message=None,
)

_DEGRADED_RESULT = EventTopicsResult(
    events=[],
    ok=False,
    message="camera unreachable",
)

# The two patch targets that matter:
#   - GET path:  _settings_form_context lives in projects.py and imports
#                _enumerate_event_topics from _shared; patch the bound name.
#   - POST path: _build_event_triggers_from_form lives in _shared.py and calls
#                _enumerate_event_topics by its unqualified name in that module.
_GET_PATCH = "timelapse_manager.web.routers.projects._enumerate_event_topics"
_POST_PATCH = "timelapse_manager.web.routers._shared._enumerate_event_topics"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_camera(*, name: str) -> int:
    """Insert a Camera into the running app's DB; return its id."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(
            name=name,
            address=_CAMERA_ADDRESS,
            protocol="vapix",
            snapshot_uri=f"http://{_CAMERA_ADDRESS}/snap",
        )
        db.add(cam)
        db.flush()
        return cam.id


def _seed_project(
    *,
    name: str,
    camera_id: int,
    event_triggers: list[dict[str, Any]] | None = None,
) -> int:
    """Insert a Project; return its id."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        project = Project(
            camera_id=camera_id,
            name=name,
            capture_interval_seconds=60,
            lifecycle_state="active",
            event_triggers=event_triggers,
        )
        db.add(project)
        db.flush()
        return project.id


def _get_project(project_id: int) -> Project:
    """Fetch the project row from the running app's DB."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        return db.query(Project).filter(Project.id == project_id).one()


def _settings_path(project_id: int) -> str:
    return f"/projects/{project_id}/settings"


def _minimal_post_data(
    *,
    client: TestClient,
    project_id: int,
    camera_id: int,
    extra: list[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """Return the minimum POST body for a valid settings form submission."""
    csrf = csrf_of(client, _settings_path(project_id))
    base: list[tuple[str, str]] = [
        ("name", "test-project"),
        ("camera_id", str(camera_id)),
        ("capture_interval_value", "60"),
        ("capture_interval_unit", "seconds"),
        ("csrf_token", csrf),
    ]
    if extra:
        base.extend(extra)
    return base


def _post_settings(
    client: TestClient,
    project_id: int,
    data: list[tuple[str, str]],
    *,
    follow_redirects: bool = False,
) -> Any:
    # httpx 0.28+ does not accept list-of-tuples for data=; use content= with a
    # manually URL-encoded body so repeated keys (trigger_id, trigger_topic, …)
    # are preserved correctly.
    body = urllib.parse.urlencode(data)
    return client.post(
        _settings_path(project_id),
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=follow_redirects,
    )


# ---------------------------------------------------------------------------
# Test 1 — GET settings renders the event-trigger fieldset
# ---------------------------------------------------------------------------


class TestGetSettingsRendersEventTriggerFieldset:
    def test_discovered_topics_appear_in_select_and_marker_is_present(
        self, admin_client: TestClient
    ) -> None:
        """GET /projects/<id>/settings renders topic options and the presence marker."""
        camera_id = _seed_camera(name="et-get-cam")
        project_id = _seed_project(name="et-get-proj", camera_id=camera_id)

        with patch(_GET_PATCH, new=AsyncMock(return_value=_TWO_EVENTS_RESULT)):
            resp = admin_client.get(_settings_path(project_id))

        assert resp.status_code == 200
        html = resp.text
        # Presence marker hidden input must be present.
        assert 'name="event_triggers_present"' in html
        # Both discovered topic ids must appear as <option value="..."> entries.
        assert 'value="Device/IO/VirtualInput"' in html
        assert 'value="RuleEngine/MotionRegionDetector/Motion"' in html
        # Human labels should also appear.
        assert "Virtual Input" in html
        assert "Motion" in html


# ---------------------------------------------------------------------------
# Test 2 — POST settings persists a new trigger
# ---------------------------------------------------------------------------


class TestPostSettingsPeristsNewTrigger:
    def test_new_trigger_stored_with_enriched_label_and_category(
        self, admin_client: TestClient
    ) -> None:
        """POST a new trigger row: topic, cooldown, enabled, label, category stored."""
        camera_id = _seed_camera(name="et-post-cam")
        project_id = _seed_project(name="et-post-proj", camera_id=camera_id)

        # A new row: trigger_id blank, trigger_enabled value "new:0".
        extra: list[tuple[str, str]] = [
            ("event_triggers_present", "1"),
            ("trigger_id", ""),
            ("trigger_topic", "Device/IO/VirtualInput"),
            ("trigger_cooldown", "15"),
            ("trigger_enabled", "new:0"),
        ]
        data = _minimal_post_data(
            client=admin_client, project_id=project_id, camera_id=camera_id, extra=extra
        )

        with patch(_POST_PATCH, new=AsyncMock(return_value=_TWO_EVENTS_RESULT)):
            resp = _post_settings(admin_client, project_id, data)

        assert resp.status_code == 303

        project = _get_project(project_id)
        triggers = project.event_triggers
        assert triggers is not None and len(triggers) == 1
        t = triggers[0]
        assert t["topic_id"] == "Device/IO/VirtualInput"
        assert t["cooldown_seconds"] == 15
        assert t["enabled"] is True
        # Label and category enriched from discovery.
        assert t["label"] == "Virtual Input"
        assert t["category"] == "io"
        # A stable id was generated.
        assert t["id"] and len(t["id"]) > 0


# ---------------------------------------------------------------------------
# Test 3 — Edit existing trigger
# ---------------------------------------------------------------------------


class TestEditExistingTrigger:
    def test_edit_cooldown_and_disable_updates_stored_trigger(
        self, admin_client: TestClient
    ) -> None:
        """POST with changed cooldown + unchecked enabled updates the stored trigger."""
        camera_id = _seed_camera(name="et-edit-cam")
        # Pre-seed one trigger with a known stable id.
        existing_id = "aabbccddeeff00112233445566778899"
        project_id = _seed_project(
            name="et-edit-proj",
            camera_id=camera_id,
            event_triggers=[
                {
                    "id": existing_id,
                    "topic_id": "Device/IO/VirtualInput",
                    "label": "Virtual Input",
                    "category": "io",
                    "enabled": True,
                    "cooldown_seconds": 5,
                }
            ],
        )

        # Re-submit the same trigger with cooldown=30 and enabled unchecked
        # (omit trigger_enabled entirely so checkbox is absent).
        extra: list[tuple[str, str]] = [
            ("event_triggers_present", "1"),
            ("trigger_id", existing_id),
            ("trigger_topic", "Device/IO/VirtualInput"),
            ("trigger_cooldown", "30"),
            # trigger_enabled intentionally omitted → disabled
        ]
        data = _minimal_post_data(
            client=admin_client, project_id=project_id, camera_id=camera_id, extra=extra
        )

        with patch(_POST_PATCH, new=AsyncMock(return_value=_TWO_EVENTS_RESULT)):
            resp = _post_settings(admin_client, project_id, data)

        assert resp.status_code == 303

        project = _get_project(project_id)
        triggers = project.event_triggers
        assert triggers is not None and len(triggers) == 1
        t = triggers[0]
        assert t["id"] == existing_id
        assert t["cooldown_seconds"] == 30
        assert t["enabled"] is False


# ---------------------------------------------------------------------------
# Test 4 — Remove a trigger
# ---------------------------------------------------------------------------


class TestRemoveTrigger:
    def test_posting_one_row_when_two_stored_drops_the_other(
        self, admin_client: TestClient
    ) -> None:
        """Submitting only one trigger row when two are stored drops the missing one."""
        camera_id = _seed_camera(name="et-remove-cam")
        id_keep = "111111111111111111111111111111aa"
        id_drop = "222222222222222222222222222222bb"
        project_id = _seed_project(
            name="et-remove-proj",
            camera_id=camera_id,
            event_triggers=[
                {
                    "id": id_keep,
                    "topic_id": "Device/IO/VirtualInput",
                    "label": "Virtual Input",
                    "category": "io",
                    "enabled": True,
                    "cooldown_seconds": 0,
                },
                {
                    "id": id_drop,
                    "topic_id": "RuleEngine/MotionRegionDetector/Motion",
                    "label": "Motion",
                    "category": "motion",
                    "enabled": True,
                    "cooldown_seconds": 0,
                },
            ],
        )

        # Only submit the first row; the second is "removed" client-side.
        extra: list[tuple[str, str]] = [
            ("event_triggers_present", "1"),
            ("trigger_id", id_keep),
            ("trigger_topic", "Device/IO/VirtualInput"),
            ("trigger_cooldown", "0"),
            ("trigger_enabled", id_keep),
        ]
        data = _minimal_post_data(
            client=admin_client, project_id=project_id, camera_id=camera_id, extra=extra
        )

        with patch(_POST_PATCH, new=AsyncMock(return_value=_TWO_EVENTS_RESULT)):
            resp = _post_settings(admin_client, project_id, data)

        assert resp.status_code == 303

        project = _get_project(project_id)
        triggers = project.event_triggers
        assert triggers is not None and len(triggers) == 1
        assert triggers[0]["id"] == id_keep


# ---------------------------------------------------------------------------
# Test 5 — Cooldown default and validation
# ---------------------------------------------------------------------------


class TestCooldownDefaultAndValidation:
    def test_blank_cooldown_defaults_to_ten_seconds(
        self, admin_client: TestClient
    ) -> None:
        """A blank cooldown field defaults to 10 (the form default)."""
        camera_id = _seed_camera(name="et-cd-blank-cam")
        project_id = _seed_project(name="et-cd-blank-proj", camera_id=camera_id)

        extra: list[tuple[str, str]] = [
            ("event_triggers_present", "1"),
            ("trigger_id", ""),
            ("trigger_topic", "Device/IO/VirtualInput"),
            ("trigger_cooldown", ""),  # blank
            ("trigger_enabled", "new:0"),
        ]
        data = _minimal_post_data(
            client=admin_client, project_id=project_id, camera_id=camera_id, extra=extra
        )

        with patch(_POST_PATCH, new=AsyncMock(return_value=_TWO_EVENTS_RESULT)):
            resp = _post_settings(admin_client, project_id, data)

        assert resp.status_code == 303
        project = _get_project(project_id)
        triggers = project.event_triggers
        assert triggers is not None and len(triggers) == 1
        assert triggers[0]["cooldown_seconds"] == 10

    def test_negative_cooldown_returns_400_and_leaves_stored_triggers_unchanged(
        self, admin_client: TestClient
    ) -> None:
        """A negative cooldown re-renders with 400; stored triggers stay intact."""
        camera_id = _seed_camera(name="et-cd-neg-cam")
        stored_id = "storedid00000000000000000000aaaa"
        project_id = _seed_project(
            name="et-cd-neg-proj",
            camera_id=camera_id,
            event_triggers=[
                {
                    "id": stored_id,
                    "topic_id": "Device/IO/VirtualInput",
                    "label": "Virtual Input",
                    "category": "io",
                    "enabled": True,
                    "cooldown_seconds": 5,
                }
            ],
        )

        extra: list[tuple[str, str]] = [
            ("event_triggers_present", "1"),
            ("trigger_id", ""),
            ("trigger_topic", "Device/IO/VirtualInput"),
            ("trigger_cooldown", "-1"),
            ("trigger_enabled", "new:0"),
        ]
        data = _minimal_post_data(
            client=admin_client, project_id=project_id, camera_id=camera_id, extra=extra
        )

        with patch(_POST_PATCH, new=AsyncMock(return_value=_TWO_EVENTS_RESULT)):
            resp = _post_settings(admin_client, project_id, data)

        # Validation error → 400 re-render, no commit.
        assert resp.status_code == 400

        # The originally-stored trigger is still there, unchanged.
        project = _get_project(project_id)
        triggers = project.event_triggers
        assert triggers is not None and len(triggers) == 1
        assert triggers[0]["id"] == stored_id
        assert triggers[0]["cooldown_seconds"] == 5


# ---------------------------------------------------------------------------
# Test 6 — Degraded discovery
# ---------------------------------------------------------------------------


class TestDegradedDiscovery:
    def test_get_still_renders_existing_triggers_and_unavailable_notice(
        self, admin_client: TestClient
    ) -> None:
        """GET with ok=False still renders current_triggers and an unavailable note."""
        camera_id = _seed_camera(name="et-deg-get-cam")
        stored_id = "degraded0000000000000000000000aa"
        project_id = _seed_project(
            name="et-deg-get-proj",
            camera_id=camera_id,
            event_triggers=[
                {
                    "id": stored_id,
                    "topic_id": "Device/IO/VirtualInput",
                    "label": "Virtual Input",
                    "category": "io",
                    "enabled": True,
                    "cooldown_seconds": 0,
                }
            ],
        )

        with patch(_GET_PATCH, new=AsyncMock(return_value=_DEGRADED_RESULT)):
            resp = admin_client.get(_settings_path(project_id))

        assert resp.status_code == 200
        html = resp.text
        # The unavailable notice should appear.
        assert "Event list unavailable" in html
        # The existing trigger's topic must still be rendered so it round-trips.
        assert "Device/IO/VirtualInput" in html

    def test_post_in_degraded_mode_round_trips_existing_trigger(
        self, admin_client: TestClient
    ) -> None:
        """POST with degraded discovery (ok=False) preserves the stored trigger."""
        camera_id = _seed_camera(name="et-deg-post-cam")
        stored_id = "degraded0000000000000000000000bb"
        project_id = _seed_project(
            name="et-deg-post-proj",
            camera_id=camera_id,
            event_triggers=[
                {
                    "id": stored_id,
                    "topic_id": "Device/IO/VirtualInput",
                    "label": "Virtual Input",
                    "category": "io",
                    "enabled": True,
                    "cooldown_seconds": 20,
                }
            ],
        )

        # Degraded mode: hidden trigger_topic round-trips, no discovery.
        extra: list[tuple[str, str]] = [
            ("event_triggers_present", "1"),
            ("trigger_id", stored_id),
            ("trigger_topic", "Device/IO/VirtualInput"),
            ("trigger_cooldown", "20"),
            ("trigger_enabled", stored_id),
        ]
        data = _minimal_post_data(
            client=admin_client, project_id=project_id, camera_id=camera_id, extra=extra
        )

        with patch(_POST_PATCH, new=AsyncMock(return_value=_DEGRADED_RESULT)):
            resp = _post_settings(admin_client, project_id, data)

        assert resp.status_code == 303

        project = _get_project(project_id)
        triggers = project.event_triggers
        assert triggers is not None and len(triggers) == 1
        t = triggers[0]
        assert t["id"] == stored_id
        assert t["topic_id"] == "Device/IO/VirtualInput"
        assert t["enabled"] is True
        assert t["cooldown_seconds"] == 20


# ---------------------------------------------------------------------------
# Test 7 — Presence marker semantics
# ---------------------------------------------------------------------------


class TestPresenceMarkerSemantics:
    def test_post_without_marker_leaves_stored_triggers_untouched(
        self, admin_client: TestClient
    ) -> None:
        """A POST that omits event_triggers_present must not mutate stored triggers."""
        camera_id = _seed_camera(name="et-marker-cam")
        stored_id = "marker000000000000000000000000cc"
        project_id = _seed_project(
            name="et-marker-proj",
            camera_id=camera_id,
            event_triggers=[
                {
                    "id": stored_id,
                    "topic_id": "Device/IO/VirtualInput",
                    "label": "Virtual Input",
                    "category": "io",
                    "enabled": True,
                    "cooldown_seconds": 7,
                }
            ],
        )

        # Deliberately omit event_triggers_present — no trigger rows either.
        data = _minimal_post_data(
            client=admin_client, project_id=project_id, camera_id=camera_id
        )

        with patch(_POST_PATCH, new=AsyncMock(return_value=_TWO_EVENTS_RESULT)):
            resp = _post_settings(admin_client, project_id, data)

        assert resp.status_code == 303

        project = _get_project(project_id)
        triggers = project.event_triggers
        # The stored trigger must be completely unchanged.
        assert triggers is not None and len(triggers) == 1
        assert triggers[0]["id"] == stored_id
        assert triggers[0]["cooldown_seconds"] == 7


# ---------------------------------------------------------------------------
# Test 8 — RBAC
# ---------------------------------------------------------------------------


class TestRBAC:
    def test_viewer_cannot_post_settings(self, viewer_client: TestClient) -> None:
        """A viewer-role user gets a 403 or redirect when POSTing settings."""
        camera_id = _seed_camera(name="et-rbac-viewer-cam")
        project_id = _seed_project(name="et-rbac-viewer-proj", camera_id=camera_id)

        # We cannot get a valid CSRF token as viewer for this page because
        # GET /projects/{id}/settings is gated to operator+. Attempt the POST
        # anyway with a placeholder token to exercise the role gate.
        body = urllib.parse.urlencode(
            {
                "name": "et-rbac-viewer-proj",
                "camera_id": str(camera_id),
                "capture_interval_value": "60",
                "capture_interval_unit": "seconds",
                "csrf_token": "placeholder",
            }
        )
        resp = viewer_client.post(
            _settings_path(project_id),
            content=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        # The OperatorUser dependency rejects viewers with 403 or a login redirect.
        assert resp.status_code in {302, 303, 403}

    def test_operator_can_post_settings(self, operator_client: TestClient) -> None:
        """An operator-role user can successfully POST settings."""
        camera_id = _seed_camera(name="et-rbac-op-cam")
        project_id = _seed_project(name="et-rbac-op-proj", camera_id=camera_id)

        extra: list[tuple[str, str]] = [
            ("event_triggers_present", "1"),
            ("trigger_id", ""),
            ("trigger_topic", "Device/IO/VirtualInput"),
            ("trigger_cooldown", "5"),
            ("trigger_enabled", "new:0"),
        ]
        data = _minimal_post_data(
            client=operator_client,
            project_id=project_id,
            camera_id=camera_id,
            extra=extra,
        )

        with patch(_POST_PATCH, new=AsyncMock(return_value=_TWO_EVENTS_RESULT)):
            resp = _post_settings(operator_client, project_id, data)

        assert resp.status_code == 303
        project = _get_project(project_id)
        assert project.event_triggers is not None
        assert len(project.event_triggers) == 1

    def test_admin_can_post_settings(self, admin_client: TestClient) -> None:
        """An admin-role user can successfully POST settings."""
        camera_id = _seed_camera(name="et-rbac-admin-cam")
        project_id = _seed_project(name="et-rbac-admin-proj", camera_id=camera_id)

        extra: list[tuple[str, str]] = [
            ("event_triggers_present", "1"),
            ("trigger_id", ""),
            ("trigger_topic", "Device/IO/VirtualInput"),
            ("trigger_cooldown", "0"),
            ("trigger_enabled", "new:0"),
        ]
        data = _minimal_post_data(
            client=admin_client,
            project_id=project_id,
            camera_id=camera_id,
            extra=extra,
        )

        with patch(_POST_PATCH, new=AsyncMock(return_value=_TWO_EVENTS_RESULT)):
            resp = _post_settings(admin_client, project_id, data)

        assert resp.status_code == 303
        project = _get_project(project_id)
        assert project.event_triggers is not None
        assert len(project.event_triggers) == 1


# ---------------------------------------------------------------------------
# Test 9 — Integration: supervisor event consumption
# ---------------------------------------------------------------------------

# ``tests/unit/test_supervisor.py::TestConsumeEventSource`` already covers the
# supervisor's ``_consume_event_source`` method end-to-end: trigger matching,
# debounce cooldown, rising-vs-falling edge, fault isolation, and capture
# dispatch. Those eight cases exercise the full event→capture pipeline at the
# unit level without requiring a running web server. Duplicating that coverage
# here as an integration test would add noise without adding signal, so this
# file intentionally omits a web-integration test for supervisor consumption.
