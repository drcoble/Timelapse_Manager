"""The 12:05 anchor path against an HTTPS camera offering Basic + Digest (#69 WP-69-H).

Every deployed camera is an Axis device at an ``https://`` address answering
``401`` with both Basic and Digest challenges, and every deployed project is
anchor-only. A regression in auth negotiation would fail every capture, so
these tests drive the real anchor fire path (claim, one-shot capture, frame
write, fire row) through the real adapter factory, VAPIX adapter and snapshot
helper, against a fake camera that verifies the Digest answer independently
(:mod:`tests.unit.camera_auth_support`). Only the HTTP transport is fake.

They pin that the outcome is the one the anchor path always produced (a
``captured`` fire row, one ``anchor:clock`` frame, the same event), that the
password never crosses the wire (Digest is chosen over Basic), and that the
session's auth state makes the next day's anchor one request instead of three.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from tests.unit.camera_auth_support import FakeAxisCamera
from tests.unit.capture.test_anchor_retry import (
    _ANCHOR,
    _DAY,
    _INSTANT,
    _NY,
    FakeClock,
    _events,
    _fire,
    _frames,
    _supervisor,
)
from tests.unit.credential_binding_support import (  # noqa: F401
    _allowed_private_subnets,
)
from timelapse_manager.capture.supervisor import CaptureSupervisor, CaptureTarget
from timelapse_manager.db.models import Camera, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.security.crypto import encrypt_credentials

_ADDRESS = "https://driveway.cam.test"


@pytest.fixture(autouse=True)
def _no_runtime_context() -> Iterator[None]:
    """The snapshot helper's SSRF guard and size cap without an app context."""
    with (
        patch("timelapse_manager.cameras.http_jpeg._guard_snapshot_url"),
        patch(
            "timelapse_manager.cameras.http_jpeg._resolve_max_image_bytes",
            return_value=64 * 1024 * 1024,
        ),
    ):
        yield


def _seed_https_camera(
    factory: Any, tmp_path: Path, camera: FakeAxisCamera
) -> CaptureTarget:
    """An anchor-only VAPIX project on an HTTPS camera with its own login."""
    storage = tmp_path / "frames" / "anchor"
    storage.mkdir(parents=True, exist_ok=True)
    schedule = {"enabled": True, "timezone": _NY}
    with session_scope(factory) as session:
        cam = Camera(
            name="Driveway",
            address=_ADDRESS,
            protocol="vapix",
            credentials=encrypt_credentials(
                {"username": camera.username, "password": camera.password}
            ),
            credentials_inherit_default=False,
        )
        session.add(cam)
        session.flush()
        proj = Project(
            camera_id=cam.id,
            name="anchor",
            capture_interval_seconds=None,
            lifecycle_state="active",
            operational_status="idle",
            storage_path=str(storage),
            schedule=schedule,
            exact_time_anchors=[_ANCHOR],
        )
        session.add(proj)
        session.flush()
        return CaptureTarget(
            project_id=proj.id,
            project_name=proj.name,
            camera_id=cam.id,
            interval_seconds=None,
            schedule=schedule,
            storage_path=str(storage),
            exact_time_anchors=[_ANCHOR],
        )


def _use_camera(supervisor: CaptureSupervisor, camera: FakeAxisCamera) -> None:
    """Route the supervisor's capture client to the fake camera."""
    supervisor._http_client = httpx.AsyncClient(transport=camera.transport())


def _snapshot_requests(camera: FakeAxisCamera) -> list[httpx.Request]:
    return [r for r in camera.requests if r.url.path == "/axis-cgi/jpg/image.cgi"]


class TestAnchorOnHttpsBasicPlusDigestCamera:
    @pytest.mark.parametrize("basic_first", [True, False])
    async def test_1205_anchor_captures_exactly_as_before(
        self, migrated_factory: Any, tmp_path: Path, basic_first: bool
    ) -> None:
        camera = FakeAxisCamera(basic_first=basic_first)
        clock = FakeClock(_INSTANT + timedelta(seconds=3))
        supervisor = _supervisor(migrated_factory, tmp_path, clock)
        _use_camera(supervisor, camera)
        target = _seed_https_camera(migrated_factory, tmp_path, camera)

        wake = await supervisor._evaluate_exact_time_anchors(target, clock.now())
        await supervisor.stop()

        fire = _fire(migrated_factory, target.project_id)
        assert (fire.status, fire.attempts, fire.next_attempt_at, fire.detail) == (
            "captured",
            1,
            None,
            None,
        )
        assert fire.frame_id is not None
        assert _frames(migrated_factory, target.project_id) == ["anchor:clock"]
        assert _events(migrated_factory, target.project_id) == [
            (
                "info",
                {
                    "reason": "anchor:clock",
                    "anchor_id": "noon",
                    "local_date": _DAY,
                    "dedup_key": f"{target.project_id}:noon:{_DAY}",
                },
            )
        ]
        assert wake == _INSTANT + timedelta(days=1)
        # Digest, never Basic: the password did not cross the wire.
        assert camera.accepted and set(camera.accepted) == {"digest"}
        for request in camera.requests:
            header = request.headers.get("authorization", "")
            assert not header.startswith("Basic")
            assert camera.password not in header
        # The snapshot went to the configured HTTPS host.
        snaps = _snapshot_requests(camera)
        assert [r.url.scheme for r in snaps] == ["https", "https"]
        assert [r.url.host for r in snaps] == ["driveway.cam.test"] * 2

    async def test_next_days_anchor_is_one_snapshot_request(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        camera = FakeAxisCamera()
        clock = FakeClock(_INSTANT + timedelta(seconds=3))
        supervisor = _supervisor(migrated_factory, tmp_path, clock)
        _use_camera(supervisor, camera)
        target = _seed_https_camera(migrated_factory, tmp_path, camera)

        await supervisor._evaluate_exact_time_anchors(target, clock.now())
        first_day = len(_snapshot_requests(camera))
        clock.current = _INSTANT + timedelta(days=1, seconds=3)
        await supervisor._evaluate_exact_time_anchors(target, clock.now())
        await supervisor.stop()

        assert first_day == 2  # challenge + Digest answer (was 3 before #69)
        assert len(_snapshot_requests(camera)) == 3  # day two: one request
        next_day = "2026-10-15"  # the day after _DAY, in New York
        assert _fire(migrated_factory, target.project_id, next_day).status == (
            "captured"
        )
        assert len(_frames(migrated_factory, target.project_id)) == 2

    async def test_a_rotated_nonce_still_captures_on_the_first_attempt(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        camera = FakeAxisCamera()
        clock = FakeClock(_INSTANT + timedelta(seconds=3))
        supervisor = _supervisor(migrated_factory, tmp_path, clock)
        _use_camera(supervisor, camera)
        target = _seed_https_camera(migrated_factory, tmp_path, camera)

        await supervisor._evaluate_exact_time_anchors(target, clock.now())
        camera.rotate_nonce(stale=False)  # the camera rebooted overnight
        clock.current = _INSTANT + timedelta(days=1, seconds=3)
        await supervisor._evaluate_exact_time_anchors(target, clock.now())
        await supervisor.stop()

        next_day = "2026-10-15"  # the day after _DAY, in New York
        fire = _fire(migrated_factory, target.project_id, next_day)
        assert (fire.status, fire.attempts) == ("captured", 1)
