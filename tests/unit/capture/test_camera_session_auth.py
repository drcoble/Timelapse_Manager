"""The camera session carries the HTTP auth state (#69 WP-69-H, CAM-P1/CAM-S1).

* One :class:`CameraAuthState` per cached session, shared by every capture of
  that camera (so Digest is negotiated once), with the camera's Basic policy.
* A config change or an authentication failure drops the session, and with it
  the negotiated state, so the next capture negotiates afresh.
* The Basic-over-HTTP flag is loaded, fingerprinted (toggling it reloads the
  session) and leaves every existing camera's fingerprint unchanged.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any
from unittest.mock import patch

from sqlalchemy.orm import Session, sessionmaker

from tests.conftest import FakeAdapter
from tests.unit.capture.test_camera_config import _add_camera
from tests.unit.capture.test_camera_sessions import (
    _BUILD,
    _BUILD_ONE_SHOT,
    _config,
    _inline,
    _Loader,
    _observe,
    _Recorder,
    _supervisor,
)
from tests.unit.test_supervisor import _seed_shared_camera
from timelapse_manager.cameras.base import AuthCaptureError, CapturedFrame
from timelapse_manager.cameras.http_auth import BasicPolicy
from timelapse_manager.capture.camera_config import config_version, load_capture_config
from timelapse_manager.capture.camera_sessions import CameraSessionRegistry
from timelapse_manager.capture.supervisor import CaptureState
from timelapse_manager.db.models import Camera
from timelapse_manager.db.session import session_scope


def _set_flag(factory: sessionmaker[Session], camera_id: int, value: bool) -> None:
    with session_scope(factory) as session:
        camera = session.get(Camera, camera_id)
        assert camera is not None
        camera.allow_basic_over_http = value


class TestRegistryAuthState:
    async def test_a_cached_session_keeps_one_state(self) -> None:
        registry = CameraSessionRegistry(_Loader(_config()), _inline)
        first = await registry.get(1)
        second = await registry.get(1)
        assert first.auth_state is second.auth_state

    async def test_invalidation_starts_a_fresh_state(self) -> None:
        registry = CameraSessionRegistry(_Loader(_config()), _inline)
        before = (await registry.get(1)).auth_state
        registry.invalidate(1)
        assert (await registry.get(1)).auth_state is not before

    async def test_the_state_has_the_camera_policy(self) -> None:
        registry = CameraSessionRegistry(
            _Loader(_config(allow_basic_over_http=True)), _inline
        )
        session = await registry.get(1)
        assert session.auth_state.policy == BasicPolicy.for_camera(True)
        assert "auth_state" not in repr(session)


class TestConfigFlag:
    def test_loaded_and_fingerprinted(
        self, migrated_factory: sessionmaker[Session]
    ) -> None:
        camera_id = _add_camera(migrated_factory, own=True, inherit=False, name="c")
        off = load_capture_config(migrated_factory, camera_id)
        _set_flag(migrated_factory, camera_id, True)
        on = load_capture_config(migrated_factory, camera_id)

        assert off is not None and on is not None
        assert (off.allow_basic_over_http, on.allow_basic_over_http) == (False, True)
        assert off.config_version != on.config_version

    def test_an_unset_flag_leaves_existing_fingerprints_alone(self) -> None:
        class _Cam:
            protocol = "vapix"
            address = "https://driveway.cam.test"
            credentials = None
            credentials_inherit_default = False
            snapshot_uri = None
            stream_uri = None
            default_resolution = None

        class _CamWithFlag(_Cam):
            allow_basic_over_http = False

        assert config_version(_Cam(), None) == config_version(_CamWithFlag(), None)


class TestSupervisorPassesTheState:
    async def test_interval_and_one_shot_captures_share_the_session_state(
        self, migrated_factory: sessionmaker[Session], tmp_path: Path
    ) -> None:
        from timelapse_manager.capture.one_shot import capture_one_now

        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["alpha"])
        supervisor = _supervisor(migrated_factory, tmp_path)
        state = CaptureState(project_id=target.project_id, camera_id=target.camera_id)
        recorder = _Recorder()
        with (
            patch(_BUILD, new=recorder),
            patch(_BUILD_ONE_SHOT, new=recorder),
            patch.object(supervisor._writer, "write") as write,
        ):
            write.return_value.project_frame_count = 1
            await supervisor._capture_once(target, state)
        with patch(_BUILD_ONE_SHOT, new=recorder):
            assert await capture_one_now(supervisor, target, reason="anchor:x")
        await supervisor.stop()

        states = [call["auth_state"] for call in recorder.calls]
        session = supervisor.camera_sessions.cached(target.camera_id)
        assert session is not None
        assert states == [session.auth_state, session.auth_state]

    async def test_an_auth_failure_drops_the_negotiated_state(
        self, migrated_factory: sessionmaker[Session], tmp_path: Path
    ) -> None:
        class _Rejected(FakeAdapter):
            async def capture(self) -> CapturedFrame:
                raise AuthCaptureError("authentication rejected (401)")

        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["alpha"])
        supervisor = _supervisor(migrated_factory, tmp_path)
        state = CaptureState(project_id=target.project_id, camera_id=target.camera_id)
        rejected = _Recorder(_Rejected)
        with patch(_BUILD, new=rejected):
            await supervisor._attempt_capture(target, state, random.Random(1))
        recorder = _Recorder()
        with patch(_BUILD, new=recorder):
            await supervisor._attempt_capture(target, state, random.Random(1))
        await supervisor.stop()

        assert recorder.calls[0]["auth_state"] is not rejected.calls[0]["auth_state"]

    async def test_toggling_the_flag_reaches_the_next_capture(
        self, migrated_factory: sessionmaker[Session], tmp_path: Path
    ) -> None:
        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["alpha"])
        supervisor = _supervisor(migrated_factory, tmp_path)
        state = CaptureState(project_id=target.project_id, camera_id=target.camera_id)
        recorder = _Recorder()
        with patch(_BUILD, new=recorder):
            await _observe(supervisor)
            await supervisor._capture_once(target, state)
            _set_flag(migrated_factory, target.camera_id, True)
            await _observe(supervisor)
            await supervisor._capture_once(target, state)
        await supervisor.stop()

        policies: list[Any] = [call["auth_state"].policy for call in recorder.calls]
        assert policies == [BasicPolicy.for_camera(False), BasicPolicy.for_camera(True)]
