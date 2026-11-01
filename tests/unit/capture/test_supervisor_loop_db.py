"""The capture path's database reads stay off the event loop (#63).

The loop guard records database work on the event loop in background tasks
(#62). Two capture reads used to run there and were allow-listed against #63:

* the frame-cap pre-capture guard (``CaptureSupervisor._run_cycle``);
* the global default-credentials lookup when an event listener subscribes
  (``EventListenerFactory._onvif_pullpoint_events`` / ``_vapix_ws_events``).

Both now ride a thread hop, and ``KNOWN_BACKGROUND_LOOP_DB`` is empty. These
tests run the same scenarios the way production runs them (the supervisor's
runner and listener as their own tasks) and assert that no background violation
is recorded. The autouse guard in ``tests/conftest.py`` would also fail them,
since background violations are gated in every test directory.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests import loop_guard_support as guard
from tests.conftest import FakeAdapter
from tests.unit.credential_binding_support import (  # noqa: F401
    _allowed_private_subnets,
)
from tests.unit.test_campaign_bounds import (
    _INTERVAL,
    _UTC,
    FakeClock,
    _make_settings,
    _permissive_disk_monitor,
    _seed_camera_and_project,
)
from timelapse_manager.capture.camera_config import CaptureCameraConfig
from timelapse_manager.capture.event_listener import EventListenerFactory
from timelapse_manager.capture.supervisor import (
    CaptureState,
    CaptureSupervisor,
    CaptureTarget,
)
from timelapse_manager.db.models import Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.security.camera_defaults_service import (
    CameraDefaultsUpdate,
    update_settings,
)

_CAPTURE_PREFIX = "timelapse_manager.capture."


def _background_origins() -> set[str]:
    return {v.key for v in guard.collect() if v.background}


def _capture_origins() -> set[str]:
    return {o for o in _background_origins() if o.startswith(_CAPTURE_PREFIX)}


def test_loop_db_allow_lists_are_empty() -> None:
    """No route and no background origin may query on the loop (#62, #63).

    A failure here means an entry was added. Fix the read instead: move it into
    a thread hop (in capture/, through ``CaptureSupervisor._run_blocking``). See
    the 0.2.0 backlog plan, WP-63-A.
    """
    assert guard.KNOWN_LOOP_DB == {}
    assert guard.KNOWN_BACKGROUND_LOOP_DB == {}


class _CountingAdapter(FakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.captures = 0

    async def capture(self) -> Any:
        self.captures += 1
        return await super().capture()


def _supervisor(migrated_factory: Any, tmp_path: Path) -> CaptureSupervisor:
    return CaptureSupervisor(
        _make_settings(tmp_path),
        migrated_factory,
        clock=FakeClock(start=datetime(2026, 7, 1, 8, 0, tzinfo=_UTC)),
        disk_monitor=_permissive_disk_monitor(),
    )


async def test_frame_cap_guard_runs_off_the_loop(
    migrated_factory: Any, tmp_path: Path
) -> None:
    """A capped project at its cap ends its campaign with no loop-DB read."""
    supervisor = _supervisor(migrated_factory, tmp_path)
    ctx = _seed_camera_and_project(
        migrated_factory, tmp_path, name="atcap", max_frame_count=2
    )
    with session_scope(migrated_factory) as session:
        project = session.get(Project, ctx["project_id"])
        assert project is not None
        project.frame_count = 2
    target = CaptureTarget(
        project_id=ctx["project_id"],
        project_name="atcap",
        camera_id=ctx["camera_id"],
        interval_seconds=_INTERVAL,
        storage_path=str(ctx["storage_path"]),
        max_frame_count=2,
    )
    state = CaptureState(project_id=ctx["project_id"], camera_id=ctx["camera_id"])
    adapter = _CountingAdapter()

    with patch(
        "timelapse_manager.capture.supervisor.build_adapter", return_value=adapter
    ):
        # Its own task, as the supervisor launches a project runner.
        runner = asyncio.create_task(supervisor._run_project(target, state))
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(runner, 10.0)
    await supervisor.stop()

    # The campaign still ends at the cap, without a capture attempt.
    assert state.state == "stopped"
    assert adapter.captures == 0
    with session_scope(migrated_factory) as session:
        project = session.get(Project, ctx["project_id"])
        assert project is not None
        assert project.lifecycle_state == "archived"
    assert _capture_origins() == set()


async def test_uncapped_project_makes_no_cap_hop(
    migrated_factory: Any, tmp_path: Path
) -> None:
    """An uncapped project's cycle never sends the cap read to a thread."""
    supervisor = _supervisor(migrated_factory, tmp_path)
    ctx = _seed_camera_and_project(migrated_factory, tmp_path, name="nocap")
    target = CaptureTarget(
        project_id=ctx["project_id"],
        project_name="nocap",
        camera_id=ctx["camera_id"],
        interval_seconds=_INTERVAL,
        storage_path=str(ctx["storage_path"]),
    )
    state = CaptureState(project_id=ctx["project_id"], camera_id=ctx["camera_id"])

    hopped: list[Callable[..., Any]] = []
    real = supervisor._run_blocking

    async def spy(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        hopped.append(fn)
        return await real(fn, *args, **kwargs)

    with (
        patch.object(supervisor, "_run_blocking", side_effect=spy),
        patch(
            "timelapse_manager.capture.supervisor.build_adapter",
            return_value=FakeAdapter(),
        ),
    ):
        runner = asyncio.create_task(supervisor._run_project(target, state))
        try:
            await _wait_until(lambda: state.frames_captured >= 1)
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
    await supervisor.stop()

    names = [getattr(fn, "__name__", repr(fn)) for fn in hopped]
    assert "_frame_cap_reached" not in names
    # It did capture, loading the camera session (the merged single-hop loader
    # plus the credential decrypt) in one hop (#69 CAM-P1).
    assert names.count("_load") == 1
    assert _capture_origins() == set()


def _camera(protocol: str, *, own_credentials: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        protocol=protocol,
        address="10.0.0.5",
        credentials={"username": "own", "password": "x"} if own_credentials else None,
        credentials_inherit_default=True,
        snapshot_uri=None,
        stream_uri=None,
        default_resolution=None,
    )


async def _parked_source() -> AsyncIterator[Any]:
    await asyncio.Event().wait()
    yield  # pragma: no cover - makes this an async generator


async def _failing_source() -> AsyncIterator[Any]:
    raise ConnectionError("websocket upgrade refused")
    yield  # pragma: no cover - makes this an async generator


def _event_adapter(source: Callable[[], AsyncIterator[Any]]) -> MagicMock:
    adapter = MagicMock()
    adapter.open_event_source = MagicMock(side_effect=source)
    adapter.close = AsyncMock()
    return adapter


async def _wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail("condition not reached")
        await asyncio.sleep(0.02)


def _save_default(migrated_factory: Any) -> None:
    with session_scope(migrated_factory) as session:
        update_settings(
            session,
            CameraDefaultsUpdate(
                enabled=True, username="fallback", password="fallback-pw"
            ),
        )


@pytest.mark.parametrize("protocol", ["onvif", "vapix"])
async def test_listener_default_credentials_resolved_off_the_loop(
    migrated_factory: Any, tmp_path: Path, protocol: str
) -> None:
    """The default lookup runs in the subscription's thread hop, once."""
    _save_default(migrated_factory)
    supervisor = _supervisor(migrated_factory, tmp_path)
    calls: list[tuple[str, str] | None] = []

    def default_loader() -> tuple[str, str] | None:
        result = supervisor._load_default_credentials()
        calls.append(result)
        return result

    supervisor.set_event_source_factory(
        EventListenerFactory(
            MagicMock(),
            camera_loader=lambda _camera_id: _camera(protocol),
            default_credentials_loader=default_loader,
        )
    )
    ctx = _seed_camera_and_project(migrated_factory, tmp_path, name="events")
    target = CaptureTarget(
        project_id=ctx["project_id"],
        project_name="events",
        camera_id=ctx["camera_id"],
        interval_seconds=None,
        storage_path=str(ctx["storage_path"]),
    )
    adapter = _event_adapter(_parked_source)

    with patch(
        "timelapse_manager.capture.event_listener.build_adapter",
        return_value=adapter,
    ) as build:
        # Its own task, as the supervisor launches a listener.
        listener = asyncio.create_task(supervisor._run_listener(target))
        try:
            await _wait_until(lambda: adapter.open_event_source.called)
        finally:
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
    await supervisor.stop()

    assert calls == [("fallback", "fallback-pw")]
    assert build.call_args.kwargs["default_credentials"] == ("fallback", "fallback-pw")
    assert _capture_origins() == set()


@pytest.mark.parametrize(
    ("own_credentials", "expected_default_reads"), [(False, 1), (True, 0)]
)
async def test_vapix_fallback_reuses_one_default_lookup(
    own_credentials: bool, expected_default_reads: int
) -> None:
    """A WebSocket failure falls back to ONVIF without a second lookup.

    An inheriting camera reads the default once per subscription attempt; a
    camera with its own credentials never reads it (#69 CAM-R8).
    """
    calls = 0

    def default_loader() -> tuple[str, str]:
        nonlocal calls
        calls += 1
        return ("fallback", "pw")

    factory = EventListenerFactory(
        MagicMock(),
        camera_loader=lambda _camera_id: _camera(
            "vapix", own_credentials=own_credentials
        ),
        default_credentials_loader=default_loader,
    )
    ws = _event_adapter(_failing_source)
    pullpoint = _event_adapter(_parked_source)
    target = CaptureTarget(
        project_id=1, project_name="p", camera_id=7, interval_seconds=None
    )

    with patch(
        "timelapse_manager.capture.event_listener.build_adapter",
        side_effect=[ws, pullpoint],
    ) as build:
        source = factory(target)
        consumer = asyncio.create_task(anext(source))
        try:
            await _wait_until(lambda: pullpoint.open_event_source.called)
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            await source.aclose()

    assert calls == expected_default_reads
    # Both transports were built with the same resolved default.
    expected = None if own_credentials else ("fallback", "pw")
    assert [c.kwargs["default_credentials"] for c in build.call_args_list] == [
        expected,
        expected,
    ]


async def test_merged_config_needs_no_default_loader_call() -> None:
    """A loader that returns the merged config carries the default already."""
    calls = 0

    def default_loader() -> tuple[str, str]:
        nonlocal calls
        calls += 1
        return ("unused", "unused")

    merged = CaptureCameraConfig(
        protocol="onvif",
        address="10.0.0.5",
        credentials=None,
        credentials_inherit_default=True,
        snapshot_uri=None,
        stream_uri=None,
        default_resolution=None,
        default_credentials=("merged", "pw"),
    )
    factory = EventListenerFactory(
        MagicMock(),
        camera_loader=lambda _camera_id: merged,
        default_credentials_loader=default_loader,
    )

    assert factory._load_subscription(7) == (merged, ("merged", "pw"))
    assert calls == 0
