"""Poll-based asynchronous notification dispatcher.

The dispatcher is the delivery side of the monitoring subsystem. A single
long-lived asyncio task polls the event table for rows newer than a read mark,
evaluates the routing rules for each, and fans matching events out to the
channels with debounce, bounded retry and bounded concurrency.

Design points the rest of the system relies on (these mirror the render worker's
reliability shape):

* **Persisted delivery cursor (#67 R8).** The dispatcher keeps two marks in the
  singleton ``notification_cursor`` row:

  - the *delivery cursor*: every event at or below it has settled (delivered,
    failed after its retries, debounced or routed nowhere). It is the event id
    just below the lowest dispatch still in flight, or the read mark when none
    is;
  - the *read mark* (``_high_water_mark``): every event at or below it has been
    read and run through the alert auto-clear.

  :meth:`start` resumes from the delivery cursor, so events logged while the
  dispatcher was stopped, and sends that :meth:`stop` cut off, are delivered on
  the next start. Delivery is therefore **at-least-once**: a rare duplicate after
  a crash is preferred to a lost alert. The catch-up only delivers events
  younger than ``catchup_max_age_seconds``; older ones are skipped and counted
  in one log line. The first start after the upgrade (no row yet) starts from
  the newest event, the old behaviour, so an upgrade never replays history.
* **Read-then-auto-clear poll.** The poll query (:meth:`_poll_new_events`) is
  read-only, but each poll cycle additionally runs the active-alert auto-clear
  evaluator over the same just-pulled batch (:meth:`_auto_clear_alerts`, a
  contained ``to_thread`` write). The poll is the one chokepoint that sees both
  event write paths (``log_event`` and the supervisor's untyped ``_write_event``)
  and runs over every new event regardless of level, which is exactly what
  auto-clear-on-resolve needs (resolve signals are info level). Auto-clear runs
  only above the persisted read mark, so a restart that re-delivers events never
  replays a resolve, and each resolve clears only alerts raised before it
  (#134).
* **The poll never waits for delivery.** Each cycle launches one tracked task
  per event and goes back to polling, so a dead webhook host cannot make the
  poll heartbeat (:attr:`last_poll_at`, read by ``/healthz``) go stale. Sends
  share one semaphore of ``max_concurrent_sends``. While ``_POLL_BATCH_SIZE``
  dispatches are still in flight the cycle reads nothing new (backpressure).
* **Bulletproof stop.** :meth:`stop` cancels the poll task **and every in-flight
  per-event dispatch task**, awaits them all with ``return_exceptions=True``,
  then persists the cursor below the first cancelled dispatch. It never merely
  waits-with-timeout: an orphan dispatch to a dead host would otherwise leak onto
  a closing loop. Idempotent and safe with zero tasks.
* **Hard per-send timeout.** Every channel ``send`` runs under
  ``asyncio.timeout(channel_send_timeout_seconds)`` so a cooperatively awaiting
  channel cannot block shutdown. ``asyncio.wait_for`` is deliberately not used:
  on Python 3.11 it returns the send's result instead of raising when the
  caller is cancelled in the same loop step in which the send finishes, which
  lost :meth:`stop`'s cancel (#108, the dispatcher's twin of #33). A blocking
  synchronous send is bounded only by the channel's own socket/HTTP timeout
  (part of the channel contract).
* **Loop prevention.** A channel failure is caught, retried up to ``max_retries``
  with backoff, and -- on final failure -- recorded as a delivery-failure event
  that is never itself routed to a channel. A channel never crashes the loop.
* **Debounce.** A notification for a given ``(event_type, scope, scope_id)`` key
  is suppressed per channel if one was sent within ``debounce_window_seconds``.
  The key is reserved before the send starts, so two concurrent events with the
  same key cannot both send; a failed or cancelled send releases it. The key
  table is pruned so memory stays bounded.
* **Channels hot-reload.** With a ``channels_provider``, each poll compares the
  stored transport settings' version token and rebuilds the channels when it
  changed, so an SMTP or webhook edit takes effect without a restart.

:meth:`run_once` performs exactly one poll-and-dispatch pass synchronously with
respect to the caller (awaiting all dispatch tasks it spawns), so tests can drive
delivery deterministically without the background loop.

Secrets and encryption at rest
------------------------------
The dispatcher itself handles no credentials: channels are built by the injected
provider from their (decrypted) transport configuration, and the dispatcher only
calls ``channel.send()`` and re-reads routing rules (which carry no secret).
At-rest encryption is therefore transparent here -- the decrypt-at-use seam lives
in :func:`~timelapse_manager.monitoring.channels.build_channels`, not in this
loop.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, func, update
from sqlalchemy.orm import Session, sessionmaker

from ..db.engine import run_with_write_retry
from ..db.models import Event, NotificationCursor, NotificationSettings
from ..db.session import session_scope
from .alerts import auto_clear_for_event
from .channels import (
    ChannelSendError,
    ChannelsProvider,
    NotificationChannel,
    NotificationMessage,
)
from .events import EventType, log_event
from .routing import evaluate_routing_rules

if TYPE_CHECKING:
    from ..config.settings import MonitoringSettings

logger = logging.getLogger(__name__)

# Maximum number of event rows pulled in a single poll, so a large burst is
# delivered in bounded batches rather than loaded all at once. Also the most
# dispatches in flight before a cycle stops reading new events.
_POLL_BATCH_SIZE = 200

# The JSON details key holding the event type (see events.log_event).
_TYPE_KEY = "type"

# The singleton cursor row's primary key.
_CURSOR_ROW_ID = 1


@dataclass(frozen=True)
class _PolledEvent:
    """One event read by a poll, as a detached snapshot.

    ``deliver`` is False for an event of the startup catch-up range that is
    older than ``catchup_max_age_seconds``: it is still read (and auto-cleared
    when above the read mark) but not sent.
    """

    event_id: int
    message: NotificationMessage
    deliver: bool


@dataclass(frozen=True)
class _ChannelsUpdate:
    """Channels rebuilt by a poll because the provider's version changed."""

    version: str
    channels: list[NotificationChannel]


