"""Unit tests for the NotificationDispatcher.

Covers: run_once delivers events; startup high-water-mark skips backlog;
debounce suppresses repeats; raising channel retried exactly max_retries
and emits notify.delivery_failed; delivery_failed not re-routed; channel
failure does not crash the loop; stop() cancels a hanging channel within
a bound; run_once raises when the background loop is live.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

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


_RULES = [_ALL_INFO_RULE]


def _gap_msg(
    event_type: str, *, scope: str = "system", scope_id: int | None = None
) -> NotificationMessage:
    return NotificationMessage(
        event_type=event_type,
        scope=scope,
        scope_id=scope_id,
        level="info",
        message="event",
        timestamp=datetime(2026, 1, 1),
        metadata=None,
    )


class GatedChannel(NotificationChannel):
    """Signals entry, then waits for ``release`` before recording the send."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.sends = 0

    @property
    def name(self) -> str:
        return "fake"

    async def send(self, message: NotificationMessage) -> None:
        self.sends += 1
        self.entered.set()
        await self.release.wait()


class FlakyChannel(FakeChannel):
    """Raises ChannelSendError for the first ``failures`` sends, then records."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self._failures = failures

    async def send(self, message: NotificationMessage) -> None:
        if self._failures > 0:
            self._failures -= 1
            raise ChannelSendError("simulated transport failure")
        await super().send(message)


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

    async def test_concurrent_same_key_events_send_once(self, migrated_factory) -> None:
        """#149: the key is reserved before the send; a concurrent twin is dropped."""
        channel = GatedChannel()
        disp = _make_dispatcher(migrated_factory, channel, debounce_window_seconds=60.0)

        first = asyncio.create_task(
            disp._dispatch_event(_gap_msg("capture.gap"), _RULES)
        )
        await channel.entered.wait()  # first is mid-send, nothing recorded yet
        second = asyncio.create_task(
            disp._dispatch_event(_gap_msg("capture.gap"), _RULES)
        )
        async with asyncio.timeout(5):
            await second  # debounced by the reservation: no send
        assert channel.sends == 1
        channel.release.set()
        await first
        assert channel.sends == 1

    async def test_concurrent_different_keys_both_send(self, migrated_factory) -> None:
        channel = GatedChannel()
        disp = _make_dispatcher(migrated_factory, channel, debounce_window_seconds=60.0)
        channel.release.set()
        await asyncio.gather(
            disp._dispatch_event(_gap_msg("capture.gap", scope_id=1), _RULES),
            disp._dispatch_event(_gap_msg("capture.gap", scope_id=2), _RULES),
        )
        assert channel.sends == 2

    async def test_failed_send_releases_the_key(self, migrated_factory) -> None:
        """A send that never succeeded must not debounce the next event."""
        channel = FlakyChannel(failures=1)
        disp = _make_dispatcher(
            migrated_factory, channel, debounce_window_seconds=60.0, max_retries=1
        )
        await disp._dispatch_event(_gap_msg("capture.gap"), _RULES)
        assert channel.received == []
        await disp._dispatch_event(_gap_msg("capture.gap"), _RULES)
        assert len(channel.received) == 1
        # ...and now the successful send does debounce a repeat.
        await disp._dispatch_event(_gap_msg("capture.gap"), _RULES)
        assert len(channel.received) == 1

    async def test_cancelled_send_releases_the_key(self, migrated_factory) -> None:
        channel = GatedChannel()
        disp = _make_dispatcher(migrated_factory, channel, debounce_window_seconds=60.0)
        task = asyncio.create_task(
            disp._dispatch_event(_gap_msg("capture.gap"), _RULES)
        )
        await channel.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        channel.release.set()
        await disp._dispatch_event(_gap_msg("capture.gap"), _RULES)
        assert channel.sends == 2

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


# ---------------------------------------------------------------------------
# WP-67-R: persisted delivery cursor and catch-up (#67 R8)
# ---------------------------------------------------------------------------


