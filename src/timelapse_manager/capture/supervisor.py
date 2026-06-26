"""Per-project capture supervisor.

The supervisor owns the background side of the capture engine: for every active
project that is configured to capture (it has an interval and an associated
camera), it runs one asyncio task that grabs frames and hands them to the
:class:`~timelapse_manager.capture.frame_writer.FrameWriter`.

Timing is **schedule-driven**. Each project's loop parses its stored schedule
once and, every cycle, asks the pure evaluator in
:mod:`timelapse_manager.capture.schedule` whether the capture gate is open and
when it next changes. The "what to do next and for how long" decision is itself
factored into a pure, synchronous helper (:func:`_plan_next`) so it can be
reasoned about and tested without a wall clock. A project with no schedule (or a
disabled/empty one) sees an always-open gate and so behaves exactly like a plain
fixed-interval capture.

Design points that keep the loop robust:

* **Per-project isolation.** Each project's loop catches its own exceptions,
  records them as state, and keeps going, so one misbehaving camera never stalls
  another project. ``asyncio.CancelledError`` is *not* caught -- it must
  propagate so shutdown can cancel cleanly.
* **Reconnect with capped exponential backoff + jitter.** A transient capture
  failure (unreachable / timeout / other) backs off before the next attempt;
  the delay grows exponentially up to a cap, with per-project jitter so failing
  peers do not retry in lockstep. The first success resets the backoff. The
  schedule gate still applies: a closed window is never overridden by a pending
  retry.
* **Frozen-frame detection.** After each successful capture the image bytes are
  hashed; a run of identical hashes that reaches the configured threshold emits
  a warning event. Capture is never stopped on a frozen camera.
* **Restart-survival gap logging.** On start, each resumed project's last frame
  timestamp is read; a meaningful gap to now is recorded as an informational
  event. Capture always resumes *forward* from the next sequence index (the
  writer computes ``max(sequence_index)+1``); missed frames are never
  synthesised and existing frames are never overwritten.
* **Bulletproof shutdown.** :meth:`stop` cancels every task, awaits them with
  ``return_exceptions=True`` so nothing leaks onto a closing event loop, and
  closes the shared HTTP client. It is idempotent and safe with zero tasks.
* **Construct-without-start.** The supervisor is always constructed (the manual
  capture endpoint needs the shared HTTP client and the writer), but background
  loops only run after :meth:`start`. Startup wiring gates that call on the
  autostart setting.

Archived projects are excluded at :meth:`start` (``_load_targets`` filters to
``lifecycle_state == "active"``), so they are never resumed.

* **Runtime reconciliation.** A background reconcile loop periodically re-reads
  the qualifying set (the same ``_load_targets`` query) and converges the running
  tasks to it: a project that becomes qualifying while the service runs (newly
  created, reactivated) is launched, and one that stops qualifying (archived,
  interval cleared, camera protocol removed, project deleted) has its task
  cancelled and cleaned up -- all without a process restart. The loop wakes on a
  configurable interval or early when :meth:`notify_reconcile` is called (e.g. by
  a project-create/edit handler). Reconciliation is keyed by ``project_id``: a
  still-qualifying project keeps its already-running task *unless* its
  loop-affecting configuration (interval, camera, schedule, storage path) was
  edited, in which case the loop is stopped and relaunched with the fresh target
  so the change takes runtime effect; a rename or geolocation tweak leaves the
  live task untouched.

Synchronous database access (reading capture targets, writing frames, writing
events) is moved off the event loop with :func:`asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple, Protocol

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from ..cameras import CameraAdapter, CapturedFrame, build_adapter
from ..cameras.base import CaptureError, TimeoutCaptureError
from ..config import Settings
from ..db.models import Camera, Event, ExactTimeFire, Frame, Project
from ..db.session import session_scope
from ..ffmpeg_pin import resolve_ffmpeg_binary
from ..security.camera_defaults_service import resolve_default_credentials
from ..storage import paths
from ..storage.monitor import DiskSpaceMonitor
from . import anchors as anchors_mod
from . import event_triggers as event_triggers_mod
from . import geo
from .frame_writer import FrameWriter
from .one_shot import capture_one_now
from .schedule import Schedule, next_transition, parse_schedule

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from typing import Any

logger = logging.getLogger(__name__)

# A resumed project whose last frame is older than this multiple of its capture
# interval is treated as having a "meaningful" downtime gap worth recording. The
# multiple (rather than a flat number of seconds) keeps the threshold sensible
# across very different interval scales.
_GAP_INTERVAL_MULTIPLE = 2

# Pause-reason markers recorded on CaptureState when the capture gate is closed.
# ``None`` means the gate is open (capturing or due to capture).
_PAUSE_LOW_DISK = "low_disk"
_PAUSE_WINDOW = "window"

# Reason markers for the camera offline/recovery alert pair, written on the event
# under the JSON ``"reason"`` key. ``camera_recovered`` is the resolve signal that
# auto-clears an active ``camera_offline`` alert for the same project scope (see
# the monitoring alerts module's resolve map).
_REASON_CAMERA_OFFLINE = "camera_offline"
_REASON_CAMERA_RECOVERED = "camera_recovered"

# Reasons a project's campaign runs to completion (stop capture + archive).
_CAMPAIGN_END_DATE = "end_date"
_CAMPAIGN_END_FRAME_COUNT = "frame_count"


def _as_aware_utc(value: datetime | None) -> datetime | None:
    """Normalise a possibly-naive stored datetime to aware UTC, or pass None.

    The campaign-bound columns are stored naive (the project's other datetimes
    use the same convention); the running loop and the clock work in aware UTC,
    so a naive value is interpreted as UTC before any comparison.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _campaign_end_reason(
    *,
    now: datetime,
    end_date: datetime | None,
    frame_count: int,
    max_frame_count: int | None,
) -> str | None:
    """Return why a campaign is complete at ``now``, or ``None`` if it is not.

    End-date is checked before the frame cap, but the order is immaterial -- a
    project that has hit either bound is done. ``end_date`` may be naive (it is
    normalised here); ``frame_count`` is the project's current active count.
    """
    end = _as_aware_utc(end_date)
    if end is not None and now >= end:
        return _CAMPAIGN_END_DATE
    if max_frame_count is not None and frame_count >= max_frame_count:
        return _CAMPAIGN_END_FRAME_COUNT
    return None


def _build_disk_monitor(settings: Settings) -> DiskSpaceMonitor:
    """Build a :class:`DiskSpaceMonitor` from the storage settings section."""
    storage = settings.storage
    return DiskSpaceMonitor(
        low_watermark_bytes=storage.low_watermark_bytes,
        low_watermark_percent=storage.low_watermark_percent,
        resume_watermark_bytes=storage.resume_watermark_bytes,
        resume_watermark_percent=storage.resume_watermark_percent,
        check_interval_seconds=storage.check_interval_seconds,
    )


class EventSourceFactory(Protocol):
    """Builds the event source a project's listener consumes.

    The concrete event-source contract (subscribe, yield camera events, tear
    down) is owned by a later phase; this supervisor depends only on the *shape*
    of a factory it can call to obtain one per project. Injected at construction
    so the listener lifecycle is testable with a fake source and pluggable with a
    real one without changing the supervisor. Returns an async iterator of opaque
    event objects, or ``None`` when the project exposes no consumable source.
    """

    def __call__(self, target: CaptureTarget) -> AsyncIterator[Any] | None:
        """Return an event source for ``target``, or ``None`` if unavailable."""
        ...


class Clock(Protocol):
    """The minimal time surface the supervisor depends on.

    Abstracted so tests can drive time deterministically (advance ``now`` and
    make ``sleep`` return immediately) while production uses the real wall clock
    and event loop. Correctness lives in the pure :func:`_plan_next` helper, not
    in wall-clock waits, so a fake clock never needs to model elapsed time.
    """

    def now(self) -> datetime:
        """Return the current aware-UTC instant."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Sleep for ``seconds`` (cancellable)."""
        ...


class _RealClock:
    """The default :class:`Clock`: real UTC time and ``asyncio.sleep``."""

    def now(self) -> datetime:
        """Return the current aware-UTC instant."""
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        """Sleep for ``seconds`` on the event loop."""
        await asyncio.sleep(seconds)


@dataclass
class CaptureTarget:
    """A snapshot of one project's capture configuration, read from the DB.

    Decoupled from the ORM rows so the running loop holds plain values rather
    than detached SQLAlchemy instances. ``schedule``/``latitude``/``longitude``
    default to "no schedule / no location", which yields an always-open gate and
    the plain fixed-interval behaviour for projects that never opt in.

    ``interval_seconds`` is ``None`` for an *anchor-only* project: one that
    qualifies for capture through exact-time anchors (or, later, event triggers)
    but has no recurring interval. The loop tolerates this -- it does no interval
    captures, only gate/anchor waits -- so the runner can exist purely to fire
    daily anchors. A ``None`` interval is flowed through untouched (never
    replaced by a default) so an anchor-only project does not silently start
    interval capturing.
    """

    project_id: int
    project_name: str
    camera_id: int
    interval_seconds: int | None
    schedule: dict[str, object] | None = None
    latitude: float | None = None
    longitude: float | None = None
    storage_path: str | None = None
    # Which of the camera's named streams/profiles to capture from, or None for
    # the camera default. Carried from the project and passed to build_adapter so
    # the chosen stream is honoured on every capture. Loop-affecting: changing it
    # changes the captured image, so an edit relaunches the runner (see
    # :func:`_loop_affecting_fields`).
    stream_id: str | None = None
    # Per-project PTZ position to send the camera to before each capture, so a
    # shared movable camera frames this project's scene. ``ptz_preset`` recalls a
    # camera-defined named position; ``ptz_pan``/``ptz_tilt``/``ptz_zoom`` are a
    # raw absolute position in the camera's own units. Raw overrides preset (the
    # UI's precedence). All default to None -- "no PTZ configured", which skips
    # positioning entirely and preserves the plain fixed-camera behaviour.
    ptz_preset: str | None = None
    ptz_pan: float | None = None
    ptz_tilt: float | None = None
    ptz_zoom: float | None = None
    # Campaign bounds. ``start_date``/``end_date`` are aware-UTC instants (the
    # loop normalises the naive stored values on read); ``max_frame_count`` is a
    # cap on the project's active frame count. All default to "no bound", which
    # preserves the open-ended fixed-interval behaviour. These are deliberately
    # *not* part of :func:`_loop_affecting_fields`: an edited end-date is picked
    # up by the next reconcile read, so it never needs a live-runner restart.
    start_date: datetime | None = None
    end_date: datetime | None = None
    max_frame_count: int | None = None
    # Exact-time capture anchors carried from the project, as the raw stored list
    # (the pure anchor parser lives in a separate module filled by a later phase;
    # the runner keeps the raw value and hands it to the evaluation hook). ``None``
    # means no anchors. Loop-affecting: an anchor edit must relaunch the runner so
    # the new wake times take effect (see :func:`_loop_affecting_fields`).
    exact_time_anchors: list[Any] | None = None
    # Event-trigger configuration carried from the project, as the raw stored
    # list. Used only to decide whether the project qualifies for an event
    # listener (a separate concern from the interval/anchor runner); ``None`` or
    # an empty/all-disabled list means no listener.
    event_triggers: list[Any] | None = None


