"""Per-camera capture sessions (#69 WP-69-D, with WP-69-F merged).

Pins the session cache (CAM-P1), the named decrypt failure (CAM-R8), the
per-camera capture slot and the PTZ discipline (CAM-R4), and -- because a
missed, delayed or doubled capture is the worst outcome in this deployment --
that a capture through a cached session is the same capture as a cold one.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from tests.conftest import FakeAdapter
from tests.unit.capture.test_camera_config import _DEFAULT, _add_camera, _set_default
from tests.unit.credential_binding_support import (  # noqa: F401
    _allowed_private_subnets,
)
from tests.unit.test_supervisor import (
    _health_events,
    _health_instant,
    _health_reasons,
    _health_supervisor,
    _HealthClock,
    _seed_shared_camera,
)
from timelapse_manager.cameras.base import (
    AuthCaptureError,
    CameraMissingError,
    CapturedFrame,
    OtherCaptureError,
    PtzPosition,
    TimeoutCaptureError,
)
from timelapse_manager.cameras.clients import allow_uncontained_double
from timelapse_manager.cameras.onvif import OnvifAdapter
from timelapse_manager.cameras.registry import (
    UNSET,
    build_adapter,
    effective_credentials,
)
from timelapse_manager.cameras.vapix import VapixAdapter
from timelapse_manager.capture import supervisor as sup_mod
from timelapse_manager.capture.camera_config import (
    CaptureCameraConfig,
    config_version,
    load_capture_config,
    resolve_capture_credentials,
)
from timelapse_manager.capture.camera_sessions import (
    PTZ_POSITION_TRUST_SECONDS,
    CameraSessionRegistry,
    TtlCache,
)
from timelapse_manager.capture.supervisor import (
    CaptureState,
    CaptureSupervisor,
    CaptureTarget,
)
from timelapse_manager.db.models import Camera, ExactTimeFire, Frame
from timelapse_manager.db.models.camera_default_credentials import (
    CameraDefaultCredentials,
)
from timelapse_manager.db.session import session_scope
from timelapse_manager.security.crypto import (
    CREDENTIAL_DECRYPT_MESSAGE,
    CredentialDecryptError,
    encrypt_credentials,
)

_BUILD = "timelapse_manager.capture.supervisor.build_adapter"
_BUILD_ONE_SHOT = "timelapse_manager.capture.one_shot.build_adapter"


# ---------------------------------------------------------------------------
# The registry on its own
# ---------------------------------------------------------------------------


def _config(version: str = "v1", **overrides: Any) -> CaptureCameraConfig:
    values: dict[str, Any] = {
        "protocol": "vapix",
        "address": "10.0.16.5",
        "credentials": None,
        "credentials_inherit_default": False,
        "snapshot_uri": None,
        "stream_uri": None,
        "default_resolution": None,
        "config_version": version,
    }
    values.update(overrides)
    return CaptureCameraConfig(**values)


async def _inline(fn: Callable[..., Any], *args: Any) -> Any:
    return fn(*args)


class _Loader:
    def __init__(self, *configs: CaptureCameraConfig | None) -> None:
        self.configs = list(configs)
        self.calls = 0

    def __call__(self, camera_id: int) -> CaptureCameraConfig | None:
        self.calls += 1
        return self.configs[min(self.calls, len(self.configs)) - 1]


class TestRegistryCache:
    async def test_a_hit_costs_no_load(self) -> None:
        loader = _Loader(_config())
        registry = CameraSessionRegistry(loader, _inline)
        first = await registry.get(7)
        second = await registry.get(7)
        assert first is second
        assert loader.calls == 1

    async def test_invalidate_reloads(self) -> None:
        loader = _Loader(_config("v1"), _config("v2"))
        registry = CameraSessionRegistry(loader, _inline)
        await registry.get(7)
        registry.invalidate(7)
        assert (await registry.get(7)).config_version == "v2"
        assert loader.calls == 2

    async def test_observe_drops_a_changed_or_unused_camera_only(self) -> None:
        loader = _Loader(_config("v1"))
        registry = CameraSessionRegistry(loader, _inline)
        for camera_id in (1, 2, 3):
            await registry.get(camera_id)
        registry.observe({1: "v1", 2: "v2"})
        assert registry.cached(1) is not None  # unchanged
        assert registry.cached(2) is None  # its configuration changed
        assert registry.cached(3) is None  # no longer used by any project

    async def test_a_load_older_than_the_last_observed_version_is_not_kept(
        self,
    ) -> None:
        loader = _Loader(_config("v1"))
        registry = CameraSessionRegistry(loader, _inline)
        registry.observe({7: "v2"})  # an edit committed while the load ran
        session = await registry.get(7)
        assert session.config_version == "v1"  # this capture uses what it read
        assert registry.cached(7) is None  # but the next one reloads

    async def test_concurrent_misses_share_one_load(self) -> None:
        loader = _Loader(_config())

        async def slow(fn: Callable[..., Any], *args: Any) -> Any:
            await asyncio.sleep(0.02)
            return fn(*args)

        registry = CameraSessionRegistry(loader, slow)
        sessions = await asyncio.gather(*(registry.get(7) for _ in range(4)))
        assert loader.calls == 1
        assert all(s is sessions[0] for s in sessions)

    async def test_a_deleted_camera_is_a_local_missing_error(self) -> None:
        registry = CameraSessionRegistry(_Loader(None), _inline)
        with pytest.raises(CameraMissingError) as raised:
            await registry.get(7)
        assert raised.value.local is True

    async def test_a_credential_that_cannot_be_decrypted_is_named_and_local(
        self,
    ) -> None:
        def broken(_camera_id: int) -> CaptureCameraConfig:
            raise CredentialDecryptError

        registry = CameraSessionRegistry(broken, _inline)
        with pytest.raises(OtherCaptureError) as raised:
            await registry.get(7)
        assert raised.value.local is True
        assert str(raised.value) == CREDENTIAL_DECRYPT_MESSAGE
        assert registry.cached(7) is None


class TestTtlCache:
    def test_entries_expire(self) -> None:
        now = [0.0]
        cache = TtlCache(10.0, clock=lambda: now[0])
        cache.put("k", "v")
        now[0] = 9.9
        assert cache.get("k") == "v"
        now[0] = 10.0
        assert cache.get("k") is None


class TestCaptureSlot:
    async def test_two_shared_slots_then_a_queue(self) -> None:
        registry = CameraSessionRegistry(_Loader(_config()), _inline)
        active = 0
        peak = 0

        async def capture() -> None:
            nonlocal active, peak
            async with registry.capture_slot(7, exclusive=False, wait_seconds=5):
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.02)
                active -= 1

        await asyncio.gather(*(capture() for _ in range(4)))
        assert peak == 2

    async def test_a_ptz_camera_runs_one_capture_at_a_time(self) -> None:
        registry = CameraSessionRegistry(_Loader(_config()), _inline)
        registry.set_ptz_cameras({7})
        active = 0
        peak = 0

        async def capture() -> None:
            nonlocal active, peak
            async with registry.capture_slot(7, exclusive=False, wait_seconds=5):
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.02)
                active -= 1

        await asyncio.gather(*(capture() for _ in range(3)))
        assert peak == 1

    async def test_a_slot_wait_that_runs_out_is_a_local_camera_busy(self) -> None:
        registry = CameraSessionRegistry(_Loader(_config()), _inline)
        async with registry.capture_slot(7, exclusive=True, wait_seconds=5):
            with pytest.raises(OtherCaptureError, match="camera busy") as raised:
                async with registry.capture_slot(7, exclusive=True, wait_seconds=0.05):
                    pass  # pragma: no cover
        assert raised.value.local is True
        # The slot is free again afterwards.
        async with registry.capture_slot(7, exclusive=True, wait_seconds=0.05):
            pass

    def test_a_confirmed_position_is_trusted_for_a_while(self) -> None:
        now = [100.0]
        registry = CameraSessionRegistry(
            _Loader(_config()), _inline, clock=lambda: now[0]
        )
        registry.note_position(7, ("preset", "gate"))
        assert registry.position_confirmed(7, ("preset", "gate"))
        assert not registry.position_confirmed(7, ("preset", "yard"))
        now[0] += PTZ_POSITION_TRUST_SECONDS
        assert not registry.position_confirmed(7, ("preset", "gate"))


class TestBuildAdapterCredentials:
    def test_given_credentials_win_and_none_means_none(self) -> None:
        config = _config(credentials_inherit_default=True)
        given = build_adapter(
            config, AsyncMock(), default_credentials=_DEFAULT, credentials=("u", "p")
        )
        assert given._credentials == ("u", "p")  # type: ignore[attr-defined]
        none = build_adapter(
            config, AsyncMock(), default_credentials=_DEFAULT, credentials=None
        )
        assert none._credentials is None  # type: ignore[attr-defined]
        legacy = build_adapter(
            config,
            allow_uncontained_double(AsyncMock()),
            default_credentials=_DEFAULT,
            credentials=UNSET,
        )
        assert legacy._credentials == _DEFAULT  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Through the supervisor, against the database
# ---------------------------------------------------------------------------


class _Recorder:
    """Patches ``build_adapter`` and records what each capture was built with."""

    def __init__(self, adapter_factory: Callable[[], Any] = FakeAdapter) -> None:
        self.calls: list[dict[str, Any]] = []
        self._factory = adapter_factory

    def __call__(self, camera: Any, client: Any, **kwargs: Any) -> Any:
        self.calls.append({"camera": camera, **kwargs})
        return self._factory()


def _supervisor(factory: sessionmaker[Session], tmp_path: Path) -> CaptureSupervisor:
    return _health_supervisor(factory, tmp_path, threshold=3)


def _target(camera_id: int, project_id: int = 1, **kw: Any) -> CaptureTarget:
    return CaptureTarget(
        project_id=project_id,
        project_name=f"p{project_id}",
        camera_id=camera_id,
        interval_seconds=60,
        **kw,
    )


async def _observe(supervisor: CaptureSupervisor) -> None:
    """What a reconcile pass does for the session cache."""
    targets = await supervisor._run_blocking(supervisor._load_targets)
    supervisor._observe_camera_configs(targets)


def _set_password(factory: sessionmaker[Session], camera_id: int, pw: str) -> None:
    with session_scope(factory) as session:
        camera = session.get(Camera, camera_id)
        assert camera is not None
        camera.credentials = encrypt_credentials({"username": "own", "password": pw})


class TestSupervisorSessions:
    async def test_a_cached_capture_uses_the_same_config_and_credentials(
        self, migrated_factory: sessionmaker[Session], tmp_path: Path
    ) -> None:
        camera_id = _add_camera(migrated_factory, own=True, inherit=False, name="c")
        supervisor = _supervisor(migrated_factory, tmp_path)
        state = CaptureState(project_id=1, camera_id=camera_id)
        recorder = _Recorder()
        real_load = supervisor._load_capture_config
        with (
            patch(_BUILD, new=recorder),
            patch.object(supervisor, "_load_capture_config", wraps=real_load) as loads,
            patch.object(supervisor._writer, "write") as write,
        ):
            write.return_value.project_frame_count = 1
            await supervisor._capture_once(_target(camera_id), state)
            await supervisor._capture_once(_target(camera_id), state)
        await supervisor.stop()

        assert loads.call_count == 1  # the second capture was a cache hit
        cold, warm = recorder.calls
        assert cold["credentials"] == warm["credentials"] == ("own", "own-pw")
        assert cold["camera"] is warm["camera"]
        assert cold["uri_cache"] is warm["uri_cache"]
        assert write.call_count == 2

    async def test_an_edit_seen_by_reconcile_reaches_the_next_capture(
        self, migrated_factory: sessionmaker[Session], tmp_path: Path
    ) -> None:
        """Two edits in the same second still reload (a fingerprint, not a time)."""
        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["alpha"])
        _set_password(migrated_factory, target.camera_id, "first")
        supervisor = _supervisor(migrated_factory, tmp_path)
        state = CaptureState(project_id=target.project_id, camera_id=target.camera_id)
        recorder = _Recorder()
        with patch(_BUILD, new=recorder):
            await _observe(supervisor)
            await supervisor._capture_once(target, state)
            _set_password(migrated_factory, target.camera_id, "second")
            await _observe(supervisor)
            await supervisor._capture_once(target, state)
            _set_password(migrated_factory, target.camera_id, "third")
            await _observe(supervisor)
            await supervisor._capture_once(target, state)
        await supervisor.stop()

        assert [c["credentials"][1] for c in recorder.calls] == [
            "first",
            "second",
            "third",
        ]

    async def test_an_unchanged_camera_stays_cached_across_reconcile(
        self, migrated_factory: sessionmaker[Session], tmp_path: Path
    ) -> None:
        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["alpha"])
        supervisor = _supervisor(migrated_factory, tmp_path)
        state = CaptureState(project_id=target.project_id, camera_id=target.camera_id)
        with patch(_BUILD, new=_Recorder()):
            await _observe(supervisor)
            await supervisor._capture_once(target, state)
            await _observe(supervisor)
            await supervisor._capture_once(target, state)
        await supervisor.stop()
        assert supervisor.camera_sessions.loads == 1

    async def test_a_401_drops_the_session_so_the_retry_reloads(
        self, migrated_factory: sessionmaker[Session], tmp_path: Path
    ) -> None:
        import random

        class _Rejected(FakeAdapter):
            async def capture(self) -> CapturedFrame:
                raise AuthCaptureError("401 Unauthorized")

        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["alpha"])
        _set_password(migrated_factory, target.camera_id, "stale")
        supervisor = _supervisor(migrated_factory, tmp_path)
        state = CaptureState(project_id=target.project_id, camera_id=target.camera_id)
        with patch(_BUILD, new=_Recorder(_Rejected)):
            await supervisor._attempt_capture(target, state, random.Random(1))
        assert supervisor.camera_sessions.cached(target.camera_id) is None
        # The operator fixes the password; the retry needs no reconcile pass.
        _set_password(migrated_factory, target.camera_id, "fixed")
        recorder = _Recorder()
        with patch(_BUILD, new=recorder):
            await supervisor._attempt_capture(target, state, random.Random(1))
        await supervisor.stop()
        assert recorder.calls[0]["credentials"] == ("own", "fixed")
        assert state.last_error is None

    async def test_a_corrupted_default_stops_only_the_inheriting_camera(
        self, migrated_factory: sessionmaker[Session], tmp_path: Path
    ) -> None:
        """CAM-R8: a named, local failure; no offline alert; others unaffected."""
        import random

        _set_default(migrated_factory, present=True)
        with session_scope(migrated_factory) as session:
            session.execute(
                update(CameraDefaultCredentials).values(password="enc:v1:not-a-token")
            )
        own_id = _add_camera(migrated_factory, own=True, inherit=True, name="own")
        inherit_id = _add_camera(migrated_factory, own=False, inherit=True, name="inh")
        supervisor = _health_supervisor(migrated_factory, tmp_path, threshold=1)
        own_state = CaptureState(project_id=1, camera_id=own_id)
        inherit_state = CaptureState(project_id=2, camera_id=inherit_id)
        with (
            patch(_BUILD, new=_Recorder()),
            patch.object(supervisor._writer, "write") as write,
        ):
            write.return_value.project_frame_count = 1
            assert (
                await supervisor._attempt_capture(
                    _target(own_id, 1), own_state, random.Random(1)
                )
                == 1
            )
            for _ in range(3):
                assert (
                    await supervisor._attempt_capture(
                        _target(inherit_id, 2), inherit_state, random.Random(1)
                    )
                    is None
                )
        await supervisor.stop()

        assert inherit_state.last_error == CREDENTIAL_DECRYPT_MESSAGE
        assert own_state.last_error is None
        assert _health_reasons(migrated_factory, scope="camera") == []
        system = _health_events(migrated_factory, scope="system")
        assert [(lvl, m["reason"]) for lvl, _, m in system] == [
            ("info", "capture_local_error")
        ]
        assert CREDENTIAL_DECRYPT_MESSAGE in system[0][1]
        assert "not-a-token" not in repr(system)

    async def test_one_shot_captures_go_through_the_session_and_the_slot(
        self, migrated_factory: sessionmaker[Session], tmp_path: Path
    ) -> None:
        from timelapse_manager.capture.one_shot import capture_one_now

        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["alpha"])
        supervisor = _supervisor(migrated_factory, tmp_path)
        recorder = _Recorder()
        slots: list[int] = []
        real_slot = supervisor.capture_slot

        def spy_slot(t: CaptureTarget, **kwargs: Any) -> Any:
            slots.append(t.camera_id)
            return real_slot(t, **kwargs)

        with (
            patch(_BUILD_ONE_SHOT, new=recorder),
            patch.object(supervisor, "capture_slot", new=spy_slot),
        ):
            for _ in range(2):
                assert await capture_one_now(supervisor, target, reason="anchor:x")
        await supervisor.stop()
        assert supervisor.camera_sessions.loads == 1
        assert slots == [target.camera_id] * 2
        assert recorder.calls[0]["credentials"] == recorder.calls[1]["credentials"]


@pytest.mark.parametrize(
    ("own", "inherit", "default_present"),
    list(itertools.product([True, False], repeat=3)),
)
def test_session_credentials_equal_the_pre_cache_resolution(
    migrated_factory: sessionmaker[Session],
    own: bool,
    inherit: bool,
    default_present: bool,
) -> None:
    """The cached path authenticates with exactly the pair it did before."""
    _set_default(migrated_factory, present=default_present)
    camera_id = _add_camera(migrated_factory, own=own, inherit=inherit, name="cam")
    config = load_capture_config(migrated_factory, camera_id)
    assert config is not None
    assert resolve_capture_credentials(config) == effective_credentials(
        config, config.default_credentials
    )


@pytest.mark.parametrize(
    "stored",
    [
        {"username": "legacy", "password": "plain-pw"},  # written before encryption
        {"username": "nopw"},
        {"username": "", "password": "orphan"},
        {"password": "only"},
    ],
)
def test_legacy_and_partial_documents_resolve_as_before(
    migrated_factory: sessionmaker[Session], stored: dict[str, str]
) -> None:
    with session_scope(migrated_factory) as session:
        camera = Camera(
            name="legacy",
            address="10.0.16.5",
            protocol="vapix",
            credentials=stored,
            credentials_inherit_default=False,
        )
        session.add(camera)
        session.flush()
        camera_id = camera.id
    config = load_capture_config(migrated_factory, camera_id)
    assert config is not None
    assert resolve_capture_credentials(config) == effective_credentials(config, None)


def test_config_version_tracks_the_stored_values() -> None:
    class _Cam:
        protocol = "vapix"
        address = "10.0.16.5"
        credentials: dict[str, str] | None = {"username": "u", "password": "c1"}
        credentials_inherit_default = True
        snapshot_uri = None
        stream_uri = None
        default_resolution = None

    class _Defaults:
        enabled = True
        username = "d"
        password = "x1"

    base = config_version(_Cam(), _Defaults())
    assert base == config_version(_Cam(), _Defaults())
    # A camera with its own credentials does not depend on the default row.
    _Defaults.password = "x2"
    assert config_version(_Cam(), _Defaults()) == base
    _Cam.credentials = {"username": "u", "password": "c2"}
    assert config_version(_Cam(), _Defaults()) != base
    _Cam.credentials = None
    inheriting = config_version(_Cam(), _Defaults())
    _Defaults.password = "x3"
    assert config_version(_Cam(), _Defaults()) != inheriting


def test_the_anchor_attempt_outlasts_a_slot_wait_and_stays_under_the_wedge(
    tmp_path: Path, migrated_factory: sessionmaker[Session]
) -> None:
    """At default settings: 50 s slot wait, 120 s attempt, 685 s wedge."""
    from timelapse_manager.config.settings import CaptureSettings

    supervisor = _health_supervisor(migrated_factory, tmp_path)
    supervisor._settings = supervisor._settings.model_copy(
        update={"capture": CaptureSettings()}
    )
    slot_wait = supervisor._slot_wait_seconds()
    attempt = supervisor._anchor_fire_timeout()
    timeout = supervisor._settings.capture.timeout_seconds
    assert (slot_wait, attempt) == (50.0, 120.0)
    assert attempt > slot_wait + timeout
    guard = attempt + sup_mod._ANCHOR_FIRE_MARGIN_SECONDS
    assert timedelta(seconds=guard) < supervisor._wedge_threshold()


# ---------------------------------------------------------------------------
# Four anchor projects on one camera (prod camera 2)
# ---------------------------------------------------------------------------


class _SlowCapture(FakeAdapter):
    active = 0
    peak = 0

    async def capture(self) -> CapturedFrame:
        cls = type(self)
        cls.active += 1
        cls.peak = max(cls.peak, cls.active)
        try:
            await asyncio.sleep(1.2)  # a slow camera: 60 % of the 2 s budget
            return await super().capture()
        finally:
            cls.active -= 1


async def test_four_anchor_projects_on_one_camera_all_capture_once(
    migrated_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Two at a time on the camera; the queued two wait outside the budget.

    Each capture takes 1.2 s of a 2 s budget. Were the slot wait inside the
    budget, the queued pair would need 2.4 s and time out.
    """
    from timelapse_manager.config.settings import CaptureSettings

    clock = _HealthClock(_health_instant() + timedelta(seconds=3))
    supervisor = _health_supervisor(migrated_factory, tmp_path, clock=clock)
    supervisor._settings = supervisor._settings.model_copy(
        update={
            "capture": CaptureSettings(
                autostart=False, timeout_seconds=2.0, offline_failure_threshold=3
            )
        }
    )
    targets = _seed_shared_camera(
        migrated_factory, tmp_path, ["a", "b", "c", "d"], anchors=True
    )
    _SlowCapture.active = _SlowCapture.peak = 0
    with patch(_BUILD_ONE_SHOT, lambda *a, **k: _SlowCapture()):
        await asyncio.gather(
            *(supervisor._evaluate_exact_time_anchors(t, clock.now()) for t in targets)
        )
    await supervisor.stop()

    assert _SlowCapture.peak == 2
    with session_scope(migrated_factory) as session:
        for target in targets:
            frames = session.query(Frame).filter_by(project_id=target.project_id).all()
            assert len(frames) == 1
            (fire,) = (
                session.query(ExactTimeFire)
                .filter_by(project_id=target.project_id)
                .all()
            )
            assert (fire.status, fire.attempts) == ("captured", 1)
    assert "anchor_retry" not in _health_reasons(migrated_factory, scope="project")
    assert _health_events(migrated_factory, scope="camera") == []
    assert supervisor.camera_sessions.loads == 1