def _cursor(factory) -> tuple[int, int] | None:
    """The stored ``(delivery cursor, read mark)``, or None when no row."""
    from timelapse_manager.db.models import NotificationCursor

    with session_scope(factory) as session:
        row = session.get(NotificationCursor, 1)
        if row is None:
            return None
        return row.last_event_id, row.last_read_event_id


def _max_event_id(factory) -> int:
    from sqlalchemy import func

    from timelapse_manager.db.models import Event

    with session_scope(factory) as session:
        return int(session.query(func.max(Event.id)).scalar() or 0)


def _age_event(factory, event_id: int, *, seconds: float) -> None:
    """Backdate one event (``log_event`` always stamps the server clock)."""
    from datetime import UTC, datetime, timedelta

    from timelapse_manager.db.models import Event

    with session_scope(factory) as session:
        row = session.get(Event, event_id)
        assert row is not None
        row.timestamp = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            seconds=seconds
        )


async def _wait_for(predicate, *, timeout: float = 5.0) -> None:
    """Poll ``predicate`` on the loop until it holds (fails the test if not)."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


class TestPersistedCursor:
    async def test_first_start_initialises_cursor_at_newest_event(
        self, migrated_factory
    ) -> None:
        """No row yet (first start after the upgrade): old behaviour, no flood."""
        _seed(migrated_factory, message="history 1")
        _seed(migrated_factory, message="history 2")
        newest = _max_event_id(migrated_factory)
        assert _cursor(migrated_factory) is None

        channel = FakeChannel()
        disp = _make_dispatcher(migrated_factory, channel, poll_interval_seconds=0.05)
        await disp.start()
        try:
            assert _cursor(migrated_factory) == (newest, newest)
            _seed(migrated_factory, message="live")
            await _wait_for(lambda: len(channel.received) == 1)
        finally:
            await disp.stop()
        assert [m.message for m in channel.received] == ["live"]
        assert _cursor(migrated_factory) == (newest + 1, newest + 1)

    async def test_restart_delivers_events_logged_while_stopped(
        self, migrated_factory
    ) -> None:
        first = FakeChannel()
        disp = _make_dispatcher(migrated_factory, first, poll_interval_seconds=0.05)
        await disp.start()
        _seed(migrated_factory, message="before stop")
        await _wait_for(lambda: len(first.received) == 1)
        await disp.stop()

        _seed(migrated_factory, message="while stopped 1")
        _seed(migrated_factory, message="while stopped 2")

        second = FakeChannel()
        disp2 = _make_dispatcher(
            migrated_factory,
            second,
            poll_interval_seconds=0.05,
            debounce_window_seconds=0.0,
        )
        await disp2.start()
        try:
            await _wait_for(lambda: len(second.received) == 2)
        finally:
            await disp2.stop()
        assert sorted(m.message for m in second.received) == [
            "while stopped 1",
            "while stopped 2",
        ]
        newest = _max_event_id(migrated_factory)
        assert _cursor(migrated_factory) == (newest, newest)

    async def test_catchup_skips_events_older_than_the_age_limit(
        self, migrated_factory, caplog
    ) -> None:
        import logging

        disp = _make_dispatcher(migrated_factory, FakeChannel())
        await disp.start()
        await disp.stop()
        _seed(migrated_factory, message="stale")
        _age_event(migrated_factory, _max_event_id(migrated_factory), seconds=7200)
        _seed(migrated_factory, message="recent")

        channel = FakeChannel()
        disp2 = _make_dispatcher(
            migrated_factory,
            channel,
            poll_interval_seconds=0.05,
            debounce_window_seconds=0.0,
            catchup_max_age_seconds=3600.0,
        )
        with caplog.at_level(
            logging.INFO, logger="timelapse_manager.monitoring.dispatcher"
        ):
            await disp2.start()
            try:
                await _wait_for(lambda: len(channel.received) == 1)
                await asyncio.sleep(0.2)  # a few more polls: nothing else arrives
            finally:
                await disp2.stop()
        assert [m.message for m in channel.received] == ["recent"]
        skips = [r for r in caplog.records if "skipping 1 event(s)" in r.message]
        assert len(skips) == 1
        newest = _max_event_id(migrated_factory)
        assert _cursor(migrated_factory) == (newest, newest)

    async def test_stop_keeps_the_cursor_below_a_cancelled_send(
        self, migrated_factory
    ) -> None:
        """A send cut off by stop() is redelivered on the next start."""
        hanging = HangingChannel()
        disp = _make_dispatcher(migrated_factory, hanging, poll_interval_seconds=0.05)
        await disp.start()
        _seed(migrated_factory, message="cut off")
        event_id = _max_event_id(migrated_factory)
        await asyncio.wait_for(hanging.entered.wait(), timeout=5.0)
        await disp.stop()

        # Read (and auto-cleared) but not delivered.
        assert _cursor(migrated_factory) == (event_id - 1, event_id)

        channel = FakeChannel()
        disp2 = _make_dispatcher(migrated_factory, channel, poll_interval_seconds=0.05)
        await disp2.start()
        try:
            await _wait_for(lambda: len(channel.received) == 1)
        finally:
            await disp2.stop()
        assert channel.received[0].message == "cut off"
        assert _cursor(migrated_factory) == (event_id, event_id)

    async def test_stop_before_dispatches_are_registered_keeps_the_batch(
        self, migrated_factory
    ) -> None:
        """A stop while the cycle awaits the rules read must not mark it sent."""
        import threading

        entered = threading.Event()
        release = threading.Event()

        def blocking_rules() -> list:
            entered.set()
            release.wait(5.0)
            return [_ALL_INFO_RULE]

        disp = NotificationDispatcher(
            session_factory=migrated_factory,
            channels=[FakeChannel()],
            settings=_minimal_settings(poll_interval_seconds=0.05),
            routing_rules_fn=blocking_rules,
        )
        await disp.start()
        _seed(migrated_factory, message="mid-cycle")
        event_id = _max_event_id(migrated_factory)
        try:
            await _wait_for(entered.is_set)
            await disp.stop()
        finally:
            release.set()

        stored = _cursor(migrated_factory)
        assert stored is not None and stored[0] < event_id

        channel = FakeChannel()
        disp2 = _make_dispatcher(migrated_factory, channel, poll_interval_seconds=0.05)
        await disp2.start()
        try:
            await _wait_for(lambda: len(channel.received) == 1)
        finally:
            await disp2.stop()
        assert channel.received[0].message == "mid-cycle"

    async def test_redelivery_does_not_replay_auto_clear(
        self, migrated_factory
    ) -> None:
        """Events between the delivery cursor and the read mark skip auto-clear."""
        hanging = HangingChannel()
        disp = _make_dispatcher(migrated_factory, hanging, poll_interval_seconds=0.05)
        await disp.start()
        _seed(migrated_factory, message="cut off")
        event_id = _max_event_id(migrated_factory)
        await asyncio.wait_for(hanging.entered.wait(), timeout=5.0)
        await disp.stop()

        _seed(migrated_factory, message="new")
        cleared_ids: list[int] = []
        channel = FakeChannel()
        disp2 = _make_dispatcher(
            migrated_factory,
            channel,
            poll_interval_seconds=0.05,
            debounce_window_seconds=0.0,
        )
        disp2._auto_clear_alerts_sync = lambda events: cleared_ids.extend(  # type: ignore[method-assign]  # noqa: SLF001
            polled_id for polled_id, _ in events
        )
        await disp2.start()
        try:
            await _wait_for(lambda: len(channel.received) == 2)
        finally:
            await disp2.stop()
        # Both were delivered, but only the unread one went through auto-clear.
        assert sorted(m.message for m in channel.received) == ["cut off", "new"]
        assert cleared_ids == [event_id + 1]

    async def test_run_once_persists_the_cursor(self, migrated_factory) -> None:
        disp = _make_dispatcher(migrated_factory, FakeChannel())
        _seed(migrated_factory, message="one")
        await disp.run_once()
        newest = _max_event_id(migrated_factory)
        assert _cursor(migrated_factory) == (newest, newest)

    async def test_cursor_beyond_the_newest_event_is_reset(
        self, migrated_factory
    ) -> None:
        """A database restored from an older backup must not hide new events."""
        from timelapse_manager.db.models import NotificationCursor

        _seed(migrated_factory, message="existing")
        newest = _max_event_id(migrated_factory)
        with session_scope(migrated_factory) as session:
            session.add(
                NotificationCursor(id=1, last_event_id=999, last_read_event_id=999)
            )
        channel = FakeChannel()
        disp = _make_dispatcher(migrated_factory, channel, poll_interval_seconds=0.05)
        await disp.start()
        try:
            assert _cursor(migrated_factory) == (newest, newest)
            _seed(migrated_factory, message="after restore")
            await _wait_for(lambda: len(channel.received) == 1)
        finally:
            await disp.stop()

    async def test_start_survives_a_missing_cursor_table(
        self, migrated_factory
    ) -> None:
        """An unmigrated database: start from the newest event, never crash."""
        from sqlalchemy import text

        with session_scope(migrated_factory) as session:
            session.execute(text("DROP TABLE notification_cursor"))
        _seed(migrated_factory, message="history")
        channel = FakeChannel()
        disp = _make_dispatcher(migrated_factory, channel, poll_interval_seconds=0.05)
        await disp.start()
        try:
            assert disp.is_running()
            _seed(migrated_factory, message="live")
            await _wait_for(lambda: len(channel.received) == 1)
        finally:
            await disp.stop()
        assert channel.received[0].message == "live"


# ---------------------------------------------------------------------------
# WP-67-R: bounded concurrency; the poll never waits for delivery
# ---------------------------------------------------------------------------


class _CountingChannel(NotificationChannel):
    """Records the peak number of concurrent sends."""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self.delivered = 0

    @property
    def name(self) -> str:
        return "fake"

    async def send(self, message: NotificationMessage) -> None:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.002)
        finally:
            self.active -= 1
        self.delivered += 1


class TestBoundedConcurrency:
    async def test_burst_of_200_never_exceeds_max_concurrent_sends(
        self, migrated_factory
    ) -> None:
        with session_scope(migrated_factory) as session:
            for index in range(200):
                log_event(
                    session,
                    scope="project",
                    scope_id=index,
                    level="info",
                    message=f"burst {index}",
                )
        channel = _CountingChannel()
        disp = _make_dispatcher(
            migrated_factory,
            channel,
            debounce_window_seconds=0.0,
            max_concurrent_sends=4,
        )
        assert await disp.run_once() == 200
        assert channel.delivered == 200
        assert channel.peak == 4  # bounded, and the bound is actually used

    async def test_slow_send_does_not_stall_the_poll_heartbeat(
        self, migrated_factory
    ) -> None:
        """A send slower than 6 poll intervals must not make /healthz stale."""
        hanging = HangingChannel()
        disp = _make_dispatcher(
            migrated_factory,
            hanging,
            poll_interval_seconds=0.05,
            channel_send_timeout_seconds=30.0,
        )
        await disp.start()
        try:
            _seed(migrated_factory, message="slow")
            await asyncio.wait_for(hanging.entered.wait(), timeout=5.0)
            await asyncio.sleep(0.5)  # 10 intervals with the send still hung
            age = disp.last_poll_age_seconds()
            assert age is not None and age < 0.3
            # The loop keeps reading: a later event moves the read mark on.
            _seed(migrated_factory, message="later")
            later = _max_event_id(migrated_factory)
            await _wait_for(lambda: disp._high_water_mark == later)  # noqa: SLF001
        finally:
            await disp.stop()


# ---------------------------------------------------------------------------
# WP-67-R: channels hot-reload (#67 R8/R17)
# ---------------------------------------------------------------------------


class _FakeProvider:
    """A provider whose version and channels the test controls."""

    def __init__(self, channels: list[NotificationChannel]) -> None:
        self.version_token = "v1"
        self.channels = channels
        self.builds = 0
        self.fail = False

    def version(self, session) -> str:
        return self.version_token

    def build(self, session) -> list[NotificationChannel]:
        self.builds += 1
        if self.fail:
            raise RuntimeError("decrypt failed")
        return list(self.channels)


class TestChannelHotReload:
    async def test_settings_change_rebuilds_channels_without_restart(
        self, migrated_factory
    ) -> None:
        first = FakeChannel()
        provider = _FakeProvider([first])
        disp = NotificationDispatcher(
            session_factory=migrated_factory,
            channels=[],
            settings=_minimal_settings(debounce_window_seconds=0.0),
            routing_rules_fn=lambda: [_ALL_INFO_RULE],
            channels_provider=provider,
        )
        _seed(migrated_factory, message="one")
        await disp.run_once()
        await disp.run_once()  # unchanged version: no rebuild
        assert provider.builds == 1

        second = FakeChannel()
        provider.channels = [second]
        provider.version_token = "v2"
        _seed(migrated_factory, message="two")
        await disp.run_once()

        assert [m.message for m in first.received] == ["one"]
        assert [m.message for m in second.received] == ["two"]
        assert provider.builds == 2

    async def test_failed_rebuild_keeps_current_channels_and_retries(
        self, migrated_factory
    ) -> None:
        first = FakeChannel()
        provider = _FakeProvider([first])
        disp = NotificationDispatcher(
            session_factory=migrated_factory,
            channels=[],
            settings=_minimal_settings(debounce_window_seconds=0.0),
            routing_rules_fn=lambda: [_ALL_INFO_RULE],
            channels_provider=provider,
        )
        await disp.run_once()
        provider.version_token = "v2"
        provider.fail = True
        _seed(migrated_factory, message="still delivered")
        await disp.run_once()
        assert [m.message for m in first.received] == ["still delivered"]
        await disp.run_once()  # version still unrecorded: tries again
        assert provider.builds == 3

    async def test_stored_provider_picks_up_a_webhook_enabled_in_settings(
        self, migrated_factory
    ) -> None:
        from timelapse_manager.db.models import NotificationSettings
        from timelapse_manager.monitoring.channels import StoredChannelsProvider

        disp = NotificationDispatcher(
            session_factory=migrated_factory,
            channels=[],
            settings=_minimal_settings(),
            routing_rules_fn=lambda: [],
            channels_provider=StoredChannelsProvider(send_timeout_seconds=5.0),
        )
        await disp.run_once()
        assert disp._channels == {}  # noqa: SLF001

        with session_scope(migrated_factory) as session:
            session.add(
                NotificationSettings(
                    id=1,
                    enabled_channels=["webhook"],
                    webhook_urls=["https://hooks.example.test/notify"],
                )
            )
        await disp.run_once()
        assert set(disp._channels) == {"webhook"}  # noqa: SLF001

        with session_scope(migrated_factory) as session:
            row = session.get(NotificationSettings, 1)
            assert row is not None
            row.enabled_channels = []
        await disp.run_once()
        assert disp._channels == {}  # noqa: SLF001


# ---------------------------------------------------------------------------
# #108: a cancel landing as a send finishes must not be swallowed (Python 3.11)
# ---------------------------------------------------------------------------


class _GatedChannel(NotificationChannel):
    """A send that parks until released, then succeeds or fails."""

    def __init__(self, name: str, *, fail: bool) -> None:
        self._name = name
        self._fail = fail
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def name(self) -> str:
        return self._name

    async def send(self, message: NotificationMessage) -> None:
        self.entered.set()
        await self.release.wait()
        if self._fail:
            raise ChannelSendError("receiver went away")


class _ParkedChannel(NotificationChannel):
    """A send that never finishes (reached only if a cancel was swallowed)."""

    def __init__(self, name: str) -> None:
        self._name = name
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    async def send(self, message: NotificationMessage) -> None:
        self.calls += 1
        await asyncio.Event().wait()


def _message() -> NotificationMessage:
    from datetime import UTC, datetime

    return NotificationMessage(
        event_type="render.failed",
        scope="system",
        scope_id=None,
        level="error",
        message="m",
        timestamp=datetime.now(UTC).replace(tzinfo=None),
        metadata=None,
    )


class TestCancelNotSwallowed:
    @pytest.mark.parametrize("outcome", ["sent", "error"])
    async def test_cancel_landing_as_send_finishes_propagates(
        self, migrated_factory, outcome: str
    ) -> None:
        # Regression for #108 (the dispatcher's twin of #33). On Python 3.11,
        # ``asyncio.wait_for`` returned the send's result (or raised its error)
        # instead of CancelledError when the caller was cancelled in the same
        # loop step in which the send finished. Only 3.11 can turn this red:
        # 3.12's wait_for is built on asyncio.timeout.
        channel = _GatedChannel("a", fail=outcome == "error")
        disp = _make_dispatcher(migrated_factory, channel)
        task = asyncio.create_task(disp._deliver_with_retry(channel, _message()))  # noqa: SLF001
        await channel.entered.wait()

        # Same synchronous block: the send becomes ready to finish and the
        # caller is cancelled before either runs.
        channel.release.set()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.parametrize("outcome", ["sent", "error"])
    async def test_stop_completes_when_its_cancel_meets_a_finishing_send(
        self, migrated_factory, outcome: str
    ) -> None:
        # The event routes to "a" (gated) then "b" (never finishes). If stop()'s
        # cancel is swallowed as "a" finishes, the dispatch carries on into "b"
        # and stop() waits on it for the 30 s send timeout and beyond.
        gated = _GatedChannel("a", fail=outcome == "error")
        parked = _ParkedChannel("b")
        disp = NotificationDispatcher(
            session_factory=migrated_factory,
            channels=[gated, parked],
            settings=_minimal_settings(
                poll_interval_seconds=0.05, channel_send_timeout_seconds=30.0
            ),
            routing_rules_fn=lambda: [
                {"event_types": ["all"], "min_level": "info", "channels": ["a", "b"]}
            ],
        )
        await disp.start()
        _seed(migrated_factory, message="race")
        event_id = _max_event_id(migrated_factory)
        await asyncio.wait_for(gated.entered.wait(), timeout=5.0)

        # stop() cancels synchronously before its first await, so the release
        # and the cancel land in the same loop step. Do not wrap stop() in
        # wait_for or a task: that would move the cancel to a later step.
        gated.release.set()
        async with asyncio.timeout(5.0):
            await disp.stop()

        assert parked.calls == 0
        stored = _cursor(migrated_factory)
        assert stored is not None and stored[0] < event_id


# ---------------------------------------------------------------------------
# #134: a resolve clears only alerts raised before it
# ---------------------------------------------------------------------------


class TestAutoClearBound:
    async def test_offline_recovered_offline_keeps_the_second_alert(
        self, migrated_factory
    ) -> None:
        from timelapse_manager.db.models import Event

        def _log(level: str, reason: str) -> int:
            with session_scope(migrated_factory) as session:
                log_event(
                    session,
                    scope="project",
                    scope_id=3,
                    level=level,
                    message=reason,
                    metadata={"reason": reason},
                )
            return _max_event_id(migrated_factory)

        # All three commit before the dispatcher reads "recovered".
        first = _log("warning", "camera_offline")
        _log("info", "camera_recovered")
        second = _log("warning", "camera_offline")

        disp = _make_dispatcher(migrated_factory, FakeChannel())
        await disp.run_once()

        with session_scope(migrated_factory) as session:
            assert session.get(Event, first).alert_cleared_at is not None
            assert session.get(Event, second).alert_cleared_at is None