@dataclass
class _CameraConfig:
    """The subset of a camera record an adapter needs, as plain values.

    Holding a detached snapshot avoids touching the ORM (and its session) from
    the async capture path.
    """

    protocol: str | None
    address: str | None
    credentials: dict[str, object] | None
    credentials_inherit_default: bool
    snapshot_uri: str | None
    stream_uri: str | None
    default_resolution: str | None


@dataclass
class CaptureState:
    """Live status for one project's capture loop, exposed to the API.

    All fields beyond ``camera_id`` default, so existing construction
    (``CaptureState(project_id=..., camera_id=...)``) is unaffected. The
    reliability counters (``attempt_count``, ``next_retry_at``,
    ``frozen_frame_run_count``, ``last_frame_hash``) are purely in-memory and are
    never persisted to the database.

    ``pause_reason`` distinguishes *why* the loop is currently idle:
    ``"window"`` (the schedule window is closed), ``"low_disk"`` (the window is
    open but free space is below the watermark), or ``None`` (the gate is open --
    capturing, or merely between captures).
    """

    project_id: int
    camera_id: int
    state: str = "idle"
    last_success_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None
    frames_captured: int = 0
    attempt_count: int = 0
    next_retry_at: datetime | None = None
    frozen_frame_run_count: int = 0
    last_frame_hash: str | None = None
    last_capture_at: datetime | None = None
    pause_reason: str | None = None
    # Whether a camera-offline alert has been raised for the current outage and
    # not yet resolved. Edge-trigger latch: set when consecutive failures cross
    # ``offline_failure_threshold`` so a sustained outage emits one offline event
    # (not one per failed attempt), and cleared by the first success after the
    # outage, which emits the matching recovery event. Purely in-memory.
    offline_alerted: bool = False
    # When this runner began capturing (the basis for reported uptime). Set when
    # the loop task is launched; a reconcile restart (config change, resume)
    # starts a fresh runner and therefore a fresh ``started_at``.
    started_at: datetime | None = None


@dataclass
class _ProjectRunner:
    """Internal pairing of a running task with the state it reports into.

    The launch-time :class:`CaptureTarget` is held so a later reconcile tick can
    detect that a still-qualifying project's loop-affecting configuration
    (interval, camera, schedule, storage path) has changed and restart the loop.
    """

    state: CaptureState
    target: CaptureTarget
    task: asyncio.Task[None] | None = field(default=None)


@dataclass
class _ListenerRunner:
    """Internal pairing of a running event-listener task with its launch target.

    Parallel to :class:`_ProjectRunner` but for the event-listener registry: a
    listener consumes a camera's event stream and fires a one-shot capture on a
    matching, debounced event. The launch-time :class:`CaptureTarget` is held so
    a later reconcile tick can detect that a still-qualifying project's
    listener-affecting configuration (camera, triggers) changed and restart the
    listener with the fresh target.
    """

    target: CaptureTarget
    task: asyncio.Task[None] | None = field(default=None)


def _has_enabled_event_trigger(triggers: list[Any] | None) -> bool:
    """Return whether ``triggers`` contains at least one enabled trigger.

    A minimal, self-contained predicate (it does not depend on the event-trigger
    parser a later phase adds): a project qualifies for an event listener when
    its stored trigger list has any entry whose ``enabled`` flag is truthy.
    Malformed entries (anything that is not a mapping, or that omits ``enabled``)
    are treated as not-enabled rather than raising, so a bad stored value can
    never crash reconciliation -- at worst it yields no listener.
    """
    if not triggers:
        return False
    for trigger in triggers:
        if isinstance(trigger, dict) and bool(trigger.get("enabled", False)):
            return True
    return False


def _listener_affecting_fields(target: CaptureTarget) -> tuple[int, object]:
    """Return the subset of a target that, if changed, restarts the listener.

    Only the fields the listener reads -- the bound camera (which device it
    subscribes to) and the event-trigger configuration (which topics fire and
    their cooldowns) -- are included. A rename, schedule edit, or interval change
    must not disturb a live event subscription.
    """
    return (target.camera_id, target.event_triggers)


class Decision(NamedTuple):
    """The outcome of one planning step: what to do, and for how long to wait.

    :param action: ``"capture"`` to grab a frame now, ``"wait"`` to sleep only.
    :param sleep_seconds: how long the loop should sleep before re-planning.
    :param next_retry_at: when a pending backoff retry is due, else ``None``.
        Carried through so callers (and tests) can inspect the scheduled retry.
    """

    action: Literal["capture", "wait"]
    sleep_seconds: float
    next_retry_at: datetime | None


def _seconds_until(when: datetime | None, now: datetime, default: float) -> float:
    """Return non-negative seconds from ``now`` to ``when`` (``default`` if None)."""
    if when is None:
        return default
    return max(0.0, (when - now).total_seconds())


def _plan_next(
    now: datetime,
    *,
    is_open: bool,
    next_change: datetime | None,
    interval: float | None,
    max_idle_sleep: float,
    last_capture: datetime | None,
    next_retry_at: datetime | None,
    next_wake: datetime | None = None,
) -> Decision:
    """Decide the next capture action and sleep duration (pure, synchronous).

    The schedule gate is authoritative for *interval* capture: while the gate is
    **closed** the loop only ever waits, even if a backoff retry is due. The wait
    is capped at ``max_idle_sleep`` so a long closed window stays cancellable and
    config/clock drift is re-evaluated.

    While the gate is **open** the loop captures only when a frame is actually
    due -- when no frame has been taken yet, or at least ``interval`` has elapsed
    since the last capture, *and* any pending backoff retry instant has passed.
    Otherwise it waits the smaller of the remaining interval, the time to the
    next gate change, and ``max_idle_sleep``. Capping the open-wait this way
    makes ``max_idle_sleep`` a re-evaluation ceiling rather than a cadence
    override: an interval longer than the cap still yields one capture per
    interval, while an interval shorter than the cap (the common case, and every
    fixed-interval project) is unaffected.

    An ``interval`` of ``None`` means the project has no recurring interval
    (anchor-only): the loop never returns a ``"capture"`` action from here -- it
    only ever waits, so the runner can exist purely to service exact-time
    anchors. The interval cadence math is entirely skipped in that case.

    ``next_wake`` is an *additional* candidate wake instant (e.g. the next
    exact-time anchor fire time) that the loop folds into its sleep budget. It is
    only a wake hint -- it never produces a ``"capture"`` action here (anchors
    fire through their own evaluation, not the interval path) -- so the loop wakes
    no later than ``next_wake`` to re-evaluate. It applies even while the gate is
    closed, because anchors fire independent of the interval schedule gate.

    :param now: current aware-UTC instant.
    :param is_open: whether the capture gate is open at ``now``.
    :param next_change: aware-UTC instant the gate next flips, or ``None``.
    :param interval: desired seconds between captures while open, or ``None`` for
        an anchor-only project with no interval capture.
    :param max_idle_sleep: ceiling on any single sleep.
    :param last_capture: instant of the most recent successful capture, or None.
    :param next_retry_at: instant a pending backoff retry is due, or None.
    :param next_wake: an additional future instant to wake no later than (e.g.
        the next anchor fire time), or ``None``.
    """
    until_change = _seconds_until(next_change, now, default=max_idle_sleep)
    until_wake = _seconds_until(next_wake, now, default=max_idle_sleep)

    if not is_open or interval is None:
        # No interval capture this cycle (gate closed, or anchor-only project):
        # wait only, but never past the next gate change or the next anchor wake.
        sleep = min(until_change, until_wake, max_idle_sleep)
        return Decision(action="wait", sleep_seconds=sleep, next_retry_at=next_retry_at)

    retry_pending = next_retry_at is not None and next_retry_at > now
    if last_capture is None:
        due_in = 0.0
    else:
        elapsed = (now - last_capture).total_seconds()
        due_in = max(0.0, interval - elapsed)
    if retry_pending:
        due_in = max(due_in, (next_retry_at - now).total_seconds())  # type: ignore[operator]

    if due_in <= 0.0:
        return Decision(action="capture", sleep_seconds=0.0, next_retry_at=None)

    sleep = min(due_in, until_change, until_wake, max_idle_sleep)
    return Decision(action="wait", sleep_seconds=sleep, next_retry_at=next_retry_at)


