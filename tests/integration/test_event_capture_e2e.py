"""Integration test: camera-event trigger to persisted Frame, end-to-end.

Closes the gap left by the existing event-trigger unit tests, which all mock
``capture_one_now`` and therefore never exercise the persistence path.

Wiring exercised here:
    ``_consume_event_source``
    → ``match_trigger`` (real; pure matching function)
    → monotonic cooldown gate (real; monotonic clock)
    → ``capture_one_now`` (real)
        → disk gate (real; permissive_disk_monitor)
        → ``_load_camera`` (stubbed: MagicMock, so camera DB lookup is bypassed)
        → ``_load_default_credentials`` (stubbed: returns None)
        → ``build_adapter`` (stubbed: returns FakeAdapter, avoiding real HTTP)
        → ``_apply_ptz`` (real; no-op — target has no PTZ fields set)
        → ``_capture_with_timeout`` (real; calls FakeAdapter.capture())
        → ``FrameWriter.write`` (real; writes file + inserts Frame row)
        → ``_write_event`` (real; inserts Event row)

Note on capture_reason: ``_consume_event_source`` calls ``capture_one_now``
with ``reason=f"event:{event.topic_id}"``, which is stored verbatim as
``Frame.capture_reason``.  The correct assertion value is therefore
``"event:Device/IO/VirtualInput"``, not the literal string ``"event_trigger"``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import FakeAdapter
from timelapse_manager.cameras.base import CameraEvent
from timelapse_manager.capture.supervisor import CaptureSupervisor, CaptureTarget
from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_TOPIC_ID = "Device/IO/VirtualInput"

_TRIGGER = {
    "id": "trig-e2e-001",
    "topic_id": _TOPIC_ID,
    "label": "Virtual Input",
    "category": "io",
    "enabled": True,
    "cooldown_seconds": 0,
}

# Patch target: build_adapter is imported into one_shot.py so the call-site
# module path must match, not the cameras package.
_PATCH_BUILD_ADAPTER = "timelapse_manager.capture.one_shot.build_adapter"


def _make_event() -> CameraEvent:
    """Return a rising-edge camera event whose topic matches ``_TRIGGER``."""
    return CameraEvent(
        topic_id=_TOPIC_ID,
        category="io",
        source={"port": "1"},
        data={"active": "1"},
        active=True,
        occurred_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Local fixture: seeded Camera + Project with an event trigger
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded_project(migrated_factory, tmp_path: Path) -> dict:
    """Seed a Camera + Project with one enabled event trigger.

    The camera snapshot URI is never reached — build_adapter is patched to
    return FakeAdapter.  The project has no capture interval; it exists only
    to receive event-triggered frames.
    """
    storage = tmp_path / "event_frames"
    storage.mkdir()
    with session_scope(migrated_factory) as session:
        cam = Camera(
            name="event-test-cam",
            address="10.0.0.2",
            protocol="vapix",
            snapshot_uri="http://10.0.0.2/snapshot.jpg",
        )
        session.add(cam)
        session.flush()
        cam_id = cam.id
        proj = Project(
            camera_id=cam_id,
            name="event-test-proj",
            lifecycle_state="active",
            operational_status="idle",
            storage_path=str(storage),
        )
        session.add(proj)
        session.flush()
        proj_id = proj.id
    return {"camera_id": cam_id, "project_id": proj_id, "storage_path": storage}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEventCaptureE2E:
    async def test_camera_event_persists_frame_with_event_capture_reason(
        self,
        settings_no_autostart,
        migrated_factory,
        permissive_disk_monitor,
        seeded_project: dict,
    ) -> None:
        """A matching rising-edge event writes exactly one Frame with the event reason.

        Asserts:
        - Exactly one Frame row is written (not zero, not two).
        - Frame.project_id matches the seeded project.
        - Frame.capture_reason is ``"event:Device/IO/VirtualInput"``
          (the literal value the supervisor stamps, derived from the event topic).
        """
        project_id = seeded_project["project_id"]
        camera_id = seeded_project["camera_id"]
        storage_path = str(seeded_project["storage_path"])

        supervisor = CaptureSupervisor(
            settings_no_autostart,
            migrated_factory,
            disk_monitor=permissive_disk_monitor,
        )
        target = CaptureTarget(
            project_id=project_id,
            project_name="event-test-proj",
            camera_id=camera_id,
            interval_seconds=None,
            storage_path=storage_path,
            event_triggers=[_TRIGGER],
        )

        async def _one_event() -> AsyncIterator[CameraEvent]:
            yield _make_event()

        try:
            with (
                patch.object(supervisor, "_load_camera", return_value=MagicMock()),
                patch.object(
                    supervisor, "_load_default_credentials", return_value=None
                ),
                patch(_PATCH_BUILD_ADAPTER, return_value=FakeAdapter()),
            ):
                await supervisor._consume_event_source(target, _one_event())
        finally:
            await supervisor.stop()

        with session_scope(migrated_factory) as session:
            frames = session.query(Frame).filter_by(project_id=project_id).all()

        assert len(frames) == 1
        frame = frames[0]
        assert frame.project_id == project_id
        assert frame.capture_reason == f"event:{_TOPIC_ID}"

    async def test_second_event_within_cooldown_is_suppressed(
        self,
        settings_no_autostart,
        migrated_factory,
        permissive_disk_monitor,
        seeded_project: dict,
    ) -> None:
        """A second identical event within the cooldown window writes no second Frame.

        Uses ``cooldown_seconds=3600``.  Two events with the same topic arrive
        back-to-back in a single ``_consume_event_source`` call.  The monotonic
        cooldown gate stamps ``last_fired[topic]`` before the first capture
        attempt; the second event's elapsed time is well under 3600 s, so it is
        suppressed before ``capture_one_now`` is called.

        Asserts exactly one Frame is persisted (not two).
        """
        trigger_with_cooldown = {**_TRIGGER, "cooldown_seconds": 3600}
        project_id = seeded_project["project_id"]
        camera_id = seeded_project["camera_id"]
        storage_path = str(seeded_project["storage_path"])

        supervisor = CaptureSupervisor(
            settings_no_autostart,
            migrated_factory,
            disk_monitor=permissive_disk_monitor,
        )
        target = CaptureTarget(
            project_id=project_id,
            project_name="event-test-proj",
            camera_id=camera_id,
            interval_seconds=None,
            storage_path=storage_path,
            event_triggers=[trigger_with_cooldown],
        )

        async def _two_events() -> AsyncIterator[CameraEvent]:
            yield _make_event()
            yield _make_event()

        try:
            with (
                patch.object(supervisor, "_load_camera", return_value=MagicMock()),
                patch.object(
                    supervisor, "_load_default_credentials", return_value=None
                ),
                patch(_PATCH_BUILD_ADAPTER, return_value=FakeAdapter()),
            ):
                await supervisor._consume_event_source(target, _two_events())
        finally:
            await supervisor.stop()

        with session_scope(migrated_factory) as session:
            count = session.query(Frame).filter_by(project_id=project_id).count()

        assert count == 1
