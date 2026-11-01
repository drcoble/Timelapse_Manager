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
events) is moved off the event loop through :meth:`CaptureSupervisor._run_blocking`,
the single executor seam (see :mod:`timelapse_manager.capture.executor`).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import random
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple, ParamSpec, Protocol, TypeVar

import httpx
from sqlalchemy import CursorResult, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from ..cameras import CameraAdapter, CapturedFrame, build_adapter
from ..cameras.base import (
    PTZ_SETTLE_MAX_SECONDS,
    AuthCaptureError,
    CaptureError,
    TimeoutCaptureError,
)
from ..cameras.clients import CameraHttpClients, ClientPurpose
from ..config import Settings
from ..db.engine import run_with_write_retry
from ..db.models import Camera, Event, ExactTimeFire, Frame, Project
from ..db.models.event import OPEN_ALERT_INDEX_LEVELS
from ..db.session import session_scope
from ..ffmpeg_pin import resolve_ffmpeg_binary
from ..logging import redact_text
from ..security.camera_defaults_service import resolve_default_credentials
from ..storage import paths
from ..storage.monitor import DiskSpaceMonitor
from . import anchors as anchors_mod
from . import event_triggers as event_triggers_mod
from . import geo
from .camera_config import (
    CaptureCameraConfig,
    config_version,
    load_capture_config,
    load_defaults_row,
)
from .camera_health import CameraHealthBook, HealthTransition, classify_failure
from .camera_sessions import CameraSessionRegistry, ptz_position_of
from .event_listener import TRANSPORT_VAPIX_WEBSOCKET
from .executor import run_blocking
from .frame_writer import FrameWriter, WrittenFrame
from .one_shot import capture_one_now
from .schedule import Schedule, next_transition, parse_schedule

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
    from concurrent.futures import Executor
    from typing import Any
    from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_P = ParamSpec("_P")
_T = TypeVar("_T")

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
# under the JSON ``"reason"`` key. Both are camera-scoped (``scope="camera"``,
# ``scope_id=camera_id``) since #69 CAM-R6: ``camera_recovered`` is the resolve
# signal that auto-clears an active ``camera_offline`` alert for the same camera
# (see the monitoring alerts module's resolve map).
_REASON_CAMERA_OFFLINE = "camera_offline"
_REASON_CAMERA_RECOVERED = "camera_recovered"
# A capture failed for a local (server-side) reason; one informational system
# event per streak, not counted against the camera (#69 CAM-R6). Info, not
# warning (#133): nothing resolves it, and a warning would stand as an alert.
_REASON_CAPTURE_LOCAL_ERROR = "capture_local_error"
# The one-time upgrade sweep of pre-0.2.0 project-scoped offline alerts.
_REASON_CAMERA_ALERTS_MIGRATED = "camera_offline_alerts_migrated"
# Offline alerts of cameras deleted since they were raised, cleared at start
# (#132); ``services.cameras.delete_camera`` clears them on delete.
_REASON_CAMERA_ALERTS_ORPHANED = "camera_offline_alerts_orphaned"
# The event-listener alert pair (#69 CAM-R7, #67-R9), camera-scoped like the
# offline pair: ``events_available`` (info) auto-clears ``events_unavailable``
# (warning) for the same camera (see the monitoring alerts module's resolve map).
_REASON_EVENTS_UNAVAILABLE = "events_unavailable"
_REASON_EVENTS_AVAILABLE = "events_available"
# Exact-time anchor retry and alert reasons (#69, CAM-R1). ``anchor_failed``
# (warning) is auto-cleared by ``anchor_recovered`` (info), see
# ``monitoring.alerts.RESOLVE_TO_RAISE_REASONS``; ``anchor_retry`` (info) records
# a failed attempt that will be retried.
_REASON_ANCHOR_FAILED = "anchor_failed"
_REASON_ANCHOR_RECOVERED = "anchor_recovered"
_REASON_ANCHOR_RETRY = "anchor_retry"
_FIRE_RETRY_PENDING = "retry_pending"

# Reasons a project's campaign runs to completion (stop capture + archive).
_CAMPAIGN_END_DATE = "end_date"
_CAMPAIGN_END_FRAME_COUNT = "frame_count"

# Runner health (#6). A cycle that raises unexpectedly is retried after this many
# seconds (capped at the idle ceiling) instead of ending the runner.
_CYCLE_ERROR_RETRY_SECONDS = 30.0
# Upper bound of the per-project delay added to an anchor wake, so projects
# sharing an anchor do not all fire in the same millisecond. Far inside
# ``anchors.EXACT_TIME_GRACE`` (30 min).
ANCHOR_JITTER_MAX = timedelta(seconds=5)
# Extra time (on top of the slot wait and the capture timeout) one anchor fire
# may take before it is abandoned as hung; its claimed fire-log row keeps the
# provisional ``failed``.
_ANCHOR_FIRE_MARGIN_SECONDS = 60.0
# The longest a capture waits for a free slot on its camera is two capture
# timeouts plus this margin (#69 CAM-R4): room for the captures ahead of it
# (prod camera 2 has four anchor projects firing within 5 s) to finish.
_SLOT_WAIT_MARGIN_SECONDS = 30.0
# Total time the best-effort scene-metadata read may take after a frame is in
# hand (#69 CAM-R2). It runs between capture and write, outside the capture
# budget, so a slow metadata read can delay a frame by at most this long but can
# never cause it to be discarded. A timeout leaves the frame without metadata.
SCENE_METADATA_TOTAL_SECONDS = 2.5
# Event reason for a capture that ran at the camera default because the
# selected stream profile could not be applied (owner decision OD-2).
_REASON_STREAM_PROFILE_DEGRADED = "stream_profile_degraded"


def _anchor_jitter(project_id: int) -> timedelta:
    """Return this project's fixed anchor-wake delay in ``[0, ANCHOR_JITTER_MAX)``.

    Seeded from the project id (like the backoff RNG) so it is stable across
    cycles and restarts and differs between projects.
    """
    fraction = random.Random(f"anchor-jitter:{project_id}").random()
    return ANCHOR_JITTER_MAX * fraction