def _backoff_delay(
    attempt: int,
    *,
    base: float,
    maximum: float,
    jitter_fraction: float,
    rng: random.Random,
) -> float:
    """Capped exponential backoff with symmetric jitter (pure, deterministic).

    ``delay = min(base * 2**(attempt-1), maximum)`` then scaled by a random
    factor in ``[1 - jitter_fraction, 1 + jitter_fraction]`` drawn from ``rng``.
    Passing a seeded ``rng`` makes the result reproducible, which is what lets
    the retry instant be asserted in a test.

    :param attempt: 1-based count of consecutive failures (1 = first retry).
    """
    exponent = max(0, attempt - 1)
    raw = base * (2.0**exponent)
    capped = min(raw, maximum)
    jitter = rng.uniform(-jitter_fraction, jitter_fraction)
    return max(0.0, capped * (1.0 + jitter))


def _loop_affecting_fields(
    target: CaptureTarget,
) -> tuple[
    int | None,
    int,
    object,
    str | None,
    str | None,
    str | None,
    float | None,
    float | None,
    float | None,
    object,
]:
    """Return the subset of a target that, if changed, requires a loop restart.

    Only the fields the running loop reads -- the capture interval, the bound
    camera, the schedule (gate), the storage path (capture destination + disk
    gate), the selected stream (which stream the adapter captures from), the
    PTZ position (which way the camera points each capture), and the exact-time
    anchors (which add per-day wake/fire instants) -- are included. A PTZ-position
    edit changes the captured image just as a stream change does, so it must
    relaunch the runner; otherwise the live loop keeps pointing the camera at the
    old position. An anchor edit changes the runner's wake times and fire set, so
    it must relaunch too. ``project_name`` and the camera geolocation are
    deliberately excluded: a rename or a geolocation tweak must *not* disturb a
    live capture, since the loop re-reads neither in a way a restart would fix.
    Event triggers are excluded here too -- they drive the *listener* registry,
    not this runner, and have their own reconcile comparison.
    """
    return (
        target.interval_seconds,
        target.camera_id,
        target.schedule,
        target.storage_path,
        target.stream_id,
        target.ptz_preset,
        target.ptz_pan,
        target.ptz_tilt,
        target.ptz_zoom,
        target.exact_time_anchors,
    )