# ---------------------------------------------------------------------------
# PTZ discipline
# ---------------------------------------------------------------------------


class _PtzRecorder(FakeAdapter):
    def __init__(self, moves: list[dict[str, Any]], delay: float = 0.0) -> None:
        super().__init__()
        self._moves = moves
        self._delay = delay

    async def move_to(self, **kwargs: Any) -> None:
        self._moves.append(kwargs)
        await asyncio.sleep(self._delay)


class TestPtzDiscipline:
    async def test_an_unchanged_position_is_not_sent_again(
        self, migrated_factory: sessionmaker[Session], tmp_path: Path
    ) -> None:
        camera_id = _add_camera(migrated_factory, own=True, inherit=False, name="d")
        supervisor = _supervisor(migrated_factory, tmp_path)
        moves: list[dict[str, Any]] = []
        gate = _target(camera_id, 1, ptz_preset="gate")
        yard = _target(camera_id, 2, ptz_preset="yard")
        state = CaptureState(project_id=1, camera_id=camera_id)
        with (
            patch(_BUILD, new=_Recorder(lambda: _PtzRecorder(moves))),
            patch.object(supervisor._writer, "write") as write,
        ):
            write.return_value.project_frame_count = 1
            for target in (gate, gate, yard, gate):
                await supervisor._capture_once(target, state)
        await supervisor.stop()
        assert [m["preset_id"] for m in moves] == ["gate", "yard", "gate"]

    async def test_a_hung_move_fails_bounded_as_a_timeout(
        self, migrated_factory: sessionmaker[Session], tmp_path: Path
    ) -> None:
        camera_id = _add_camera(migrated_factory, own=True, inherit=False, name="h")
        supervisor = _supervisor(migrated_factory, tmp_path)
        moves: list[dict[str, Any]] = []
        state = CaptureState(project_id=1, camera_id=camera_id)
        started = time.monotonic()
        with (
            patch.object(sup_mod, "PTZ_SETTLE_MAX_SECONDS", 0.1),
            patch(_BUILD, new=_Recorder(lambda: _PtzRecorder(moves, delay=30))),
            pytest.raises(TimeoutCaptureError, match="PTZ positioning"),
        ):
            await supervisor._capture_once(_target(camera_id, ptz_preset="g"), state)
        await supervisor.stop()
        assert time.monotonic() - started < 5
        # Not confirmed, so the next capture sends the move again.
        assert not supervisor.camera_sessions.position_confirmed(
            camera_id, ("preset", "g")
        )

    async def test_vapix_settles_when_two_readings_agree(self) -> None:
        readings = iter(
            [
                PtzPosition(10, -5, 1),
                PtzPosition(40, -5, 1),
                PtzPosition(90, -9, 2),
                PtzPosition(90, -9, 2),
                PtzPosition(0, 0, 0),
            ]
        )
        adapter = VapixAdapter(AsyncMock(), address="10.0.16.5")
        queries = 0

        async def position() -> PtzPosition:
            nonlocal queries
            queries += 1
            return next(readings)

        vapix = "timelapse_manager.cameras.vapix"
        with (
            patch(f"{vapix}.http_get_image", new=AsyncMock(return_value=b"")),
            patch(f"{vapix}.PTZ_SETTLE_POLL_SECONDS", 0.01),
            patch(f"{vapix}.PTZ_SETTLE_MIN_SECONDS", 0.0),
            patch.object(adapter, "query_position", new=position),
        ):
            await adapter.move_to(preset_id="gate")
        assert queries == 4

    async def test_vapix_settle_is_bounded_while_the_dome_keeps_moving(self) -> None:
        adapter = VapixAdapter(AsyncMock(), address="10.0.16.5")
        count = 0

        async def position() -> PtzPosition:
            nonlocal count
            count += 1
            return PtzPosition(float(count), 0, 0)

        vapix = "timelapse_manager.cameras.vapix"
        started = time.monotonic()
        with (
            patch(f"{vapix}.http_get_image", new=AsyncMock(return_value=b"")),
            patch(f"{vapix}.PTZ_SETTLE_POLL_SECONDS", 0.01),
            patch(f"{vapix}.PTZ_SETTLE_MAX_SECONDS", 0.2),
            patch.object(adapter, "query_position", new=position),
        ):
            await adapter.move_to(preset_id="gate")
        assert 0.2 <= time.monotonic() - started < 2

    def test_position_body_parses(self) -> None:
        from timelapse_manager.cameras.vapix import parse_ptz_position

        assert parse_ptz_position("pan=12.5\ntilt=-3\nzoom=1\n") == PtzPosition(
            12.5, -3.0, 1.0
        )
        assert parse_ptz_position("Error: no PTZ") is None