def _as_aware_utc(value: datetime | None) -> datetime | None:
    """Normalise a possibly-naive stored datetime to aware UTC, or pass None.

    The campaign-bound columns are stored naive (the project's other datetimes
    use the same convention); the running loop and the clock work in aware UTC,
    so a naive value is interpreted as UTC before any comparison.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class _AnchorClaim(NamedTuple):
    """The outcome of claiming (or reclaiming) an anchor's fire-log row (#69).

    ``outcome`` is ``"claimed"`` (this worker makes attempt ``attempts`` now),
    ``"pending"`` (a retry is due at ``next_attempt_at``, aware UTC),
    ``"finalized"`` (a pending retry ran out of grace and was finalised, with its
    alert; ``detail`` is the last error) or ``"done"`` (nothing to do).
    """

    outcome: Literal["claimed", "pending", "finalized", "done"]
    attempts: int
    next_attempt_at: datetime | None = None
    detail: str | None = None


def _claim_label(claim: _AnchorClaim) -> str:
    """The debug-log label for a claim outcome (#7's format, plus retries)."""
    if claim.outcome == "done":
        return "already_recorded"
    if claim.outcome == "claimed":
        return "claimed" if claim.attempts == 1 else f"retry_claimed:{claim.attempts}"
    return claim.outcome


def _naive_utc(value: datetime) -> datetime:
    """Return an aware instant as the naive UTC the database columns store."""
    return value.astimezone(UTC).replace(tzinfo=None)


def _rowcount(result: object) -> int:
    """The number of rows an UPDATE matched (``CursorResult.rowcount``)."""
    assert isinstance(result, CursorResult)
    return int(result.rowcount)


def _redact_urls_in(value: Any) -> Any:
    """Return ``value`` with the login of any URL in its strings masked.

    Event metadata holds identifiers the capture path reads back (a dedup key,
    an anchor id), so unlike :func:`~timelapse_manager.logging.redact` this does
    not mask by field name: it applies :func:`redact_text` to string values and
    leaves everything else as it was.
    """
    if isinstance(value, dict):
        return {key: _redact_urls_in(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_urls_in(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _anchor_error_text(exc: BaseException) -> str:
    """A short, redacted description of a failed anchor attempt.

    The error text can carry a camera URL (with credentials in its userinfo or
    query), so it is redacted before it is stored, logged or evented.
    """
    text = str(exc) or type(exc).__name__
    return redact_text(text)[:200]


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


def _live_frame_cap(target: CaptureTarget, state: CaptureState) -> int | None:
    """Return the frame cap a running loop enforces (#104).

    The cap the last frame write reported, once there is one, else the
    launch-time cap: a project edited while its runner lives is held to the new
    cap from its next cycle instead of the next reconcile pass.
    """
    return state.live_max_frame_count if state.live_cap_read else target.max_frame_count


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

    def monotonic(self) -> float:
        """Return a monotonic reading in seconds (never steps backwards).

        Cadence, retry instants and runner heartbeats are measured on this
        clock, so a wall-clock step (NTP, a manual change, a VM resume) can
        neither stall nor burst captures and can neither mask nor fake a wedged
        runner (#69 CAM-R5, CAM-R13). The origin is arbitrary: only differences
        mean anything.
        """
        ...

    async def sleep(self, seconds: float) -> None:
        """Sleep for ``seconds`` (cancellable)."""
        ...


class _RealClock:
    """The default :class:`Clock`: real UTC time and ``asyncio.sleep``."""

    def now(self) -> datetime:
        """Return the current aware-UTC instant."""
        return datetime.now(UTC)

    def monotonic(self) -> float:
        """Return :func:`time.monotonic`."""
        return time.monotonic()

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
    # When the project last became active (aware UTC): the exact-time catch-up
    # records no missed anchor for an instant before it (#280). ``None`` = unknown
    # (no floor). Not loop-affecting: only a fresh runner start reads it.
    active_since: datetime | None = None
    # The camera configuration's fingerprint as the reconcile read saw it
    # (:func:`~.camera_config.config_version`), which the camera session cache
    # is checked against. Not loop-affecting and not compared: a camera edit
    # reloads the cached session, never relaunches a runner (which could drop
    # an anchor fire in progress).
    camera_version: str | None = field(default=None, repr=False, compare=False)


# The camera snapshot the capture path builds adapters from now lives in
# :mod:`.camera_config`; the old private name is kept as an alias.
_CameraConfig = CaptureCameraConfig


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
    # Whether a camera-offline alert stands for this project's camera. A
    # read-only mirror for the status surfaces: the latch itself lives in the
    # supervisor's per-camera health book (#69 CAM-R6), which every runner on
    # the camera shares, and is copied here after each outcome. Purely in-memory.
    offline_alerted: bool = False
    # When this runner began capturing (the basis for reported uptime). Set when
    # the loop task is launched; a reconcile restart (config change, resume)
    # starts a fresh runner and therefore a fresh ``started_at``.
    started_at: datetime | None = None
    # Heartbeat (#6): stamped at the top of every loop iteration. The reconcile
    # tick relaunches a runner whose heartbeat is older than its wedge threshold
    # (or whose task has ended unexpectedly); ``restarts`` counts those relaunches
    # for this project since the supervisor started.
    last_cycle_at: datetime | None = None
    restarts: int = 0
    # Monotonic twins of the wall-clock fields above (#69 CAM-R5, CAM-R13). The
    # loop's decisions use these; the wall-clock fields are for display only.
    # ``next_due_mono`` is the interval grid's next slot, ``next_retry_mono`` a
    # pending backoff retry (always at or before the next slot), and
    # ``started_mono``/``last_cycle_mono`` the runner's heartbeat. ``None`` for
    # the first two means "capture as soon as the gate opens". In-memory only.
    next_due_mono: float | None = None
    next_retry_mono: float | None = None
    started_mono: float | None = None
    last_cycle_mono: float | None = None
    # The project's frame cap as the last frame write saw it (#104), so an edit to
    # ``max_frame_count`` reaches a running loop without a reconcile restart.
    # ``live_cap_read`` is False until a write has reported it, and the loop uses
    # the launch-time cap until then. In-memory only.
    live_max_frame_count: int | None = None
    live_cap_read: bool = False


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


# The shortest gap between two subscriptions of one listener, however the last
# one ended. A source that ends at once (a camera that closes every subscription,
# a factory that returns an empty stream) was resubscribed in a hot loop.
_LISTENER_MIN_RESUBSCRIBE_SECONDS = 5.0
# A subscription must have lived this long before its listener's backoff starts
# over: a camera that accepts a subscription and drops it a second later is
# still a failing camera.
_LISTENER_HEALTHY_SECONDS = 60.0
# Captures a listener may have waiting. A burst of events behind a slow capture
# keeps the newest; the oldest is dropped (and counted).
_LISTENER_QUEUE_SIZE = 8
# The default budget for :meth:`CaptureSupervisor.stop`.
_STOP_BUDGET_SECONDS = 10.0


@dataclass
class ListenerState:
    """What one event listener is doing, for the debug endpoint and health (#69 CAM-R7).

    Written on the event loop only. Every field is a plain value; error text is
    redacted before it is stored.

    :param transport: the event transport in use (``vapix-websocket`` or
        ``onvif-pullpoint``), or ``None`` before the first subscribe.
    :param connected_since: when the current subscription was established;
        ``None`` while not subscribed.
    :param last_event_at: when the last event of any topic arrived.
    :param last_progress_mono: the last time the listener made progress -- a
        subscription attempt began or succeeded, or an event arrived -- on the
        supervisor's monotonic clock. A quiet, healthy subscription does not
        advance it between events, so it is not a wedge signal by itself.
    :param last_error: the last failure's text, redacted.
    :param consecutive_failures: subscription attempts in a row that failed
        before subscribing.
    :param restarts: times the supervisor relaunched a listener that had exited.
    :param dropped_events: matched events dropped because the capture queue was
        full.
    :param downgrades: times a VAPIX camera's WebSocket failed and the listener
        fell back to ONVIF PullPoint.
    """

    transport: str | None = None
    connected_since: datetime | None = None
    last_event_at: datetime | None = None
    last_progress_mono: float | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    restarts: int = 0
    dropped_events: int = 0
    downgrades: int = 0
    #: Whether the current subscription attempt has subscribed (or delivered).
    attempt_subscribed: bool = False
    #: Whether the current downgrade has had its warning event.
    downgrade_reported: bool = False


@dataclass
class _ListenerRunner:
    """Internal pairing of a running event-listener task with its launch target.

    Parallel to :class:`_ProjectRunner` but for the event-listener registry: a
    listener consumes a camera's event stream and fires a one-shot capture on a
    matching, debounced event. The launch-time :class:`CaptureTarget` is held so
    a later reconcile tick can detect that a still-qualifying project's
    listener-affecting configuration (camera, triggers) changed and restart the
    listener with the fresh target.

    ``relaunch_not_before`` (the supervisor's monotonic clock) is when a
    listener whose task died may be relaunched, so a listener that dies at once
    is not relaunched on every reconcile tick.
    """

    target: CaptureTarget
    task: asyncio.Task[None] | None = field(default=None)
    state: ListenerState = field(default_factory=ListenerState)
    relaunch_not_before: float | None = field(default=None)


class _EventCaptureQueue:
    """The hand-off between a listener's reader and the captures its events cause.

    The reader matches events and offers a job per capture; one consumer task
    runs them one at a time. A slow capture therefore never stops the event
    source being read (a camera whose socket is not drained drops it), and a job
    that raises never reaches the subscription. The queue holds at most
    ``maxsize`` jobs: offering to a full queue drops the *oldest*, counted through
    ``on_drop``. Owned by one listener, so it outlives its re-subscribes.
    """

    def __init__(self, *, maxsize: int, on_drop: Callable[[], None]) -> None:
        self._queue: asyncio.Queue[Callable[[], Awaitable[None]]] = asyncio.Queue(
            maxsize=maxsize
        )
        self._on_drop = on_drop
        self._task: asyncio.Task[None] | None = None

    def start(self, name: str) -> None:
        """Start the consumer task."""
        self._task = asyncio.create_task(self._consume(), name=name)

    def offer(self, job: Callable[[], Awaitable[None]]) -> None:
        """Queue ``job``; when the queue is full the oldest waiting job is dropped."""
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - raced with the consumer
                pass
            else:
                self._queue.task_done()
                self._on_drop()
        self._queue.put_nowait(job)

    async def _consume(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await job()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a job must never end the consumer
                logger.exception("event-triggered capture failed unexpectedly")
            finally:
                self._queue.task_done()

    async def drain(self) -> None:
        """Wait until every queued job has run (the source ended cleanly)."""
        await self._queue.join()

    async def aclose(self) -> None:
        """Cancel the consumer and wait for it, whatever it was doing."""
        task = self._task
        if task is None:
            return
        self._task = None
        task.cancel()
        await asyncio.wait({task})


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


@dataclass(frozen=True)
class RunnerPlan:
    """What a runner decided on its most recent cycle (diagnostics only, #7).

    Recorded once per cycle so the debug endpoint and the SIGUSR2 dump can say
    when a runner next intends to wake and why, without re-deriving the schedule.
    Purely in-memory; never persisted and never read by the loop itself.
    """

    planned_at: datetime
    action: str
    sleep_seconds: float
    schedule_open: bool
    disk_ok: bool
    next_change: datetime | None
    next_anchor_wake: datetime | None
    next_retry_at: datetime | None

    @property
    def next_wake_at(self) -> datetime:
        """The instant the runner planned to start its next cycle."""
        return self.planned_at + timedelta(seconds=self.sleep_seconds)


@dataclass(frozen=True)
class RunnerSnapshot:
    """A point-in-time copy of one runner for diagnostics (#7).

    ``state`` is a copy, so a reader on another thread never observes a
    half-updated record and can never mutate the live one.
    """

    project_name: str
    state: CaptureState
    plan: RunnerPlan | None
    task_name: str | None
    task_done: bool


class RunnerProblem(NamedTuple):
    """A runner the reconcile tick would relaunch, and why (see ``runner_problems``)."""

    project_id: int
    reason: str


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


async def _cancel_and_wait(task: asyncio.Task[None]) -> None:
    """Cancel ``task`` and wait for it to finish, without eating our own cancel.

    Awaiting a task you just cancelled raises its ``CancelledError``, which the
    caller wants to ignore. But if the *calling* task is cancelled while parked
    on that await, asyncio delegates the cancel to ``task`` and the caller sees
    the very same ``CancelledError`` -- a blanket ``suppress`` would swallow the
    caller's own cancellation and keep it running. So the error is ignored only
    when the caller has no cancellation pending (``Task.cancelling()``, 3.11+).
    """
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise


def _plan_next(
    now: datetime,
    *,
    is_open: bool,
    next_change: datetime | None,
    interval: float | None,
    max_idle_sleep: float,
    now_mono: float,
    next_due_mono: float | None,
    next_retry_mono: float | None,
    next_wake: datetime | None = None,
) -> Decision:
    """Decide the next capture action and sleep duration (pure, synchronous).

    The schedule gate is authoritative for *interval* capture: while the gate is
    **closed** the loop only ever waits, even if a backoff retry is due. The wait
    is capped at ``max_idle_sleep`` so a long closed window stays cancellable and
    config/clock drift is re-evaluated.

    While the gate is **open** the loop captures only when a frame is actually
    due: at the pending backoff retry instant if there is one, else at the
    interval grid's next slot (``next_due_mono``), or at once when neither is
    set (the first frame). All of these are monotonic readings (#69 CAM-R5,
    CAM-R13), so a wall-clock step cannot move them; the wall-clock ``now`` is
    used only for the schedule gate and the anchor wake. Otherwise it waits the
    smaller of the time to that instant, the time to the
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
    :param now_mono: the current monotonic reading, in seconds.
    :param next_due_mono: monotonic instant of the grid's next slot, or None
        when no frame has been attempted yet.
    :param next_retry_mono: monotonic instant a pending backoff retry is due, or
        None. Never later than ``next_due_mono``.
    :param next_wake: an additional future instant to wake no later than (e.g.
        the next anchor fire time), or ``None``.
    """
    until_change = _seconds_until(next_change, now, default=max_idle_sleep)
    until_wake = _seconds_until(next_wake, now, default=max_idle_sleep)

    if not is_open or interval is None:
        # No interval capture this cycle (gate closed, or anchor-only project):
        # wait only, but never past the next gate change or the next anchor wake.
        sleep = min(until_change, until_wake, max_idle_sleep)
        return Decision(
            action="wait",
            sleep_seconds=sleep,
            next_retry_at=_retry_wall(now, now_mono, next_retry_mono),
        )

    due_mono = next_retry_mono if next_retry_mono is not None else next_due_mono
    due_in = 0.0 if due_mono is None else max(0.0, due_mono - now_mono)

    if due_in <= 0.0:
        return Decision(action="capture", sleep_seconds=0.0, next_retry_at=None)

    sleep = min(due_in, until_change, until_wake, max_idle_sleep)
    return Decision(
        action="wait",
        sleep_seconds=sleep,
        next_retry_at=_retry_wall(now, now_mono, next_retry_mono),
    )


def _retry_wall(
    now: datetime, now_mono: float, next_retry_mono: float | None
) -> datetime | None:
    """Derive the wall-clock display instant of a pending retry, or ``None``."""
    if next_retry_mono is None or next_retry_mono <= now_mono:
        return None
    return now + timedelta(seconds=next_retry_mono - now_mono)


def _advance_due(due: float | None, interval: float, now_mono: float) -> float:
    """Return the first grid slot strictly after ``now_mono`` (pure).

    The grid is ``due + k * interval``. After an attempt the next slot is the
    first one still in the future: a slot that was missed (a slow capture, an
    outage, a closed window) is skipped, never made up in a burst, and a capture
    that takes 1.5 s on a 60 s grid does not push the next one to 61.5 s. A
    ``None`` ``due`` starts the grid at ``now_mono``. A ``due`` already in the
    future (a retry fired ahead of its slot) is returned unchanged.
    """
    if due is None:
        due = now_mono
    if due > now_mono:
        return due
    missed = int((now_mono - due) // interval) + 1
    return due + missed * interval


def _retry_instant(now_mono: float, delay: float, next_due_mono: float | None) -> float:
    """Return the monotonic instant of the next retry: backoff, capped at the slot.

    A camera that comes back mid-backoff is picked up by the next grid slot at
    the latest, so recovery never waits longer than one interval however large
    the backoff has grown (#69 CAM-R5).
    """
    retry = now_mono + delay
    if next_due_mono is not None:
        retry = min(retry, next_due_mono)
    return retry


def _backoff_delay(
    attempt: int,
    *,
    base: float,
    maximum: float,
    jitter_fraction: float,
    rng: random.Random,
    saturate: bool = False,
) -> float:
    """Capped exponential backoff with symmetric jitter (pure, deterministic).

    ``delay = min(base * 2**(attempt-1), maximum)`` then scaled by a random
    factor in ``[1 - jitter_fraction, 1 + jitter_fraction]`` drawn from ``rng``.
    Passing a seeded ``rng`` makes the result reproducible, which is what lets
    the retry instant be asserted in a test.

    :param attempt: 1-based count of consecutive failures (1 = first retry).
    :param saturate: start at the ceiling whatever ``attempt`` is. A refused
        login will not fix itself between retries, so the caller skips the
        ramp-up (#69 CAM-R5).
    """
    exponent = max(0, attempt - 1)
    raw = maximum if saturate else base * (2.0**exponent)
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

    Owns the camera HTTP clients (:class:`CameraHttpClients`) used to build
    HTTP-based adapters and a :class:`FrameWriter` for atomic persistence. Both
    are available before :meth:`start` so the manual-capture endpoint can reuse
    them.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        clock: Clock | None = None,
        disk_monitor: DiskSpaceMonitor | None = None,
        event_source_factory: EventSourceFactory | None = None,
        executor: Executor | None = None,
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
        :param executor: where blocking capture work (database reads and writes,
            disk probes, hashing) runs. ``None`` (the default) uses the event
            loop's default executor, exactly as :func:`asyncio.to_thread` does;
            a dedicated executor keeps capture from queueing behind other
            threadpool work. Every hop goes through :meth:`_run_blocking`.
        """
        self._settings = settings
        self._executor = executor
        self._session_factory = session_factory
        self._clock: Clock = clock if clock is not None else _RealClock()
        frames_root = settings.paths.frames_root
        assert frames_root is not None  # populated by PathsSettings validator
        self._frames_root = Path(frames_root)
        # Camera traffic uses three bounded clients, one pool per purpose
        # (capture, events, probe; #69 CAM-P2), so a probe burst or event
        # long-polls never make a capture wait for a connection. They talk to
        # untrusted cameras: redirects stay off and body ceilings are enforced
        # per read by the adapters. Building them opens no connection.
        self._clients = CameraHttpClients(settings.capture)
        self._http_client = self._clients.capture
        # Resolved once here (at startup wiring): the RTSP adapter grabs frames
        # with the same ffmpeg the encoder uses -- bundled when frozen, an
        # explicit knob when set, else ``ffmpeg`` on ``PATH``.
        self._ffmpeg_binary = resolve_ffmpeg_binary(settings)
        self._writer = FrameWriter(session_factory, self._frames_root)
        self._disk_monitor = (
            disk_monitor if disk_monitor is not None else _build_disk_monitor(settings)
        )
        self._runners: dict[int, _ProjectRunner] = {}
        # Each runner's most recent plan, keyed by project id (diagnostics only).
        self._plans: dict[int, RunnerPlan] = {}
        # Event-listener registry, parallel to ``_runners`` and keyed the same way
        # (by project id). A project qualifies for a listener when it is active,
        # bound to a camera with a protocol, and has at least one enabled event
        # trigger -- a predicate separate from the interval/anchor qualifying set.
        self._listeners: dict[int, _ListenerRunner] = {}
        # Each running listener's state by project id, set by ``_run_listener``
        # itself so a listener driven directly (tests) is as visible to the
        # factory's reports as a launched one.
        self._listener_states: dict[int, ListenerState] = {}
        self._event_source_factory = event_source_factory
        self._attach_listener_reporter(event_source_factory)
        # Capture degradations already reported, per project (#69 CAM-R2): one
        # warning event per project, reason and runner lifetime. Kept here, not
        # on CaptureState, because one-shot captures deliberately take no
        # CaptureState; ``_launch`` clears a project's entry, so a relaunched
        # runner reports afresh.
        self._degradations_reported: dict[int, set[str]] = {}
        self._started = False
        self._stopped = False
        # Background reconcile loop plumbing. The task is tracked separately from
        # ``_runners`` so per-project introspection (state_for_project, the test
        # suite's _runners assertions) is unaffected. ``_reconcile_wakeup`` lets a
        # create/reactivate notification wake the loop early instead of waiting
        # out the periodic interval.
        self._reconcile_task: asyncio.Task[None] | None = None
        self._reconcile_wakeup = asyncio.Event()
        # The loop :meth:`start` ran on. ``asyncio.Event`` is not thread-safe, so
        # :meth:`notify_reconcile` marshals onto this loop when called from any
        # other thread (#66 L5). ``None`` until started.
        self._loop: asyncio.AbstractEventLoop | None = None
        # Serializes reconcile passes. Each pass awaits between reading the
        # qualifying set and acting on it, so two overlapping passes (the loop
        # tick plus a direct call) could both launch the same project and orphan
        # the first runner's task, which stop() would then never cancel.
        self._reconcile_lock = asyncio.Lock()
        # When the last reconcile pass finished cleanly (diagnostics only, #8).
        self.last_reconcile_at: datetime | None = None
        # Per-camera capture health (#69 CAM-R6), shared by every project on a
        # camera. Seeded from the database (and the pre-0.2.0 project-scoped
        # offline alerts swept) once, before the first runner launches; see
        # :meth:`_ensure_camera_health_synced`.
        self._camera_health = CameraHealthBook(
            settings.capture.offline_failure_threshold
        )
        self._camera_health_synced = False
        # One lock per camera orders its health-event writes (#135): taken right
        # after a transition is decided, with no await in between, so the FIFO
        # lock hands the executor the writes in decision order.
        self._camera_health_locks: dict[int, asyncio.Lock] = {}
        # Per-camera sessions (#69 CAM-P1, CAM-R4): the cached camera config and
        # decrypted credentials, the adapters' resolved values, the capture
        # slot and the last PTZ position. Built here, not in start(), because
        # the one-shot path captures through it on a supervisor that was never
        # started. The loader and the executor seam are looked up at call
        # time, so the load is one ``_run_blocking`` hop like every other.
        self._camera_sessions = CameraSessionRegistry(
            lambda camera_id: self._load_capture_config(camera_id),
            lambda fn, *args: self._run_blocking(fn, *args),
        )

    @property
    def http_client(self) -> httpx.AsyncClient:
        """The capture HTTP client; kept for callers that capture.

        Equal to ``client_for("capture")``. Probes and event listeners use
        :meth:`client_for` with their own purpose instead.
        """
        return self._http_client

    def capture_client_for(self, config: Any) -> httpx.AsyncClient:
        """Return the capture client for a camera config: its pinned one, if pinned.

        A camera without a pin uses the shared capture client (``http_client``).
        """
        pin = getattr(config, "tls_pinned_cert_pem", None)
        if pin:
            return self.client_for("capture", str(pin))
        return self._http_client

    def client_for(
        self, purpose: ClientPurpose, pinned_cert_pem: str | None = None
    ) -> httpx.AsyncClient:
        """Return the camera HTTP client for ``purpose``.

        ``"capture"`` for captures, ``"events"`` for event listeners and
        ``"probe"`` for operator probes, queries and discovery enrichment. Each
        has its own connection pool (#69 CAM-P2).

        :param pinned_cert_pem: a pinned camera's certificate
            (``camera.tls_pinned_cert_pem``). Every caller that holds a camera
            passes it, so a pinned camera is only ever reached through a client
            that trusts that certificate alone (#69 CAM-S1).
        """
        return self._clients.for_purpose(purpose, pinned_cert_pem)

    @property
    def camera_sessions(self) -> CameraSessionRegistry:
        """The per-camera session cache, capture slots and PTZ state."""
        return self._camera_sessions

    @property
    def executor(self) -> Executor | None:
        """The executor blocking capture work runs on (``None``: loop default)."""
        return self._executor

    async def _run_blocking(
        self, fn: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs
    ) -> _T:
        """Run a synchronous callable off the event loop, on the capture executor.

        The one seam every blocking hop in ``capture/`` goes through (the
        one-shot path reaches it through the supervisor it is given), so the
        executor is chosen in one place. See
        :func:`timelapse_manager.capture.executor.run_blocking`.
        """
        return await run_blocking(self._executor, fn, *args, **kwargs)

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

        A factory that takes a reporter (``set_reporter``, as the real
        :class:`~timelapse_manager.capture.event_listener.EventListenerFactory`
        does) is handed this supervisor, so its subscribe and downgrade reports
        reach the listener's state.
        """
        self._event_source_factory = factory
        self._attach_listener_reporter(factory)

    def _attach_listener_reporter(self, factory: object | None) -> None:
        """Give a factory that accepts one this supervisor as its reporter."""
        setter = getattr(factory, "set_reporter", None)
        if callable(setter):
            setter(self)

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
        # Captured before the first await, so a notify that lands while start is
        # still loading targets already marshals onto this loop.
        self._loop = asyncio.get_running_loop()
        try:
            targets = await self._run_blocking(self._load_targets)
        except Exception:
            # Loading capture targets must never crash service startup: the API,
            # health endpoint, and web UI need to come up so the operator can
            # diagnose (e.g. an unmigrated or temporarily unavailable database).
            # Start with no tasks rather than taking the whole process down.
            logger.exception(
                "failed to load capture targets; starting with no capture tasks"
            )
            targets = []
        # Seed camera health before any runner can report an outcome, so a
        # success right after a restart resolves an alert raised before it.
        await self._ensure_camera_health_synced()
        # Likewise the camera sessions: the first captures already know each
        # camera's version and which cameras take an exclusive (PTZ) slot.
        self._observe_camera_configs(targets)
        for target in targets:
            # Restart-survival: record any downtime gap before resuming. Best
            # effort -- a failure here must never prevent the loop from starting.
            try:
                await self._run_blocking(self._log_resume_gap, target)
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
            listener_targets = await self._run_blocking(self._load_listener_targets)
        except Exception:
            logger.exception(
                "failed to load event-listener targets; starting with no listeners"
            )
            listener_targets = []
        for listener_target in listener_targets:
            self._launch_listener(listener_target)
        self._observe_camera_configs([*targets, *listener_targets])
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

        Safe to call from any thread. Called (via
        :func:`~timelapse_manager.services._base.run_effects`) after a commit that
        changes what should be capturing, so capture follows promptly rather than
        waiting out the periodic reconcile interval. Sync request handlers run in
        FastAPI's threadpool, and :class:`asyncio.Event` is not thread-safe: set
        from another thread it would not wake a loop parked in its selector, and
        could corrupt the waiter's state. So the set is marshalled onto the
        supervisor's loop (see :meth:`_on_loop`); on that loop it runs at once.

        Before :meth:`start` it only arms the event (no loop waits on it yet; the
        first reconcile tick picks up the change). Once the loop has closed it is
        dropped: nothing is left to wake.
        """
        self._on_loop(self._reconcile_wakeup.set)

    def _on_loop(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` on the supervisor's event loop, from whichever thread calls.

        * Not started: call ``fn`` directly; no loop is waiting on it yet.
        * Already on the supervisor's loop: call ``fn`` directly (fast path).
        * Another thread: hand it to ``loop.call_soon_threadsafe``, which also
          wakes the loop's selector, so it runs on the next iteration.
        * The loop has closed (a threadpool request finishing during shutdown):
          drop it.
        """
        loop = self._loop
        if loop is None:
            fn()
            return
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            fn()
            return
        if loop.is_closed():
            return
        # The loop can close between the check above and this call.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(fn)

    def is_running(self) -> bool:
        """Return True when the supervisor is started, not stopped, and alive.

        A pure, non-raising predicate for the liveness probe: True only when
        ``start`` ran, ``stop`` has not, and the reconcile-loop task exists and
        has not finished (a crashed loop leaves a done task). The reconcile loop
        is the supervisor's long-lived heartbeat -- per-project capture tasks come
        and go, but the loop runs for the supervisor's whole life.
        """
        return (
            self._started
            and not self._stopped
            and self._reconcile_task is not None
            and not self._reconcile_task.done()
        )

    def _launch(self, target: CaptureTarget) -> None:
        """Create and register the loop task for one capture target."""
        state = CaptureState(
            project_id=target.project_id,
            camera_id=target.camera_id,
            state="running",
            started_at=self._clock.now(),
            started_mono=self._clock.monotonic(),
        )
        # The grid starts at launch: the first frame is due at once, then every
        # interval, with no alignment to the wall clock (owner decision OD-4).
        state.next_due_mono = state.started_mono
        runner = _ProjectRunner(state=state, target=target)
        self._plans.pop(target.project_id, None)
        self._degradations_reported.pop(target.project_id, None)
        runner.task = asyncio.create_task(
            self._run_project(target, state),
            name=f"capture-project-{target.project_id}",
        )
        runner.task.add_done_callback(self._on_runner_done)
        self._runners[target.project_id] = runner

    def _wedge_threshold(self) -> timedelta:
        """How stale a heartbeat may get before the runner counts as wedged.

        One cycle sleeps at most the idle ceiling and may spend up to two capture
        timeouts (PTZ + snapshot) plus database work, so allow two full cycles
        with margin before declaring it stuck.
        """
        capture = self._settings.capture
        max_idle = max(1.0, capture.max_idle_sleep_seconds)
        return (
            timedelta(seconds=2 * max_idle + 2 * capture.timeout_seconds + 60.0)
            + ANCHOR_JITTER_MAX
        )

    def _runner_problem(self, runner: _ProjectRunner) -> str | None:
        """Describe why a registered runner needs a relaunch, or ``None`` if healthy."""
        task = runner.task
        if task is None:
            return None
        if task.done():
            if task.cancelled():
                return "was cancelled unexpectedly"
            exc = task.exception()
            if exc is not None:
                return redact_text(f"had stopped with {type(exc).__name__}: {exc}")
            if runner.state.state == "stopped":
                # Normal campaign end; the project leaves the qualifying set
                # once its lifecycle change is visible, so do not relaunch yet.
                return None
            return "had exited"
        beat_mono = runner.state.last_cycle_mono
        if beat_mono is None:
            beat_mono = runner.state.started_mono
        if beat_mono is None:
            return None
        # Monotonic (#67 R13): a wall-clock step of any size neither relaunches a
        # healthy runner nor hides a stuck one. The wall time is for the message.
        idle = self._clock.monotonic() - beat_mono
        if idle > self._wedge_threshold().total_seconds():
            beat = runner.state.last_cycle_at or runner.state.started_at
            since = beat.isoformat() if beat is not None else f"{idle:.0f}s ago"
            return f"made no progress since {since} (wedged)"
        return None

    def runner_problems(self) -> list[RunnerProblem]:
        """Return every runner the reconcile tick would relaunch, with why.

        The one definition of "needs a relaunch" for both the reconcile loop and
        the ``/healthz`` readiness probe, which counts these rather than copying
        the wedge logic (#116). Read-only and non-raising, so any thread may call
        it; a runner in the ``error`` state (an offline camera) is not a problem.
        """
        problems: list[RunnerProblem] = []
        for project_id, runner in list(self._runners.items()):
            reason = self._runner_problem(runner)
            if reason is not None:
                problems.append(RunnerProblem(project_id, reason))
        return problems

    def _on_runner_done(self, task: asyncio.Task[None]) -> None:
        """Log a runner task that ends with an exception (it should never)."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "capture task %s ended with an error; the next reconcile tick "
                "relaunches it",
                task.get_name(),
                exc_info=exc,
            )

    def _launch_listener(self, target: CaptureTarget, restarts: int = 0) -> None:
        """Create and register the event-listener task for one project.

        ``restarts`` carries the relaunch count of the listener this replaces,
        so the debug endpoint shows a listener that keeps dying.
        """
        runner = _ListenerRunner(target=target)
        runner.state.restarts = restarts
        runner.task = asyncio.create_task(
            self._run_listener(target, runner.state),
            name=f"capture-listener-{target.project_id}",
        )
        runner.task.add_done_callback(partial(self._on_listener_done, runner))
        self._listeners[target.project_id] = runner

    def _on_listener_done(
        self, runner: _ListenerRunner, task: asyncio.Task[None]
    ) -> None:
        """Log a listener task that ended on its own, and delay its relaunch.

        ``_run_listener`` contains every failure of a subscription, so reaching
        here without a cancel means the task itself broke. The next reconcile
        tick relaunches it, but not before a backoff that grows with each such
        death, so a listener that dies at once does not spin (#67-R9).
        """
        if task.cancelled():
            return
        exc = task.exception()
        cfg = self._settings.capture
        delay = _backoff_delay(
            runner.state.restarts + 1,
            base=cfg.backoff_base_seconds,
            maximum=cfg.backoff_max_seconds,
            jitter_fraction=0.0,
            rng=random.Random(runner.target.project_id),
        )
        runner.relaunch_not_before = self._clock.monotonic() + max(
            delay, _LISTENER_MIN_RESUBSCRIBE_SECONDS
        )
        logger.error(
            "event listener task %s ended%s; the next reconcile tick after the "
            "backoff relaunches it",
            task.get_name(),
            " with an error" if exc is not None else " without being cancelled",
            exc_info=exc,
        )

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
                self.last_reconcile_at = datetime.now(UTC)
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

        Passes are serialized by ``_reconcile_lock`` so overlapping calls cannot
        double-launch a project.
        """
        async with self._reconcile_lock:
            await self._reconcile_once_locked()

    async def _reconcile_once_locked(self) -> None:
        """Run one reconcile pass; the caller holds ``_reconcile_lock``."""
        targets = await self._run_blocking(self._load_targets)
        # A no-op once done; retries here only when start() could not seed.
        await self._ensure_camera_health_synced()
        qualifying = {target.project_id: target for target in targets}

        # Health pass (#6): a runner whose task ended while its project still
        # qualifies, or whose heartbeat is stale, is stopped here and relaunched
        # by the launch pass below.
        restarts: dict[int, int] = {}
        for project_id in list(self._runners):
            if project_id not in qualifying:
                continue
            problem = self._runner_problem(self._runners[project_id])
            if problem is None:
                continue
            restarts[project_id] = self._runners[project_id].state.restarts + 1
            logger.error(
                "capture runner for project=%s %s; relaunching", project_id, problem
            )
            await self._stop_runner(project_id)
            try:
                await self._run_blocking(
                    self._write_event,
                    scope_id=project_id,
                    level="error",
                    message=(
                        f"capture runner for project "
                        f"{qualifying[project_id].project_name!r} {problem}; "
                        "relaunched"
                    ),
                    metadata={"reason": "runner_relaunch"},
                )
            except Exception:  # noqa: BLE001 - reporting must not block relaunch
                logger.exception("failed to record runner relaunch event")

        for project_id, target in qualifying.items():
            if project_id not in self._runners:
                # Mirror start()'s restart-survival gap logging; for a brand-new
                # project this is a no-op (no prior frames). Best effort -- a
                # failure here must never prevent the launch.
                try:
                    await self._run_blocking(self._log_resume_gap, target)
                except Exception:
                    logger.exception("failed to log resume gap project=%s", project_id)
                logger.info("reconcile: launching capture project=%s", project_id)
                self._launch(target)
                if project_id in restarts:
                    self._runners[project_id].state.restarts = restarts[project_id]

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

        listener_targets = await self._reconcile_listeners()
        self._observe_camera_configs([*targets, *listener_targets])
        await self._evict_unused_pinned_clients()

    async def _evict_unused_pinned_clients(self) -> None:
        """Drop cached pinned clients no camera is pinned to any more (#233).

        A re-pin or an unpin wakes reconcile, so the old certificate's client
        leaves the cache within one pass instead of living until shutdown. Never
        raises: a failed sweep must not fail the reconcile pass.
        """
        try:
            pins = await self._run_blocking(self._load_pinned_certs)
            self._clients.retain_pinned(pins)
        except Exception:  # noqa: BLE001 - a sweep must never fail reconcile
            logger.exception("pinned-client eviction failed; will retry next pass")

    def _load_pinned_certs(self) -> list[str]:
        """Read every certificate any camera is pinned to. Synchronous (a thread)."""
        with session_scope(self._session_factory) as session:
            rows = session.execute(
                select(Camera.tls_pinned_cert_pem)
                .where(Camera.tls_pinned_cert_pem.is_not(None))
                .distinct()
            ).scalars()
            return [str(pem) for pem in rows if pem]

    def _observe_camera_configs(self, targets: Iterable[CaptureTarget]) -> None:
        """Report the reconcile read's camera versions and PTZ use to the sessions.

        Drops any cached camera session whose configuration changed (or whose
        camera is no longer in use), and records which cameras have a project
        with a PTZ position, which makes their capture slot exclusive.
        """
        versions: dict[int, str | None] = {}
        ptz_cameras: set[int] = set()
        for target in targets:
            versions[target.camera_id] = target.camera_version
            if ptz_position_of(target) is not None:
                ptz_cameras.add(target.camera_id)
        self._camera_sessions.observe(versions)
        self._camera_sessions.set_ptz_cameras(ptz_cameras)

    async def _reconcile_listeners(self) -> list[CaptureTarget]:
        """Converge the event-listener registry to its own qualifying set.

        Symmetric to the runner reconcile but keyed on the listener qualifying
        set (active + protocol + an enabled event trigger) and the
        listener-affecting fields (camera, triggers): launch a newly-qualifying
        project's listener, stop one no longer qualifying (deactivated, deleted,
        all triggers removed/disabled, campaign ended), and restart one whose
        camera or trigger configuration changed. Because the qualifying set is
        read independently of the interval/anchor runners, a listener may exist
        for a project that has no capture runner and vice versa. Returns the
        qualifying targets it read.
        """
        targets = await self._run_blocking(self._load_listener_targets)
        qualifying = {target.project_id: target for target in targets}

        # Health pass (#67-R9): a listener whose task ended while its project
        # still qualifies is dropped here and relaunched by the launch pass
        # below, once its backoff has passed.
        restarts: dict[int, int] = {}
        for project_id in list(self._listeners):
            if project_id not in qualifying:
                continue
            runner = self._listeners[project_id]
            if runner.task is None or not runner.task.done():
                continue
            not_before = runner.relaunch_not_before
            if not_before is not None and self._clock.monotonic() < not_before:
                continue
            restarts[project_id] = runner.state.restarts + 1
            logger.error(
                "event listener for project=%s had exited; relaunching", project_id
            )
            await self._stop_listener(project_id)
            try:
                await self._run_blocking(
                    self._write_event,
                    scope_id=project_id,
                    level="error",
                    message=(
                        f"event listener for project "
                        f"{qualifying[project_id].project_name!r} had exited; "
                        "relaunched"
                    ),
                    metadata={"reason": "listener_relaunch"},
                )
            except Exception:  # noqa: BLE001 - reporting must not block relaunch
                logger.exception("failed to record listener relaunch event")

        for project_id, target in qualifying.items():
            if project_id not in self._listeners:
                logger.info(
                    "reconcile: launching event listener project=%s", project_id
                )
                self._launch_listener(target, restarts.get(project_id, 0))

        stale = [pid for pid in self._listeners if pid not in qualifying]
        for project_id in stale:
            logger.info("reconcile: stopping event listener project=%s", project_id)
            await self._stop_listener(project_id, retire=True)

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
        return targets

    async def _stop_runner(self, project_id: int) -> None:
        """Cancel and await one project's task, then drop it from the registry.

        Awaits the cancelled task so nothing leaks onto the loop before the
        runner is removed, ignoring the runner's ``CancelledError`` but not the
        caller's own (see :func:`_cancel_and_wait`). Safe if the task is already
        finished or absent.
        """
        runner = self._runners.pop(project_id, None)
        self._plans.pop(project_id, None)
        if runner is None:
            return
        runner.state.state = "stopped"
        task = runner.task
        if task is not None and not task.done():
            await _cancel_and_wait(task)

    async def _stop_listener(self, project_id: int, *, retire: bool = False) -> None:
        """Cancel and await one project's listener, then drop it from the registry.

        ``retire`` is set when the project no longer has a listener at all: the
        camera's ``events_unavailable`` alert is then resolved if this was the
        last listener that could (a restart or relaunch leaves it to the new
        one).

        Mirrors :meth:`_stop_runner`: awaits the cancelled task (via
        :func:`_cancel_and_wait`, so the caller's own cancellation still
        propagates) so nothing leaks onto the loop before the listener is
        removed. The listener's own teardown (unsubscribe, etc.) runs as its
        cancellation unwinds. Safe if the task is already finished or absent.
        """
        runner = self._listeners.pop(project_id, None)
        if runner is None:
            return
        task = runner.task
        if task is not None and not task.done():
            await _cancel_and_wait(task)
        camera_id = runner.target.camera_id
        self._camera_health.forget_listener(camera_id, project_id)
        if retire and not any(
            other.target.camera_id == camera_id for other in self._listeners.values()
        ):
            transition = self._camera_health.resolve_events(camera_id)
            if transition is not None:
                await self._emit_camera_health_transition(transition, None)

    async def stop(self, budget: float = _STOP_BUDGET_SECONDS) -> None:
        """Cancel every capture task and release the shared HTTP client.

        Cancels all tasks, awaits them for at most ``budget`` seconds in all,
        then closes the HTTP client. A task still running when the budget is
        spent is named in the log and left cancelled: the shutdown goes on
        rather than hang on one wedged listener or capture (#69 CAM-R15). The
        owner's shutdown sequence calls this with the default, which leaves
        time for the rest of it. Idempotent and safe to call when nothing was
        started.
        """
        if self._stopped:
            return
        self._stopped = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget

        def remaining() -> float:
            return max(0.0, deadline - loop.time())

        pending_names: list[str] = []

        async def wait_for_tasks(tasks: list[asyncio.Task[None]]) -> None:
            # ``asyncio.wait`` rather than ``_cancel_and_wait``: stop() runs in
            # the app lifespan's ``finally`` and so may itself be executing
            # during a cancellation (``cancelling() > 0``). It must still finish
            # the whole teardown then, and ``asyncio.wait`` raises only on a
            # *new* cancel.
            if not tasks:
                return
            _, still_pending = await asyncio.wait(tasks, timeout=remaining())
            pending_names.extend(task.get_name() for task in still_pending)

        # Tear down the reconcile loop first so it cannot launch a new runner or
        # listener while we are cancelling the existing ones.
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            await wait_for_tasks([self._reconcile_task])
            self._reconcile_task = None

        # Tear down event listeners *before* the capture runners: a listener
        # could otherwise fire a one-shot capture mid-shutdown. Both are torn down
        # before the shared HTTP client is closed, since both borrow it.
        listener_tasks = [
            r.task for r in self._listeners.values() if r.task is not None
        ]
        for task in listener_tasks:
            task.cancel()
        await wait_for_tasks(listener_tasks)

        tasks = [r.task for r in self._runners.values() if r.task is not None]
        for task in tasks:
            task.cancel()
        await wait_for_tasks(tasks)
        for task in tasks:
            if not task.done() or task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                # A runner that had already died must not vanish silently.
                logger.error(
                    "capture task %s had ended with an error: %r", task.get_name(), exc
                )
        for runner in self._runners.values():
            runner.state.state = "stopped"

        if pending_names:
            logger.error(
                "capture supervisor stop: %d task(s) still running after the "
                "%.1fs budget: %s",
                len(pending_names),
                budget,
                ", ".join(sorted(pending_names)),
            )

        # Closed last so neither a listener nor a runner is mid-request when the
        # transport disappears. All three camera clients close even if one fails.
        await self._clients.aclose()
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
            # Heartbeat (#6): stamped at the top of every iteration so the
            # reconcile tick can tell a live runner from a wedged one.
            state.last_cycle_at = self._clock.now()
            state.last_cycle_mono = self._clock.monotonic()
            try:
                sleep_for = await self._run_cycle(
                    target,
                    state,
                    interval=interval,
                    max_idle=max_idle,
                    rng=rng,
                    schedule=schedule,
                    volume_path=volume_path,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad cycle must not end the loop
                # Containment (#6): before 0.1.1 an exception here (e.g. SQLite
                # "database is locked" while claiming an anchor) ended the task
                # silently -- no fire-log row, no log line, status still
                # "running". Log it, surface it, and keep the runner alive.
                sleep_for = await self._contain_cycle_error(
                    target, state, exc, max_idle
                )
            if sleep_for is None:
                return
            await self._clock.sleep(sleep_for)

    async def _run_cycle(
        self,
        target: CaptureTarget,
        state: CaptureState,
        *,
        interval: float | None,
        max_idle: float,
        rng: random.Random,
        schedule: Schedule,
        volume_path: Path,
    ) -> float | None:
        """Run one loop cycle; return the sleep before the next, or ``None`` to stop.

        Evaluate the schedule, disk and start-date gates, fire any due exact-time
        anchors, then either capture or wait. ``None`` means the campaign ended
        (frame cap reached) and the runner should exit.
        """
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
            disk_ok = await self._evaluate_disk_gate(target, state, volume_path, now)

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
        if next_anchor_wake is not None:
            # De-synchronise projects sharing an anchor (#6): each project wakes
            # a fixed, project-seeded 0..ANCHOR_JITTER_MAX after the instant, so
            # ten runners do not claim, hit SQLite and the cameras in the same
            # millisecond. Far inside the anchor grace window.
            next_anchor_wake += _anchor_jitter(target.project_id)

        # No-overshoot guard: never *start* a capture once the project is at or
        # over its frame cap (e.g. frames added by another path). The
        # authoritative stop is the post-write check below; this just avoids a
        # wasted attempt and an off-by-one if the count moved underneath us.
        # The cap read is a database query, so it runs off the loop (#63); the
        # ``is not None`` check comes first so an uncapped project pays no hop.
        # The cap is the live one (#104): the last frame write reported it, so
        # an edit while the loop runs applies from the next cycle.
        frame_cap = _live_frame_cap(target, state)
        if (
            is_open
            and frame_cap is not None
            and await self._run_blocking(self._frame_cap_reached, target)
        ):
            await self._run_blocking(
                self._end_campaign,
                target.project_id,
                target.project_name,
                _CAMPAIGN_END_FRAME_COUNT,
            )
            state.state = "stopped"
            return None

        decision = _plan_next(
            now,
            is_open=is_open,
            next_change=next_change,
            interval=interval,
            max_idle_sleep=max_idle,
            now_mono=self._clock.monotonic(),
            next_due_mono=state.next_due_mono,
            next_retry_mono=state.next_retry_mono,
            next_wake=next_anchor_wake,
        )
        self._plans[target.project_id] = RunnerPlan(
            planned_at=now,
            action=decision.action,
            sleep_seconds=decision.sleep_seconds,
            schedule_open=schedule_open,
            disk_ok=disk_ok,
            next_change=next_change,
            next_anchor_wake=next_anchor_wake,
            next_retry_at=decision.next_retry_at,
        )
        # Guarded so the formatting costs nothing at INFO; raised at runtime via
        # the debug log-level endpoint (#7).
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "runner cycle project=%s open=%s disk_ok=%s next_change=%s "
                "next_anchor_wake=%s decision=%s sleep=%.1f",
                target.project_id,
                schedule_open,
                disk_ok,
                next_change.isoformat() if next_change else None,
                next_anchor_wake.isoformat() if next_anchor_wake else None,
                decision.action,
                decision.sleep_seconds,
            )

        if decision.action == "capture":
            frame_count = await self._attempt_capture(target, state, rng)
            # End the campaign at exactly the cap: the write already happened,
            # so stopping here (rather than before the next attempt) means no
            # overshoot. ``_end_campaign`` is idempotent, so racing the
            # reconcile enforcer is safe.
            frame_cap = _live_frame_cap(target, state)
            if (
                frame_count is not None
                and frame_cap is not None
                and frame_count >= frame_cap
            ):
                await self._run_blocking(
                    self._end_campaign,
                    target.project_id,
                    target.project_name,
                    _CAMPAIGN_END_FRAME_COUNT,
                )
                state.state = "stopped"
                return None
        else:
            self._mark_waiting(state, schedule_open=schedule_open, disk_ok=disk_ok)

        return decision.sleep_seconds

    async def _contain_cycle_error(
        self,
        target: CaptureTarget,
        state: CaptureState,
        exc: Exception,
        max_idle: float,
    ) -> float:
        """Record an unexpected cycle failure and return a short retry sleep."""
        logger.exception(
            "capture loop cycle failed project=%s; the runner continues",
            target.project_id,
        )
        message = redact_text(f"{type(exc).__name__}: {exc}")[:500]
        first_of_streak = state.last_error != message
        state.last_error = message
        state.last_error_at = self._clock.now()
        if first_of_streak:
            # One ERROR event per distinct failure, not one per retry cycle.
            try:
                await self._run_blocking(
                    self._write_event,
                    scope_id=target.project_id,
                    level="error",
                    message=(
                        f"capture loop error for project {target.project_name!r}: "
                        f"{message}; retrying"
                    ),
                    metadata={"reason": "runner_error"},
                )
            except Exception:  # noqa: BLE001 - reporting must not raise
                logger.exception(
                    "failed to record runner error event project=%s",
                    target.project_id,
                )
        return min(_CYCLE_ERROR_RETRY_SECONDS, max_idle)

    async def _run_listener(
        self, target: CaptureTarget, state: ListenerState | None = None
    ) -> None:
        """Consume a project's camera events and fire one-shot captures, supervised.

        The supervised side of event-triggered capture, parallel to
        :meth:`_run_project`: obtain the project's event source from the injected
        factory; consume its events; match each against the configured triggers,
        keep only rising edges, debounce (per-trigger cooldown) and queue a
        one-shot :func:`~timelapse_manager.capture.one_shot.capture_one_now` for
        a match. A dropped subscription or transport failure backs off (the same
        capped exponential + jitter the capture runner uses) and re-subscribes;
        ``CancelledError`` propagates for clean teardown.

        The lifecycle (registry, reconcile, teardown-first shutdown) is wired
        here; the event *mechanism* is pluggable via ``event_source_factory``.
        When no factory is configured -- or it returns no source for this
        project -- the listener parks cancellably forever rather than returning:
        a body that returned immediately inside a restart loop would be an
        infinite hot loop. Parking keeps the task alive and observable in the
        registry until reconcile or :meth:`stop` cancels it.

        **Pacing.** Subscriptions are at least
        ``max(5 s, 2 x backoff_base_seconds)`` apart, however the last ended. The
        backoff starts over only after a subscription lived
        :data:`_LISTENER_HEALTHY_SECONDS`, so a camera that accepts and drops a
        subscription in a loop backs off like one that refuses it.

        **State that outlives a subscription**, owned here: the per-topic
        cooldown (``last_fired``), the rising-edge tracker and the capture queue
        with its consumer task. Persisting them means a camera that flaps does
        not fire a fresh capture on every reconnect, and a capture already queued
        survives its subscription. A reconcile restart spins up a new task and so
        resets them, which is correct -- that path only runs when the camera or
        trigger configuration actually changed.

        A failure before the subscription was established counts towards the
        camera's ``events_unavailable`` alert (see :class:`CameraHealthBook`); a
        refused login also drops the camera's cached session, so the next
        subscribe reads fresh credentials, and saturates the backoff (#162).
        """
        if state is None:
            state = ListenerState()
        self._listener_states[target.project_id] = state
        cfg = self._settings.capture
        rng = random.Random(target.project_id)
        min_gap = max(_LISTENER_MIN_RESUBSCRIBE_SECONDS, 2.0 * cfg.backoff_base_seconds)
        attempt = 0
        last_fired: dict[str, float] = {}
        tracker = event_triggers_mod.EdgeTracker()
        queue = _EventCaptureQueue(
            maxsize=_LISTENER_QUEUE_SIZE,
            on_drop=partial(self._note_event_dropped, target, state),
        )
        queue.start(f"capture-listener-queue-{target.project_id}")
        try:
            while True:
                started = self._clock.monotonic()
                state.last_progress_mono = started
                state.attempt_subscribed = False
                error: Exception | None = None
                try:
                    source = (
                        self._event_source_factory(target)
                        if self._event_source_factory is not None
                        else None
                    )
                    if source is None:
                        # No event mechanism connected for this project: park
                        # until cancelled (the registry/reconcile/teardown
                        # lifecycle is what is under test here, not a live
                        # subscription).
                        await asyncio.Event().wait()
                        return
                    await self._consume_event_source(
                        target,
                        source,
                        last_fired,
                        tracker=tracker,
                        dispatcher=queue,
                        state=state,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one bad camera must not stall others
                    error = exc
                state.connected_since = None
                lived = self._clock.monotonic() - started
                if lived >= _LISTENER_HEALTHY_SECONDS:
                    attempt = 0
                if error is not None or lived < _LISTENER_HEALTHY_SECONDS:
                    attempt += 1
                auth_failed = error is not None and classify_failure(error) == "auth"
                delay = max(
                    min_gap,
                    _backoff_delay(
                        attempt,
                        base=cfg.backoff_base_seconds,
                        maximum=cfg.backoff_max_seconds,
                        jitter_fraction=cfg.backoff_jitter_fraction,
                        rng=rng,
                        saturate=auth_failed,
                    ),
                )
                if error is not None:
                    await self._note_listener_failure(target, state, error, delay)
                else:
                    logger.info(
                        "event source ended project=%s; re-subscribing in %.1fs",
                        target.project_id,
                        delay,
                    )
                await self._clock.sleep(delay)
        finally:
            if self._listener_states.get(target.project_id) is state:
                del self._listener_states[target.project_id]
            await queue.aclose()

    async def _note_listener_failure(
        self,
        target: CaptureTarget,
        state: ListenerState,
        exc: Exception,
        delay: float,
    ) -> None:
        """Record a failed subscription: log it, keep it, feed the listener alert.

        A refused login (#162) drops the camera's cached session -- the stored
        credentials may be stale, and captures reload them the same way. It does
        *not* feed the camera's offline health: the camera may capture
        perfectly well (a VAPIX camera whose ONVIF user is not provisioned).
        """
        text = redact_text(str(exc)) or type(exc).__name__
        state.last_error = text
        kind = classify_failure(exc)
        if kind == "auth":
            self._camera_sessions.invalidate(target.camera_id)
        logger.warning(
            "event listener failed project=%s; retrying in %.1fs: %s",
            target.project_id,
            delay,
            text,
        )
        if state.attempt_subscribed:
            # It had subscribed: a drop, not a failure to subscribe.
            return
        state.consecutive_failures += 1
        if kind == "local":
            return
        transition = self._camera_health.record_listener_failure(
            target.camera_id, target.project_id, error=text
        )
        if transition is not None:
            await self._emit_camera_health_transition(transition, None)

    def _note_event_dropped(self, target: CaptureTarget, state: ListenerState) -> None:
        """Count a matched event dropped from a full capture queue."""
        state.dropped_events += 1
        logger.warning(
            "event capture queue full project=%s; dropped the oldest queued "
            "capture (%d dropped so far)",
            target.project_id,
            state.dropped_events,
        )

    async def listener_subscribed(self, target: CaptureTarget, transport: str) -> None:
        """Record that a listener's subscription is established (factory report).

        Called from inside the listener's own task by the event-source factory
        (through ``set_reporter``) once the camera has accepted the
        subscription, so "subscribed and quiet" is told apart from "never
        subscribed". Clears the failure streak and, if the camera's
        ``events_unavailable`` alert stands and no other listener on it is
        failing, resolves it.
        """
        state = self._listener_states.get(target.project_id)
        if state is None:
            return
        state.transport = transport
        if transport == TRANSPORT_VAPIX_WEBSOCKET:
            state.downgrade_reported = False
        await self._mark_subscribed(target, state)

    async def listener_downgraded(
        self,
        target: CaptureTarget,
        *,
        from_transport: str,
        to_transport: str,
        reason: str,
    ) -> None:
        """Record that a listener fell back to a slower transport (factory report).

        One warning event per downgrade: while the fast transport keeps failing
        and the listener keeps falling back, only the first is reported, and the
        next report comes after the fast transport has worked again.
        """
        state = self._listener_states.get(target.project_id)
        if state is None:
            return
        state.transport = to_transport
        state.downgrades += 1
        if state.downgrade_reported:
            return
        state.downgrade_reported = True
        text = redact_text(reason)
        logger.warning(
            "event listener project=%s fell back from %s to %s: %s",
            target.project_id,
            from_transport,
            to_transport,
            text,
        )
        try:
            await self._run_blocking(
                self._write_event,
                scope_id=target.project_id,
                level="warning",
                message=(
                    f"event listener for project {target.project_name!r} fell "
                    f"back from {from_transport} to {to_transport}: {text}"
                ),
                metadata={
                    "reason": "listener_transport_downgrade",
                    "camera_id": target.camera_id,
                    "from_transport": from_transport,
                    "to_transport": to_transport,
                },
            )
        except Exception:  # noqa: BLE001 - reporting must never fail the listener
            logger.exception("failed to record listener downgrade event")

    async def _mark_subscribed(
        self, target: CaptureTarget, state: ListenerState
    ) -> None:
        """Mark the current attempt subscribed (once) and clear its failure streak."""
        if state.attempt_subscribed:
            return
        state.attempt_subscribed = True
        state.consecutive_failures = 0
        state.connected_since = self._clock.now()
        state.last_progress_mono = self._clock.monotonic()
        transition = self._camera_health.record_listener_success(
            target.camera_id, target.project_id
        )
        if transition is not None:
            await self._emit_camera_health_transition(transition, None)

    async def _consume_event_source(
        self,
        target: CaptureTarget,
        source: AsyncIterator[Any],
        last_fired: dict[str, float] | None = None,
        *,
        tracker: event_triggers_mod.EdgeTracker | None = None,
        dispatcher: _EventCaptureQueue | None = None,
        state: ListenerState | None = None,
    ) -> None:
        """Consume one subscription's events, matching and queueing captures.

        Each event delivered by ``source`` goes through three gates before it
        causes a capture:

        1. **A trigger watches its topic**
           (:func:`~timelapse_manager.capture.event_triggers.trigger_for_topic`).
        2. **It is a rising edge**
           (:class:`~timelapse_manager.capture.event_triggers.EdgeTracker`):
           a repeat of a state already active, a falling edge and an ONVIF
           ``Initialized`` snapshot are not. The tracker sees the falling edges
           too, which is how it knows the next assertion is new.
        3. **Debounce**: a per-topic cooldown (the trigger's ``cooldown_seconds``)
           on the supervisor's monotonic clock, so a wall-clock adjustment cannot
           collapse or stretch it. The cooldown is *attempt*-based:
           ``last_fired`` is stamped when a capture is queued, before the
           (possibly slow or failing) capture runs, so a failing camera or a
           low-disk skip during an event storm cannot defeat it.

        A capture that passes goes on the listener's queue and a consumer runs it
        via :func:`~timelapse_manager.capture.one_shot.capture_one_now`, so a slow
        capture never stops ``source`` being read. ``last_fired``, ``tracker`` and
        ``dispatcher`` are supplied by :meth:`_run_listener` and *persist across
        re-subscribes*; called directly (tests) they default to fresh ones, and
        the queue is then drained before this returns when the source ends.

        ``capture_one_now`` writes its own audit event -- for a success, a
        low-disk skip and (since #69) a failure -- and enforces the disk gate; any
        failure of one capture is logged and swallowed by the consumer so a
        single bad capture does not tear down the subscription. ``CancelledError``
        propagates for clean teardown.

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

        if last_fired is None:
            last_fired = {}
        if tracker is None:
            tracker = event_triggers_mod.EdgeTracker()
        if state is None:
            state = self._listener_states.get(target.project_id) or ListenerState()
        own_queue = dispatcher is None
        queue = dispatcher
        if queue is None:
            queue = _EventCaptureQueue(
                maxsize=_LISTENER_QUEUE_SIZE,
                on_drop=partial(self._note_event_dropped, target, state),
            )
            queue.start(f"capture-listener-queue-{target.project_id}")
        try:
            async for event in source:
                # A delivered event is proof of a subscription, for a source that
                # reports nothing else (a factory without a reporter).
                await self._mark_subscribed(target, state)
                now_mono = self._clock.monotonic()
                state.last_event_at = self._clock.now()
                state.last_progress_mono = now_mono
                trigger = event_triggers_mod.trigger_for_topic(event, triggers)
                if trigger is None:
                    continue
                if not tracker.should_fire(event, trigger, now_mono):
                    continue

                topic = event.topic_id
                previous = last_fired.get(topic)
                if (
                    previous is not None
                    and (now_mono - previous) < trigger.cooldown_seconds
                ):
                    # Still within this topic's cooldown: suppress the capture.
                    continue

                # Stamp before the attempt so the cooldown governs attempt
                # cadence even when the capture is slow, fails, or is skipped
                # for low disk.
                last_fired[topic] = now_mono
                queue.offer(partial(self._fire_event_capture, target, event, trigger))
            if own_queue:
                await queue.drain()
        finally:
            if own_queue:
                await queue.aclose()

    async def _fire_event_capture(
        self,
        target: CaptureTarget,
        event: Any,
        trigger: event_triggers_mod.EventTrigger,
    ) -> None:
        """Run one queued event-triggered capture and feed camera health.

        Never raises (bar cancellation): a :class:`CaptureError` is logged and
        reported to camera health, and any other failure of one capture is logged
        and dropped, so neither tears down the subscription or the consumer.
        ``capture_one_now`` already wrote the failed attempt's warning event.
        """
        try:
            written = await capture_one_now(
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
            logger.warning(
                "event-triggered capture failed project=%s topic=%s: %s",
                target.project_id,
                event.topic_id,
                redact_text(str(exc)),
            )
            await self._report_camera_outcome(target, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one capture must not stop the listener
            logger.exception(
                "event-triggered capture raised project=%s topic=%s: %s",
                target.project_id,
                event.topic_id,
                redact_text(str(exc)),
            )
        else:
            if written is not None:
                # A low-disk skip (None) says nothing about the camera.
                await self._report_camera_outcome(target, None)

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
        # An instant before the project existed, or before it was last resumed or
        # activated, was never expected: it is not a miss, so it gets no row and
        # no alert (#280). A miss while the project was active is untouched.
        floor = target.active_since
        if floor is not None:
            decisions = [
                d for d in decisions if d.instant is None or d.instant >= floor
            ]
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "anchor evaluation project=%s anchors=%d due=%d",
                target.project_id,
                len(anchors),
                len(decisions),
            )
        # The capture attempt itself is bounded inside the fire by
        # ``_anchor_fire_timeout`` and handled there as a failed attempt (#69).
        # This outer guard is a safety net for a hung database hop around it,
        # so it allows the bookkeeping one more margin.
        guard_timeout = self._anchor_fire_timeout() + _ANCHOR_FIRE_MARGIN_SECONDS
        retry_wakes: list[datetime] = []
        for decision in decisions:
            # ``tz`` is the schedule timezone that governs clock anchors; it is
            # passed through so the fire site can detect (and log) a clock anchor
            # whose wall-clock time was shifted forward out of a DST gap.
            # Bounded (#6): a fire that hangs (database) must not hold the
            # runner -- the claimed row stays at its provisional ``failed``
            # status (at-most-once) and the loop moves on.
            try:
                async with asyncio.timeout(guard_timeout):
                    retry_at = await self._fire_anchor_decision(
                        target, decision, tz, now=now
                    )
            except TimeoutError:
                logger.error(
                    "exact-time anchor fire timed out after %.0fs project=%s anchor=%s",
                    guard_timeout,
                    target.project_id,
                    decision.anchor.id,
                )
                continue
            if retry_at is not None:
                retry_wakes.append(retry_at)

        # Fold any pending retry (scheduled or found pending this cycle) into the
        # wake, so an anchor-only runner sleeps no longer than the next attempt.
        wake = anchors_mod.next_anchor_wake(
            anchors, now, tz, lat, lon, solar_tz=solar_tz
        )
        if retry_wakes:
            soonest_retry = min(retry_wakes)
            wake = soonest_retry if wake is None else min(wake, soonest_retry)
        return wake

    def _anchor_fire_timeout(self) -> float:
        """Seconds one anchor capture attempt may take before it counts as failed.

        Covers the wait for a capture slot on the camera, the camera-config hop,
        PTZ, the capture budget, scene metadata and the frame write (#6, #69
        CAM-R4): ``slot_wait + timeout + 60 s``, 120 s at the default 10 s
        timeout. It must stay above one slot wait plus one capture, or the
        fourth of four anchors queued on one camera would time out.
        """
        return (
            self._slot_wait_seconds()
            + self._settings.capture.timeout_seconds
            + _ANCHOR_FIRE_MARGIN_SECONDS
        )

    def _slot_wait_seconds(self) -> float:
        """The longest a capture waits for a free slot on its camera."""
        return 2 * self._settings.capture.timeout_seconds + _SLOT_WAIT_MARGIN_SECONDS

    def capture_slot(
        self, target: CaptureTarget, *, wait_seconds: float | None = None
    ) -> contextlib.AbstractAsyncContextManager[None]:
        """Hold a capture slot on ``target``'s camera (bounded wait, #69 CAM-R4).

        Taken before, and outside, the capture budget, and held through PTZ,
        the capture and the scene-metadata read; released before the frame is
        written. Exclusive when the target or any project on its camera has a
        PTZ position. A capture that cannot get a slot in time fails as a local
        "camera busy" error, which camera health ignores.

        :param wait_seconds: the longest to wait for a free slot; ``None`` is
            the scheduled-capture wait (:meth:`_slot_wait_seconds`). ``0``
            takes a slot only when one is free now and never queues -- the API
            manual capture's mode, so it can never wait in line ahead of an
            anchor.
        """
        return self._camera_sessions.capture_slot(
            target.camera_id,
            exclusive=ptz_position_of(target) is not None,
            wait_seconds=(
                self._slot_wait_seconds() if wait_seconds is None else wait_seconds
            ),
        )

    async def _fire_anchor_decision(
        self,
        target: CaptureTarget,
        decision: anchors_mod.AnchorDecision,
        tz: ZoneInfo,
        *,
        now: datetime | None = None,
    ) -> datetime | None:
        """Act on one due-anchor decision: claim the fire-log row, then capture.

        The durable fire-log row is the once-per-day idempotency guard. We claim
        it *first* (a committed insert); the unique constraint on
        ``(project, anchor, local_date)`` arbitrates races -- a duplicate insert
        means another worker or a prior run already handled this anchor today.
        Only after a successful claim do we capture (within grace, with
        geolocation) and update the row's outcome. Claiming before capturing
        means a crash mid-capture leaves a decision row at its provisional
        ``failed`` status, so the anchor is not re-fired (at-most-once).

        A failed attempt inside the grace window is retried (#69, CAM-R1): the
        row is parked at ``retry_pending`` with ``next_attempt_at``, and a later
        cycle whose claim hits the existing row reclaims it with a conditional
        UPDATE (in the same hop), so exactly one worker makes each attempt. The
        last failure -- out of attempts, or out of grace -- is final: the row
        stays ``failed`` and an ``anchor_failed`` warning event raises an alert.

        ``tz`` is the schedule timezone governing clock anchors; it is used only
        to log, once per fire, a clock anchor whose configured wall-clock time
        was deliberately shifted forward out of a daylight-saving spring-forward
        gap (see :func:`anchors.anchor_fire_instant`). ``now`` is the cycle's
        instant (the clock's, by default).

        :returns: the instant a retry of this anchor is due, when one is pending,
            so the caller folds it into the runner's wake; otherwise ``None``.
        """
        anchor = decision.anchor
        if now is None:
            now = self._clock.now()

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

        claim = await self._run_blocking(
            self._claim_anchor_attempt,
            target.project_id,
            target.project_name,
            anchor.id,
            anchor.kind,
            decision.local_date,
            status,
            detail,
            within_grace=decision.within_grace,
            now=now,
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "anchor decision project=%s anchor=%s local_date=%s within_grace=%s "
                "has_geo=%s status=%s claim=%s",
                target.project_id,
                anchor.id,
                decision.local_date,
                decision.within_grace,
                decision.has_geo,
                status,
                _claim_label(claim),
            )
        if claim.outcome == "pending":
            return claim.next_attempt_at
        if claim.outcome == "finalized":
            # A retry was still pending when the grace window closed (the app
            # was down, or the runner was late): the helper finalised the row
            # and wrote the alert in the same transaction.
            self._note_anchor_failure(target, claim.detail)
            logger.warning(
                "exact-time anchor failed project=%s anchor=%s after %d attempt(s); "
                "grace window closed with a retry pending",
                target.project_id,
                anchor.id,
                claim.attempts,
            )
            return None
        if claim.outcome == "done":
            # Already captured, failed or skipped for this anchor + local day, or
            # another worker holds the attempt. Idempotent: do nothing further.
            return None

        # The first claim is the once-per-day guard, so logging the deliberate
        # DST shift here -- only on attempt 1 -- emits exactly one line per
        # shifted fire rather than one per loop cycle or retry within grace.
        if claim.attempts == 1 and anchors_mod.clock_anchor_in_dst_gap(
            anchor, date.fromisoformat(decision.local_date), tz
        ):
            logger.info(
                "exact-time clock anchor %s for project=%s had a wall-clock time "
                "(%s) that does not exist on %s due to a daylight-saving "
                "spring-forward gap; shifted forward to the first valid instant "
                "after the gap (%s)",
                anchor.id,
                target.project_id,
                anchor.time,
                decision.local_date,
                decision.instant.isoformat() if decision.instant else "unknown",
            )

        if status != "failed":
            # A skip decision (no geo / missed): the committed claim row and its
            # event are the whole outcome.
            level = "warning"
            message = (
                f"exact-time anchor skipped for project {target.project_name!r} "
                f"({anchor.kind} {decision.local_date}): {detail}"
            )
            await self._run_blocking(
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
            return None

        return await self._attempt_anchor_capture(target, decision, claim.attempts, now)

    async def _attempt_anchor_capture(
        self,
        target: CaptureTarget,
        decision: anchors_mod.AnchorDecision,
        attempts: int,
        now: datetime,
    ) -> datetime | None:
        """Make capture attempt number ``attempts`` for a claimed anchor.

        ``capture_one_now`` honours the universal disk gate (a low-disk skip
        returns ``None``) and writes the success event. The fire-log row is
        marked ``captured`` inside the frame-write hop itself (``on_written``),
        so a cancellation landing after the write cannot leave the row looking
        failed and get the frame captured twice.

        Any failure -- a :class:`CaptureError`, the attempt's own timeout, or
        another error such as a camera deleted mid-flight -- is passed to
        :meth:`_handle_anchor_failure`, outside the attempt's timeout scope.

        Each attempt's outcome also feeds the camera's health exactly once
        (#69 CAM-R1 item 3, CAM-R6), so an anchor-only camera can raise
        ``camera_offline``: a written frame is a success, a failure is reported
        *after* the retry is scheduled or the anchor finalised (a health event
        that cannot be written never costs a retry). A low-disk skip is neither.
        ``capture_one_now`` itself reports nothing, so nothing is counted twice.

        :returns: the retry instant when a retry was scheduled, else ``None``.
        """
        anchor = decision.anchor
        reason = f"anchor:{anchor.kind}"
        trigger: dict[str, object] = {
            "anchor_id": anchor.id,
            "local_date": decision.local_date,
        }
        if attempts > 1:
            # A first-attempt capture's event is unchanged; a retry says which.
            trigger["attempt"] = attempts
        written_frame_id: list[int] = []

        def mark_captured(written: WrittenFrame) -> None:
            written_frame_id.append(written.frame_id)
            self._record_exact_time_outcome(
                target.project_id,
                anchor.id,
                decision.local_date,
                status="captured",
                frame_id=written.frame_id,
                detail=None,
                project_name=target.project_name,
            )

        attempt_timeout = self._anchor_fire_timeout()
        started = self._clock.now()
        error: Exception
        try:
            async with asyncio.timeout(attempt_timeout):
                written = await capture_one_now(
                    self,
                    target,
                    reason=reason,
                    trigger=trigger,
                    dedup_key=f"{target.project_id}:{anchor.id}:{decision.local_date}",
                    on_failure_event=False,
                    on_written=mark_captured,
                )
        except TimeoutError:
            error = TimeoutCaptureError(
                f"anchor capture attempt timed out after {attempt_timeout:.0f}s"
            )
        except Exception as exc:  # noqa: BLE001 - every failure is an attempt
            error = exc
        else:
            if written is None:
                # Low-disk skip is a skip, not a fire: release the provisional
                # claim so the anchor can fire on a later cycle while still
                # within grace (if disk frees up). The disk gate -- not a burned
                # fire-log row -- keeps it from capturing onto a full disk.
                # capture_one_now has already logged the low-disk skip event.
                await self._run_blocking(
                    self._release_exact_time_fire,
                    target.project_id,
                    anchor.id,
                    decision.local_date,
                )
            else:
                await self._report_camera_outcome(target, None)
            return None

        if written_frame_id:
            # The frame was written and the row marked captured in the write
            # hop; what failed came after it (the audit event, say). Retrying
            # would capture twice. The camera delivered, so it is a success.
            logger.warning(
                "exact-time anchor captured project=%s anchor=%s but a later step "
                "failed: %s",
                target.project_id,
                anchor.id,
                _anchor_error_text(error),
            )
            await self._report_camera_outcome(target, None)
            return None
        # Measure the attempt on the injected clock, relative to the cycle's
        # ``now``: the attempt may have taken up to ``attempt_timeout``.
        elapsed = self._clock.now() - started
        retry_at = await self._handle_anchor_failure(
            target, decision, attempts, now + elapsed, error
        )
        await self._report_camera_outcome(target, error)
        return retry_at

    async def _handle_anchor_failure(
        self,
        target: CaptureTarget,
        decision: anchors_mod.AnchorDecision,
        attempts: int,
        failed_at: datetime,
        error: Exception,
    ) -> datetime | None:
        """Schedule the next attempt of a failed anchor, or finalise the failure.

        :returns: the retry instant when one was scheduled, else ``None``.
        """
        anchor = decision.anchor
        text = _anchor_error_text(error)
        instant = decision.instant if decision.instant is not None else failed_at
        remaining = instant + anchors_mod.EXACT_TIME_GRACE - failed_at
        delay = anchors_mod.anchor_retry_delay(
            attempts, remaining, auth=classify_failure(error) == "auth"
        )
        if delay is not None:
            retry_at = failed_at + delay
            scheduled = await self._run_blocking(
                self._schedule_anchor_retry,
                target.project_id,
                target.project_name,
                anchor.id,
                decision.local_date,
                attempts=attempts,
                next_attempt_at=retry_at,
                detail=text,
            )
            logger.warning(
                "exact-time anchor attempt %d failed project=%s anchor=%s; "
                "retry at %s: %s",
                attempts,
                target.project_id,
                anchor.id,
                retry_at.isoformat(),
                text,
            )
            return retry_at if scheduled else None

        finalized = await self._run_blocking(
            self._finalize_anchor_failure,
            target.project_id,
            target.project_name,
            anchor.id,
            anchor.kind,
            decision.local_date,
            attempts=attempts,
            detail=text,
            expected_status="failed",
        )
        if finalized:
            self._note_anchor_failure(target, text)
        logger.warning(
            "exact-time anchor capture failed project=%s anchor=%s after %d "
            "attempt(s): %s",
            target.project_id,
            anchor.id,
            attempts,
            text,
        )
        return None

    def _note_anchor_failure(self, target: CaptureTarget, detail: str | None) -> None:
        """Show a final anchor failure on the project's live runner state."""
        runner = self._runners.get(target.project_id)
        if runner is None:
            return
        runner.state.last_error = detail or "exact-time anchor capture failed"
        runner.state.last_error_at = self._clock.now()

    def _claim_anchor_attempt(
        self,
        project_id: int,
        project_name: str,
        anchor_id: str,
        anchor_kind: str,
        local_date: str,
        status: str,
        detail: str | None,
        *,
        within_grace: bool,
        now: datetime,
    ) -> _AnchorClaim:
        """Claim today's fire-log row, or reclaim a due retry, in one hop.

        The insert is tried first, exactly as before #69. When the row already
        exists and the decision has geolocation, :meth:`_reclaim_anchor_retry`
        decides whether a pending retry is now this worker's to make. Doing both
        in one blocking hop keeps the per-cycle cost of an already-handled
        anchor at one hop, as before. Synchronous; call via a thread.
        """
        if (
            self._claim_exact_time_fire(
                project_id, anchor_id, local_date, status, detail
            )
            is not None
        ):
            return _AnchorClaim("claimed", 1)
        if status == "skipped_no_geo":
            return _AnchorClaim("done", 0)
        return self._reclaim_anchor_retry(
            project_id,
            project_name,
            anchor_id,
            anchor_kind,
            local_date,
            now,
            within_grace=within_grace,
        )

    def _reclaim_anchor_retry(
        self,
        project_id: int,
        project_name: str,
        anchor_id: str,
        anchor_kind: str,
        local_date: str,
        now: datetime,
        *,
        within_grace: bool,
    ) -> _AnchorClaim:
        """Take a due ``retry_pending`` row for a new attempt, if it is ours.

        * Not ``retry_pending``: ``done`` (captured, failed, skipped, or an
          attempt in flight).
        * Pending and not yet due: ``pending`` with the due instant.
        * Pending and due, inside grace: a conditional UPDATE (``status`` and
          ``next_attempt_at`` re-checked in its WHERE) moves it back to the
          provisional ``failed`` and adds one attempt. SQLite serialises
          writers, so of two workers (or a restarted runner and a stale one)
          only one sees ``rowcount == 1``: ``claimed``. The other gets ``done``.
        * Pending when the grace window has closed: finalised here, with the
          ``anchor_failed`` event in the same transaction (``finalized``).

        Synchronous; call via a thread.
        """
        now_naive = _naive_utc(now)
        where = (
            ExactTimeFire.project_id == project_id,
            ExactTimeFire.anchor_id == anchor_id,
            ExactTimeFire.local_date == local_date,
        )
        with session_scope(self._session_factory) as session:
            row = session.execute(
                select(
                    ExactTimeFire.status,
                    ExactTimeFire.attempts,
                    ExactTimeFire.next_attempt_at,
                    ExactTimeFire.detail,
                ).where(*where)
            ).one_or_none()
            if row is None or row.status != _FIRE_RETRY_PENDING:
                return _AnchorClaim("done", 0)
            if not within_grace:
                finalized = self._finalize_in_session(
                    session,
                    project_id,
                    project_name,
                    anchor_id,
                    anchor_kind,
                    local_date,
                    attempts=row.attempts,
                    detail=row.detail,
                    expected_status=_FIRE_RETRY_PENDING,
                )
                if finalized:
                    return _AnchorClaim("finalized", row.attempts, detail=row.detail)
                return _AnchorClaim("done", 0)
            due = row.next_attempt_at
            if due is not None and due > now_naive:
                return _AnchorClaim(
                    "pending", row.attempts, next_attempt_at=due.replace(tzinfo=UTC)
                )
            result = session.execute(
                update(ExactTimeFire)
                .where(
                    *where,
                    ExactTimeFire.status == _FIRE_RETRY_PENDING,
                    or_(
                        ExactTimeFire.next_attempt_at.is_(None),
                        ExactTimeFire.next_attempt_at <= now_naive,
                    ),
                )
                .values(
                    status="failed",
                    attempts=ExactTimeFire.attempts + 1,
                    next_attempt_at=None,
                )
            )
            if _rowcount(result) != 1:
                return _AnchorClaim("done", 0)
            return _AnchorClaim("claimed", row.attempts + 1)

    def _schedule_anchor_retry(
        self,
        project_id: int,
        project_name: str,
        anchor_id: str,
        local_date: str,
        *,
        attempts: int,
        next_attempt_at: datetime,
        detail: str,
    ) -> bool:
        """Park the in-flight attempt's row at ``retry_pending``.

        Conditional on the row still being this attempt's provisional state
        (``failed``, the same ``attempts``, no ``next_attempt_at``): if the
        frame write in a timed-out attempt finished and marked the row
        ``captured`` first, nothing changes. Writes one info event naming the
        retry (below the alert threshold). Synchronous; call via a thread.

        :returns: whether the row was parked.
        """
        with session_scope(self._session_factory) as session:
            result = session.execute(
                update(ExactTimeFire)
                .where(
                    ExactTimeFire.project_id == project_id,
                    ExactTimeFire.anchor_id == anchor_id,
                    ExactTimeFire.local_date == local_date,
                    ExactTimeFire.status == "failed",
                    ExactTimeFire.attempts == attempts,
                    ExactTimeFire.next_attempt_at.is_(None),
                )
                .values(
                    status=_FIRE_RETRY_PENDING,
                    next_attempt_at=_naive_utc(next_attempt_at),
                    detail=detail,
                )
            )
            if _rowcount(result) != 1:
                return False
            self._write_event(
                scope_id=project_id,
                level="info",
                message=(
                    f"exact-time anchor attempt {attempts} failed for project "
                    f"{project_name!r} ({local_date}); retrying at "
                    f"{next_attempt_at.isoformat(timespec='seconds')}: {detail}"
                ),
                metadata={
                    "reason": _REASON_ANCHOR_RETRY,
                    "anchor_id": anchor_id,
                    "local_date": local_date,
                    "attempts": attempts,
                    "next_attempt_at": next_attempt_at.isoformat(),
                    "last_error": detail,
                },
                session=session,
            )
            return True

    def _finalize_anchor_failure(
        self,
        project_id: int,
        project_name: str,
        anchor_id: str,
        anchor_kind: str,
        local_date: str,
        *,
        attempts: int,
        detail: str | None,
        expected_status: str,
    ) -> bool:
        """Make an anchor's failure final and raise the ``anchor_failed`` alert.

        Synchronous; call via a thread. See :meth:`_finalize_in_session`.
        """
        with session_scope(self._session_factory) as session:
            return self._finalize_in_session(
                session,
                project_id,
                project_name,
                anchor_id,
                anchor_kind,
                local_date,
                attempts=attempts,
                detail=detail,
                expected_status=expected_status,
            )

    def _finalize_in_session(
        self,
        session: Session,
        project_id: int,
        project_name: str,
        anchor_id: str,
        anchor_kind: str,
        local_date: str,
        *,
        attempts: int,
        detail: str | None,
        expected_status: str,
    ) -> bool:
        """Set the row to final ``failed`` and write ``anchor_failed``, atomically.

        Conditional on ``expected_status`` and ``attempts``, so of two workers
        racing to finalise the same row only one writes the event. The event is
        a **warning** (an active alert) that the next day's success clears with
        ``anchor_recovered``.

        :returns: whether this call finalised the row.
        """
        result = session.execute(
            update(ExactTimeFire)
            .where(
                ExactTimeFire.project_id == project_id,
                ExactTimeFire.anchor_id == anchor_id,
                ExactTimeFire.local_date == local_date,
                ExactTimeFire.status == expected_status,
                ExactTimeFire.attempts == attempts,
            )
            .values(status="failed", next_attempt_at=None, detail=detail)
        )
        if _rowcount(result) != 1:
            return False
        self._write_event(
            scope_id=project_id,
            level="warning",
            message=(
                f"exact-time anchor failed for project {project_name!r} "
                f"({anchor_kind} {local_date}) after {attempts} attempt(s): "
                f"{detail or 'unknown error'}"
            ),
            metadata={
                "reason": _REASON_ANCHOR_FAILED,
                "anchor_id": anchor_id,
                "kind": anchor_kind,
                "local_date": local_date,
                "attempts": attempts,
                "last_error": detail,
            },
            session=session,
        )
        return True

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
        project_name: str | None = None,
    ) -> None:
        """Update an already-claimed fire-log row with the capture outcome.

        Synchronous; call via a thread. The row was claimed (committed) earlier;
        here we reconcile it to ``captured`` or ``failed`` once the attempt has
        resolved. (A low-disk skip is handled separately by releasing the claim.)
        Any pending retry instant is cleared.

        On ``captured``, in the same transaction: when this anchor's previous
        day ended ``failed`` (or was left ``retry_pending``), write an info
        ``anchor_recovered`` event, which auto-clears the project's standing
        ``anchor_failed`` alert (#69).
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
            row.next_attempt_at = None
            if status != "captured":
                return
            previous = session.execute(
                select(ExactTimeFire.status, ExactTimeFire.local_date)
                .where(
                    ExactTimeFire.project_id == project_id,
                    ExactTimeFire.anchor_id == anchor_id,
                    ExactTimeFire.local_date < local_date,
                )
                .order_by(ExactTimeFire.local_date.desc())
                .limit(1)
            ).one_or_none()
            if previous is None or previous.status not in (
                "failed",
                _FIRE_RETRY_PENDING,
            ):
                return
            name = project_name if project_name is not None else str(project_id)
            self._write_event(
                scope_id=project_id,
                level="info",
                message=(
                    f"exact-time anchor captured again for project {name!r} "
                    f"({local_date}) after failing on {previous.local_date}"
                ),
                metadata={
                    "reason": _REASON_ANCHOR_RECOVERED,
                    "anchor_id": anchor_id,
                    "local_date": local_date,
                    "previous_local_date": previous.local_date,
                },
                session=session,
            )

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
        Compares with the cap **stored now**, not the one the runner launched with
        (#104), and returns ``False`` when none is set. Synchronous; the loop
        calls it through :meth:`_run_blocking`.
        """
        with session_scope(self._session_factory) as session:
            project = session.get(Project, target.project_id)
            if project is None or project.max_frame_count is None:
                return False
            return project.frame_count >= project.max_frame_count

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
        allowed = await self._run_blocking(
            self._disk_monitor.is_capture_allowed, volume_path, now=now
        )
        now_paused = not allowed
        if now_paused and not was_low:
            logger.warning(
                "capture paused for low disk space project=%s path=%s",
                target.project_id,
                volume_path,
            )
            await self._run_blocking(
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
            await self._run_blocking(
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

        The backoff stays per project; the offline alert is per camera (#69
        CAM-R6): the outcome feeds the camera's health, shared with every other
        project on the camera.
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
            self._advance_cadence(target, state)
            self._record_transient_failure(state, exc, rng)
            logger.warning(
                "capture failed project=%s reason=%s: %s",
                target.project_id,
                exc.reason.value,
                redact_text(str(exc)),
            )
            await self._report_camera_outcome(target, exc, state=state)
            return None
        except Exception as exc:  # noqa: BLE001 - deliberate per-task isolation
            self._advance_cadence(target, state)
            self._record_transient_failure(state, exc, rng)
            logger.warning(
                "capture failed project=%s: %s",
                target.project_id,
                redact_text(str(exc)),
            )
            await self._report_camera_outcome(target, exc, state=state)
            return None
        self._advance_cadence(target, state)
        # Success: clear any standing offline alert with a recovery event. Done
        # here (the async caller) rather than in the sync ``_record_success`` so
        # the event write stays off the event loop via ``_run_blocking``.
        await self._report_camera_outcome(target, None, state=state)
        return result

    async def _capture_once(self, target: CaptureTarget, state: CaptureState) -> int:
        """Perform one bounded capture for a project and persist it.

        The capture is wrapped in :func:`asyncio.timeout`; a timeout is logged
        as a gap and raised as a :class:`TimeoutCaptureError` so the loop's
        transient-failure handling (backoff) applies. On success the frame is
        persisted and frozen-frame detection runs. Returns the project's active
        frame count after the write so the caller can enforce a frame cap.

        Only the snapshot sits inside the capture budget (#69 CAM-R2). Scene
        metadata is collected after the frame is in hand, under its own total
        timeout, so a slow metadata read never discards a fetched frame.

        The camera's configuration and credentials come from its cached session
        (#69 CAM-P1); the camera work runs while holding one of the camera's
        capture slots (CAM-R4), released before the write.
        """
        session = await self._camera_sessions.get(target.camera_id)
        adapter = build_adapter(
            session.config,
            self.capture_client_for(session.config),
            ffmpeg_binary=self._ffmpeg_binary,
            default_credentials=session.config.default_credentials,
            stream_id=target.stream_id,
            credentials=session.credentials,
            uri_cache=session.uri_cache,
            knob_cache=session.knob_cache,
            auth_state=session.auth_state,
        )
        try:
            async with self.capture_slot(target):
                await self._apply_ptz(target, adapter)
                captured = await self._capture_with_timeout(adapter)
                if captured is not None:
                    await self.collect_scene_metadata(adapter, captured)
        finally:
            await adapter.close()
        if captured is None:
            logger.warning(
                "capture timed out project=%s; gap recorded", target.project_id
            )
            raise TimeoutCaptureError("capture timed out")
        written = await self._run_blocking(
            self._writer.write,
            target.project_id,
            captured,
            stream_id=target.stream_id,
        )
        self._record_success(state)
        if isinstance(written, WrittenFrame):
            state.live_max_frame_count = written.project_max_frame_count
            state.live_cap_read = True
        await self._report_capture_degradations(target, captured)
        await self._check_frozen_frame(target, state, captured)
        return written.project_frame_count

    async def collect_scene_metadata(
        self, adapter: CameraAdapter, frame: CapturedFrame
    ) -> None:
        """Attach best-effort scene metadata to a captured frame, bounded.

        Runs after the capture budget, before the frame is written, and gives the
        adapter at most :data:`SCENE_METADATA_TOTAL_SECONDS` in total. A timeout or
        any error leaves ``frame.scene_metadata`` as ``None``; the frame is always
        kept. Cancellation is not caught, so shutdown still propagates. Shared by
        the interval loop, the one-shot path and the API manual capture.
        """
        try:
            async with asyncio.timeout(SCENE_METADATA_TOTAL_SECONDS):
                frame.scene_metadata = await adapter.collect_scene_metadata(frame)
        except TimeoutError:
            frame.scene_metadata = None
            logger.debug(
                "scene metadata timed out after %.1fs; frame kept without it",
                SCENE_METADATA_TOTAL_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - metadata must never fail capture
            frame.scene_metadata = None
            logger.debug("scene metadata failed; frame kept without it: %s", exc)

    async def _report_capture_degradations(
        self, target: CaptureTarget, frame: CapturedFrame
    ) -> None:
        """Emit one warning event per project, reason and runner lifetime.

        A frame's ``capture_notes`` name how the capture fell short of the
        project's configuration without failing (owner decision OD-2: a missing
        stream profile captures at the camera default). Each note is reported
        once, then latched until the project's runner is relaunched. A failure to
        write the event is logged and never fails the capture.
        """
        if not frame.capture_notes:
            return
        reported = self._degradations_reported.setdefault(target.project_id, set())
        for note in frame.capture_notes:
            if note in reported:
                continue
            reported.add(note)
            logger.warning(
                "capture degraded project=%s stream=%r: %s",
                target.project_id,
                target.stream_id,
                note,
            )
            try:
                await self._run_blocking(
                    self._write_event,
                    scope_id=target.project_id,
                    level="warning",
                    message=(
                        f"capture for project {target.project_name!r} used the "
                        f"camera default stream: the selected stream profile "
                        f"{target.stream_id!r} could not be applied ({note})"
                    ),
                    metadata={
                        "reason": _REASON_STREAM_PROFILE_DEGRADED,
                        "detail": note,
                        "stream_id": target.stream_id,
                    },
                )
            except Exception:  # noqa: BLE001 - reporting must never fail capture
                logger.exception(
                    "failed to record capture degradation project=%s",
                    target.project_id,
                )

    async def _apply_ptz(self, target: CaptureTarget, adapter: CameraAdapter) -> None:
        """Position the camera for this project before capturing, fail-closed.

        Selects a position by the same precedence the UI uses: a raw
        ``pan``/``tilt``/``zoom`` (any one present) overrides a named preset, and
        when neither is configured this is a no-op (no positioning request is
        sent). The adapter owns the physical settle after a successful move.

        A positioning failure is treated exactly like a capture failure: the
        adapter raises a :class:`PTZError` (a :class:`CaptureError`), which this
        method re-raises so it propagates out of :meth:`_capture_once` and into
        the loop's transient-failure/backoff handling -- no frame is ever captured
        from a position that could not be confirmed. The re-raised error keeps the
        original failure ``reason`` but carries a message naming the requested
        position, so the recorded event makes clear it was a positioning failure.
        Camera health classifies it by what it wraps (#131).

        Since #69 CAM-R4 the move is skipped when a confirmed move put the
        camera at this same position within the trust window (the caller holds
        the camera's exclusive slot, so no other project moved it since), and
        the move plus its settle is bounded by ``timeout_seconds`` plus the
        adapter's longest settle; overrunning that is a
        :class:`TimeoutCaptureError`.
        """
        position = ptz_position_of(target)
        if position is None:
            return
        sessions = self._camera_sessions
        if sessions.position_confirmed(target.camera_id, position):
            logger.debug(
                "PTZ position unchanged camera=%s project=%s; move skipped",
                target.camera_id,
                target.project_id,
            )
            return
        if target.ptz_preset is not None and position[0] == "preset":
            requested = f"preset {target.ptz_preset!r}"
            move = adapter.move_to(preset_id=target.ptz_preset)
        else:
            requested = (
                f"pan={target.ptz_pan} tilt={target.ptz_tilt} zoom={target.ptz_zoom}"
            )
            move = adapter.move_to(
                pan=target.ptz_pan, tilt=target.ptz_tilt, zoom=target.ptz_zoom
            )
        # Unknown until this move is confirmed: a failure below leaves it so.
        sessions.note_position(target.camera_id, None)
        budget = self._settings.capture.timeout_seconds + PTZ_SETTLE_MAX_SECONDS
        try:
            async with asyncio.timeout(budget):
                await move
        except TimeoutError:
            raise TimeoutCaptureError(
                f"PTZ positioning to {requested} timed out after {budget:.0f}s"
            ) from None
        except CaptureError as exc:
            # Any capture-classified failure out of ``move_to`` is a positioning
            # failure (this method only awaits the move). Re-raise the same type
            # -- preserving its ``reason`` -- with a message naming the requested
            # position, so it routes through the caller's identical
            # transient-failure/backoff handling and the recorded event is clear.
            raise type(exc)(
                f"PTZ positioning to {requested} failed: {exc}", local=exc.local
            ) from exc
        sessions.note_position(target.camera_id, position)

    def _capture_budget_seconds(self, adapter: CameraAdapter) -> float:
        """The longest one capture may take: the larger of the shared and own budget.

        ``capture.timeout_seconds`` bounds a snapshot. An adapter whose grab is
        slower by design (RTSP through ffmpeg) declares its own total, and the
        capture is bounded by the larger of the two so the shared timeout cannot
        cut a healthy grab short (#69 CAM-R3).
        """
        return max(
            self._settings.capture.timeout_seconds,
            adapter.capture_budget_seconds or 0.0,
        )

    async def _capture_with_timeout(
        self, adapter: CameraAdapter
    ) -> CapturedFrame | None:
        """Return a captured frame, or None if the attempt timed out.

        ``asyncio.timeout`` rather than ``asyncio.wait_for`` (#33): on Python
        3.11, a cancel that lands in the loop iteration in which the capture
        finishes makes ``wait_for`` return the frame -- or raise the capture's
        own error -- instead of the ``CancelledError``. The runner then keeps
        going with the cancel consumed, and :meth:`stop` (or a reconcile
        ``_stop_runner``, holding the reconcile lock) waits on it forever. 3.12's
        ``wait_for`` is built on ``asyncio.timeout``, so behaviour there is
        unchanged.
        """
        timeout = self._capture_budget_seconds(adapter)
        try:
            async with asyncio.timeout(timeout):
                return await adapter.capture()
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
        # Hashing the full image can be non-trivial for a large frame; run it in
        # a thread so it never blocks the event loop (and the other projects'
        # loops sharing it).
        digest = await self._run_blocking(
            lambda: hashlib.sha256(captured.image_bytes).hexdigest()
        )
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
            await self._run_blocking(
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

    def runner_snapshots(self) -> list[RunnerSnapshot]:
        """Return a copy of every tracked runner's state and last plan (#7).

        Read-only and non-raising by construction (plain attribute reads and a
        dataclass copy), so the debug surfaces can call it from any thread.
        """
        snapshots: list[RunnerSnapshot] = []
        for project_id, runner in list(self._runners.items()):
            task = runner.task
            snapshots.append(
                RunnerSnapshot(
                    project_name=runner.target.project_name,
                    state=replace(runner.state),
                    plan=self._plans.get(project_id),
                    task_name=task.get_name() if task is not None else None,
                    task_done=task.done() if task is not None else True,
                )
            )
        return snapshots

    def listener_snapshots(self) -> list[dict[str, Any]]:
        """Return each event listener's project, task and live state (#8, #69 CAM-R7).

        Read-only and non-raising like :meth:`runner_snapshots`. Times are ages in
        seconds rather than raw monotonic readings, whose origin means nothing to
        a reader; ``last_error`` is already redacted.
        """
        now_mono = self._clock.monotonic()
        now = self._clock.now()
        listeners: list[dict[str, Any]] = []
        for project_id, runner in list(self._listeners.items()):
            task = runner.task
            state = runner.state
            progress = state.last_progress_mono
            listeners.append(
                {
                    "project_id": project_id,
                    "project_name": runner.target.project_name,
                    "camera_id": runner.target.camera_id,
                    "task_name": task.get_name() if task is not None else None,
                    "task_done": task.done() if task is not None else True,
                    "transport": state.transport,
                    "connected": state.connected_since is not None,
                    "connected_since": (
                        state.connected_since.isoformat()
                        if state.connected_since is not None
                        else None
                    ),
                    "connected_for_seconds": (
                        round((now - state.connected_since).total_seconds(), 1)
                        if state.connected_since is not None
                        else None
                    ),
                    "last_event_at": (
                        state.last_event_at.isoformat()
                        if state.last_event_at is not None
                        else None
                    ),
                    "last_progress_age_seconds": (
                        round(now_mono - progress, 1) if progress is not None else None
                    ),
                    "last_error": state.last_error,
                    "consecutive_failures": state.consecutive_failures,
                    "restarts": state.restarts,
                    "dropped_events": state.dropped_events,
                    "downgrades": state.downgrades,
                    "events_unavailable": self._camera_health.is_events_unavailable(
                        runner.target.camera_id
                    ),
                }
            )
        return listeners

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

    def _advance_cadence(self, target: CaptureTarget, state: CaptureState) -> None:
        """Move the interval grid past the attempt that just finished (#69 CAM-R5).

        Called once per attempt, success or failure, before the outcome is
        recorded. The next slot is the first one after now, so cadence does not
        drift by the capture latency, a missed slot is skipped rather than made
        up, and a project with no interval (never here) is left alone.
        """
        if target.interval_seconds is None:
            return
        state.next_due_mono = _advance_due(
            state.next_due_mono,
            float(max(1, target.interval_seconds)),
            self._clock.monotonic(),
        )

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
        state.next_retry_mono = None
        state.pause_reason = None

    def _record_transient_failure(
        self, state: CaptureState, exc: BaseException, rng: random.Random
    ) -> None:
        """Record a contained failure and schedule the next backoff retry."""
        cfg = self._settings.capture
        now = self._clock.now()
        state.state = "error"
        state.last_error_at = now
        state.last_error = redact_text(str(exc))
        state.attempt_count += 1
        delay = _backoff_delay(
            state.attempt_count,
            base=cfg.backoff_base_seconds,
            maximum=cfg.backoff_max_seconds,
            jitter_fraction=cfg.backoff_jitter_fraction,
            rng=rng,
            # A refused login will not fix itself in a few seconds: go straight
            # to the ceiling instead of ramping up through it (#69 CAM-R5).
            saturate=isinstance(exc, AuthCaptureError),
        )
        # The retry never lies beyond the next grid slot, so a camera that
        # recovers is captured within one interval whatever the backoff.
        now_mono = self._clock.monotonic()
        retry_mono = _retry_instant(now_mono, delay, state.next_due_mono)
        state.next_retry_mono = retry_mono
        state.next_retry_at = now + timedelta(seconds=retry_mono - now_mono)

    def _mirror_offline(self, camera_id: int, state: CaptureState | None) -> None:
        """Copy the camera's offline latch onto every runner state that shows it.

        ``CaptureState.offline_alerted`` is a read-only mirror of the per-camera
        book for the status surfaces; ``state`` covers a caller-held state that
        is not (or not yet) registered as a runner.
        """
        offline = self._camera_health.is_offline(camera_id)
        if state is not None:
            state.offline_alerted = offline
        for runner in self._runners.values():
            if runner.state.camera_id == camera_id:
                runner.state.offline_alerted = offline

    async def _report_camera_outcome(
        self,
        target: CaptureTarget,
        exc: BaseException | None,
        *,
        state: CaptureState | None = None,
    ) -> None:
        """Feed one capture outcome into the camera's health; event any change.

        ``exc`` is ``None`` for a success. Every capture path calls this once per
        attempt: interval capture, each exact-time anchor attempt and each
        event-triggered capture (#69 CAM-R6). The failure is classified first
        (:func:`~timelapse_manager.capture.camera_health.classify_failure`): a
        project failure, such as a rejected PTZ move, is not recorded at all
        (#131). The book decides the transition synchronously, before any await,
        so of several runners sharing the camera exactly one writes each
        ``camera_offline`` or ``camera_recovered``.

        The write is ordered by a per-camera lock taken with no await between
        the decision and the acquisition (#135): ``asyncio.Lock`` is FIFO, so
        the writes reach the executor, and commit, in decision order, and an
        ``offline`` can never commit after the ``recovered`` that resolves it.
        A failure to write the event is logged and re-arms the transition for
        the next outcome; it never fails the capture path.
        """
        camera_id = target.camera_id
        transition: HealthTransition | None
        if exc is None:
            transition = self._camera_health.record_success(camera_id)
        else:
            kind = classify_failure(exc)
            if kind == "project":
                logger.info(
                    "capture failure on camera=%s is specific to project=%s; "
                    "not counted against the camera: %s",
                    camera_id,
                    target.project_id,
                    _anchor_error_text(exc),
                )
                return
            if kind == "auth":
                # The cached credentials may be stale (edited since they were
                # loaded): the next attempt reloads them.
                self._camera_sessions.invalidate(camera_id)
            transition = self._camera_health.record_failure(
                camera_id,
                error=_anchor_error_text(exc),
                auth=kind == "auth",
                local=kind == "local",
                project_id=target.project_id,
            )
        self._mirror_offline(camera_id, state)
        if transition is None:
            return
        await self._emit_camera_health_transition(transition, state)

    async def _emit_camera_health_transition(
        self, transition: HealthTransition, state: CaptureState | None
    ) -> None:
        """Write one decided health transition, in decision order.

        The per-camera lock is taken with no await between the decision and the
        acquisition (#135): ``asyncio.Lock`` is FIFO, so the writes reach the
        executor, and commit, in decision order. The caller must have decided
        the transition just before calling, with no await in between. A failure
        to write is logged and re-arms the transition; it never raises (bar
        cancellation).
        """
        camera_id = transition.camera_id
        lock = self._camera_health_locks.setdefault(camera_id, asyncio.Lock())
        try:
            await lock.acquire()
        except asyncio.CancelledError:
            # Never written: let the next outcome on the camera try again.
            self._camera_health.rearm(transition)
            self._mirror_offline(camera_id, state)
            raise
        try:
            await self._write_camera_health_transition(transition, state)
        finally:
            lock.release()

    async def _write_camera_health_transition(
        self, transition: HealthTransition, state: CaptureState | None
    ) -> None:
        """Log and write one decided transition; the caller holds its lock."""
        camera_id = transition.camera_id
        if transition.kind == "offline":
            logger.warning(
                "camera considered offline camera=%s after %d consecutive failure(s)%s",
                camera_id,
                transition.consecutive_failures,
                " (credentials rejected)" if transition.auth_failed else "",
            )
        elif transition.kind == "recovered":
            logger.info("camera recovered camera=%s", camera_id)
        elif transition.kind == "events_unavailable":
            logger.warning(
                "camera events unavailable camera=%s after %d consecutive failed "
                "subscribe(s)",
                camera_id,
                transition.consecutive_failures,
            )
        elif transition.kind == "events_available":
            logger.info("camera events available again camera=%s", camera_id)
        else:
            logger.warning(
                "capture on camera=%s failed for a local reason (not counted "
                "against the camera): %s",
                camera_id,
                transition.error,
            )
        try:
            await self._run_blocking(self._write_camera_health_event, transition)
        except Exception:  # noqa: BLE001 - reporting must never fail capture
            logger.exception(
                "failed to record camera health event camera=%s kind=%s",
                camera_id,
                transition.kind,
            )
            self._camera_health.rearm(transition)
            self._mirror_offline(camera_id, state)

    def _write_camera_health_event(self, transition: HealthTransition) -> None:
        """Write the event for one camera-health transition, in one session.

        Reads the camera's name and the names of its active projects in the
        same session, so the message says which camera and what it affects:
        "camera 'Driveway' is offline (projects: a, b)". ``offline`` and
        ``recovered`` are camera-scoped (``scope_id`` is the camera id); a
        ``local`` failure is a system-scoped **info** event, because the camera
        did not fail and nothing would resolve a warning (#133). An ``offline``
        for a camera deleted meanwhile is not written: no recovery could ever
        resolve it (#132). The error text was redacted when the transition was
        recorded. Synchronous; call through :meth:`_run_blocking`.
        """
        camera_id = transition.camera_id
        with session_scope(self._session_factory) as session:
            camera_name = session.execute(
                select(Camera.name).where(Camera.id == camera_id)
            ).scalar_one_or_none()
            projects = list(
                session.execute(
                    select(Project.name)
                    .where(
                        Project.camera_id == camera_id,
                        Project.lifecycle_state == "active",
                    )
                    .order_by(Project.name)
                ).scalars()
            )
            if camera_name is None and transition.kind in (
                "offline",
                "events_unavailable",
            ):
                logger.info(
                    "camera=%s no longer exists; %s alert not raised",
                    camera_id,
                    transition.kind,
                )
                return
            label = repr(camera_name) if camera_name else f"#{camera_id}"
            affects = f" (projects: {', '.join(projects)})" if projects else ""
            if transition.kind == "offline":
                if transition.auth_failed:
                    cause = f"the camera rejected the credentials: {transition.error}"
                else:
                    cause = (
                        f"{transition.consecutive_failures} consecutive capture "
                        f"attempts failed; last error: {transition.error}"
                    )
                self._write_event(
                    scope="camera",
                    scope_id=camera_id,
                    level="warning",
                    message=f"camera {label} is offline{affects}: {cause}",
                    metadata={
                        "reason": _REASON_CAMERA_OFFLINE,
                        "camera_id": camera_id,
                        "consecutive_failures": transition.consecutive_failures,
                        "auth_failed": transition.auth_failed,
                        "last_error": transition.error,
                        "projects": projects,
                    },
                    session=session,
                )
            elif transition.kind == "recovered":
                self._write_event(
                    scope="camera",
                    scope_id=camera_id,
                    level="info",
                    message=(
                        f"camera {label} recovered and is capturing again{affects}"
                    ),
                    metadata={
                        "reason": _REASON_CAMERA_RECOVERED,
                        "camera_id": camera_id,
                    },
                    session=session,
                )
            elif transition.kind == "events_unavailable":
                self._write_event(
                    scope="camera",
                    scope_id=camera_id,
                    level="warning",
                    message=(
                        f"camera {label} events are unavailable{affects}: "
                        f"{transition.consecutive_failures} consecutive "
                        f"subscriptions failed; last error: {transition.error}"
                    ),
                    metadata={
                        "reason": _REASON_EVENTS_UNAVAILABLE,
                        "camera_id": camera_id,
                        "consecutive_failures": transition.consecutive_failures,
                        "last_error": transition.error,
                        "projects": projects,
                    },
                    session=session,
                )
            elif transition.kind == "events_available":
                self._write_event(
                    scope="camera",
                    scope_id=camera_id,
                    level="info",
                    message=(f"camera {label} events are available again{affects}"),
                    metadata={
                        "reason": _REASON_EVENTS_AVAILABLE,
                        "camera_id": camera_id,
                    },
                    session=session,
                )
            else:
                self._write_event(
                    scope="system",
                    scope_id=None,
                    level="info",
                    message=(
                        f"capture on camera {label} failed for a local reason, "
                        f"not counted against the camera: {transition.error}"
                    ),
                    metadata={
                        "reason": _REASON_CAPTURE_LOCAL_ERROR,
                        "camera_id": camera_id,
                        "error": transition.error,
                    },
                    session=session,
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

    def target_from_rows(
        self, project: Project, camera: Camera, *, camera_version: str | None = None
    ) -> CaptureTarget:
        """Snapshot one project and its camera into a :class:`CaptureTarget`.

        The one field mapping both the reconcile read (:meth:`_load_targets`)
        and the API manual capture use, so a manual frame is captured with the
        project's own stream, PTZ position, storage path and campaign bounds,
        exactly as a scheduled one is (#69 CAM-R10). Pure: reads only the
        rows' loaded columns and the settings, no I/O.

        A missing interval flows through as ``None`` (an anchor-only project):
        it must not be replaced by the default, or the project would silently
        start interval capturing. A falsey interval of 0 still falls back to
        the default, preserving prior behaviour for the interval case.

        :param camera_version: the camera's
            :func:`~.camera_config.config_version` as the caller read it, for
            the session cache's reconcile check; ``None`` when not compared.
        """
        interval: int | None
        if project.capture_interval_seconds is None:
            interval = None
        else:
            interval = (
                project.capture_interval_seconds
                or self._settings.capture.default_interval_seconds
            )
        latitude, longitude = _resolve_geolocation(camera)
        return CaptureTarget(
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
            camera_version=camera_version,
            active_since=_as_aware_utc(project.active_since or project.created_at),
        )

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
            defaults = load_defaults_row(session)
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
                targets.append(
                    self.target_from_rows(
                        project,
                        camera,
                        camera_version=config_version(camera, defaults),
                    )
                )
        for project_id, project_name, reason in ended:
            self._end_campaign(project_id, project_name, reason)
        return targets

    async def _ensure_camera_health_synced(self) -> None:
        """Seed camera health from the database once, sweeping old alerts.

        Runs before the first runner launches (from :meth:`start`) and, until it
        has succeeded once, again on each reconcile tick. Seeding happens only
        once per supervisor: re-reading on every tick would re-latch a camera
        whose recovery the dispatcher has not yet applied to its alert, and
        emit a second recovery. A failure is logged and never stops capture.
        """
        if self._camera_health_synced:
            return
        try:
            alerted = await self._run_blocking(self._sync_camera_health)
        except Exception:  # noqa: BLE001 - health seeding must not stop capture
            logger.exception("failed to seed camera health; retrying next reconcile")
            return
        self._camera_health.seed(alerted)
        try:
            events_alerted = await self._run_blocking(self._sync_events_alerts)
        except Exception:  # noqa: BLE001 - a stale alert is better than no capture
            logger.exception("failed to seed listener health; alerts may stand")
            events_alerted = []
        self._camera_health.seed_events(events_alerted)
        self._camera_health_synced = True

    def _sync_events_alerts(self) -> list[int]:
        """Return the cameras that have an active ``events_unavailable`` alert.

        Seeds the listener track so a restart during an events outage still
        resolves the alert when a listener next subscribes. Synchronous; call
        through :meth:`_run_blocking`.
        """
        reason = func.json_extract(Event.event_metadata, "$.reason")
        with session_scope(self._session_factory) as session:
            rows = session.execute(
                select(Event.scope_id)
                .where(
                    Event.alert_cleared_at.is_(None),
                    Event.level.in_(OPEN_ALERT_INDEX_LEVELS),
                    reason == _REASON_EVENTS_UNAVAILABLE,
                    Event.scope == "camera",
                    Event.scope_id.is_not(None),
                )
                .distinct()
            ).scalars()
            return sorted(int(camera_id) for camera_id in rows if camera_id is not None)

    def _sync_camera_health(self) -> list[int]:
        """Sweep pre-0.2.0 offline alerts and read the cameras still alerted.

        In one transaction (#69 CAM-R6):

        * **Upgrade sweep.** Every *project-scoped* active ``camera_offline``
          alert is auto-cleared (``alert_clear_reason="auto"``). Before 0.2.0
          the alert was per project; a camera-scoped recovery can never resolve
          one, so it would stand forever. When any were cleared, one info system
          event names the count. Idempotent: later runs find none and write
          nothing. A camera still offline raises a fresh camera-scoped alert at
          its next failures.
        * **Deleted cameras (#132).** Every active camera-scoped
          ``camera_offline`` alert whose camera no longer exists is
          auto-cleared, with one info system event naming the count. Nothing
          could ever resolve it, and seeding from it would latch a camera that
          is gone. The delete path clears them too; this covers a delete made
          before that fix, or outside the service.
        * **Seed.** Returns the camera ids that have an active camera-scoped
          ``camera_offline`` alert, so a restart during an outage still resolves
          the alert on recovery.

        Synchronous; call through :meth:`_run_blocking`.
        """
        reason = func.json_extract(Event.event_metadata, "$.reason")
        open_offline = (
            Event.alert_cleared_at.is_(None),
            Event.level.in_(OPEN_ALERT_INDEX_LEVELS),
            reason == _REASON_CAMERA_OFFLINE,
        )
        with session_scope(self._session_factory) as session:
            swept = _rowcount(
                session.execute(
                    update(Event)
                    .where(*open_offline, Event.scope == "project")
                    .values(
                        alert_cleared_at=datetime.now(UTC).replace(tzinfo=None),
                        alert_cleared_by=None,
                        alert_clear_reason="auto",
                    )
                    .execution_options(synchronize_session=False)
                )
            )
            if swept:
                logger.info(
                    "cleared %d project-scoped camera_offline alert(s); camera "
                    "health is now per camera",
                    swept,
                )
                self._write_event(
                    scope="system",
                    scope_id=None,
                    level="info",
                    message=(
                        f"cleared {swept} camera-offline alert(s) raised per project "
                        "by an earlier version; camera health is now tracked per "
                        "camera and a camera still offline raises a new alert"
                    ),
                    metadata={
                        "reason": _REASON_CAMERA_ALERTS_MIGRATED,
                        "cleared": swept,
                    },
                    session=session,
                )
            orphaned = _rowcount(
                session.execute(
                    update(Event)
                    .where(
                        *open_offline,
                        Event.scope == "camera",
                        or_(
                            Event.scope_id.is_(None),
                            Event.scope_id.not_in(select(Camera.id)),
                        ),
                    )
                    .values(
                        alert_cleared_at=datetime.now(UTC).replace(tzinfo=None),
                        alert_cleared_by=None,
                        alert_clear_reason="auto",
                    )
                    .execution_options(synchronize_session=False)
                )
            )
            if orphaned:
                logger.info(
                    "cleared %d camera_offline alert(s) of deleted cameras", orphaned
                )
                self._write_event(
                    scope="system",
                    scope_id=None,
                    level="info",
                    message=(
                        f"cleared {orphaned} camera-offline alert(s) of cameras "
                        "that no longer exist"
                    ),
                    metadata={
                        "reason": _REASON_CAMERA_ALERTS_ORPHANED,
                        "cleared": orphaned,
                    },
                    session=session,
                )
            alerted = session.execute(
                select(Event.scope_id)
                .where(
                    *open_offline,
                    Event.scope == "camera",
                    Event.scope_id.is_not(None),
                )
                .distinct()
            ).scalars()
            return sorted(
                int(camera_id) for camera_id in alerted if camera_id is not None
            )

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
            defaults = load_defaults_row(session)
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
                        camera_version=config_version(camera, defaults),
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

        Two paths can race to end the same campaign (the capture loop's
        post-write check and the reconcile read), so this short state write is
        wrapped in :func:`run_with_write_retry`: a transient SQLite "database is
        locked" error is retried with bounded backoff rather than surfaced. The
        write is a single atomic transaction that touches only the database (no
        files), and the ``archived`` guard makes re-running it a no-op, so it is
        safe to retry. ``run_with_write_retry`` sleeps between attempts, so this
        must stay off the event loop (every caller already runs it in a thread).
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

        def _archive() -> None:
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

        run_with_write_retry(_archive)

    def _load_capture_config(self, camera_id: int) -> CaptureCameraConfig | None:
        """Read the camera and, if it needs them, the default credentials.

        One session, one hop (#66 C12): see
        :func:`~timelapse_manager.capture.camera_config.load_capture_config`.
        Returns ``None`` when the camera no longer exists. Synchronous; call it
        through :meth:`_run_blocking`.
        """
        return load_capture_config(self._session_factory, camera_id)

    def _load_camera(self, camera_id: int) -> CaptureCameraConfig | None:
        """Read a camera's adapter-relevant fields as a detached snapshot.

        The camera-config loader the event-listener factory is wired with. It is
        the same merged load as :meth:`_load_capture_config`, so the resolved
        default rides along and the listener needs no second read.
        """
        return self._load_capture_config(camera_id)

    def _load_default_credentials(self) -> tuple[str, str] | None:
        """Resolve the global fallback ``(username, password)``, or ``None``.

        The default-credentials loader the event-listener factory is wired with,
        used only for a camera config that does not carry a resolved default.
        Returns ``None`` when no fallback applies.
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
        scope_id: int | None,
        level: str,
        message: str,
        metadata: dict[str, object] | None = None,
        session: Session | None = None,
        scope: str = "project",
    ) -> None:
        """Insert an event row (project-scoped by default). Synchronous.

        ``scope`` is ``"project"`` unless a caller names ``"camera"`` (camera
        health) or ``"system"`` (``scope_id`` ``None``). When ``session`` is
        provided the event is added to that open scope; otherwise a fresh
        transactional scope is opened just for this write. Use a thread executor.
        """
        # The chokepoint for every event the capture loop records: a camera's
        # error text can carry the URL it was fetching, userinfo and all, so
        # nothing reaches the row unredacted (#69 CAM-S2, #67-S14). It does not
        # go through ``log_event``, which would have done this.
        event = Event(
            scope=scope,
            scope_id=scope_id,
            level=level,
            message=redact_text(message),
            timestamp=datetime.now(UTC).replace(tzinfo=None),
            event_metadata=(
                _redact_urls_in(metadata) if metadata is not None else None
            ),
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
