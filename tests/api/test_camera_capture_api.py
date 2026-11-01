"""API manual capture goes through the one-shot capture path (#69 CAM-R10).

``POST /api/v1/cameras/{id}/capture`` used to build its own adapter on the
shared client and call ``capture()`` with no timeout, no PTZ, no stream, no
disk gate, no slot and no frame cap; any failure was a 500. It now calls
``capture_one_now`` with ``reason="manual"``, so:

* the project's PTZ position is applied before the capture;
* a low-disk skip is a ``507``; a camera timeout a ``504``; any other camera
  failure a ``502`` with the adapter text redacted; a local failure (a busy
  camera) a ``503``; a project at its frame cap a ``409``; a camera deleted
  mid-capture a ``404``;
* the manual capture never waits for a capture slot, so it cannot queue ahead
  of a scheduled anchor on the same camera;
* the success body (``CaptureOut``) is unchanged, and the frame is tagged
  ``capture_reason="manual"``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeAdapter
from timelapse_manager.cameras.base import (
    AuthCaptureError,
    CameraMissingError,
    CapturedFrame,
    TimeoutCaptureError,
)
from timelapse_manager.capture.supervisor import CaptureSupervisor
from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context

API = "/api/v1/cameras"
_BUILD = "timelapse_manager.capture.one_shot.build_adapter"


class _Adapter(FakeAdapter):
    """A fake adapter that records PTZ moves and can fail its capture."""

    def __init__(self, error: Exception | None = None) -> None:
        super().__init__()
        self.error = error
        self.moves: list[dict[str, Any]] = []
        self.captures = 0

    async def move_to(self, **kwargs: Any) -> None:
        self.moves.append(kwargs)

    async def capture(self) -> CapturedFrame:
        self.captures += 1
        if self.error is not None:
            raise self.error
        return await super().capture()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed(**project_fields: Any) -> tuple[int, int]:
    """Insert a camera and one active project on it; return their ids."""
    with session_scope(get_context().session_factory) as db:
        camera = Camera(name="cam", address="10.0.0.1", protocol="vapix")
        db.add(camera)
        db.flush()
        project = Project(
            camera_id=camera.id,
            name="manual-proj",
            exact_time_anchors=[{"id": "a1", "kind": "clock", "time": "12:05"}],
            lifecycle_state="active",
            operational_status="idle",
            **project_fields,
        )
        db.add(project)
        db.flush()
        return camera.id, project.id


def _supervisor() -> CaptureSupervisor:
    supervisor = get_context().capture_supervisor
    assert supervisor is not None
    return supervisor


def _frames() -> list[tuple[str | None, int]]:
    with session_scope(get_context().session_factory) as db:
        return [
            (reason, project_id)
            for reason, project_id in db.query(Frame.capture_reason, Frame.project_id)
        ]


@pytest.fixture()
def client(migrated_client: TestClient) -> Iterator[TestClient]:
    yield migrated_client


def _post(
    client: TestClient, token: str, camera_id: int, project_id: int, adapter: Any
) -> Any:
    with patch(_BUILD, return_value=adapter):
        return client.post(
            f"{API}/{camera_id}/capture",
            json={"project_id": project_id},
            headers=_auth(token),
        )


def test_success_shape_is_unchanged_and_the_frame_is_manual(
    client: TestClient, cam_auth_token: str
) -> None:
    camera_id, project_id = _seed()
    resp = _post(client, cam_auth_token, camera_id, project_id, _Adapter())

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {
        "frame_id",
        "project_id",
        "sequence_index",
        "file_path",
        "width",
        "height",
        "file_size_bytes",
        "captured_at",
    }
    assert body["project_id"] == project_id
    assert (body["width"], body["height"]) == (1, 1)
    assert _frames() == [("manual", project_id)]


def test_the_project_ptz_position_is_applied(
    client: TestClient, cam_auth_token: str
) -> None:
    camera_id, project_id = _seed(ptz_preset="gate")
    adapter = _Adapter()
    resp = _post(client, cam_auth_token, camera_id, project_id, adapter)

    assert resp.status_code == 200, resp.text
    assert adapter.moves == [{"preset_id": "gate"}]
    assert adapter.captures == 1


def test_low_disk_is_507_and_nothing_is_captured(
    client: TestClient, cam_auth_token: str
) -> None:
    camera_id, project_id = _seed()
    adapter = _Adapter()
    monitor = _supervisor()._disk_monitor
    with patch.object(monitor, "is_capture_allowed", return_value=False):
        resp = _post(client, cam_auth_token, camera_id, project_id, adapter)

    assert resp.status_code == 507
    assert resp.json()["detail"] == "capture skipped: low disk"
    assert adapter.captures == 0
    assert _frames() == []


def test_a_camera_timeout_is_504(client: TestClient, cam_auth_token: str) -> None:
    camera_id, project_id = _seed()
    adapter = _Adapter(TimeoutCaptureError("timed out requesting the snapshot"))
    resp = _post(client, cam_auth_token, camera_id, project_id, adapter)

    assert resp.status_code == 504
    assert "timed out" in resp.json()["detail"]


def test_a_capture_over_the_budget_is_504(
    client: TestClient, cam_auth_token: str
) -> None:
    """The capture timeout applies (the old path awaited capture() unbounded)."""
    camera_id, project_id = _seed()

    class _Hanging(_Adapter):
        async def capture(self) -> CapturedFrame:
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")

    supervisor = _supervisor()
    capture_settings = supervisor._settings.capture.model_copy(
        update={"timeout_seconds": 0.2}
    )
    settings = supervisor._settings.model_copy(update={"capture": capture_settings})
    with patch.object(supervisor, "_settings", settings):
        started = time.monotonic()
        resp = _post(client, cam_auth_token, camera_id, project_id, _Hanging())

    assert resp.status_code == 504
    assert time.monotonic() - started < 30


def test_an_auth_failure_is_502_with_the_message_redacted(
    client: TestClient, cam_auth_token: str
) -> None:
    camera_id, project_id = _seed()
    adapter = _Adapter(
        AuthCaptureError(
            "authentication rejected (401) for "
            "http://admin:hunter2@10.0.0.1/axis-cgi/jpg/image.cgi"
        )
    )
    resp = _post(client, cam_auth_token, camera_id, project_id, adapter)

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "authentication rejected" in detail
    assert "hunter2" not in detail


def test_a_project_at_its_frame_cap_is_409(
    client: TestClient, cam_auth_token: str
) -> None:
    camera_id, project_id = _seed(max_frame_count=3, frame_count=3)
    adapter = _Adapter()
    resp = _post(client, cam_auth_token, camera_id, project_id, adapter)

    assert resp.status_code == 409
    assert "frame cap" in resp.json()["detail"]
    assert adapter.captures == 0


def test_a_camera_deleted_mid_capture_is_404(
    client: TestClient, cam_auth_token: str
) -> None:
    camera_id, project_id = _seed()
    sessions = _supervisor().camera_sessions

    async def _gone(cid: int) -> Any:
        raise CameraMissingError(cid)

    with patch.object(sessions, "get", new=_gone):
        resp = _post(client, cam_auth_token, camera_id, project_id, _Adapter())

    assert resp.status_code == 404


def test_an_unusable_configuration_is_400(
    client: TestClient, cam_auth_token: str
) -> None:
    camera_id, project_id = _seed()
    with patch(_BUILD, side_effect=ValueError("unsupported protocol 'nope'")):
        resp = client.post(
            f"{API}/{camera_id}/capture",
            json={"project_id": project_id},
            headers=_auth(cam_auth_token),
        )

    assert resp.status_code == 400


def test_a_busy_camera_is_503_at_once_and_never_queues(
    client: TestClient, cam_auth_token: str
) -> None:
    """With every slot taken (anchors capturing), manual capture does not wait.

    The scheduled wait for a slot is 2 x timeout + 30 s; a manual capture that
    waited would sit in the camera's FIFO queue and could go ahead of an anchor
    that arrives after it. It is refused at once instead.
    """
    camera_id, project_id = _seed()
    runtime = _supervisor().camera_sessions._runtime(camera_id)
    runtime.semaphore = asyncio.Semaphore(0)  # every shared slot is held
    adapter = _Adapter()

    started = time.monotonic()
    resp = _post(client, cam_auth_token, camera_id, project_id, adapter)

    assert resp.status_code == 503
    assert "camera busy" in resp.json()["detail"]
    assert time.monotonic() - started < 5
    assert adapter.captures == 0
    assert _frames() == []


def test_a_free_slot_is_released_after_a_manual_capture(
    client: TestClient, cam_auth_token: str
) -> None:
    """A manual capture holds a slot only while it captures."""
    camera_id, project_id = _seed()
    for _ in range(3):
        resp = _post(client, cam_auth_token, camera_id, project_id, _Adapter())
        assert resp.status_code == 200, resp.text
    runtime = _supervisor().camera_sessions._runtime(camera_id)
    assert not runtime.semaphore.locked()


async def _hang(*_args: object, **_kwargs: object) -> None:
    await asyncio.sleep(3600)


def test_validate_overrunning_its_bound_is_504(
    client: TestClient, cam_auth_token: str
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    camera_id, _ = _seed()
    adapter = MagicMock()
    adapter.validate_connection = _hang
    adapter.close = AsyncMock()
    with (
        patch(
            "timelapse_manager.api.cameras._validate_bound_seconds", return_value=0.1
        ),
        patch("timelapse_manager.api.cameras.build_adapter", return_value=adapter),
    ):
        resp = client.post(f"{API}/{camera_id}/validate", headers=_auth(cam_auth_token))
    assert resp.status_code == 504
    adapter.close.assert_awaited_once()


def test_detect_overrunning_its_bound_is_504(
    client: TestClient, cam_auth_token: str
) -> None:
    """It used to escape ``wait_for`` as an unhandled TimeoutError (a 500)."""
    from unittest.mock import AsyncMock

    with (
        patch(
            "timelapse_manager.api.cameras._resolve_camera_host_async",
            AsyncMock(return_value=None),
        ),
        patch("timelapse_manager.api.cameras._DETECT_TIMEOUT_SECONDS", 0.1),
        patch("timelapse_manager.api.cameras.detect_protocols", _hang),
    ):
        resp = client.post(
            f"{API}/detect-protocol",
            json={"address": "10.0.0.9"},
            headers=_auth(cam_auth_token),
        )
    assert resp.status_code == 504
