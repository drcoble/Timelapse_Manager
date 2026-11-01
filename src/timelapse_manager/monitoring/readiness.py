"""Readiness evaluation behind ``/healthz`` and ``tlm_health_status`` (#67 R5).

``/healthz`` used to answer 200 for any process whose database answered and
whose background loops were alive, even while capture had stopped reconciling,
a render sat in ``encoding`` with no task, or the notification poll had not run
for an hour. This module turns the in-memory signals the components already
keep into one of three statuses:

* ``failed`` -- the database probe failed or a started background loop died.
  ``/healthz`` answers 503, exactly as before.
* ``degraded`` -- the process serves, but something it is responsible for has
  stalled (see :func:`evaluate_readiness` for the list). ``/healthz`` answers
  200, so an existing status-code poller does not alarm on it (owner decision
  D4), but the body and the metric say so.
* ``ok`` otherwise.

Everything here reads memory only: no query beyond the caller's own ``SELECT 1``
probe, no filesystem access and no awaits, so the probe stays cheap on a busy
host and can run on a worker thread. Every reader is guarded so a broken
component degrades its own check rather than raising into the probe.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from ..runtime import AppContext

logger = logging.getLogger(__name__)

Status = Literal["ok", "degraded", "failed"]
CheckStatus = Literal["ok", "degraded", "failed", "absent"]

# Every status, in a fixed order, for the one-hot ``tlm_health_status`` series.
STATUSES: tuple[Status, ...] = ("ok", "degraded", "failed")

# The capture supervisor must finish a reconcile pass within this many of its
# intervals.
RECONCILE_STALE_FACTOR = 3.0
# An ``encoding`` render is overdue once it outlives the render watchdog's
# ceiling for it by more than this many stall timeouts. The ceiling runs from
# ffmpeg's spawn, so the margin covers preparation, the watchdog's tick and the
# kill and settle that follow.
ENCODING_OVERDUE_GRACE_STALL_TIMEOUTS = 1.0
# The notification dispatcher must finish a poll within this many intervals.
POLL_STALE_FACTOR = 6.0
# A ``loop-lag`` tick later than this is "late", in seconds.
LOOP_LAG_LIMIT_SECONDS = 1.0
# The loop counts as lagging after this many consecutive late ticks, so one slow
# tick (a large frame write, a GC pause) does not flap ``/healthz`` (#117).
LOOP_LAG_CONSECUTIVE_TICKS = 3
# A single tick this late is a long stall in itself and degrades at once: a
# blocked loop yields only one late tick, however long the block.
LOOP_LAG_SEVERE_SECONDS = 5.0

# The capture pause reason the supervisor records for a low-disk pause.
_PAUSE_LOW_DISK = "low_disk"

# A reference point for "how long has this component had to report": a
# component that has not reported yet is only stale once it has been silent for
# its threshold since the process started. Importing this module is part of
# building the application, so this is close enough to process start.
PROCESS_STARTED_MONOTONIC = time.monotonic()


@dataclass(frozen=True)
class Check:
    """One readiness signal: its status and the figure behind it.

    ``value`` is a plain JSON scalar (seconds, a count, or a status word), or
    ``None`` when the component has not reported yet.
    """

    status: CheckStatus
    value: float | int | str | None

    def as_dict(self) -> dict[str, Any]:
        """The ``{"status", "value"}`` form served in the full ``/healthz`` body."""
        return {"status": self.status, "value": self.value}


@dataclass(frozen=True)
class Readiness:
    """The evaluated status, the database word, and every check by name."""

    status: Status
    db_status: str
    checks: dict[str, Check] = field(default_factory=dict)

    @property
    def http_status(self) -> int:
        """503 for ``failed``; 200 for ``ok`` and ``degraded`` (decision D4)."""
        return 503 if self.status == "failed" else 200


def db_status(session_factory: sessionmaker[Session]) -> str:
    """Return ``"ok"`` if a trivial query succeeds, else ``"error"``."""
    try:
        session = session_factory()
        try:
            session.execute(text("SELECT 1"))
        finally:
            session.close()
    except Exception:
        return "error"
    return "ok"


def component_status(component: Any) -> str:
    """Classify a long-lived background component's liveness for the probe.

    ``"ok"`` when the component exists and its loop is running; ``"dead"`` when it
    was started but its loop is no longer running (a crash or unexpected stop);
    ``"absent"`` when it is not participating -- either never constructed
    (``None``) or constructed but never started because its feature is not
    autostarted. Treating a never-started component as absent (not dead) keeps a
    deliberately-idle instance healthy, so only a genuine crash trips 503.

    The component's ``is_running()`` predicate is pure and non-raising, but this
    degrades any unexpected failure to ``"dead"`` so the probe never raises.
    """
    if component is None:
        return "absent"
    try:
        if component.is_running():
            return "ok"
        # ``_started`` is the one flag common to every component (their task
        # attributes differ). A component that was never started is disabled by
        # configuration, not failed; only a started-then-stopped loop is dead.
        if not getattr(component, "_started", False):
            return "absent"
        return "dead"
    except Exception:
        return "dead"


def component_statuses(context: AppContext) -> dict[str, str]:
    """The per-component liveness map ``/healthz`` has always reported."""
    return {
        "capture_supervisor": component_status(context.capture_supervisor),
        "render_queue": component_status(context.render_queue),
        "render_scheduler": component_status(context.render_scheduler),
        "notification_dispatcher": component_status(context.notification_dispatcher),
    }


def evaluate_readiness(
    context: AppContext,
    *,
    database: str,
    components: dict[str, str],
    now: datetime | None = None,
    now_monotonic: float | None = None,
    process_started_monotonic: float = PROCESS_STARTED_MONOTONIC,
) -> Readiness:
    """Combine the database word, the component map and the stall signals.

    ``failed`` when ``database`` is not ``"ok"`` or any component is
    ``"dead"``. Otherwise ``degraded`` when any of these holds, each derived
    from an existing setting:

    * ``capture_reconcile``: the supervisor's last clean reconcile pass is
      older than 3x ``capture.reconcile_interval_seconds``;
    * ``capture_runners``: a capture runner exited unexpectedly or its
      heartbeat is past the supervisor's wedge threshold (the condition the
      reconcile loop relaunches it for);
    * ``render_orphans``: ``encoding`` rows the render worker found with no
      live task and could not fail;
    * ``render_overdue_encoding``: an ``encoding`` render has outlived the
      render watchdog's own ceiling for it (``max(3600, 2 * frames)`` s, or
      ``render.max_job_seconds``) by more than
      ``render.stall_timeout_seconds``, i.e. the watchdog should have ended it;
    * ``notification_poll``: the dispatcher's last successful poll is older
      than 6x ``monitoring.poll_interval_seconds``;
    * ``event_loop_lag``: the last 3 ``loop-lag`` ticks were each more than 1 s
      late, or the latest tick was 5 s or more late (one slow tick alone is
      not enough, #117);
    * ``capture_low_disk``: a capture runner is paused for low disk.

    A component that is absent (not constructed or not started) reports its
    checks as ``absent`` and never trips ``degraded``. ``now`` and
    ``now_monotonic`` are injectable for tests.
    """
    moment = now if now is not None else datetime.now(UTC)
    tick = now_monotonic if now_monotonic is not None else time.monotonic()
    uptime = max(0.0, tick - process_started_monotonic)

    checks: dict[str, Check] = {
        "database": Check("ok" if database == "ok" else "failed", database),
    }
    for name, word in components.items():
        checks[f"component_{name}"] = Check(_component_check_status(word), word)

    readers = (
        ("capture_reconcile", lambda: _reconcile_check(context, moment, uptime)),
        ("capture_runners", lambda: _runner_problem_check(context)),
        ("capture_low_disk", lambda: _low_disk_check(context)),
        ("render_orphans", lambda: _render_orphans_check(context)),
        ("render_overdue_encoding", lambda: _overdue_encoding_check(context, moment)),
        ("notification_poll", lambda: _poll_check(context, tick)),
        ("event_loop_lag", lambda: _loop_lag_check(context)),
    )
    for name, read in readers:
        try:
            checks[name] = read()
        except Exception:
            # A reader that breaks is itself a sign of trouble, but never a
            # reason for the probe to fail: report it and carry on.
            logger.exception("readiness check %s failed", name)
            checks[name] = Check("degraded", "error")

    statuses = {check.status for check in checks.values()}
    status: Status
    if "failed" in statuses:
        status = "failed"
    elif "degraded" in statuses:
        status = "degraded"
    else:
        status = "ok"
    return Readiness(status=status, db_status=database, checks=checks)


def _component_check_status(word: str) -> CheckStatus:
    if word == "dead":
        return "failed"
    if word == "absent":
        return "absent"
    return "ok"


def _running(component: Any) -> bool:
    return component is not None and component_status(component) == "ok"


def _stale(age: float, limit: float) -> CheckStatus:
    return "degraded" if age > limit else "ok"


def _reconcile_check(context: AppContext, now: datetime, uptime: float) -> Check:
    supervisor = context.capture_supervisor
    if not _running(supervisor):
        return Check("absent", None)
    assert supervisor is not None  # for mypy: _running() checked it
    interval = max(1.0, context.settings.capture.reconcile_interval_seconds)
    limit = RECONCILE_STALE_FACTOR * interval
    last = supervisor.last_reconcile_at
    if last is None:
        # No clean pass yet: stale only once the process has been up that long.
        return Check(_stale(uptime, limit), None)
    age = max(0.0, (now - last).total_seconds())
    return Check(_stale(age, limit), round(age, 3))


def _runner_problem_check(context: AppContext) -> Check:
    """Count runners the reconcile loop would relaunch: exited or wedged.

    The supervisor's own :meth:`~CaptureSupervisor.runner_problems` is the single
    definition, so the probe cannot drift from what the reconcile tick does
    (#116). A runner in the ``error`` state (a camera that is offline) is not
    counted: that is a camera problem the runner is handling, not a stall of the
    application. Read-only, so it runs on any thread.
    """
    supervisor = context.capture_supervisor
    if not _running(supervisor):
        return Check("absent", None)
    assert supervisor is not None  # for mypy: _running() checked it
    problems = len(supervisor.runner_problems())
    return Check("degraded" if problems else "ok", problems)


def _low_disk_check(context: AppContext) -> Check:
    supervisor = context.capture_supervisor
    if not _running(supervisor):
        return Check("absent", None)
    assert supervisor is not None  # for mypy: _running() checked it
    paused = sum(
        1
        for snapshot in supervisor.runner_snapshots()
        if snapshot.state.pause_reason == _PAUSE_LOW_DISK
    )
    return Check("degraded" if paused else "ok", paused)


def _render_orphans_check(context: AppContext) -> Check:
    queue = context.render_queue
    if not _running(queue):
        return Check("absent", None)
    assert queue is not None  # for mypy: _running() checked it
    orphans = queue.unreclaimed_orphans
    return Check("degraded" if orphans > 0 else "ok", orphans)


def _overdue_encoding_check(context: AppContext, now: datetime) -> Check:
    """Count ``encoding`` renders the watchdog should already have ended.

    A long render inside its budget is not a stall; only a job past its
    watchdog ceiling (plus a stall timeout of grace) means the watchdog itself
    is not working.
    """
    queue = context.render_queue
    if not _running(queue):
        return Check("absent", None)
    assert queue is not None  # for mypy: _running() checked it
    grace = (
        ENCODING_OVERDUE_GRACE_STALL_TIMEOUTS
        * context.settings.render.stall_timeout_seconds
    )
    overdue = sum(1 for overrun in queue.encoding_overruns(now) if overrun > grace)
    return Check("degraded" if overdue else "ok", overdue)


def oldest_encoding_age_seconds(
    started_at: datetime | None, now: datetime
) -> float | None:
    """Seconds since ``started_at`` (naive UTC, like the column), or ``None``."""
    if started_at is None:
        return None
    naive_now = now.astimezone(UTC).replace(tzinfo=None)
    started = (
        started_at.astimezone(UTC).replace(tzinfo=None)
        if started_at.tzinfo is not None
        else started_at
    )
    return max(0.0, (naive_now - started).total_seconds())


def _poll_check(context: AppContext, now_monotonic: float) -> Check:
    dispatcher = context.notification_dispatcher
    if not _running(dispatcher):
        return Check("absent", None)
    assert dispatcher is not None  # for mypy: _running() checked it
    interval = max(0.1, context.settings.monitoring.poll_interval_seconds)
    # Counts from the dispatcher's start until its first successful poll.
    age = dispatcher.last_poll_age_seconds(now_monotonic)
    if age is None:
        return Check("absent", None)
    return Check(_stale(age, POLL_STALE_FACTOR * interval), round(age, 3))


def _loop_lag_check(context: AppContext) -> Check:
    monitor = context.loop_lag_monitor
    if monitor is None:
        return Check("absent", None)
    lag = monitor.last_lag_seconds
    recent = monitor.recent_lags(LOOP_LAG_CONSECUTIVE_TICKS)
    sustained = len(recent) >= LOOP_LAG_CONSECUTIVE_TICKS and all(
        tick > LOOP_LAG_LIMIT_SECONDS for tick in recent
    )
    severe = lag >= LOOP_LAG_SEVERE_SECONDS
    return Check("degraded" if sustained or severe else "ok", round(lag, 6))