@dataclass(frozen=True)
class _StartPosition:
    """Where :meth:`NotificationDispatcher.start` resumes, read in one thread hop.

    ``persisted`` is False when the cursor could not be read or written (for
    example an unmigrated database): the dispatcher then starts from the newest
    event, as before the cursor existed.
    """

    delivery_mark: int
    read_mark: int
    catchup_until_id: int
    catchup_cutoff: datetime | None
    persisted: bool


class NotificationDispatcher:
    """Polls for new events and fans them out to injected channels.

    The channels come either fixed at construction (``channels``) or from a
    ``channels_provider`` that rebuilds them when the stored settings change;
    this class never imports a concrete channel. The current routing rules are
    supplied by ``routing_rules_fn`` so the configuration can change at runtime
    without restarting the dispatcher; the default reads them from the
    notification settings row.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        channels: Sequence[NotificationChannel],
        settings: MonitoringSettings,
        routing_rules_fn: Callable[[], list[dict[str, Any]]] | None = None,
        channels_provider: ChannelsProvider | None = None,
    ) -> None:
        """Create the dispatcher; performs no I/O and starts no tasks.

        :param session_factory: factory for synchronous ORM sessions.
        :param channels: the channels to deliver through, keyed by their
            ``name``. Duplicates by name are last-wins. With a
            ``channels_provider`` these are only the initial set, replaced by
            the provider's first build.
        :param settings: the monitoring section of application settings.
        :param routing_rules_fn: returns the current routing rules; defaults to
            reading them from the notification settings row.
        :param channels_provider: builds the channels from stored settings and
            reports a version token; checked on every poll so channel edits
            apply without a restart. ``None`` keeps ``channels`` fixed.
        """
        self._session_factory = session_factory
        self._channels: dict[str, NotificationChannel] = {
            channel.name: channel for channel in channels
        }
        self._channels_provider = channels_provider
        self._channels_version: str | None = None
        self._settings = settings
        self._routing_rules_fn = routing_rules_fn or self._load_routing_rules
        # The read mark: every event at or below it has been read.
        self._high_water_mark = 0
        # Auto-clear runs only above this id (the persisted read mark at start),
        # so a restart that re-delivers events never replays a resolve.
        self._auto_clear_floor = 0
        # Startup catch-up: events up to this id that are older than the cutoff
        # are read but not delivered. Cleared once the read mark passes it.
        self._catchup_until_id = 0
        self._catchup_cutoff: datetime | None = None
        # Whether the marks are meaningful enough to persist: set once start()
        # has resolved them or a poll has read the table.
        self._position_known = False
        self._persisted_marks: tuple[int, int] | None = None
        self._loop_task: asyncio.Task[None] | None = None
        # In-flight dispatches by event id. A cancelled task stays in the map so
        # the delivery cursor stays below it and it is redelivered next start.
        self._in_flight: dict[int, asyncio.Task[None]] = {}
        self._send_slots = asyncio.Semaphore(max(1, settings.max_concurrent_sends))
        self._started = False
        self._stopped = False
        # Debounce table: (event_type, scope, scope_id, channel) -> last-sent
        # monotonic time. Pruned in dispatch so it stays bounded.
        self._last_sent: dict[tuple[str, str, int | None, str], float] = {}
        # Readiness heartbeat (#67 R5): ``time.monotonic()`` when the loop
        # started and after each successful poll. Plain floats, so the probe
        # reads them from a worker thread without touching the loop.
        self._started_monotonic: float | None = None
        self.last_poll_at: float | None = None

    async def start(self) -> None:
        """Resume from the persisted cursor, build channels, start the loop.

        Idempotent. The read and the channel build run in worker threads. If
        the cursor row is missing (first start after the upgrade) it is created
        at the newest event, so no history is delivered. If the cursor cannot be
        read at all the dispatcher starts from the newest event; if even that
        fails it starts from zero. Errors are logged, never raised, so
        dispatcher startup never aborts process boot.
        """
        if self._started:
            return
        self._started = True
        self._started_monotonic = time.monotonic()
        try:
            position = await asyncio.to_thread(self._load_start_position)
        except Exception:  # noqa: BLE001 - never block startup on the mark read
            logger.exception("failed to read initial event high-water mark")
            position = _StartPosition(0, 0, 0, None, persisted=False)
        self._high_water_mark = position.delivery_mark
        self._auto_clear_floor = position.read_mark
        self._catchup_until_id = position.catchup_until_id
        self._catchup_cutoff = position.catchup_cutoff
        if position.persisted:
            self._position_known = True
            self._persisted_marks = (position.delivery_mark, position.read_mark)
        update_ = await asyncio.to_thread(self._refresh_channels)
        self._apply_channels(update_)
        self._loop_task = asyncio.create_task(
            self._poll_loop(), name="notification-dispatcher"
        )
        logger.info(
            "notification dispatcher started (high_water_mark=%d, channels=%d)",
            self._high_water_mark,
            len(self._channels),
        )

    def is_running(self) -> bool:
        """Return True when the poll loop is started, not stopped, and alive.

        A pure, non-raising predicate for the liveness probe: True only when
        ``start`` ran, ``stop`` has not, and the poll-loop task exists and has not
        finished (a crashed loop leaves a done task).
        """
        return (
            self._started
            and not self._stopped
            and self._loop_task is not None
            and not self._loop_task.done()
        )

    def last_poll_age_seconds(self, now: float | None = None) -> float | None:
        """Seconds since the last successful poll, for the readiness probe.

        Before the first successful poll this counts from :meth:`start`, so a
        loop that never manages a poll still goes stale. ``None`` when the
        dispatcher was never started. ``now`` is a ``time.monotonic()`` reading,
        injectable for tests. Pure and non-raising.
        """
        reference = (
            self.last_poll_at
            if self.last_poll_at is not None
            else self._started_monotonic
        )
        if reference is None:
            return None
        moment = time.monotonic() if now is None else now
        return max(0.0, moment - reference)

    async def stop(self) -> None:
        """Cancel the poll loop and every in-flight dispatch, then save the cursor.

        Cancels the poll task and all per-event dispatch tasks, then awaits them
        with ``return_exceptions=True`` so a hanging channel send (cancelled
        through its ``asyncio.timeout`` scope) cannot leak onto a closing loop.
        A poll running in a worker thread is not cancellable; this never waits
        on it, so a poll that finishes after cancellation merely discards its
        result. The cursor is then persisted **below** the first cancelled
        dispatch, so those events are redelivered on the next start. Idempotent
        and safe with zero tasks.
        """
        if self._stopped:
            return
        self._stopped = True

        tasks: list[asyncio.Task[None]] = []
        if self._loop_task is not None:
            self._loop_task.cancel()
            tasks.append(self._loop_task)
        for task in list(self._in_flight.values()):
            task.cancel()
            tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._persist_cursor()
        logger.info(
            "notification dispatcher stopped (%d dispatch task(s) cut off; "
            "delivery cursor=%d)",
            len(self._in_flight),
            self._delivery_mark(),
        )

    async def run_once(self) -> int:
        """Run exactly one poll-and-dispatch pass; return the number of events.

        Polls for events newer than the read mark, advances it, dispatches each
        matching event to completion (awaiting the per-event dispatch tasks it
        spawns) and persists the cursor before returning. Intended for
        deterministic testing without the background loop. Safe to call
        repeatedly.

        :raises RuntimeError: if the background poll loop is running. The two
            share the marks without locking by design (the loop is the only
            poller in production), so driving both at once would double the
            mark advance and double-deliver. This guard turns that misuse into a
            clear error instead of a confusing intermittent result.
        """
        if self._loop_task is not None and not self._loop_task.done():
            raise RuntimeError(
                "run_once() cannot be used while the background poll loop is "
                "running; it is a standalone driver for tests."
            )
        count = await self._poll_and_launch()
        pending = list(self._in_flight.values())
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await self._persist_cursor()
        return count

    async def _poll_loop(self) -> None:
        """Poll for new events on the configured interval until cancelled.

        Each cycle polls, advances the read mark, launches a dispatch task per
        event and persists the cursor if it moved. Launched tasks are tracked so
        :meth:`stop` can cancel them; the cycle never waits for them.
        ``asyncio.sleep`` between cycles is cleanly cancellable; no
        ``wait_for``/event-wait is used here (that pattern can swallow
        cancellation), and any poll error is contained so the loop never dies.
        A successful cycle stamps :attr:`last_poll_at`; a failing one does not,
        so a poll that keeps failing shows as stale in ``/healthz`` (#67 R5).
        """
        interval = max(0.1, self._settings.poll_interval_seconds)
        while True:
            try:
                await self._poll_and_launch()
                self.last_poll_at = time.monotonic()
                await self._persist_cursor()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a poll error must not kill the loop
                logger.exception("notification poll cycle failed")
            await asyncio.sleep(interval)

    async def _poll_and_launch(self) -> int:
        """Poll once and launch a tracked dispatch task per deliverable event.

        Reads nothing while ``_POLL_BATCH_SIZE`` dispatches are still in flight
        (backpressure; the cycle still counts as a successful poll, since the
        loop is alive and delivery is progressing). Returns the number of events
        read.
        """
        if len(self._in_flight) >= _POLL_BATCH_SIZE:
            return 0
        events, channels_update = await asyncio.to_thread(self._poll_sync)
        self._apply_channels(channels_update)
        self._position_known = True
        if not events:
            self._end_catchup_if_passed()
            return 0
        await self._auto_clear_alerts(
            [
                (event.event_id, event.message)
                for event in events
                if event.event_id > self._auto_clear_floor
            ]
        )
        deliverable = [event for event in events if event.deliver]
        if deliverable:
            # The default rules read the settings row: off the loop (#62).
            rules = await asyncio.to_thread(self._routing_rules_fn)
            for event in deliverable:
                task = asyncio.create_task(
                    self._dispatch_event(event.message, rules),
                    name=f"notification-send-{event.event_id}",
                )
                self._in_flight[event.event_id] = task
                task.add_done_callback(self._settled_callback(event.event_id))
        # Advance the read mark only now, with every dispatch registered: a stop
        # (or a failing rules read) during the awaits above must leave the batch
        # unread, or the cursor would record it as delivered. A batch read again
        # re-runs auto-clear, which is idempotent (#134 bounds each resolve).
        self._high_water_mark = max(self._high_water_mark, events[-1].event_id)
        self._end_catchup_if_passed()
        return len(events)

    def _settled_callback(self, event_id: int) -> Callable[[asyncio.Task[None]], None]:
        """Return the done-callback that retires one dispatch from the map.

        A cancelled dispatch (only :meth:`stop` cancels them) stays in the map,
        so the delivery cursor stays below it. Any other outcome settles the
        event: an unexpected error is logged and not retried.
        """

        def _settled(task: asyncio.Task[None]) -> None:
            if task.cancelled():
                return
            self._in_flight.pop(event_id, None)
            error = task.exception()
            if error is not None:
                logger.error(
                    "notification dispatch failed event_id=%d",
                    event_id,
                    exc_info=error,
                )

        return _settled

    def _end_catchup_if_passed(self) -> None:
        """Drop the startup catch-up window once the read mark is past it."""
        if (
            self._catchup_cutoff is not None
            and self._high_water_mark >= self._catchup_until_id
        ):
            self._catchup_cutoff = None

    def _delivery_mark(self) -> int:
        """The delivery cursor: just below the lowest in-flight dispatch."""
        if self._in_flight:
            return min(self._in_flight) - 1
        return self._high_water_mark

    async def _persist_cursor(self) -> None:
        """Write the delivery cursor and read mark if either moved.

        One short write in a worker thread with the shared locked-database
        retry. Best effort: a failure is logged and the next cycle retries.
        """
        if not self._position_known:
            return
        marks = (self._delivery_mark(), self._high_water_mark)
        if marks == self._persisted_marks:
            return
        try:
            await asyncio.to_thread(self._write_cursor, *marks)
        except Exception:  # noqa: BLE001 - a cursor write must not break the loop
            logger.exception("failed to persist the notification cursor")
            return
        self._persisted_marks = marks

    async def _dispatch_event(
        self, message: NotificationMessage, rules: list[dict[str, Any]]
    ) -> None:
        """Route one event and deliver it to each matching channel.

        Resolves the target channels from the routing rules, applies per-channel
        debounce, and delivers through each with bounded retry. Cancellation
        (from :meth:`stop`) propagates so an in-flight send is abandoned cleanly;
        any other failure is contained per channel and never escapes.
        """
        event_type = message.event_type
        targets = evaluate_routing_rules(event_type, message.level, rules)
        if not targets:
            return
        now = _monotonic()
        self._prune_debounce(now)
        for channel_name in sorted(targets):
            channel = self._channels.get(channel_name)
            if channel is None:
                continue
            key = (event_type, message.scope, message.scope_id, channel_name)
            if self._is_debounced(key, now):
                logger.debug(
                    "notification debounced channel=%s type=%s",
                    channel_name,
                    event_type,
                )
                continue
            # Reserve the key before the first await so a concurrent dispatch of
            # the same key sees it as debounced instead of sending a duplicate
            # (#149). Check and reserve share one loop step, so nothing can
            # interleave. A failed or cancelled send releases it, so a later
            # event is not suppressed by a send that never happened.
            self._last_sent[key] = now
            delivered = False
            try:
                delivered = await self._deliver_with_retry(channel, message)
            finally:
                if not delivered and self._last_sent.get(key) == now:
                    del self._last_sent[key]
            if delivered:
                # Re-record: a prune during a very long send may have dropped
                # the reservation, and a delivered send must always debounce.
                self._last_sent[key] = now

    async def _deliver_with_retry(
        self, channel: NotificationChannel, message: NotificationMessage
    ) -> bool:
        """Deliver one message to one channel with bounded retry; report success.

        Each attempt holds one of the ``max_concurrent_sends`` slots and runs
        under ``asyncio.timeout``, so a hanging send is bounded (and cancellable
        on shutdown); the slot is released before any backoff sleep. A
        :class:`ChannelSendError` or a per-send timeout triggers backoff and a
        retry up to ``max_retries``. When all attempts fail, a delivery-failure
        event is recorded (never routed to a channel) and ``False`` is returned.
        Cancellation is re-raised so shutdown is immediate. ``asyncio.wait_for``
        is not used: on Python 3.11 it can swallow a cancel that lands as the
        send finishes (#108).
        """
        timeout = self._settings.channel_send_timeout_seconds
        attempts = max(1, self._settings.max_retries)
        last_error = ""
        for attempt in range(attempts):
            try:
                async with self._send_slots, asyncio.timeout(timeout):
                    await channel.send(message)
                return True
            except asyncio.CancelledError:
                raise
            except (ChannelSendError, TimeoutError) as exc:
                last_error = str(exc) or exc.__class__.__name__
            except Exception as exc:  # noqa: BLE001 - contain any channel fault
                last_error = str(exc) or exc.__class__.__name__
            if attempt + 1 < attempts:
                await self._backoff(attempt)
        # A write: in a worker thread, so a held SQLite lock never stalls the
        # event loop (#62).
        await asyncio.to_thread(
            self._record_delivery_failure, channel.name, message, last_error
        )
        return False

    async def _backoff(self, attempt: int) -> None:
        """Sleep with exponential backoff and jitter before the next attempt.

        The sleep is a bare ``await`` so cancellation propagates: if ``stop``
        cancels a dispatch task while it is between retries, the ``CancelledError``
        must travel up through :meth:`_deliver_with_retry` (which only re-raises
        cancellation, never swallows it) so shutdown is immediate. Suppressing the
        cancellation here would let the retry loop run to completion and block a
        prompt stop -- the cancel-swallow footgun this design exists to avoid.
        """
        base = max(0.0, self._settings.retry_backoff_seconds)
        delay = base * (2**attempt)
        if delay > 0:
            delay += random.uniform(0, base)  # noqa: S311 - jitter, not security
        await asyncio.sleep(delay)

    def _apply_channels(self, update_: _ChannelsUpdate | None) -> None:
        """Install rebuilt channels on the loop (in-flight sends keep the old)."""
        if update_ is None:
            return
        first = self._channels_version is None
        self._channels = {channel.name: channel for channel in update_.channels}
        self._channels_version = update_.version
        if not first:
            logger.info(
                "notification channels reloaded from settings (channels=%s)",
                ",".join(sorted(self._channels)) or "none",
            )

    def _refresh_channels(self) -> _ChannelsUpdate | None:
        """Rebuild the channels if the provider's version changed. Synchronous.

        Returns ``None`` when there is no provider, the version is unchanged, or
        the rebuild failed (logged; the current channels stay, and the version
        is not recorded, so the next poll retries).
        """
        provider = self._channels_provider
        if provider is None:
            return None
        try:
            with session_scope(self._session_factory) as session:
                version = provider.version(session)
                if version == self._channels_version:
                    return None
                channels = provider.build(session)
        except Exception:  # noqa: BLE001 - keep the current channels
            logger.exception(
                "failed to build notification channels; keeping the current ones"
            )
            return None
        return _ChannelsUpdate(version=version, channels=channels)

    def _poll_sync(self) -> tuple[list[_PolledEvent], _ChannelsUpdate | None]:
        """Read new events and check the channel version, in one thread hop."""
        return self._poll_new_events(), self._refresh_channels()

    def _load_start_position(self) -> _StartPosition:
        """Resolve where to resume, creating the cursor row on first use.

        Synchronous; call via a thread executor. Raises only when the newest
        event id cannot be read either.
        """
        newest = self._current_max_id()
        try:
            stored = self._read_cursor()
        except Exception:  # noqa: BLE001 - e.g. an unmigrated database
            logger.exception(
                "failed to read the notification cursor; starting from the newest event"
            )
            return _StartPosition(newest, newest, newest, None, persisted=False)
        if stored is None or stored[0] > newest:
            if stored is not None:
                # A restored older database: ids above ``newest`` will be
                # reused, so a cursor beyond them would hide new events.
                logger.warning(
                    "notification cursor %d is beyond the newest event %d; "
                    "resetting it",
                    stored[0],
                    newest,
                )
            try:
                run_with_write_retry(lambda: self._set_cursor(newest, newest))
            except Exception:  # noqa: BLE001 - start anyway, unpersisted
                logger.exception("failed to initialise the notification cursor")
                return _StartPosition(newest, newest, newest, None, persisted=False)
            return _StartPosition(newest, newest, newest, None, persisted=True)
        delivery_mark, read_mark = stored
        read_mark = min(max(read_mark, delivery_mark), newest)
        if delivery_mark >= newest:
            return _StartPosition(
                delivery_mark, read_mark, delivery_mark, None, persisted=True
            )
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            seconds=self._settings.catchup_max_age_seconds
        )
        pending, stale = self._count_catchup(delivery_mark, newest, cutoff)
        if stale:
            logger.info(
                "notification catch-up: skipping %d event(s) older than %ds "
                "logged while the dispatcher was stopped",
                stale,
                int(self._settings.catchup_max_age_seconds),
            )
        if pending - stale:
            logger.info(
                "notification catch-up: delivering %d event(s) after event %d",
                pending - stale,
                delivery_mark,
            )
        return _StartPosition(delivery_mark, read_mark, newest, cutoff, persisted=True)

    def _read_cursor(self) -> tuple[int, int] | None:
        """Return the stored ``(delivery cursor, read mark)``, or None. Sync."""
        with session_scope(self._session_factory) as session:
            row = session.get(NotificationCursor, _CURSOR_ROW_ID)
            if row is None:
                return None
            return int(row.last_event_id), int(row.last_read_event_id)

    def _count_catchup(
        self, after_id: int, through_id: int, cutoff: datetime
    ) -> tuple[int, int]:
        """Count the catch-up range's events, and those older than ``cutoff``."""
        with session_scope(self._session_factory) as session:
            in_range = (Event.id > after_id, Event.id <= through_id)
            total = session.query(func.count(Event.id)).filter(*in_range).scalar()
            stale = (
                session.query(func.count(Event.id))
                .filter(*in_range, Event.timestamp < cutoff)
                .scalar()
            )
            return int(total or 0), int(stale or 0)

    def _set_cursor(self, delivery_mark: int, read_mark: int) -> None:
        """Create or overwrite the cursor row. Synchronous, one transaction."""
        with session_scope(self._session_factory) as session:
            row = session.get(NotificationCursor, _CURSOR_ROW_ID)
            if row is None:
                session.add(
                    NotificationCursor(
                        id=_CURSOR_ROW_ID,
                        last_event_id=delivery_mark,
                        last_read_event_id=read_mark,
                    )
                )
            else:
                row.last_event_id = delivery_mark
                row.last_read_event_id = read_mark

    def _write_cursor(self, delivery_mark: int, read_mark: int) -> None:
        """Move the persisted marks forward (never back). Synchronous.

        SQLite's two-argument ``max()`` keeps the write monotonic, so a stale
        write from a poll thread that outlived its cancelled task cannot move
        the cursor back over a newer one. Creates the row when it is missing.
        """

        def _write() -> None:
            with session_scope(self._session_factory) as session:
                result = session.execute(
                    update(NotificationCursor)
                    .where(NotificationCursor.id == _CURSOR_ROW_ID)
                    .values(
                        last_event_id=func.max(
                            NotificationCursor.last_event_id, delivery_mark
                        ),
                        last_read_event_id=func.max(
                            NotificationCursor.last_read_event_id, read_mark
                        ),
                        updated_at=func.current_timestamp(),
                    )
                )
                if not cast("CursorResult[Any]", result).rowcount:
                    session.add(
                        NotificationCursor(
                            id=_CURSOR_ROW_ID,
                            last_event_id=delivery_mark,
                            last_read_event_id=read_mark,
                        )
                    )

        run_with_write_retry(_write)

    def _is_debounced(self, key: tuple[str, str, int | None, str], now: float) -> bool:
        """Return whether a notification for ``key`` was sent within the window."""
        window = self._settings.debounce_window_seconds
        last = self._last_sent.get(key)
        return last is not None and (now - last) < window

    def _prune_debounce(self, now: float) -> None:
        """Drop debounce keys older than ~2x the window to bound memory."""
        horizon = self._settings.debounce_window_seconds * 2
        stale = [k for k, ts in self._last_sent.items() if (now - ts) > horizon]
        for key in stale:
            del self._last_sent[key]

    def _record_delivery_failure(
        self, channel_name: str, message: NotificationMessage, error: str
    ) -> None:
        """Record a delivery-failure event (never routed) after retries fail.

        The event carries the delivery-failure type, which routing hard-excludes,
        so it surfaces in the application/log only and can never trigger another
        notification. Synchronous; called via a thread executor. Best effort: any
        failure here is logged and swallowed.
        """
        logger.warning(
            "notification delivery failed channel=%s type=%s error=%s",
            channel_name,
            message.event_type,
            error,
        )
        try:
            with session_scope(self._session_factory) as session:
                log_event(
                    session,
                    scope=message.scope,
                    scope_id=message.scope_id,
                    level="error",
                    message=(
                        f"Notification delivery to channel '{channel_name}' "
                        f"failed after retries for {message.event_type}."
                    ),
                    type=EventType.NOTIFY_DELIVERY_FAILED.value,
                    metadata={
                        "channel": channel_name,
                        "failed_event_type": message.event_type,
                        "error": error,
                    },
                )
        except Exception:  # noqa: BLE001 - never raise from the failure recorder
            logger.exception("failed to record notification delivery failure")

    async def _auto_clear_alerts(
        self, events: list[tuple[int, NotificationMessage]]
    ) -> None:
        """Run the active-alert auto-clear evaluator over a polled batch.

        Inspects every event in the batch -- regardless of level, since resolve
        signals are info level -- and clears any active alert a resolve signal
        matches by scope + scope_id. The synchronous write is moved off the event
        loop. Best effort and fully contained: an auto-clear failure must never
        abort a poll cycle or block notification dispatch (``run_once`` does not
        wrap this call, so an uncontained raise would surface in tests too).
        """
        if not events:
            return
        try:
            await asyncio.to_thread(self._auto_clear_alerts_sync, events)
        except Exception:  # noqa: BLE001 - auto-clear must never break the poll
            logger.exception("alert auto-clear pass failed")

    def _auto_clear_alerts_sync(
        self, events: list[tuple[int, NotificationMessage]]
    ) -> None:
        """Evaluate auto-clear for each event in one session. Synchronous.

        Opens a single transactional scope and calls the auto-clear evaluator per
        event; the evaluator no-ops for events whose ``reason`` is not a resolve
        signal, so the common case is cheap. Each resolve clears only alerts
        raised before it (#134). The ``reason`` marker is read from
        the snapshotted message metadata, so this never touches a detached ORM
        row.
        """
        with session_scope(self._session_factory) as session:
            for event_id, message in events:
                auto_clear_for_event(
                    session,
                    scope=message.scope,
                    scope_id=message.scope_id,
                    reason=_reason_of_message(message),
                    # Only alerts raised before this resolve (#134).
                    resolve_event_id=event_id,
                )

    def _poll_new_events(self) -> list[_PolledEvent]:
        """Read a batch of events newer than the read mark as detached snapshots.

        Synchronous; call via a thread executor. Each row is converted to a
        :class:`_PolledEvent` *inside* the session, so no ORM instance escapes
        to be touched after the session closes (a detached lazy-load would
        otherwise raise). Ordered by id so the last one's id is the new read
        mark. An event of the startup catch-up range older than the catch-up
        cutoff is marked not to deliver. Best effort on a degraded database: a
        missing table yields an empty batch rather than raising.
        """
        try:
            with session_scope(self._session_factory) as session:
                rows = (
                    session.query(Event)
                    .filter(Event.id > self._high_water_mark)
                    .order_by(Event.id)
                    .limit(_POLL_BATCH_SIZE)
                    .all()
                )
                return [
                    _PolledEvent(
                        event_id=row.id,
                        message=(message := _message_from_event(row)),
                        deliver=not self._is_stale_catchup(row.id, message),
                    )
                    for row in rows
                ]
        except Exception:  # noqa: BLE001 - tolerate a degraded/unmigrated database
            logger.exception("notification event poll query failed")
            return []

    def _is_stale_catchup(self, event_id: int, message: NotificationMessage) -> bool:
        """Whether an event is in the catch-up range and older than its cutoff."""
        cutoff = self._catchup_cutoff
        if cutoff is None or event_id > self._catchup_until_id:
            return False
        return _naive_utc(message.timestamp) < cutoff

    def _current_max_id(self) -> int:
        """Return the current maximum event id, or 0 if none. Synchronous."""
        with session_scope(self._session_factory) as session:
            value = session.query(func.max(Event.id)).scalar()
            return int(value) if value is not None else 0

    def _load_routing_rules(self) -> list[dict[str, Any]]:
        """Read the configured routing rules from the notification settings row.

        Returns an empty list when no settings row exists or the rules are
        absent/malformed, so a missing configuration simply routes nothing.
        Synchronous; the poll calls it (or an injected replacement) via a thread
        executor.
        """
        try:
            with session_scope(self._session_factory) as session:
                settings = session.get(NotificationSettings, 1)
                if settings is None or settings.routing_rules is None:
                    return []
                rules = settings.routing_rules
                if not isinstance(rules, list):
                    return []
                return [rule for rule in rules if isinstance(rule, dict)]
        except Exception:  # noqa: BLE001 - tolerate a degraded database
            logger.exception("failed to load notification routing rules")
            return []


def _event_type_of(event: Event) -> str:
    """Extract the dotted event type from an event's JSON details, or ''."""
    details = event.event_metadata or {}
    value = details.get(_TYPE_KEY) if isinstance(details, dict) else None
    return str(value) if value is not None else ""


def _reason_of_message(message: NotificationMessage) -> str | None:
    """Extract the ``reason`` resolve/raise marker from a message, or None.

    The untyped supervisor events (low disk, frozen, camera offline/recovery)
    carry their condition under the ``"reason"`` details key; auto-clear matches
    on it. Read from the snapshotted metadata so no detached ORM row is touched.
    """
    details = message.metadata or {}
    if not isinstance(details, dict):
        return None
    value = details.get("reason")
    return str(value) if value is not None else None


def _message_from_event(event: Event) -> NotificationMessage:
    """Snapshot an event row into a detached, channel-agnostic message.

    Must be called while ``event`` is still attached to its session: it reads
    every attribute the dispatcher needs up front so the resulting message can
    safely outlive the session.
    """
    timestamp = event.timestamp or datetime.now(UTC).replace(tzinfo=None)
    return NotificationMessage(
        event_type=_event_type_of(event),
        scope=event.scope,
        scope_id=event.scope_id,
        level=event.level,
        message=event.message,
        timestamp=timestamp,
        metadata=event.event_metadata,
    )


def _naive_utc(moment: datetime) -> datetime:
    """Return ``moment`` as naive UTC, the form the event columns store."""
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(UTC).replace(tzinfo=None)


def _monotonic() -> float:
    """Return a monotonic clock reading for debounce timing."""
    return asyncio.get_event_loop().time()