# ---------------------------------------------------------------------------
# Adapter caches
# ---------------------------------------------------------------------------


class TestOnvifUriCache:
    async def test_a_second_capture_on_the_session_is_one_request(self) -> None:
        cache = TtlCache()
        soap = AsyncMock(side_effect=["<profiles/>", "<snapshot/>"])
        get_image = AsyncMock(return_value=FakeAdapter._BYTES)
        onvif = "timelapse_manager.cameras.onvif"
        with (
            patch(f"{onvif}.parse_first_profile_token", return_value="prof1"),
            patch(f"{onvif}.parse_uri", return_value="https://10.0.16.5/snap.jpg"),
            patch(f"{onvif}.http_get_image", new=get_image),
        ):
            for _ in range(2):
                adapter = OnvifAdapter(AsyncMock(), "10.0.16.5", uri_cache=cache)
                adapter._soap_call = soap  # type: ignore[method-assign]
                await adapter.capture()
        assert soap.await_count == 2  # GetProfiles + GetSnapshotUri, once
        assert get_image.await_count == 2  # one snapshot GET per capture

    async def test_a_failed_capture_through_the_cache_drops_it(self) -> None:
        cache = TtlCache()
        soap = AsyncMock(side_effect=["<p/>", "<s/>", "<p/>", "<s/>"])
        get_image = AsyncMock(side_effect=[AuthCaptureError("401"), FakeAdapter._BYTES])
        onvif = "timelapse_manager.cameras.onvif"
        with (
            patch(f"{onvif}.parse_first_profile_token", return_value="prof1"),
            patch(f"{onvif}.parse_uri", return_value="https://10.0.16.5/snap.jpg"),
            patch(f"{onvif}.http_get_image", new=get_image),
        ):
            adapter = OnvifAdapter(AsyncMock(), "10.0.16.5", uri_cache=cache)
            adapter._soap_call = soap  # type: ignore[method-assign]
            with pytest.raises(AuthCaptureError):
                await adapter.capture()
            assert len(cache) == 0
            adapter = OnvifAdapter(AsyncMock(), "10.0.16.5", uri_cache=cache)
            adapter._soap_call = soap  # type: ignore[method-assign]
            await adapter.capture()
        assert soap.await_count == 4  # resolved afresh after the failure


