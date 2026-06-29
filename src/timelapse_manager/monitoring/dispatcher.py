"""Poll-based asynchronous notification dispatcher.

The dispatcher is the delivery side of the monitoring subsystem. A single
long-lived asyncio task polls the event table for rows newer than a high-water
mark, evaluates the routing rules for each, and fans matching events out to the
injected channels with debounce and bounded retry.

Design points the rest of the system relies on (these mirror the render worker's
reliability shape):

* **Startup high-water mark.** :meth:`start` sets the high-water mark to the
  current maximum event id, so a historical backlog is never blasted out on
  startup -- only events logged *after* start are delivered.
* **Read-then-auto-clear poll.** The poll query (:meth:`_poll_new_events`) is
  read-only, but each poll cycle additionally runs the active-alert auto-clear
  evaluator over the same just-pulled batch (:meth:`_auto_clear_alerts`, a
  contained ``to_thread`` write). The poll is the one chokepoint that sees both
  event write paths (``log_event`` and the supervisor's untyped ``_write_event``)
  and runs over every new event regardless of level, which is exactly what
  auto-clear-on-resolve needs (resolve signals are info level). Each new event is
  processed once, gated by the high-water mark, so a resolve never auto-clears
  twice. Unlike the render worker there is no orphan to reclaim: a poll that
  completes after :meth:`stop` cancels simply discards an in-memory mark update.
  This gives at-most-once delivery under that race, which is the documented
  trade-off.
* **Bulletproof stop.** :meth:`stop` cancels the poll task **and every in-flight
  per-event dispatch task**, then awaits them all with
  ``return_exceptions=True``. It never merely waits-with-timeout: an orphan
  dispatch to a dead host would otherwise leak onto a closing loop. Idempotent
  and safe with zero tasks.
* **Hard per-send timeout.** Every channel ``send`` is wrapped in
  ``asyncio.wait_for`` using ``channel_send_timeout_seconds`` so a cooperatively
  awaiting channel cannot block shutdown. A blocking synchronous send is bounded
  only by the channel's own socket/HTTP timeout (part of the channel contract).
* **Loop prevention.** A channel failure is caught, retried up to ``max_retries``
  with backoff, and -- on final failure -- recorded as a delivery-failure event
  that is never itself routed to a channel. A channel never crashes the loop.
* **Debounce.** A notification for a given ``(event_type, scope, scope_id)`` key
  is suppressed per channel if one was sent within ``debounce_window_seconds``.
  The key table is pruned so memory stays bounded.

:meth:`run_once` performs exactly one poll-and-dispatch pass synchronously with
respect to the caller (awaiting all dispatch tasks it spawns), so tests can drive
delivery deterministically without the background loop.

Secrets and encryption at rest
------------------------------
The dispatcher itself handles no credentials: channels are injected already built
from their (decrypted) transport configuration, and the dispatcher only calls
``channel.send()`` and re-reads routing rules (which carry no secret). At-rest
encryption is therefore transparent here -- the decrypt-at-use seam lives where
the channels are constructed (the notification settings service / startup wiring),
not in this loop.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func
from sqlalchemy.orm import Session, sessionmaker

from ..db.models import Event, NotificationSettings
from ..db.session import session_scope
from .alerts import auto_clear_for_event
from .channels import ChannelSendError, NotificationChannel, NotificationMessage
from .events import EventType, log_event
from .routing import evaluate_routing_rules

if TYPE_CHECKING:
    from ..config.settings import MonitoringSettings

logger = logging.getLogger(__name__)

# Maximum number of event rows pulled in a single poll, so a large burst is
# delivered in bounded batches rather than loaded all at once.
_POLL_BATCH_SIZE = 200

# The JSON details key holding the event type (see events.log_event).
_TYPE_KEY = "type"


class NotificationDispatcher:
    """Polls for new events and fans them out to injected channels.

    Channels are injected at construction; this class never imports a concrete
    channel. The current routing rules are supplied by ``routing_rules_fn`` so
    the configuration can change at runtime without restarting the dispatcher;
    the default reads them from the notification settings row.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        channels: Sequence[NotificationChannel],
        settings: MonitoringSettings,
        routing_rules_fn: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> None:
        """Create the dispatcher; performs no I/O and starts no tasks.

        :param session_factory: factory for synchronous ORM sessions.
        :param channels: the channels to deliver through, keyed by their
            ``name``. Duplicates by name are last-wins.
        :param settings: the monitoring section of application settings.
        :param routing_rules_fn: returns the current routing rules; defaults to
            reading them from the notification settings row.
        """
        self._session_factory = session_factory
        self._channels: dict[str, NotificationChannel] = {
            channel.name: channel for channel in channels
        }
        self._settings = settings
        self._routing_rules_fn = routing_rules_fn or self._load_routing_rules
        self._high_water_mark = 0
        self._loop_task: asyncio.Task[None] | None = None
        self._dispatch_tasks: set[asyncio.Task[None]] = set()
        self._started = False
        self._stopped = False
        # Debounce table: (event_type, scope, scope_id, channel) -> last-sent
        # monotonic time. Pruned in dispatch so it stays bounded.
        self._last_sent: dict[tuple[str, str, int | None, str], float] = {}

    async def start(self) -> None:
        """Set the startup high-water mark and launch the poll loop. Idempotent.

        The high-water mark is initialised to the current maximum event id so an
        existing backlog is not delivered. If reading the maximum id fails (for
        example on an unmigrated database during degraded startup), the loop
        still starts and simply begins from zero; the error is logged, not
        raised, so dispatcher startup never aborts process boot.
        """
        if self._started:
            return
        self._started = True
        try:
            self._high_water_mark = await asyncio.to_thread(self._current_max_id)
        except Exception:  # noqa: BLE001 - never block startup on the mark read
            logger.exception("failed to read initial event high-water mark")
            self._high_water_mark = 0
        self._loop_task = asyncio.create_task(
            self._poll_loop(), name="notification-dispatcher"
        )
        logger.info(
            "notification dispatcher started (high_water_mark=%d, channels=%d)",
            self._high_water_mark,
            len(self._channels),
        )

    async def stop(self) -> None:
        """Cancel the poll loop and every in-flight dispatch, awaiting cleanup.

        Cancels the poll task and all per-event dispatch tasks, then awaits them
        with ``return_exceptions=True`` so a hanging channel send (cancelled
        through its ``wait_for`` wrapper) cannot leak onto a closing loop. A poll
        running in a worker thread is not cancellable; this never waits on it, so
        a poll that finishes after cancellation merely discards its result.
        Idempotent and safe with zero tasks.
        """
        if self._stopped:
            return
        self._stopped = True

        tasks: list[asyncio.Task[None]] = []
        if self._loop_task is not None:
            self._loop_task.cancel()
            tasks.append(self._loop_task)
        for task in list(self._dispatch_tasks):
            task.cancel()
            tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info(
            "notification dispatcher stopped (%d dispatch task(s))",
            len(self._dispatch_tasks),
        )

    async def run_once(self) -> int:
        """Run exactly one poll-and-dispatch pass; return the number of events.

        Polls for events newer than the high-water mark, advances the mark, and
        dispatches each matching event to completion (awaiting the per-event
        dispatch tasks it spawns) before returning. Intended for deterministic
        testing without the background loop. Safe to call repeatedly.

        :raises RuntimeError: if the background poll loop is running. The two
            share the high-water mark without locking by design (the loop is the
            only poller in production), so driving both at once would double the
            mark advance and double-deliver. This guard turns that misuse into a
            clear error instead of a confusing intermittent result.
        """
        if self._loop_task is not None and not self._loop_task.done():
            raise RuntimeError(
                "run_once() cannot be used while the background poll loop is "
                "running; it is a standalone driver for tests."
            )
        events = await asyncio.to_thread(self._poll_new_events)
        if not events:
            return 0
        self._high_water_mark = max(self._high_water_mark, events[-1][0])
        await self._auto_clear_alerts(events)
        rules = self._routing_rules_fn()
        tasks = [
            asyncio.create_task(self._dispatch_event(message, rules))
            for _event_id, message in events
        ]
        for task in tasks:
            self._dispatch_tasks.add(task)
            task.add_done_callback(self._dispatch_tasks.discard)
        await asyncio.gather(*tasks, return_exceptions=True)
        return len(events)

    async def _poll_loop(self) -> None:
        """Poll for new events on the configured interval until cancelled.

        Each cycle polls, advances the high-water mark, and launches a dispatch
        task per matching event. Launched tasks are tracked so :meth:`stop` can
        cancel them. ``asyncio.sleep`` between cycles is cleanly cancellable; no
        ``wait_for``/event-wait is used here (that pattern can swallow
        cancellation), and any poll error is contained so the loop never dies.
        """
        interval = max(0.1, self._settings.poll_interval_seconds)
        while True:
            try:
                await self._poll_and_launch()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a poll error must not kill the loop
                logger.exception("notification poll cycle failed")
            await asyncio.sleep(interval)

    async def _poll_and_launch(self) -> None:
        """Poll once and launch a tracked dispatch task per matching event."""
        events = await asyncio.to_thread(self._poll_new_events)
        if not events:
            return
        self._high_water_mark = max(self._high_water_mark, events[-1][0])
        await self._auto_clear_alerts(events)
        rules = self._routing_rules_fn()
        for event_id, message in events:
            task = asyncio.create_task(
                self._dispatch_event(message, rules),
                name=f"notify-dispatch-{event_id}",
            )
            self._dispatch_tasks.add(task)
            task.add_done_callback(self._dispatch_tasks.discard)

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
            delivered = await self._deliver_with_retry(channel, message)
            if delivered:
                self._last_sent[key] = now

    async def _deliver_with_retry(
        self, channel: NotificationChannel, message: NotificationMessage
    ) -> bool:
        """Deliver one message to one channel with bounded retry; report success.

        Each attempt is wrapped in ``asyncio.wait_for`` so a hanging send is
        bounded (and cancellable on shutdown). A :class:`ChannelSendError` or a
        per-send timeout triggers backoff and a retry up to ``max_retries``. When
        all attempts fail, a delivery-failure event is recorded (never routed to
        a channel) and ``False`` is returned. Cancellation is re-raised so
        shutdown is immediate.
        """
        timeout = self._settings.channel_send_timeout_seconds
        attempts = max(1, self._settings.max_retries)
        last_error = ""
        for attempt in range(attempts):
            try:
                await asyncio.wait_for(channel.send(message), timeout=timeout)
                return True
            except asyncio.CancelledError:
                raise
            except (ChannelSendError, TimeoutError) as exc:
                last_error = str(exc) or exc.__class__.__name__
            except Exception as exc:  # noqa: BLE001 - contain any channel fault
                last_error = str(exc) or exc.__class__.__name__
            if attempt + 1 < attempts:
                await self._backoff(attempt)
        self._record_delivery_failure(channel.name, message, last_error)
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
        notification. Best effort: any failure here is logged and swallowed.
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
        signal, so the common case is cheap. The ``reason`` marker is read from
        the snapshotted message metadata, so this never touches a detached ORM
        row.
        """
        with session_scope(self._session_factory) as session:
            for _event_id, message in events:
                auto_clear_for_event(
                    session,
                    scope=message.scope,
                    scope_id=message.scope_id,
                    reason=_reason_of_message(message),
                )

    def _poll_new_events(self) -> list[tuple[int, NotificationMessage]]:
        """Read a batch of events newer than the mark as detached snapshots.

        Synchronous; call via a thread executor. Each row is converted to an
        ``(event_id, NotificationMessage)`` pair *inside* the session, so no ORM
        instance escapes to be touched after the session closes (a detached
        lazy-load would otherwise raise). Ordered by id so the last pair's id is
        the new high-water mark. Best effort on a degraded database: a missing
        table yields an empty batch rather than raising.
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
                return [(row.id, _message_from_event(row)) for row in rows]
        except Exception:  # noqa: BLE001 - tolerate a degraded/unmigrated database
            logger.exception("notification event poll query failed")
            return []

    def _current_max_id(self) -> int:
        """Return the current maximum event id, or 0 if none. Synchronous."""
        with session_scope(self._session_factory) as session:
            value = session.query(func.max(Event.id)).scalar()
            return int(value) if value is not None else 0

    def _load_routing_rules(self) -> list[dict[str, Any]]:
        """Read the configured routing rules from the notification settings row.

        Returns an empty list when no settings row exists or the rules are
        absent/malformed, so a missing configuration simply routes nothing.
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


def _monotonic() -> float:
    """Return a monotonic clock reading for debounce timing."""
    return asyncio.get_event_loop().time()
