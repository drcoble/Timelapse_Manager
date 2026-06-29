"""Unit tests for the NotificationDispatcher.

Covers: run_once delivers events; startup high-water-mark skips backlog;
debounce suppresses repeats; raising channel retried exactly max_retries
and emits notify.delivery_failed; delivery_failed not re-routed; channel
failure does not crash the loop; stop() cancels a hanging channel within
a bound; run_once raises when the background loop is live.
"""

from __future__ import annotations

import asyncio

import pytest

from timelapse_manager.config.settings import MonitoringSettings
from timelapse_manager.db.session import session_scope
from timelapse_manager.monitoring import (
    EventType,
    NotificationChannel,
    NotificationDispatcher,
    NotificationMessage,
    get_events,
    log_event,
)
from timelapse_manager.monitoring.channels import ChannelSendError

# ---------------------------------------------------------------------------
# Helpers / fake channels
# ---------------------------------------------------------------------------

_ALL_INFO_RULE = {
    "event_types": ["all"],
    "min_level": "info",
    "channels": ["fake"],
}


def _minimal_settings(**overrides) -> MonitoringSettings:
    """MonitoringSettings with autostart=False and instant backoff for tests."""
    defaults: dict = {
        "autostart": False,
        "poll_interval_seconds": 0.1,
        "max_retries": 3,
        "retry_backoff_seconds": 0.0,
        "debounce_window_seconds": 60.0,
        "channel_send_timeout_seconds": 30.0,
    }
    defaults.update(overrides)
    return MonitoringSettings(**defaults)


class FakeChannel(NotificationChannel):
    """A channel that records every message it receives."""

    def __init__(self, name: str = "fake") -> None:
        self._name = name
        self.received: list[NotificationMessage] = []

    @property
    def name(self) -> str:
        return self._name

    async def send(self, message: NotificationMessage) -> None:
        self.received.append(message)


class RaisingChannel(NotificationChannel):
    """A channel that always raises ChannelSendError."""

    def __init__(self, name: str = "fake") -> None:
        self._name = name
        self.call_count = 0

    @property
    def name(self) -> str:
        return self._name

    async def send(self, message: NotificationMessage) -> None:
        self.call_count += 1
        raise ChannelSendError("simulated transport failure")