class TestVapixKnobCache:
    async def test_the_profile_is_read_once_per_ttl(self) -> None:
        now = [0.0]
        cache = TtlCache(3600.0, clock=lambda: now[0])
        read = AsyncMock(
            return_value={
                "root.StreamProfile.S0.Name": "hd",
                "root.StreamProfile.S0.Parameters": "resolution=1920x1080",
            }
        )
        get_image = AsyncMock(return_value=FakeAdapter._BYTES)
        vapix = "timelapse_manager.cameras.vapix"
        urls: list[str] = []

        async def record(client: Any, url: str, *a: Any, **k: Any) -> bytes:
            urls.append(url)
            return FakeAdapter._BYTES

        get_image.side_effect = record
        with patch(f"{vapix}.http_get_image", new=get_image):
            for step in (0.0, 10.0, 3600.0):
                now[0] = step
                adapter = VapixAdapter(
                    AsyncMock(), "10.0.16.5", stream_id="hd", knob_cache=cache
                )
                adapter._read_params = read  # type: ignore[method-assign]
                await adapter.capture()
        assert read.await_count == 2  # cold, then again after the TTL
        assert all("resolution=1920x1080" in url for url in urls)

    async def test_a_degraded_read_is_not_cached(self) -> None:
        cache = TtlCache()
        read = AsyncMock(return_value={})  # unreadable group
        vapix = "timelapse_manager.cameras.vapix"
        with patch(
            f"{vapix}.http_get_image", new=AsyncMock(return_value=FakeAdapter._BYTES)
        ):
            for _ in range(2):
                adapter = VapixAdapter(
                    AsyncMock(), "10.0.16.5", stream_id="hd", knob_cache=cache
                )
                adapter._read_params = read  # type: ignore[method-assign]
                frame = await adapter.capture()
                assert frame.capture_notes  # the degradation is still reported
        assert read.await_count == 2
        assert len(cache) == 0
