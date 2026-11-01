"""The supervisor side of event listeners (#69 CAM-R7, CAM-R15, #67-R9, #162).

Covers what the supervisor does around a camera's event source:

* only a rising edge causes a capture (``Initialized`` and repeated states do
  not), and a slow capture or one that raises never stops the source being read;
* subscriptions are paced: a minimum gap, and a backoff that starts over only
  after a subscription lived a minute;
* the listener's state is visible, a listener that dies is relaunched after a
  backoff, and a camera whose events cannot be subscribed to raises one
  ``events_unavailable`` alert that the next subscribe resolves;
* a refused login drops the camera's cached session (#162);
* ``stop(budget=...)`` returns on time with a wedged task.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.unit.test_campaign_bounds import _permissive_disk_monitor
from tests.unit.test_supervisor import (
    _health_events,
    _health_reasons,
    _health_settings,
    _HealthClock,
    _seed_shared_camera,
)
from timelapse_manager.cameras.base import (
    AuthCaptureError,
    CameraEvent,
    UnreachableCaptureError,
)
from timelapse_manager.capture import supervisor as supervisor_mod
from timelapse_manager.capture.supervisor import (
    CaptureSupervisor,
    CaptureTarget,
    ListenerState,
    _ListenerRunner,
)

_FIRE = "timelapse_manager.capture.supervisor.capture_one_now"
_TOPIC = "Device/IO/VirtualInput"
_TRIGGERS = [{"id": "t1", "topic_id": _TOPIC, "cooldown_seconds": 0}]


def _event(
    active: bool | None,
    *,
    operation: str | None = None,
    source: dict[str, str] | None = None,
) -> CameraEvent:
    return CameraEvent(
        topic_id=_TOPIC,
        category="io",
        source=source if source is not None else {"port": "1"},
        data={},
        active=active,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        raw={"operation": operation} if operation else {},
    )


class _Source:
    """An async iterator that yields ``events``, then ends or parks."""

    def __init__(self, events: list[CameraEvent], *, park: bool = False) -> None:
        self._events = list(events)
        self._park = park

    def __aiter__(self) -> _Source:
        return self

    async def __anext__(self) -> CameraEvent:
        if self._events:
            return self._events.pop(0)
        if self._park:
            await asyncio.Event().wait()
        raise StopAsyncIteration


class _RaisingSource:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def __aiter__(self) -> _RaisingSource:
        return self

    async def __anext__(self) -> CameraEvent:
        raise self._exc


class _ListenerClock(_HealthClock):
    """A fake clock whose ``sleep`` records the delay and moves time on."""

    def __init__(self) -> None:
        super().__init__(datetime(2026, 10, 29, 12, 0, tzinfo=UTC))
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await super().sleep(seconds)


def _settings(tmp_path: Path, *, threshold: int = 3, base: float = 1.0) -> Any:
    settings = _health_settings(tmp_path, threshold)
    capture = settings.capture.model_copy(
        update={"backoff_base_seconds": base, "backoff_jitter_fraction": 0.0}
    )
    return settings.model_copy(update={"capture": capture})


def _supervisor(
    factory: Any,
    tmp_path: Path,
    *,
    clock: _ListenerClock | None = None,
    threshold: int = 3,
    base: float = 1.0,
) -> CaptureSupervisor:
    return CaptureSupervisor(
        _settings(tmp_path, threshold=threshold, base=base),
        factory,
        clock=clock,
        disk_monitor=_permissive_disk_monitor(),
    )


def _target(
    camera_id: int = 1, project_id: int = 1, triggers: Any = _TRIGGERS
) -> CaptureTarget:
    return CaptureTarget(
        project_id=project_id,
        project_name=f"p{project_id}",
        camera_id=camera_id,
        interval_seconds=None,
        event_triggers=triggers,
    )


async def _until(predicate: Callable[[], bool], timeout: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("condition not reached")
        await asyncio.sleep(0.01)


async def _cancel(task: asyncio.Task[Any]) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------------------
# Edge detection
# ---------------------------------------------------------------------------


class TestEdgeDetection:
    @pytest.mark.parametrize(
        ("events", "captures"),
        [
            ([_event(True, operation="Initialized")], 0),
            ([_event(True, operation="Initialized"), _event(True)], 0),
            ([_event(True), _event(True)], 1),
            ([_event(True), _event(False), _event(True)], 2),
            ([_event(None), _event(None)], 2),
            (
                [
                    _event(True, source={"port": "1"}),
                    _event(True, source={"port": "2"}),
                ],
                2,
            ),
        ],
    )
    async def test_captures_follow_rising_edges(
        self, migrated_factory: Any, tmp_path: Path, events: list[Any], captures: int
    ) -> None:
        supervisor = _supervisor(migrated_factory, tmp_path)
        with patch(_FIRE, new=AsyncMock(return_value=None)) as fire:
            await supervisor._consume_event_source(_target(), _Source(events))
        assert fire.call_count == captures
        await supervisor.stop()

    async def test_edge_state_survives_a_resubscribe(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        """A camera re-sending its active state after a reconnect is not an edge."""
        from timelapse_manager.capture.event_triggers import EdgeTracker

        supervisor = _supervisor(migrated_factory, tmp_path)
        tracker = EdgeTracker()
        last_fired: dict[str, float] = {}
        with patch(_FIRE, new=AsyncMock(return_value=None)) as fire:
            for _ in range(2):
                await supervisor._consume_event_source(
                    _target(), _Source([_event(True)]), last_fired, tracker=tracker
                )
        assert fire.call_count == 1
        await supervisor.stop()


# ---------------------------------------------------------------------------
# Slow and failing captures never stop the source
# ---------------------------------------------------------------------------


class TestCaptureQueue:
    async def test_a_slow_capture_does_not_stop_the_source_being_read(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        supervisor = _supervisor(migrated_factory, tmp_path)
        release = asyncio.Event()
        started: list[int] = []

        async def slow_capture(*_a: Any, **_k: Any) -> None:
            started.append(1)
            await release.wait()

        events = [_event(index % 2 == 0) for index in range(6)]  # three rising edges
        consumed = asyncio.Event()

        async def source() -> AsyncIterator[CameraEvent]:
            for event in events:
                yield event
            consumed.set()
            await asyncio.Event().wait()

        state = ListenerState()
        supervisor.set_event_source_factory(lambda _t: source())
        with patch(_FIRE, new=slow_capture):
            listener = asyncio.create_task(supervisor._run_listener(_target(), state))
            try:
                # The first capture is blocked, yet every event was read.
                await asyncio.wait_for(consumed.wait(), timeout=5)
                await _until(lambda: len(started) == 1)
                release.set()
                await _until(lambda: len(started) == 3)
            finally:
                await _cancel(listener)
        assert state.dropped_events == 0
        await supervisor.stop()

    async def test_a_full_queue_drops_the_oldest_capture(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        supervisor = _supervisor(migrated_factory, tmp_path)
        release = asyncio.Event()
        ran: list[str] = []

        async def capture(_sup: Any, _target: Any, *, reason: str, **_k: Any) -> None:
            ran.append(reason)
            await release.wait()

        # 12 rising edges from 12 sources, one capture running and 8 queued.
        events = [_event(True, source={"port": str(n)}) for n in range(12)]
        events = [
            e.__class__(**{**e.__dict__, "topic_id": f"{_TOPIC}{n}"})
            for n, e in enumerate(events)
        ]
        triggers = [
            {"id": f"t{n}", "topic_id": f"{_TOPIC}{n}", "cooldown_seconds": 0}
            for n in range(12)
        ]
        consumed = asyncio.Event()

        async def source() -> AsyncIterator[CameraEvent]:
            for event in events:
                yield event
                await asyncio.sleep(0)  # the consumer takes the first capture
            consumed.set()
            await asyncio.Event().wait()

        state = ListenerState()
        supervisor.set_event_source_factory(lambda _t: source())
        with patch(_FIRE, new=capture):
            listener = asyncio.create_task(
                supervisor._run_listener(_target(triggers=triggers), state)
            )
            try:
                await asyncio.wait_for(consumed.wait(), timeout=5)
                await _until(lambda: len(ran) == 1)
                assert state.dropped_events == 3
                release.set()
                await _until(lambda: len(ran) == 9)
            finally:
                await _cancel(listener)
        # The first capture, then the eight newest; the oldest three were dropped.
        assert ran[0] == f"event:{_TOPIC}0"
        assert ran[1:] == [f"event:{_TOPIC}{n}" for n in range(4, 12)]
        await supervisor.stop()

    async def test_an_unexpected_error_in_a_capture_keeps_the_subscription(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        supervisor = _supervisor(migrated_factory, tmp_path)
        fire = AsyncMock(side_effect=[RuntimeError("boom"), None])
        events = [_event(True), _event(False), _event(True)]
        with patch(_FIRE, new=fire):
            # Returns normally: the RuntimeError never reached the subscription.
            await supervisor._consume_event_source(_target(), _Source(events))
        assert fire.call_count == 2
        await supervisor.stop()


# ---------------------------------------------------------------------------
# Pacing
# ---------------------------------------------------------------------------


class TestPacing:
    async def _run(
        self,
        supervisor: CaptureSupervisor,
        clock: _ListenerClock,
        sleeps: int,
        state: ListenerState | None = None,
    ) -> None:
        task = asyncio.create_task(supervisor._run_listener(_target(), state))
        try:
            await _until(lambda: len(clock.sleeps) >= sleeps)
        finally:
            await _cancel(task)

    async def test_a_source_that_ends_at_once_is_resubscribed_no_sooner_than_5s(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        clock = _ListenerClock()
        supervisor = _supervisor(migrated_factory, tmp_path, clock=clock)
        calls: list[int] = []

        def factory(_target: CaptureTarget) -> _Source:
            calls.append(1)
            return _Source([])

        supervisor.set_event_source_factory(factory)
        await self._run(supervisor, clock, 4)
        assert all(delay >= 5.0 for delay in clock.sleeps)
        await supervisor.stop()

    async def test_the_minimum_gap_scales_with_the_backoff_base(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        clock = _ListenerClock()
        supervisor = _supervisor(migrated_factory, tmp_path, clock=clock, base=10.0)
        supervisor.set_event_source_factory(lambda _t: _Source([]))
        await self._run(supervisor, clock, 1)
        assert clock.sleeps[0] >= 20.0
        await supervisor.stop()

    async def test_backoff_starts_over_only_after_a_subscription_lived_a_minute(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        clock = _ListenerClock()
        supervisor = _supervisor(migrated_factory, tmp_path, clock=clock, base=5.0)
        lives = iter([1.0, 1.0, 1.0, 61.0, 1.0])

        class _Lived:
            def __init__(self) -> None:
                self._life = next(lives)

            def __aiter__(self) -> _Lived:
                return self

            async def __anext__(self) -> CameraEvent:
                # The source's own lifetime moves the fake clock directly: going
                # through ``clock.sleep`` would put a transient entry in
                # ``clock.sleeps`` that a loaded host's poll could count as a
                # pacing sleep.
                clock.current += timedelta(seconds=self._life)
                await asyncio.sleep(0)
                raise StopAsyncIteration

        supervisor.set_event_source_factory(lambda _t: _Lived())
        await self._run(supervisor, clock, 5)
        # Short lives: 10 (the floor, 2 x base) twice, then the backoff (20, 40).
        assert clock.sleeps[:3] == [10.0, 10.0, 20.0]
        # After a minute-long subscription the backoff starts over...
        assert clock.sleeps[3] == 10.0
        # ...and one short life after it begins at the first step again.
        assert clock.sleeps[4] == 10.0
        await supervisor.stop()

    async def test_a_factory_that_raises_does_not_kill_the_listener(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        clock = _ListenerClock()
        supervisor = _supervisor(migrated_factory, tmp_path, clock=clock)
        state = ListenerState()

        def factory(_target: CaptureTarget) -> Any:
            raise RuntimeError("factory broke")

        supervisor.set_event_source_factory(factory)
        task = asyncio.create_task(supervisor._run_listener(_target(), state))
        try:
            await _until(lambda: len(clock.sleeps) >= 2)
            assert not task.done()
        finally:
            await _cancel(task)
        assert state.last_error == "factory broke"
        assert state.consecutive_failures >= 2
        await supervisor.stop()

    async def test_no_factory_parks_without_a_hot_loop(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        clock = _ListenerClock()
        supervisor = _supervisor(migrated_factory, tmp_path, clock=clock)
        task = asyncio.create_task(supervisor._run_listener(_target()))
        await asyncio.sleep(0.05)
        assert not task.done()
        assert clock.sleeps == []
        await _cancel(task)
        await supervisor.stop()


# ---------------------------------------------------------------------------
# Visible state
# ---------------------------------------------------------------------------


class TestListenerState:
    async def test_state_is_exposed_through_the_snapshot(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        clock = _ListenerClock()
        supervisor = _supervisor(migrated_factory, tmp_path, clock=clock)
        target = _target()
        supervisor.set_event_source_factory(
            lambda _t: _Source([_event(False), _event(True)], park=True)
        )
        with patch(_FIRE, new=AsyncMock(return_value=None)):
            supervisor._launch_listener(target)
            runner = supervisor._listeners[target.project_id]
            await _until(lambda: runner.state.last_event_at is not None)
            await _until(lambda: runner.state.connected_since is not None)
            (snapshot,) = supervisor.listener_snapshots()
            await supervisor.stop()
        assert snapshot["project_id"] == target.project_id
        assert snapshot["connected"] is True
        assert snapshot["last_event_at"] is not None
        assert snapshot["last_progress_age_seconds"] == 0.0
        assert snapshot["consecutive_failures"] == 0
        assert snapshot["dropped_events"] == 0
        assert snapshot["last_error"] is None
        assert snapshot["task_done"] is False

    async def test_last_error_is_redacted(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        clock = _ListenerClock()
        supervisor = _supervisor(migrated_factory, tmp_path, clock=clock)
        state = ListenerState()
        supervisor.set_event_source_factory(
            lambda _t: _RaisingSource(
                UnreachableCaptureError("cannot reach http://admin:hunter2@10.0.0.5/x")
            )
        )
        task = asyncio.create_task(supervisor._run_listener(_target(), state))
        try:
            await _until(lambda: state.last_error is not None)
        finally:
            await _cancel(task)
        assert state.last_error is not None
        assert "hunter2" not in state.last_error
        await supervisor.stop()


# ---------------------------------------------------------------------------
# A refused login (#162)
# ---------------------------------------------------------------------------


class TestAuthFailure:
    async def test_a_refused_login_drops_the_cached_session_and_backs_off_hard(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        clock = _ListenerClock()
        supervisor = _supervisor(migrated_factory, tmp_path, clock=clock)
        invalidate = MagicMock()
        supervisor._camera_sessions.invalidate = invalidate  # type: ignore[method-assign]
        supervisor.set_event_source_factory(
            lambda _t: _RaisingSource(AuthCaptureError("rejected"))
        )
        task = asyncio.create_task(supervisor._run_listener(_target(camera_id=7)))
        try:
            await _until(lambda: len(clock.sleeps) >= 1)
        finally:
            await _cancel(task)
        invalidate.assert_called_with(7)
        # A refused login does not fix itself: the first retry is already at the
        # ceiling, as for captures.
        assert clock.sleeps[0] == supervisor._settings.capture.backoff_max_seconds
        await supervisor.stop()

    async def test_it_does_not_mark_the_camera_offline(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        """Events failing is not captures failing (an unprovisioned ONVIF user)."""
        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["alpha"])
        clock = _ListenerClock()
        supervisor = _supervisor(migrated_factory, tmp_path, clock=clock, threshold=1)
        supervisor.set_event_source_factory(
            lambda _t: _RaisingSource(AuthCaptureError("rejected"))
        )
        task = asyncio.create_task(supervisor._run_listener(target))
        try:
            await _until(lambda: len(clock.sleeps) >= 2)
        finally:
            await _cancel(task)
        assert not supervisor._camera_health.is_offline(target.camera_id)
        assert "camera_offline" not in _health_reasons(migrated_factory, scope="camera")
        await supervisor.stop()

    async def test_the_next_subscribe_reloads_the_session(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["alpha"])
        clock = _ListenerClock()
        supervisor = _supervisor(migrated_factory, tmp_path, clock=clock)
        registry = supervisor._camera_sessions
        await registry.get(target.camera_id)
        assert registry.cached(target.camera_id) is not None
        supervisor.set_event_source_factory(
            lambda _t: _RaisingSource(AuthCaptureError("rejected"))
        )
        task = asyncio.create_task(supervisor._run_listener(target))
        try:
            await _until(lambda: len(clock.sleeps) >= 1)
        finally:
            await _cancel(task)
        assert registry.cached(target.camera_id) is None
        await supervisor.stop()


# ---------------------------------------------------------------------------
# events_unavailable
# ---------------------------------------------------------------------------


class TestEventsUnavailable:
    async def test_repeated_subscribe_failures_raise_one_alert_then_resolve(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["alpha"])
        clock = _ListenerClock()
        supervisor = _supervisor(migrated_factory, tmp_path, clock=clock, threshold=2)
        failing = True

        def factory(_t: CaptureTarget) -> Any:
            if failing:
                return _RaisingSource(UnreachableCaptureError("no events"))
            return _Source([_event(False)], park=True)

        supervisor.set_event_source_factory(factory)
        state = ListenerState()
        task = asyncio.create_task(supervisor._run_listener(target, state))
        try:
            await _until(lambda: len(clock.sleeps) >= 4)
            events = _health_events(migrated_factory, scope="camera")
            # Four failed subscribes, one alert.
            assert [m["reason"] for _, _, m in events] == ["events_unavailable"]
            assert events[0][0] == "warning"
            assert supervisor.listener_snapshots() == []  # not a launched runner
            failing = False
            await _until(
                lambda: (
                    _health_reasons(migrated_factory, scope="camera")[-1:]
                    == ["events_available"]
                )
            )
        finally:
            await _cancel(task)
        assert _health_reasons(migrated_factory, scope="camera") == [
            "events_unavailable",
            "events_available",
        ]
        assert state.consecutive_failures == 0
        assert not supervisor._camera_health.is_offline(target.camera_id)
        await supervisor.stop()

    async def test_a_drop_after_subscribing_is_not_a_subscribe_failure(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["alpha"])
        clock = _ListenerClock()
        supervisor = _supervisor(migrated_factory, tmp_path, clock=clock, threshold=1)

        class _DropsAfterAnEvent:
            def __aiter__(self) -> _DropsAfterAnEvent:
                self._sent = False
                return self

            async def __anext__(self) -> CameraEvent:
                if not self._sent:
                    self._sent = True
                    return _event(False)
                raise UnreachableCaptureError("dropped")

        supervisor.set_event_source_factory(lambda _t: _DropsAfterAnEvent())
        task = asyncio.create_task(supervisor._run_listener(target))
        try:
            await _until(lambda: len(clock.sleeps) >= 3)
        finally:
            await _cancel(task)
        assert _health_reasons(migrated_factory, scope="camera") == []
        await supervisor.stop()

    async def test_a_restart_during_an_outage_still_resolves_the_alert(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["alpha"])
        first = _supervisor(migrated_factory, tmp_path, threshold=1)
        await first._run_blocking(
            first._write_event,
            scope="camera",
            scope_id=target.camera_id,
            level="warning",
            message="events unavailable",
            metadata={"reason": "events_unavailable", "camera_id": target.camera_id},
        )
        await first.stop()

        second = _supervisor(migrated_factory, tmp_path, threshold=1)
        await second._ensure_camera_health_synced()
        assert second._camera_health.is_events_unavailable(target.camera_id)
        await second.listener_subscribed(target, "onvif-pullpoint")  # no state: no-op
        second._listener_states[target.project_id] = ListenerState()
        await second.listener_subscribed(target, "onvif-pullpoint")
        assert _health_reasons(migrated_factory, scope="camera")[-1] == (
            "events_available"
        )
        await second.stop()

    async def test_retiring_the_last_listener_resolves_the_alert(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["alpha"])
        supervisor = _supervisor(migrated_factory, tmp_path, threshold=1)
        supervisor._camera_health.record_listener_failure(
            target.camera_id, target.project_id, error="x"
        )
        supervisor._listeners[target.project_id] = _ListenerRunner(target=target)
        await supervisor._stop_listener(target.project_id, retire=True)
        assert _health_reasons(migrated_factory, scope="camera") == ["events_available"]
        await supervisor.stop()


# ---------------------------------------------------------------------------
# Transport downgrade
# ---------------------------------------------------------------------------


class TestTransportReports:
    async def test_a_downgrade_is_one_warning_until_the_fast_transport_works(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["alpha"])
        supervisor = _supervisor(migrated_factory, tmp_path)
        state = ListenerState()
        supervisor._listener_states[target.project_id] = state

        async def downgrade() -> None:
            await supervisor.listener_downgraded(
                target,
                from_transport="vapix-websocket",
                to_transport="onvif-pullpoint",
                reason="handshake refused http://u:secret@10.0.0.5/ws",
            )

        await downgrade()
        await downgrade()
        events = _health_events(migrated_factory, scope="project")
        warnings = [
            e for e in events if e[2].get("reason") == "listener_transport_downgrade"
        ]
        assert len(warnings) == 1
        assert warnings[0][0] == "warning"
        assert "secret" not in warnings[0][1]
        assert state.transport == "onvif-pullpoint"
        assert state.downgrades == 2

        await supervisor.listener_subscribed(target, "vapix-websocket")
        state.attempt_subscribed = False
        await downgrade()
        warnings = [
            e
            for e in _health_events(migrated_factory, scope="project")
            if e[2].get("reason") == "listener_transport_downgrade"
        ]
        assert len(warnings) == 2
        await supervisor.stop()

    async def test_a_factory_with_set_reporter_is_handed_the_supervisor(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        supervisor = _supervisor(migrated_factory, tmp_path)
        factory = MagicMock()
        supervisor.set_event_source_factory(factory)
        factory.set_reporter.assert_called_once_with(supervisor)
        await supervisor.stop()


# ---------------------------------------------------------------------------
# Supervision: a listener that dies is relaunched after a backoff (#67-R9)
# ---------------------------------------------------------------------------


class TestListenerRelaunch:
    async def test_a_dead_listener_is_relaunched_once_its_backoff_has_passed(
        self,
        migrated_factory: Any,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["alpha"])
        clock = _ListenerClock()
        supervisor = _supervisor(migrated_factory, tmp_path, clock=clock)
        runs: list[int] = []

        async def dies(
            _target: CaptureTarget, state: ListenerState | None = None
        ) -> None:
            runs.append(1)
            raise RuntimeError("listener bug")

        supervisor._run_listener = dies  # type: ignore[method-assign]
        supervisor._load_listener_targets = lambda: [target]  # type: ignore[method-assign]
        with caplog.at_level(logging.ERROR, logger=supervisor_mod.logger.name):
            supervisor._launch_listener(target)
            first = supervisor._listeners[target.project_id]
            await asyncio.gather(first.task, return_exceptions=True)  # type: ignore[arg-type]
            await asyncio.sleep(0)
        assert first.relaunch_not_before is not None
        assert "event listener task" in caplog.text

        await supervisor._reconcile_listeners()
        # Inside the backoff: the same dead runner stays.
        assert supervisor._listeners[target.project_id] is first
        assert len(runs) == 1

        clock.current = clock.current.replace(year=2027)
        await supervisor._reconcile_listeners()
        second = supervisor._listeners[target.project_id]
        assert second is not first
        assert second.state.restarts == 1
        await asyncio.gather(second.task, return_exceptions=True)  # type: ignore[arg-type]
        assert "reason" in str(_health_events(migrated_factory, scope="project"))
        assert "listener_relaunch" in _health_reasons(migrated_factory, scope="project")
        await supervisor.stop()

    async def test_a_cancelled_listener_is_not_a_death(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        supervisor = _supervisor(migrated_factory, tmp_path)
        target = _target()
        supervisor._launch_listener(target)
        runner = supervisor._listeners[target.project_id]
        await _cancel(runner.task)  # type: ignore[arg-type]
        await asyncio.sleep(0)
        assert runner.relaunch_not_before is None
        await supervisor.stop()


# ---------------------------------------------------------------------------
# stop(budget)
# ---------------------------------------------------------------------------


class TestStopBudget:
    async def test_a_wedged_listener_does_not_hold_stop_past_the_budget(
        self,
        migrated_factory: Any,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        supervisor = _supervisor(migrated_factory, tmp_path)
        release = asyncio.Event()

        async def wedged() -> None:
            while not release.is_set():
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    await asyncio.sleep(0.01)  # ignores the cancel

        target = _target()
        task = asyncio.create_task(wedged(), name="capture-listener-1")
        await asyncio.sleep(0)  # let it start: a task cancelled before that just ends
        supervisor._listeners[target.project_id] = _ListenerRunner(target, task)
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            with caplog.at_level(logging.ERROR, logger=supervisor_mod.logger.name):
                await supervisor.stop(budget=0.5)
            elapsed = loop.time() - started
            assert 0.4 <= elapsed < 1.5
            assert "capture-listener-1" in caplog.text
            assert not task.done()
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_the_budget_is_shared_by_every_phase(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        supervisor = _supervisor(migrated_factory, tmp_path)
        release = asyncio.Event()

        async def wedged() -> None:
            while not release.is_set():
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    await asyncio.sleep(0.01)

        tasks = [asyncio.create_task(wedged(), name=f"wedged-{n}") for n in range(2)]
        await asyncio.sleep(0)
        supervisor._listeners[1] = _ListenerRunner(_target(), tasks[0])
        runner = MagicMock()
        runner.task = tasks[1]
        supervisor._runners[2] = runner
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            await supervisor.stop(budget=0.5)
            assert loop.time() - started < 1.5
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_a_healthy_stop_closes_the_clients_and_is_idempotent(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        supervisor = _supervisor(migrated_factory, tmp_path)
        supervisor._launch_listener(_target())
        await asyncio.sleep(0)
        await supervisor.stop()
        await supervisor.stop()
        assert supervisor._stopped