class HangingChannel(NotificationChannel):
    """A channel that signals entry then sleeps forever (simulates a wedged host)."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    @property
    def name(self) -> str:
        return "fake"

    async def send(self, message: NotificationMessage) -> None:
        self.entered.set()
        await asyncio.sleep(3600)


def _make_dispatcher(
    factory,
    channel: NotificationChannel,
    routing_rules: list | None = None,
    **settings_overrides,
) -> NotificationDispatcher:
    rules = routing_rules if routing_rules is not None else [_ALL_INFO_RULE]
    return NotificationDispatcher(
        session_factory=factory,
        channels=[channel],
        settings=_minimal_settings(**settings_overrides),
        routing_rules_fn=lambda: rules,
    )


def _seed(
    factory,
    *,
    scope: str = "system",
    level: str = "info",
    message: str = "event",
    event_type: str | None = None,
) -> None:
    with session_scope(factory) as session:
        log_event(
            session,
            scope=scope,
            scope_id=None,
            level=level,
            message=message,
            type=event_type,
        )


# ---------------------------------------------------------------------------
# run_once delivers a new event
# ---------------------------------------------------------------------------


class TestRunOnceDelivers:
    async def test_run_once_delivers_new_event_to_matching_channel(
        self, migrated_factory
    ) -> None:
        channel = FakeChannel()
        disp = _make_dispatcher(migrated_factory, channel)
        _seed(migrated_factory, message="hello dispatcher")
        count = await disp.run_once()
        assert count == 1
        assert len(channel.received) == 1
        assert channel.received[0].message == "hello dispatcher"

    async def test_run_once_returns_zero_when_no_new_events(
        self, migrated_factory
    ) -> None:
        channel = FakeChannel()
        disp = _make_dispatcher(migrated_factory, channel)
        count = await disp.run_once()
        assert count == 0
        assert len(channel.received) == 0

    async def test_run_once_does_not_deliver_event_that_doesnt_match_rules(
        self, migrated_factory
    ) -> None:
        channel = FakeChannel()
        no_match_rules = [
            {
                "event_types": ["render.complete"],
                "min_level": "info",
                "channels": ["fake"],
            }
        ]
        disp = _make_dispatcher(migrated_factory, channel, routing_rules=no_match_rules)
        _seed(migrated_factory, event_type="capture.gap")
        count = await disp.run_once()
        assert count == 1
        assert len(channel.received) == 0  # routed to no channel

    async def test_run_once_delivers_event_with_correct_type_in_message(
        self, migrated_factory
    ) -> None:
        channel = FakeChannel()
        disp = _make_dispatcher(migrated_factory, channel)
        _seed(
            migrated_factory,
            event_type=EventType.RENDER_FAILED.value,
            message="render job failed",
        )
        await disp.run_once()
        assert channel.received[0].event_type == EventType.RENDER_FAILED.value


# ---------------------------------------------------------------------------
# Startup high-water mark skips backlog
# ---------------------------------------------------------------------------


class TestHighWaterMark:
    async def test_run_once_skips_events_below_high_water_mark(
        self, migrated_factory
    ) -> None:
        """Events that exist BEFORE the HWM is advanced are treated as backlog."""
        # Seed backlog events.
        _seed(migrated_factory, message="old event 1")
        _seed(migrated_factory, message="old event 2")

        # Determine current max id and set as the dispatcher HWM.
        with session_scope(migrated_factory) as session:
            from sqlalchemy import func

            from timelapse_manager.db.models import Event

            max_id = session.query(func.max(Event.id)).scalar()

        channel = FakeChannel()
        disp = _make_dispatcher(migrated_factory, channel)
        # Manually set HWM to skip the seeded backlog.
        disp._high_water_mark = max_id  # noqa: SLF001

        # Add one new event after HWM.
        _seed(migrated_factory, message="new event after hwm")

        count = await disp.run_once()
        assert count == 1
        assert len(channel.received) == 1
        assert channel.received[0].message == "new event after hwm"

    async def test_run_once_advances_high_water_mark(self, migrated_factory) -> None:
        """After run_once, subsequent run_once does not re-deliver the same events."""
        channel = FakeChannel()
        disp = _make_dispatcher(migrated_factory, channel)
        _seed(migrated_factory, message="once only")
        await disp.run_once()
        await disp.run_once()  # second pass: HWM advanced, no new events
        assert len(channel.received) == 1


# ---------------------------------------------------------------------------
# Debounce
# ---------------------------------------------------------------------------


class TestDebounce:
    async def test_second_delivery_within_window_is_suppressed(
        self, migrated_factory
    ) -> None:
        """The same (type, scope, scope_id) within the debounce window is not resent."""
        channel = FakeChannel()
        disp = _make_dispatcher(migrated_factory, channel, debounce_window_seconds=60.0)

        _seed(migrated_factory, event_type="capture.gap", message="first")
        await disp.run_once()

        _seed(migrated_factory, event_type="capture.gap", message="second")
        await disp.run_once()

        # First received; second debounced because the key matches within the window.
        assert len(channel.received) == 1

    async def test_different_type_bypasses_debounce(self, migrated_factory) -> None:
        """Different event types have independent debounce keys."""
        channel = FakeChannel()
        disp = _make_dispatcher(migrated_factory, channel, debounce_window_seconds=60.0)

        _seed(migrated_factory, event_type="capture.gap", message="gap")
        await disp.run_once()

        _seed(migrated_factory, event_type="capture.stalled", message="stalled")
        await disp.run_once()

        assert len(channel.received) == 2


# ---------------------------------------------------------------------------
# Retry behaviour — raising channel
# ---------------------------------------------------------------------------


class TestBoundedRetry:
    async def test_raising_channel_retried_exactly_max_retries_times(
        self, migrated_factory
    ) -> None:
        """A channel that always raises is attempted exactly max_retries times."""
        channel = RaisingChannel()
        disp = _make_dispatcher(
            migrated_factory,
            channel,
            max_retries=3,
            retry_backoff_seconds=0.0,
        )
        _seed(migrated_factory, message="trigger retry")
        await disp.run_once()
        assert channel.call_count == 3

    async def test_exhausted_retries_emit_delivery_failed_event(
        self, migrated_factory
    ) -> None:
        """After all retries fail, a notify.delivery_failed event is persisted."""
        channel = RaisingChannel()
        disp = _make_dispatcher(
            migrated_factory,
            channel,
            max_retries=2,
            retry_backoff_seconds=0.0,
        )
        _seed(migrated_factory, message="will fail")
        await disp.run_once()

        # A delivery-failure row must be persisted.
        with session_scope(migrated_factory) as session:
            rows, _ = get_events(session)
        types_in_db = [(r.event_metadata or {}).get("type") for r in rows]
        assert EventType.NOTIFY_DELIVERY_FAILED.value in types_in_db

    async def test_delivery_failed_event_not_re_routed_to_channel(
        self, migrated_factory
    ) -> None:
        """The delivery_failed event emitted after retries is never re-delivered.

        After the raising channel exhausts its retries, run_once again with a
        rule that would normally match everything. The delivery_failed row must
        not cause another send attempt.
        """
        channel = RaisingChannel()
        disp = _make_dispatcher(
            migrated_factory,
            channel,
            max_retries=1,
            retry_backoff_seconds=0.0,
        )

        _seed(migrated_factory, message="initial fail")
        await disp.run_once()  # exhausts retry, writes delivery_failed row
        call_count_after_first = channel.call_count

        # Second run_once: picks up the delivery_failed row.
        await disp.run_once()

        # Routing hard-excludes delivery_failed; the call count must not increase.
        assert channel.call_count == call_count_after_first

    async def test_channel_failure_does_not_crash_the_loop(
        self, migrated_factory
    ) -> None:
        """A channel raising ChannelSendError must not abort the dispatch loop."""
        channel = RaisingChannel()
        disp = _make_dispatcher(
            migrated_factory,
            channel,
            max_retries=1,
            retry_backoff_seconds=0.0,
        )
        _seed(migrated_factory, message="fail gracefully")
        # run_once must not raise.
        count = await disp.run_once()
        assert count == 1  # event was polled even though delivery failed


# ---------------------------------------------------------------------------
# stop() cancels a hanging channel within a time bound
# ---------------------------------------------------------------------------


class TestStopCancelsHangingChannel:
    async def test_stop_returns_within_bound_when_channel_hangs(
        self, migrated_factory
    ) -> None:
        """stop() cancels an in-flight hang and returns within ~2 s.

        A HangingChannel signals .entered then sleeps forever. The dispatcher's
        per-send wait_for has a high timeout (30 s) so only stop()'s cancellation
        can free the test within the bound.
        """
        channel = HangingChannel()
        disp = _make_dispatcher(
            migrated_factory,
            channel,
            poll_interval_seconds=0.05,
            channel_send_timeout_seconds=30.0,
        )

        await disp.start()
        # Log the routed event AFTER start() sets the HWM.
        _seed(migrated_factory, message="hang test event")

        # Wait until the channel is inside send().
        await asyncio.wait_for(channel.entered.wait(), timeout=5.0)

        # Now stop(). It should cancel the in-flight task and return promptly.
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        await disp.stop()
        elapsed = loop.time() - t0

        assert elapsed < 2.5, (
            f"stop() took {elapsed:.2f}s — a hanging channel was not cancelled"
        )


# ---------------------------------------------------------------------------
# run_once raises when the background loop is live
# ---------------------------------------------------------------------------


class TestRunOncePrecondition:
    async def test_run_once_raises_if_loop_is_running(self, migrated_factory) -> None:
        channel = FakeChannel()
        disp = _make_dispatcher(migrated_factory, channel)
        await disp.start()
        try:
            with pytest.raises(RuntimeError, match="run_once"):
                await disp.run_once()
        finally:
            await disp.stop()