class CaptureSupervisor:
    """Schedules and records per-project camera captures.

    Owns a shared :class:`httpx.AsyncClient` used to build HTTP-based adapters
    and a :class:`FrameWriter` for atomic persistence. Both are available before
    :meth:`start` so the manual-capture endpoint can reuse them.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        clock: Clock | None = None,
        disk_monitor: DiskSpaceMonitor | None = None,
        event_source_factory: EventSourceFactory | None = None,
    ) -> None:
        """Create a supervisor; performs no I/O and starts no tasks.

        :param settings: resolved application settings (capture + paths +
            storage).
        :param session_factory: factory for synchronous ORM sessions.
        :param clock: time source; defaults to real UTC time + ``asyncio.sleep``.
            Injectable so tests can drive timing deterministically.
        :param disk_monitor: passive free-space gate; defaults to one built from
            ``settings.storage``. The capture loop consults it synchronously each
            cycle (it re-probes only on its own throttle), so no extra background
            task is created.
        :param event_source_factory: builds the per-project event source an
            event listener consumes. ``None`` (the default) disables event
            listeners entirely: the listener registry and its reconcile passes
            still run, but a project with event triggers gets a listener that
            parks idle (it has no source to consume), so the lifecycle is wired
            and observable without any event mechanism connected. A later phase
            supplies a real factory; tests inject a fake one to drive the
            listener lifecycle.
        """
        self._settings = settings
        self._session_factory = session_factory
        self._clock: Clock = clock if clock is not None else _RealClock()
        frames_root = settings.paths.frames_root
        assert frames_root is not None  # populated by PathsSettings validator
        self._frames_root = Path(frames_root)
        self._http_client = httpx.AsyncClient()
        # Resolved once here (at startup wiring): the RTSP adapter grabs frames
        # with the same ffmpeg the encoder uses -- bundled when frozen, an
        # explicit knob when set, else ``ffmpeg`` on ``PATH``.
        self._ffmpeg_binary = resolve_ffmpeg_binary(settings)
        self._writer = FrameWriter(session_factory, self._frames_root)
        self._disk_monitor = (
            disk_monitor if disk_monitor is not None else _build_disk_monitor(settings)
        )
        self._runners: dict[int, _ProjectRunner] = {}
        # Event-listener registry, parallel to ``_runners`` and keyed the same way
        # (by project id). A project qualifies for a listener when it is active,
        # bound to a camera with a protocol, and has at least one enabled event
        # trigger -- a predicate separate from the interval/anchor qualifying set.
        self._listeners: dict[int, _ListenerRunner] = {}
        self._event_source_factory = event_source_factory
        self._started = False
        self._stopped = False
        # Background reconcile loop plumbing. The task is tracked separately from
        # ``_runners`` so per-project introspection (state_for_project, the test
        # suite's _runners assertions) is unaffected. ``_reconcile_wakeup`` lets a
        # create/reactivate notification wake the loop early instead of waiting
        # out the periodic interval.
        self._reconcile_task: asyncio.Task[None] | None = None
        self._reconcile_wakeup = asyncio.Event()

    @property
    def http_client(self) -> httpx.AsyncClient:
        """The shared async HTTP client adapters borrow."""
        return self._http_client

    @property
    def ffmpeg_binary(self) -> str:
        """The ffmpeg executable resolved for this process.

        Exposed so request handlers that build an adapter on demand (camera
        validate/capture) pass the same binary the background capture loop uses.
        """
        return self._ffmpeg_binary

    def set_event_source_factory(self, factory: EventSourceFactory | None) -> None:
        """Install the factory the per-project event listeners consume.

        Called once at startup wiring, *after* the supervisor exists, because the
        real factory needs the supervisor's shared HTTP client (built in
        ``__init__``) and its camera/credential loaders. Tests inject a fake
        factory the same way -- or at construction via the ctor argument -- to
        drive the listener lifecycle without a live camera.
        """
        self._event_source_factory = factory

    @property
    def frame_writer(self) -> FrameWriter:
        """The shared atomic frame writer."""
        return self._writer

    async def start(self) -> None:
        """Launch one capture task per qualifying project.

        Idempotent: calling it again after a start is a no-op. On an empty
        database or with no qualifying projects, it starts zero tasks and
        returns cleanly.
        """
        if self._started:
            return
        self._started = True
        try:
            targets = await asyncio.to_thread(self._load_targets)
        except Exception:
            # Loading capture targets must never crash service startup: the API,
            # health endpoint, and web UI need to come up so the operator can
            # diagnose (e.g. an unmigrated or temporarily unavailable database).
            # Start with no tasks rather than taking the whole process down.
            logger.exception(
                "failed to load capture targets; starting with no capture tasks"
            )
            targets = []
        for target in targets:
            # Restart-survival: record any downtime gap before resuming. Best
            # effort -- a failure here must never prevent the loop from starting.
            try:
                await asyncio.to_thread(self._log_resume_gap, target)
            except Exception:
                logger.exception(
                    "failed to log resume gap project=%s", target.project_id
                )
            self._launch(target)
        # Launch event listeners for the qualifying set (active + protocol + an
        # enabled event trigger). Read by a separate query: a project may have a
        # listener without a runner (event triggers but no interval/anchors) and
        # vice versa, so the two qualifying sets are independent.
        try:
            listener_targets = await asyncio.to_thread(self._load_listener_targets)
        except Exception:
            logger.exception(
                "failed to load event-listener targets; starting with no listeners"
            )
            listener_targets = []
        for listener_target in listener_targets:
            self._launch_listener(listener_target)
        # Converge to the DB at runtime: pick up projects created/reactivated and
        # drop those archived/deleted, without a restart. Started after the
        # initial launch so its first tick sees the resumed runners and treats
        # them as already-running (no double-launch).
        self._reconcile_task = asyncio.create_task(
            self._reconcile_loop(), name="capture-reconcile"
        )
        logger.info("capture supervisor started with %d task(s)", len(self._runners))

    def notify_reconcile(self) -> None:
        """Wake the reconcile loop so it re-reads qualifying projects now.

        Thread-safe to call from a synchronous request handler (FastAPI runs sync
        routes in a threadpool): :meth:`asyncio.Event.set` only flips a flag and
        schedules the waiter's wakeup. Called after a project is created or
        reactivated so capture starts promptly rather than waiting out the
        periodic reconcile interval. A no-op before :meth:`start` (the loop is not
        running yet); the first reconcile tick after start picks up the change.
        """
        self._reconcile_wakeup.set()

    def _launch(self, target: CaptureTarget) -> None:
        """Create and register the loop task for one capture target."""
        state = CaptureState(
            project_id=target.project_id,
            camera_id=target.camera_id,
            state="running",
            started_at=self._clock.now(),
        )
        runner = _ProjectRunner(state=state, target=target)
        runner.task = asyncio.create_task(
            self._run_project(target, state),
            name=f"capture-project-{target.project_id}",
        )
        self._runners[target.project_id] = runner

    def _launch_listener(self, target: CaptureTarget) -> None:
        """Create and register the event-listener task for one project."""
        runner = _ListenerRunner(target=target)
        runner.task = asyncio.create_task(
            self._run_listener(target),
            name=f"capture-listener-{target.project_id}",
        )
        self._listeners[target.project_id] = runner

    async def _reconcile_loop(self) -> None:
        """Periodically converge the running tasks to the qualifying project set.

        Each tick re-reads the qualifying targets and reconciles membership, then
        waits out the configured interval or until :meth:`notify_reconcile` wakes
        it early. A tick failure (e.g. a transient database error in
        ``_load_targets``) is logged and the loop continues -- a bad pass must
        never kill reconciliation. ``CancelledError`` propagates so :meth:`stop`
        can tear the loop down cleanly.

        ``asyncio.timeout`` (not ``asyncio.wait_for``) guards the wait: if the
        wakeup is set in the same loop iteration the task is cancelled by
        ``stop``, ``wait_for`` can return the wakeup result and swallow the
        ``CancelledError``, wedging the task forever; ``asyncio.timeout``
        propagates the cancellation correctly and raises ``TimeoutError`` on
        expiry, which the surrounding suppression covers.
        """
        interval = max(1.0, self._settings.capture.reconcile_interval_seconds)
        while True:
            self._reconcile_wakeup.clear()
            try:
                await self._reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad pass must not kill the loop
                logger.exception("capture reconcile tick failed; continuing")
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(interval):
                    await self._reconcile_wakeup.wait()

    async def _reconcile_once(self) -> None:
        """Converge running tasks to the qualifying set, restarting on edits.

        Reconciliation is keyed by ``project_id`` and runs three passes:

        * **launch** -- a project in the qualifying set but not in ``_runners`` is
          started (the guard is exactly "project_id not already running", so there
          is never a double-launch);
        * **stop** -- a running project absent from the qualifying set is
          cancelled, awaited, and removed;
        * **restart-on-change** -- a project present in *both* whose
          loop-affecting configuration (interval, camera, schedule, storage path)
          differs from its launch-time target is stopped and relaunched with the
          fresh target, so an edit to an active project takes runtime effect. A
          rename or geolocation change is *not* loop-affecting and leaves the live
          task untouched.
        """
        targets = await asyncio.to_thread(self._load_targets)
        qualifying = {target.project_id: target for target in targets}

        for project_id, target in qualifying.items():
            if project_id not in self._runners:
                # Mirror start()'s restart-survival gap logging; for a brand-new
                # project this is a no-op (no prior frames). Best effort -- a
                # failure here must never prevent the launch.
                try:
                    await asyncio.to_thread(self._log_resume_gap, target)
                except Exception:
                    logger.exception("failed to log resume gap project=%s", project_id)
                logger.info("reconcile: launching capture project=%s", project_id)
                self._launch(target)

        stale = [pid for pid in self._runners if pid not in qualifying]
        for project_id in stale:
            logger.info("reconcile: stopping capture project=%s", project_id)
            await self._stop_runner(project_id)

        # Restart any still-running project whose loop-affecting config changed.
        # Done after launch/stop so the comparison only sees projects that are in
        # both sets; the stop+relaunch reuses the same cancel-and-await path as a
        # membership change, so there is no double-launch and no race with stop().
        changed = [
            target
            for project_id, target in qualifying.items()
            if project_id in self._runners
            and _loop_affecting_fields(self._runners[project_id].target)
            != _loop_affecting_fields(target)
        ]
        for target in changed:
            logger.info(
                "reconcile: restarting capture project=%s (config changed)",
                target.project_id,
            )
            await self._stop_runner(target.project_id)
            self._launch(target)

        await self._reconcile_listeners()

    async def _reconcile_listeners(self) -> None:
        """Converge the event-listener registry to its own qualifying set.

        Symmetric to the runner reconcile but keyed on the listener qualifying
        set (active + protocol + an enabled event trigger) and the
        listener-affecting fields (camera, triggers): launch a newly-qualifying
        project's listener, stop one no longer qualifying (deactivated, deleted,
        all triggers removed/disabled, campaign ended), and restart one whose
        camera or trigger configuration changed. Because the qualifying set is
        read independently of the interval/anchor runners, a listener may exist
        for a project that has no capture runner and vice versa.
        """
        targets = await asyncio.to_thread(self._load_listener_targets)
        qualifying = {target.project_id: target for target in targets}

        for project_id, target in qualifying.items():
            if project_id not in self._listeners:
                logger.info(
                    "reconcile: launching event listener project=%s", project_id
                )
                self._launch_listener(target)

        stale = [pid for pid in self._listeners if pid not in qualifying]
        for project_id in stale:
            logger.info("reconcile: stopping event listener project=%s", project_id)
            await self._stop_listener(project_id)

        changed = [
            target
            for project_id, target in qualifying.items()
            if project_id in self._listeners
            and _listener_affecting_fields(self._listeners[project_id].target)
            != _listener_affecting_fields(target)
        ]
        for target in changed:
            logger.info(
                "reconcile: restarting event listener project=%s (config changed)",
                target.project_id,
            )
            await self._stop_listener(target.project_id)
            self._launch_listener(target)

    async def _stop_runner(self, project_id: int) -> None:
        """Cancel and await one project's task, then drop it from the registry.

        Awaits the cancelled task (suppressing ``CancelledError``) so nothing
        leaks onto the loop before the runner is removed. Safe if the task is
        already finished or absent.
        """
        runner = self._runners.pop(project_id, None)
        if runner is None:
            return
        runner.state.state = "stopped"
        task = runner.task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _stop_listener(self, project_id: int) -> None:
        """Cancel and await one project's listener, then drop it from the registry.

        Mirrors :meth:`_stop_runner`: awaits the cancelled task (suppressing
        ``CancelledError``) so nothing leaks onto the loop before the listener is
        removed. The listener's own teardown (unsubscribe, etc.) runs as its
        cancellation unwinds. Safe if the task is already finished or absent.
        """
        runner = self._listeners.pop(project_id, None)
        if runner is None:
            return
        task = runner.task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def stop(self) -> None:
        """Cancel every capture task and release the shared HTTP client.

        Cancels all tasks, awaits them with ``return_exceptions=True`` so a
        cancelled or failed task never leaks onto a closing loop, then closes the
        HTTP client. Idempotent and safe to call when nothing was started.
        """
        if self._stopped:
            return
        self._stopped = True

        # Tear down the reconcile loop first so it cannot launch a new runner or
        # listener while we are cancelling the existing ones.
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconcile_task
            self._reconcile_task = None

        # Tear down event listeners *before* the capture runners: a listener
        # could otherwise fire a one-shot capture mid-shutdown. Both are torn down
        # before the shared HTTP client is closed, since both borrow it.
        listener_tasks = [
            r.task for r in self._listeners.values() if r.task is not None
        ]
        for task in listener_tasks:
            task.cancel()
        if listener_tasks:
            await asyncio.gather(*listener_tasks, return_exceptions=True)

        tasks = [r.task for r in self._runners.values() if r.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for runner in self._runners.values():
            runner.state.state = "stopped"

        # Closed last so neither a listener nor a runner is mid-request when the
        # transport disappears.
        await self._http_client.aclose()
        logger.info(
            "capture supervisor stopped (%d capture task(s), %d listener(s))",
            len(tasks),
            len(listener_tasks),
        )

    async def _run_project(self, target: CaptureTarget, state: CaptureState) -> None:
        """Capture for one project on its schedule until cancelled.

        Each cycle: evaluate the schedule gate, plan the next action, then either
        capture (and on a transient failure schedule a backoff retry) or wait.
        Exceptions other than cancellation are contained: they are logged,
        recorded on ``state``, and the loop continues so a single project's
        failures never affect the others.
        """
        interval = (
            float(max(1, target.interval_seconds))
            if target.interval_seconds is not None
            else None
        )
        max_idle = max(1.0, self._settings.capture.max_idle_sleep_seconds)
        # Per-project RNG so peers do not retry in lockstep; seeded from the
        # project id so the jitter stream is reproducible for a given project.
        rng = random.Random(target.project_id)

        schedule = self._parse_schedule_safely(target)
        volume_path = self._volume_path(target)

        while True:
            now = self._clock.now()
            try:
                schedule_open, next_change = next_transition(
                    schedule,
                    now,
                    latitude=target.latitude,
                    longitude=target.longitude,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - schedule eval must not kill loop
                # An evaluator failure should not silently stop capture; fall
                # back to "always open" for this cycle and try again.
                logger.warning(
                    "schedule evaluation failed project=%s: %s", target.project_id, exc
                )
                schedule_open, next_change = True, None

            # The disk gate composes with the schedule gate. It is only consulted
            # while the window is open: a closed window is already a pause, and
            # skipping the probe avoids needless I/O and keeps the truth table
            # clean (window-closed is reported as a window pause regardless of
            # disk). The monitor re-probes on its own throttle, so calling it
            # every cycle adds no second task and no per-cycle disk read.
            # The start-date gate composes with the schedule window: before the
            # campaign's start instant the project captures nothing, exactly like
            # a closed schedule window. Folded into ``schedule_open`` (after the
            # evaluator's own fallback) so a not-yet-started project never even
            # probes disk, and ``next_change`` is pulled in to the start instant so
            # the loop wakes when the campaign opens.
            if target.start_date is not None and now < target.start_date:
                schedule_open = False
                next_change = (
                    target.start_date
                    if next_change is None
                    else min(next_change, target.start_date)
                )

            disk_ok = True
            if schedule_open:
                disk_ok = await self._evaluate_disk_gate(
                    target, state, volume_path, now
                )

            is_open = schedule_open and disk_ok

            # Exact-time anchors fire independent of the interval schedule gate
            # (a daily anchor under a narrow capture window would otherwise never
            # fire). Evaluate them every cycle against the durable fire-log and
            # fold the next anchor instant into the sleep budget so the loop wakes
            # to fire it. The campaign start/end and disk gates still bound this
            # via the same guards the interval path uses. The evaluation itself is
            # a hook a later phase fills; today it is a no-op returning no wake.
            # Guarded so a project with no anchors -- the only case until that
            # phase lands -- keeps the loop body byte-identical to the plain
            # interval runner (no extra await, no timing perturbation).
            next_anchor_wake = None
            if target.exact_time_anchors:
                next_anchor_wake = await self._evaluate_exact_time_anchors(target, now)

            # No-overshoot guard: never *start* a capture once the project is at or
            # over its frame cap (e.g. frames added by another path). The
            # authoritative stop is the post-write check below; this just avoids a
            # wasted attempt and an off-by-one if the count moved underneath us.
            if is_open and self._frame_cap_reached(target):
                await asyncio.to_thread(
                    self._end_campaign,
                    target.project_id,
                    target.project_name,
                    _CAMPAIGN_END_FRAME_COUNT,
                )
                state.state = "stopped"
                return

            decision = _plan_next(
                now,
                is_open=is_open,
                next_change=next_change,
                interval=interval,
                max_idle_sleep=max_idle,
                last_capture=state.last_capture_at,
                next_retry_at=state.next_retry_at,
                next_wake=next_anchor_wake,
            )

            if decision.action == "capture":
                frame_count = await self._attempt_capture(target, state, rng)
                # End the campaign at exactly the cap: the write already happened,
                # so stopping here (rather than before the next attempt) means no
                # overshoot. ``_end_campaign`` is idempotent, so racing the
                # reconcile enforcer is safe.
                if (
                    frame_count is not None
                    and target.max_frame_count is not None
                    and frame_count >= target.max_frame_count
                ):
                    await asyncio.to_thread(
                        self._end_campaign,
                        target.project_id,
                        target.project_name,
                        _CAMPAIGN_END_FRAME_COUNT,
                    )
                    state.state = "stopped"
                    return
            else:
                self._mark_waiting(state, schedule_open=schedule_open, disk_ok=disk_ok)

            await self._clock.sleep(decision.sleep_seconds)

    async def _run_listener(self, target: CaptureTarget) -> None:
        """Consume a project's camera events and fire one-shot captures, supervised.

        The supervised side of event-triggered capture, parallel to
        :meth:`_run_project`. The full body a later phase fills is: obtain the
        project's event source from the injected factory; consume its events;
        match each against the configured triggers; debounce (per-trigger
        cooldown); and fire :func:`~timelapse_manager.capture.one_shot.capture_one_now`
        on a match. A dropped subscription or transport failure backs off (the
        same capped exponential + jitter the capture runner uses) and
        re-subscribes; ``CancelledError`` propagates for clean teardown.

        The lifecycle (registry, reconcile, teardown-first shutdown) is wired now;
        the event *mechanism* is pluggable via ``event_source_factory``. When no
        factory is configured -- or it returns no source for this project -- the
        listener parks cancellably forever rather than returning: a body that
        returned immediately inside a restart loop would be an infinite hot loop.
        Parking keeps the task alive and observable in the registry until
        reconcile or :meth:`stop` cancels it.
        """
        rng = random.Random(target.project_id)
        attempt = 0
        while True:
            source = (
                self._event_source_factory(target)
                if self._event_source_factory is not None
                else None
            )
            if source is None:
                # No event mechanism connected for this project: park until
                # cancelled (the registry/reconcile/teardown lifecycle is what is
                # under test here, not a live subscription).
                await asyncio.Event().wait()
                return
            try:
                await self._consume_event_source(target, source)
                # A source that ends cleanly (no error) resets the backoff before
                # the next subscription attempt.
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad camera must not stall others
                attempt += 1
                cfg = self._settings.capture
                delay = _backoff_delay(
                    attempt,
                    base=cfg.backoff_base_seconds,
                    maximum=cfg.backoff_max_seconds,
                    jitter_fraction=cfg.backoff_jitter_fraction,
                    rng=rng,
                )
                logger.warning(
                    "event listener failed project=%s; retrying in %.1fs: %s",
                    target.project_id,
                    delay,
                    exc,
                )
                await self._clock.sleep(delay)

    async def _consume_event_source(
        self, target: CaptureTarget, source: AsyncIterator[Any]
    ) -> None:
        """Consume one subscription's events, matching and firing captures.

        For each event delivered by ``source`` we find the first matching enabled
        trigger (:func:`event_triggers_mod.match_trigger`, which already drops a
        falling edge), debounce it against a per-topic cooldown, and -- when the
        cooldown has elapsed -- fire a one-shot capture via
        :func:`~timelapse_manager.capture.one_shot.capture_one_now`.

        Debounce source of truth: the per-trigger ``cooldown_seconds``, measured
        on a monotonic clock so a wall-clock adjustment cannot collapse or stretch
        it. The ``last_fired`` map is local to this subscription (rebuilt on every
        re-subscribe) and keyed by canonical topic, matching the per-topic rule.
        The cooldown is *attempt*-based: ``last_fired`` is stamped the moment a
        capture is attempted, before the (possibly slow or failing) capture runs,
        so a failing camera or a low-disk skip during an event storm cannot defeat
        the debounce by letting every event retry immediately.

        ``capture_one_now`` writes its own audit event and enforces the disk gate
        (a low-disk skip returns ``None``); a :class:`CaptureError` from one
        capture is logged and swallowed so a single bad capture does not tear down
        the listener -- the source keeps delivering. ``CancelledError`` is not a
        ``CaptureError`` and so propagates for clean teardown.

        A malformed stored trigger list is logged once and the source is then
        drained without matching (rather than returned-from, which would re-enter
        the factory in a tight loop); reconcile restarts the listener when the
        stored triggers change, so a corrected list is picked up.
        """
        try:
            triggers = event_triggers_mod.parse_triggers(target.event_triggers)
        except ValueError as exc:
            logger.warning(
                "invalid event triggers project=%s; draining source without "
                "matching: %s",
                target.project_id,
                exc,
            )
            triggers = []

        # Per-topic last-fired instants on a monotonic clock, local to this
        # subscription. The supervisor's injectable Clock exposes only wall-clock
        # ``now``/``sleep``; the monotonic source is used solely for the debounce
        # delta so a clock adjustment cannot affect the cooldown.
        last_fired: dict[str, float] = {}

        async for event in source:
            trigger = event_triggers_mod.match_trigger(event, triggers)
            if trigger is None:
                continue

            topic = event.topic_id
            now_mono = time.monotonic()
            previous = last_fired.get(topic)
            if (
                previous is not None
                and (now_mono - previous) < trigger.cooldown_seconds
            ):
                # Still within this topic's cooldown: suppress the capture.
                continue

            # Stamp before the attempt so the cooldown governs attempt cadence
            # even when the capture is slow, fails, or is skipped for low disk.
            last_fired[topic] = now_mono

            try:
                await capture_one_now(
                    self,
                    target,
                    reason=f"event:{event.topic_id}",
                    trigger={
                        "trigger_id": trigger.id,
                        "topic_id": event.topic_id,
                        "category": event.category,
                        "source": event.source,
                    },
                    dedup_key=(
                        f"{target.project_id}:{event.topic_id}:"
                        f"{self._event_dedup_bucket(trigger.cooldown_seconds)}"
                    ),
                )
            except CaptureError as exc:
                # One bad capture must not kill the listener: the source keeps
                # delivering. capture_one_now already recorded the attempt's audit
                # event; here we only log.
                logger.warning(
                    "event-triggered capture failed project=%s topic=%s: %s",
                    target.project_id,
                    event.topic_id,
                    exc,
                )

    def _event_dedup_bucket(self, cooldown_seconds: int) -> int:
        """Return the wall-clock bucket for an event capture's ``dedup_key``.

        The dedup key is opaque provenance (``capture_one_now`` does not enforce
        idempotency on it); the bucket just groups captures that fall in the same
        cooldown window for human-readable audit trails. Built from wall-clock
        time -- never a monotonic divisor -- and floored to the cooldown so a
        zero cooldown does not divide by zero (it degrades to a per-second
        timestamp).
        """
        epoch = int(self._clock.now().timestamp())
        if cooldown_seconds <= 0:
            return epoch
        return epoch - (epoch % cooldown_seconds)

    async def _evaluate_exact_time_anchors(
        self, target: CaptureTarget, now: datetime
    ) -> datetime | None:
        """Fire any due exact-time anchors and return the next anchor wake instant.

        This is the integration seam for exact-time capture. The full rule a
        later phase fills here is: for each enabled anchor compute today's fire
        instant; if it is due and not already recorded in the durable fire-log,
        insert the decision row (the unique constraint arbitrates double-fire
        races) and -- within the grace window -- capture via
        :func:`~timelapse_manager.capture.one_shot.capture_one_now` (which ignores
        the schedule gate but honours the universal disk gate), otherwise record a
        skip. The returned instant is the earliest future anchor fire time, folded
        into the loop's sleep budget so it wakes to fire it.

        A project with no anchors returns ``None`` immediately, so the runner
        behaves exactly as the plain interval loop.
        """
        if not target.exact_time_anchors:
            return None

        try:
            anchors = anchors_mod.parse_anchors(target.exact_time_anchors)
        except ValueError as exc:
            # A malformed stored anchor list must not stop the project's interval
            # capture; log once per cycle and schedule no anchor wake.
            logger.warning(
                "invalid exact-time anchors project=%s; ignoring: %s",
                target.project_id,
                exc,
            )
            return None

        schedule = self._parse_schedule_safely(target)
        tz = schedule.tzinfo()
        lat, lon = target.latitude, target.longitude
        # Solar-noon anchors are governed by the camera's physical location, not
        # the operator-chosen schedule timezone, so the fire instant and the
        # once-per-day key are computed in the coordinate-derived zone. Clock
        # anchors continue to use the schedule timezone.
        solar_tz = geo.resolve_zoneinfo(lat, lon)

        decisions = anchors_mod.due_anchors(
            anchors, now, tz, lat, lon, solar_tz=solar_tz
        )
        for decision in decisions:
            await self._fire_anchor_decision(target, decision)

        return anchors_mod.next_anchor_wake(
            anchors, now, tz, lat, lon, solar_tz=solar_tz
        )

    async def _fire_anchor_decision(
        self, target: CaptureTarget, decision: anchors_mod.AnchorDecision
    ) -> None:
        """Act on one due-anchor decision: claim the fire-log row, then capture.

        The durable fire-log row is the once-per-day idempotency guard. We claim
        it *first* (a committed insert); the unique constraint on
        ``(project, anchor, local_date)`` arbitrates races -- a duplicate insert
        means another worker or a prior run already handled this anchor today, so
        we skip silently with no capture and no event. Only after a successful
        claim do we capture (within grace, with geolocation) and update the row's
        outcome. Claiming before capturing means a crash mid-capture leaves a
        decision row in place, so the anchor is not re-fired (at-most-once).
        """
        anchor = decision.anchor

        # Decide the claim row's initial status from the pure decision.
        if not decision.has_geo:
            status = "skipped_no_geo"
            detail: str | None = "camera has no geolocation for solar-noon anchor"
        elif decision.within_grace:
            # Provisional: updated to captured/failed after the attempt.
            status = "failed"
            detail = None
        else:
            status = "skipped_missed"
            detail = "anchor fire instant missed by more than the grace window"

        claimed = await asyncio.to_thread(
            self._claim_exact_time_fire,
            target.project_id,
            anchor.id,
            decision.local_date,
            status,
            detail,
        )
        if claimed is None:
            # Already recorded for this anchor + local day (unique-constraint
            # race or a prior run). Idempotent: do nothing further.
            return

        if status != "failed":
            # A skip decision (no geo / missed): the committed claim row and its
            # event are the whole outcome.
            level = "warning"
            message = (
                f"exact-time anchor skipped for project {target.project_name!r} "
                f"({anchor.kind} {decision.local_date}): {detail}"
            )
            await asyncio.to_thread(
                self._write_event,
                scope_id=target.project_id,
                level=level,
                message=message,
                metadata={
                    "reason": f"anchor:{anchor.kind}",
                    "anchor_id": anchor.id,
                    "local_date": decision.local_date,
                    "skipped": status,
                },
            )
            return

        # Within grace + has geolocation: capture now. capture_one_now honours the
        # universal disk gate (a low-disk skip returns None) and writes its own
        # audit event; we reconcile the fire-log row to the outcome.
        reason = f"anchor:{anchor.kind}"
        trigger: dict[str, object] = {
            "anchor_id": anchor.id,
            "local_date": decision.local_date,
        }
        try:
            written = await capture_one_now(
                self,
                target,
                reason=reason,
                trigger=trigger,
                dedup_key=f"{target.project_id}:{anchor.id}:{decision.local_date}",
            )
        except CaptureError as exc:
            await asyncio.to_thread(
                self._record_exact_time_outcome,
                target.project_id,
                anchor.id,
                decision.local_date,
                status="failed",
                frame_id=None,
                detail=str(exc)[:200],
            )
            logger.warning(
                "exact-time anchor capture failed project=%s anchor=%s: %s",
                target.project_id,
                anchor.id,
                exc,
            )
            return

        if written is None:
            # Low-disk skip is a skip, not a fire: release the provisional claim
            # so the anchor can retry on a later cycle while still within grace
            # (if disk frees up). The disk gate -- not a burned fire-log row -- is
            # what keeps it from capturing onto a full disk. capture_one_now has
            # already logged the low-disk skip event.
            await asyncio.to_thread(
                self._release_exact_time_fire,
                target.project_id,
                anchor.id,
                decision.local_date,
            )
            return

        await asyncio.to_thread(
            self._record_exact_time_outcome,
            target.project_id,
            anchor.id,
            decision.local_date,
            status="captured",
            frame_id=written.frame_id,
            detail=None,
        )

    def _claim_exact_time_fire(
        self,
        project_id: int,
        anchor_id: str,
        local_date: str,
        status: str,
        detail: str | None,
    ) -> int | None:
        """Insert the decision row, returning its id, or ``None`` if it exists.

        The unique constraint on ``(project_id, anchor_id, local_date)`` is the
        double-fire guard: a duplicate insert raises ``IntegrityError``, which we
        translate to ``None`` (already handled). Synchronous; call via a thread.
        """
        from sqlalchemy.exc import IntegrityError

        try:
            with session_scope(self._session_factory) as session:
                row = ExactTimeFire(
                    project_id=project_id,
                    anchor_id=anchor_id,
                    local_date=local_date,
                    status=status,
                    fired_at=datetime.now(UTC).replace(tzinfo=None),
                    detail=detail,
                )
                session.add(row)
                session.flush()
                return row.id
        except IntegrityError:
            return None

    def _record_exact_time_outcome(
        self,
        project_id: int,
        anchor_id: str,
        local_date: str,
        *,
        status: str,
        frame_id: int | None,
        detail: str | None,
    ) -> None:
        """Update an already-claimed fire-log row with the capture outcome.

        Synchronous; call via a thread. The row was claimed (committed) earlier;
        here we reconcile it to ``captured`` or ``failed`` once the attempt has
        resolved. (A low-disk skip is handled separately by releasing the claim.)
        """
        with session_scope(self._session_factory) as session:
            row = (
                session.query(ExactTimeFire)
                .filter_by(
                    project_id=project_id,
                    anchor_id=anchor_id,
                    local_date=local_date,
                )
                .one_or_none()
            )
            if row is None:
                return
            row.status = status
            row.frame_id = frame_id
            row.detail = detail

    def _release_exact_time_fire(
        self, project_id: int, anchor_id: str, local_date: str
    ) -> None:
        """Delete a provisional claim row so the anchor can retry within grace.

        Used when a capture was *skipped* (not failed) for low disk: removing the
        committed claim lets a later cycle re-claim and capture if disk frees up
        and the grace window has not closed. Synchronous; call via a thread.
        """
        with session_scope(self._session_factory) as session:
            row = (
                session.query(ExactTimeFire)
                .filter_by(
                    project_id=project_id,
                    anchor_id=anchor_id,
                    local_date=local_date,
                )
                .one_or_none()
            )
            if row is not None:
                session.delete(row)

    def _frame_cap_reached(self, target: CaptureTarget) -> bool:
        """Return whether the project is already at or over its frame cap.

        A cheap single-column read used as the pre-capture no-overshoot guard,
        consulted only on an open loop cycle and only when a frame cap is set
        (an uncapped project short-circuits without touching the database).
        Returns ``False`` when no cap is configured.
        """
        if target.max_frame_count is None:
            return False
        with session_scope(self._session_factory) as session:
            project = session.get(Project, target.project_id)
            if project is None:
                return False
            return project.frame_count >= target.max_frame_count

    def _volume_path(self, target: CaptureTarget) -> Path:
        """Return the directory whose volume's free space gates this project.

        Mirrors the writer's destination: the project's ``storage_path`` override
        when set, else the per-project sub-directory under the frames root. The
        monitor walks up to the nearest existing parent, so this need not exist
        yet (it will not before the first capture).
        """
        ref = paths.ProjectRef(id=target.project_id, storage_path=target.storage_path)
        return paths.frame_dir_under_root(self._frames_root, ref)

    async def _evaluate_disk_gate(
        self,
        target: CaptureTarget,
        state: CaptureState,
        volume_path: Path,
        now: datetime,
    ) -> bool:
        """Return whether free space permits capture, edge-logging transitions.

        The synchronous probe (throttled inside the monitor) is moved off the
        event loop. Pause/resume Events are emitted only on the *transition* into
        or out of low-disk for *this project* -- the edge is taken against the
        project's own ``pause_reason``, not the monitor's shared per-volume latch,
        so two projects sharing a volume each log their own pause/resume rather
        than one silently inheriting the other's latch. A sustained low-disk
        condition therefore logs once per project, not every cycle.

        Nothing is ever deleted to free space: a low disk only pauses capture.
        """
        was_low = state.pause_reason == _PAUSE_LOW_DISK
        allowed = await asyncio.to_thread(
            self._disk_monitor.is_capture_allowed, volume_path, now=now
        )
        now_paused = not allowed
        if now_paused and not was_low:
            logger.warning(
                "capture paused for low disk space project=%s path=%s",
                target.project_id,
                volume_path,
            )
            await asyncio.to_thread(
                self._write_event,
                scope_id=target.project_id,
                level="warning",
                message=(
                    f"capture paused for project {target.project_name!r}: free "
                    f"disk space below the low watermark on {volume_path}"
                ),
                metadata={"reason": _PAUSE_LOW_DISK, "path": str(volume_path)},
            )
        elif was_low and not now_paused:
            logger.info(
                "capture resumed after disk recovery project=%s path=%s",
                target.project_id,
                volume_path,
            )
            await asyncio.to_thread(
                self._write_event,
                scope_id=target.project_id,
                level="info",
                message=(
                    f"capture resumed for project {target.project_name!r}: free "
                    f"disk space recovered above the resume watermark on "
                    f"{volume_path}"
                ),
                metadata={"reason": "disk_recovered", "path": str(volume_path)},
            )
        return allowed

    async def _attempt_capture(
        self, target: CaptureTarget, state: CaptureState, rng: random.Random
    ) -> int | None:
        """Run one capture, handling success, transient failure, and backoff.

        Returns the project's active frame count *after* a successful write (so
        the caller can enforce a no-overshoot frame cap), or ``None`` when the
        attempt failed and was recorded as a contained transient failure.
        """
        # Reaching a capture means the gate is open (schedule + disk both clear),
        # so any prior pause reason is stale even if the attempt then fails into
        # an error/backoff state.
        state.pause_reason = None
        try:
            result = await self._capture_once(target, state)
        except asyncio.CancelledError:
            raise
        except CaptureError as exc:
            self._record_transient_failure(state, exc, rng)
            logger.warning(
                "capture failed project=%s reason=%s: %s",
                target.project_id,
                exc.reason.value,
                exc,
            )
            await self._maybe_emit_camera_offline(target, state)
            return None
        except Exception as exc:  # noqa: BLE001 - deliberate per-task isolation
            self._record_transient_failure(state, exc, rng)
            logger.warning("capture failed project=%s: %s", target.project_id, exc)
            await self._maybe_emit_camera_offline(target, state)
            return None
        # Success: clear any standing offline alert with a recovery event. Done
        # here (the async caller) rather than in the sync ``_record_success`` so
        # the event write stays off the event loop via ``to_thread``.
        await self._maybe_emit_camera_recovered(target, state)
        return result

    async def _capture_once(self, target: CaptureTarget, state: CaptureState) -> int:
        """Perform one bounded capture for a project and persist it.

        The capture is wrapped in :func:`asyncio.wait_for`; a timeout is logged
        as a gap and raised as a :class:`TimeoutCaptureError` so the loop's
        transient-failure handling (backoff) applies. On success the frame is
        persisted and frozen-frame detection runs. Returns the project's active
        frame count after the write so the caller can enforce a frame cap.
        """
        config = await asyncio.to_thread(self._load_camera, target.camera_id)
        if config is None:
            raise RuntimeError(f"camera {target.camera_id} no longer exists")
        default_credentials = await asyncio.to_thread(self._load_default_credentials)
        adapter = build_adapter(
            config,
            self._http_client,
            ffmpeg_binary=self._ffmpeg_binary,
            default_credentials=default_credentials,
            stream_id=target.stream_id,
        )
        try:
            await self._apply_ptz(target, adapter)
            captured = await self._capture_with_timeout(adapter)
        finally:
            await adapter.close()
        if captured is None:
            logger.warning(
                "capture timed out project=%s; gap recorded", target.project_id
            )
            raise TimeoutCaptureError("capture timed out")
        written = await asyncio.to_thread(
            self._writer.write,
            target.project_id,
            captured,
            stream_id=target.stream_id,
        )
        self._record_success(state)
        await self._check_frozen_frame(target, state, captured)
        return written.project_frame_count

    async def _apply_ptz(self, target: CaptureTarget, adapter: CameraAdapter) -> None:
        """Position the camera for this project before capturing, fail-closed.

        Selects a position by the same precedence the UI uses: a raw
        ``pan``/``tilt``/``zoom`` (any one present) overrides a named preset, and
        when neither is configured this is a no-op (no positioning request is
        sent). The adapter owns the physical settle delay after a successful move.

        A positioning failure is treated exactly like a capture failure: the
        adapter raises a :class:`PTZError` (a :class:`CaptureError`), which this
        method re-raises so it propagates out of :meth:`_capture_once` and into
        the loop's transient-failure/backoff handling -- no frame is ever captured
        from a position that could not be confirmed. The re-raised error keeps the
        original failure ``reason`` but carries a message naming the requested
        position, so the recorded event makes clear it was a positioning failure.
        """
        if (
            target.ptz_pan is not None
            or target.ptz_tilt is not None
            or target.ptz_zoom is not None
        ):
            requested = (
                f"pan={target.ptz_pan} tilt={target.ptz_tilt} zoom={target.ptz_zoom}"
            )
            move = adapter.move_to(
                pan=target.ptz_pan, tilt=target.ptz_tilt, zoom=target.ptz_zoom
            )
        elif target.ptz_preset is not None:
            requested = f"preset {target.ptz_preset!r}"
            move = adapter.move_to(preset_id=target.ptz_preset)
        else:
            return
        try:
            await move
        except CaptureError as exc:
            # Any capture-classified failure out of ``move_to`` is a positioning
            # failure (this method only awaits the move). Re-raise the same type
            # -- preserving its ``reason`` -- with a message naming the requested
            # position, so it routes through the caller's identical
            # transient-failure/backoff handling and the recorded event is clear.
            raise type(exc)(f"PTZ positioning to {requested} failed: {exc}") from exc

    async def _capture_with_timeout(
        self, adapter: CameraAdapter
    ) -> CapturedFrame | None:
        """Return a captured frame, or None if the attempt timed out."""
        timeout = self._settings.capture.timeout_seconds
        try:
            return await asyncio.wait_for(adapter.capture(), timeout=timeout)
        except TimeoutError:
            return None

    async def _check_frozen_frame(
        self, target: CaptureTarget, state: CaptureState, captured: CapturedFrame
    ) -> None:
        """Detect a run of identical frames (a frozen camera) and warn.

        Hashes the captured bytes. ``frozen_frame_run_count`` is the number of
        *consecutive identical frames* seen so far: a frame whose hash differs
        from the previous one starts a fresh run of length 1, and each identical
        follow-up extends it. When the run length reaches
        ``frozen_frame_threshold`` (so threshold ``N`` means ``N`` identical
        frames in a row) a warning event is emitted and the counter resets so the
        next identical frame must build a fresh run before warning again. Capture
        is never stopped -- a frozen camera that recovers resumes cleanly.
        """
        cfg = self._settings.capture
        if not cfg.frozen_frame_enabled:
            return
        digest = hashlib.sha256(captured.image_bytes).hexdigest()
        if state.last_frame_hash is not None and digest == state.last_frame_hash:
            state.frozen_frame_run_count += 1
        else:
            state.frozen_frame_run_count = 1
        state.last_frame_hash = digest

        if state.frozen_frame_run_count >= max(1, cfg.frozen_frame_threshold):
            run = state.frozen_frame_run_count
            state.frozen_frame_run_count = 0
            state.last_frame_hash = None
            logger.warning(
                "camera may be frozen project=%s identical_frames=%s",
                target.project_id,
                run,
            )
            await asyncio.to_thread(
                self._write_event,
                scope_id=target.project_id,
                level="warning",
                message=(
                    f"camera may be frozen: {run} consecutive identical "
                    f"frames captured for project {target.project_name!r}"
                ),
                metadata={"identical_frames": run},
            )

    def _parse_schedule_safely(self, target: CaptureTarget) -> Schedule:
        """Parse the target's schedule, falling back to always-open on error.

        A malformed schedule should not silently stop a project from capturing;
        log once at startup and treat it as "no schedule" (always open) for the
        life of the loop.
        """
        try:
            return parse_schedule(target.schedule)
        except ValueError as exc:
            logger.warning(
                "invalid schedule project=%s; treating as always-open: %s",
                target.project_id,
                exc,
            )
            return parse_schedule(None)

    def state_for_project(self, project_id: int) -> CaptureState | None:
        """Return the live capture state for a project, or None if untracked."""
        runner = self._runners.get(project_id)
        return runner.state if runner is not None else None

    def states_for_camera(self, camera_id: int) -> list[CaptureState]:
        """Return every tracked project state backed by ``camera_id``.

        A camera can back several projects, so this returns the set of per-project
        states rather than a single camera-level record.
        """
        return [
            runner.state
            for runner in self._runners.values()
            if runner.state.camera_id == camera_id
        ]

    def _record_success(self, state: CaptureState) -> None:
        """Mark a successful capture and reset the backoff/retry counters."""
        now = self._clock.now()
        state.state = "running"
        state.last_success_at = now
        state.last_capture_at = now
        state.last_error = None
        state.frames_captured += 1
        state.attempt_count = 0
        state.next_retry_at = None
        state.pause_reason = None

    def _record_transient_failure(
        self, state: CaptureState, exc: BaseException, rng: random.Random
    ) -> None:
        """Record a contained failure and schedule the next backoff retry."""
        cfg = self._settings.capture
        now = self._clock.now()
        state.state = "error"
        state.last_error_at = now
        state.last_error = str(exc)
        state.attempt_count += 1
        delay = _backoff_delay(
            state.attempt_count,
            base=cfg.backoff_base_seconds,
            maximum=cfg.backoff_max_seconds,
            jitter_fraction=cfg.backoff_jitter_fraction,
            rng=rng,
        )
        state.next_retry_at = now + timedelta(seconds=delay)

    async def _maybe_emit_camera_offline(
        self, target: CaptureTarget, state: CaptureState
    ) -> None:
        """Emit a camera-offline warning when failures cross the threshold, once.

        Edge-triggered: fires only on the transition into the offline state (when
        the consecutive-failure count -- ``attempt_count``, already incremented by
        :meth:`_record_transient_failure` -- first reaches
        ``offline_failure_threshold`` and no offline alert is currently standing),
        so a sustained outage logs one event rather than one per retry. The first
        success afterwards emits the matching recovery (see
        :meth:`_maybe_emit_camera_recovered`).
        """
        threshold = max(1, self._settings.capture.offline_failure_threshold)
        if state.offline_alerted or state.attempt_count < threshold:
            return
        state.offline_alerted = True
        logger.warning(
            "camera considered offline project=%s after %d consecutive failures",
            target.project_id,
            state.attempt_count,
        )
        await asyncio.to_thread(
            self._write_event,
            scope_id=target.project_id,
            level="warning",
            message=(
                f"camera for project {target.project_name!r} is offline: "
                f"{state.attempt_count} consecutive capture attempts failed"
            ),
            metadata={
                "reason": _REASON_CAMERA_OFFLINE,
                "consecutive_failures": state.attempt_count,
            },
        )

    async def _maybe_emit_camera_recovered(
        self, target: CaptureTarget, state: CaptureState
    ) -> None:
        """Emit a camera-recovery event on the first success after an outage.

        Edge-triggered against the ``offline_alerted`` latch: a success while no
        offline alert stands is a no-op, so a healthy camera never emits recovery
        spam. The recovery is the resolve signal that auto-clears the standing
        ``camera_offline`` alert for this project scope.
        """
        if not state.offline_alerted:
            return
        state.offline_alerted = False
        logger.info("camera recovered project=%s", target.project_id)
        await asyncio.to_thread(
            self._write_event,
            scope_id=target.project_id,
            level="info",
            message=(
                f"camera for project {target.project_name!r} recovered and is "
                "capturing again"
            ),
            metadata={"reason": _REASON_CAMERA_RECOVERED},
        )

    @staticmethod
    def _mark_waiting(
        state: CaptureState, *, schedule_open: bool, disk_ok: bool
    ) -> None:
        """Reflect a wait cycle on state without disturbing error/retry info.

        Records the distinct reason the gate is closed (``"low_disk"`` when the
        window is open but the disk is low, ``"window"`` when the schedule window
        is closed, ``None`` when the gate is open and the loop is merely between
        captures). A project currently in backoff keeps its ``error`` state so
        the status surface still shows the pending retry.
        """
        if not schedule_open:
            state.pause_reason = _PAUSE_WINDOW
        elif not disk_ok:
            state.pause_reason = _PAUSE_LOW_DISK
        else:
            state.pause_reason = None
        if state.state != "error":
            state.state = "running" if (schedule_open and disk_ok) else "idle"

    def _load_targets(self) -> list[CaptureTarget]:
        """Read active, capture-configured projects from the database.

        A project qualifies when it is active, bound to an existing camera that
        has a usable protocol, and either has a capture interval set **or** has
        exact-time anchors configured. The anchor-only case (anchors but no
        interval) lets a daily-shot project run a runner purely to fire its
        anchors; its ``interval_seconds`` is carried through as ``None`` (never
        replaced by a default) so it does no interval capturing. The project's
        schedule and the camera's geolocation (manual override preferred over a
        device-reported fix) are snapshotted so the loop never touches the ORM.

        A project whose campaign bound has been reached (past its end date, or at
        its frame-count cap) is *not* returned as a target; instead its id is
        collected and the campaign is ended (archived + event) once the read
        scope has closed -- the event write opens its own session, so it must not
        nest inside the iteration. This makes reconcile the authoritative
        time-based enforcer (it runs even when no capture is due). Synchronous;
        call via a thread executor.
        """
        now = self._clock.now()
        targets: list[CaptureTarget] = []
        ended: list[tuple[int, str, str]] = []
        with session_scope(self._session_factory) as session:
            stmt = (
                select(Project, Camera)
                .join(Camera, Project.camera_id == Camera.id)
                .where(Project.lifecycle_state == "active")
                .where(
                    or_(
                        Project.capture_interval_seconds.is_not(None),
                        Project.exact_time_anchors.is_not(None),
                    )
                )
                .where(Camera.protocol.is_not(None))
            )
            for project, camera in session.execute(stmt).all():
                reason = _campaign_end_reason(
                    now=now,
                    end_date=project.end_date,
                    frame_count=project.frame_count,
                    max_frame_count=project.max_frame_count,
                )
                if reason is not None:
                    ended.append((project.id, project.name, reason))
                    continue
                # Flow a missing interval through as None (anchor-only project):
                # it must not be replaced by the default, or the project would
                # silently start interval capturing. Projects that set an interval
                # of 0/falsey still fall back to the default, preserving prior
                # behaviour for the interval case.
                interval: int | None
                if project.capture_interval_seconds is None:
                    interval = None
                else:
                    interval = (
                        project.capture_interval_seconds
                        or self._settings.capture.default_interval_seconds
                    )
                latitude, longitude = _resolve_geolocation(camera)
                targets.append(
                    CaptureTarget(
                        project_id=project.id,
                        project_name=project.name,
                        camera_id=camera.id,
                        interval_seconds=interval,
                        schedule=project.schedule,
                        latitude=latitude,
                        longitude=longitude,
                        storage_path=project.storage_path,
                        stream_id=project.stream_id,
                        ptz_preset=project.ptz_preset,
                        ptz_pan=project.ptz_pan,
                        ptz_tilt=project.ptz_tilt,
                        ptz_zoom=project.ptz_zoom,
                        start_date=_as_aware_utc(project.start_date),
                        end_date=_as_aware_utc(project.end_date),
                        max_frame_count=project.max_frame_count,
                        exact_time_anchors=project.exact_time_anchors,
                        event_triggers=project.event_triggers,
                    )
                )
        for project_id, project_name, reason in ended:
            self._end_campaign(project_id, project_name, reason)
        return targets

    def _load_listener_targets(self) -> list[CaptureTarget]:
        """Read the projects that qualify for an event listener.

        A project qualifies when it is active, bound to a camera with a usable
        protocol, and has at least one *enabled* event trigger. This is a
        deliberately separate query from :meth:`_load_targets`: a listener is
        independent of the interval/anchor capture runner, so a project may have a
        listener without a runner and vice versa. The "enabled trigger" test runs
        in Python (over the stored JSON) via a self-contained predicate so a
        malformed trigger list yields no listener rather than an error.
        Synchronous; call via a thread executor.
        """
        targets: list[CaptureTarget] = []
        with session_scope(self._session_factory) as session:
            stmt = (
                select(Project, Camera)
                .join(Camera, Project.camera_id == Camera.id)
                .where(Project.lifecycle_state == "active")
                .where(Project.event_triggers.is_not(None))
                .where(Camera.protocol.is_not(None))
            )
            for project, camera in session.execute(stmt).all():
                if not _has_enabled_event_trigger(project.event_triggers):
                    continue
                latitude, longitude = _resolve_geolocation(camera)
                targets.append(
                    CaptureTarget(
                        project_id=project.id,
                        project_name=project.name,
                        camera_id=camera.id,
                        interval_seconds=project.capture_interval_seconds,
                        schedule=project.schedule,
                        latitude=latitude,
                        longitude=longitude,
                        storage_path=project.storage_path,
                        stream_id=project.stream_id,
                        ptz_preset=project.ptz_preset,
                        ptz_pan=project.ptz_pan,
                        ptz_tilt=project.ptz_tilt,
                        ptz_zoom=project.ptz_zoom,
                        exact_time_anchors=project.exact_time_anchors,
                        event_triggers=project.event_triggers,
                    )
                )
        return targets

    def _end_campaign(self, project_id: int, project_name: str, reason: str) -> None:
        """Archive a completed-campaign project and log why, idempotently.

        Sets ``lifecycle_state`` to ``archived`` so the project drops out of the
        qualifying set (its runner is reaped on the next reconcile) and leaves the
        dashboard, and records a project-scoped event naming the bound that was
        reached. A project already archived (e.g. ended via two paths racing) is
        left untouched. Synchronous; call via a thread executor from the loop, or
        directly from the reconcile read path once its scope has closed.
        """
        message = (
            f"project {project_name!r} reached its end date; capture stopped and "
            "the project was archived"
            if reason == _CAMPAIGN_END_DATE
            else (
                f"project {project_name!r} reached its frame-count limit; capture "
                "stopped and the project was archived"
            )
        )
        with session_scope(self._session_factory) as session:
            project = session.get(Project, project_id)
            if project is None or project.lifecycle_state == "archived":
                return
            project.lifecycle_state = "archived"
            self._write_event(
                scope_id=project_id,
                level="info",
                message=message,
                metadata={"reason": reason},
                session=session,
            )

    def _load_camera(self, camera_id: int) -> _CameraConfig | None:
        """Read a camera's adapter-relevant fields as a detached snapshot."""
        with session_scope(self._session_factory) as session:
            camera = session.get(Camera, camera_id)
            if camera is None:
                return None
            return _CameraConfig(
                protocol=camera.protocol,
                address=camera.address,
                credentials=camera.credentials,
                credentials_inherit_default=bool(camera.credentials_inherit_default),
                snapshot_uri=camera.snapshot_uri,
                stream_uri=camera.stream_uri,
                default_resolution=camera.default_resolution,
            )

    def _load_default_credentials(self) -> tuple[str, str] | None:
        """Resolve the global fallback ``(username, password)``, or ``None``.

        Read at adapter-build time (alongside :meth:`_load_camera`) so the
        background capture path applies the same effective-credential resolution
        the validate path does. Returns ``None`` when no fallback applies.
        """
        with session_scope(self._session_factory) as session:
            return resolve_default_credentials(session)

    def _log_resume_gap(self, target: CaptureTarget) -> None:
        """Record a downtime gap for a resumed project, if meaningful.

        Reads the most recent frame's capture timestamp; if the span to now
        exceeds a small multiple of the capture interval, writes an
        informational event describing the resumed-after-downtime window.
        Capture resumes forward regardless -- no frames are synthesised for the
        gap. Synchronous; call via a thread executor.

        An anchor-only project (no interval) has no interval-based gap threshold,
        so the downtime-gap log is skipped for it -- a daily anchor's "gap" is
        expected, not a downtime signal.
        """
        if target.interval_seconds is None:
            return
        with session_scope(self._session_factory) as session:
            last_ts = session.execute(
                select(Frame.capture_timestamp)
                .where(Frame.project_id == target.project_id)
                .where(Frame.capture_timestamp.is_not(None))
                .order_by(Frame.capture_timestamp.desc())
                .limit(1)
            ).scalar_one_or_none()
            if last_ts is None:
                return
            # capture_timestamp is stored naive-UTC; make it aware to subtract.
            last_aware = (
                last_ts if last_ts.tzinfo is not None else last_ts.replace(tzinfo=UTC)
            )
            now = self._clock.now()
            gap = (now - last_aware).total_seconds()
            threshold = target.interval_seconds * _GAP_INTERVAL_MULTIPLE
            if gap <= threshold:
                return
            self._write_event(
                scope_id=target.project_id,
                level="info",
                message=(
                    f"capture resumed for project {target.project_name!r} after a "
                    f"{int(gap)}s downtime gap (last frame at "
                    f"{last_aware.isoformat()})"
                ),
                metadata={
                    "gap_seconds": int(gap),
                    "last_frame_at": last_aware.isoformat(),
                },
                session=session,
            )

    def _write_event(
        self,
        *,
        scope_id: int,
        level: str,
        message: str,
        metadata: dict[str, object] | None = None,
        session: Session | None = None,
    ) -> None:
        """Insert a project-scoped event row. Synchronous; use a thread executor.

        When ``session`` is provided the event is added to that open scope;
        otherwise a fresh transactional scope is opened just for this write.
        """
        event = Event(
            scope="project",
            scope_id=scope_id,
            level=level,
            message=message,
            timestamp=datetime.now(UTC).replace(tzinfo=None),
            event_metadata=metadata,
        )
        if session is not None:
            session.add(event)
            return
        with session_scope(self._session_factory) as fresh:
            fresh.add(event)


def _resolve_geolocation(camera: Camera) -> tuple[float | None, float | None]:
    """Return the camera's ``(latitude, longitude)`` for sun-time evaluation.

    A manual operator override takes precedence; otherwise the device-reported
    (or any stored) fix is used. Returns ``(None, None)`` when no usable
    coordinate pair is present, which the schedule evaluator treats as "no
    location" (sun windows then evaluate as closed, clock windows are unaffected).
    """
    if camera.geolocation_latitude is None or camera.geolocation_longitude is None:
        return (None, None)
    return (camera.geolocation_latitude, camera.geolocation_longitude)
