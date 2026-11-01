"""Display projections for the web routers: view dataclasses and their
builders, plus the datetime/byte/duration formatters and status-vocabulary
translation the templates rely on."""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePath
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, true
from sqlalchemy.orm import Session as DbSession

from ...db.models import Camera, Event, Project, RenderJob, User
from ...encode.codec_string import (
    media_type_for_container,
    media_type_for_path,
    playback_label,
)
from ...logging import redact_text
from ...render import settings as render_settings
from ...runtime import get_context
from ...security.credential_binding import split_userinfo
from ...services.cameras import pinned_fingerprint
from ..interval import decompose_seconds

if TYPE_CHECKING:
    from ...storage.frames import FrameAggregates

logger = logging.getLogger(__name__)


_RENDER_STATUS_DISPLAY = {
    "pending": "queued",
    "encoding": "running",
    "done": "complete",
    "failed": "error",
}


def _fmt_dt(value: datetime.datetime | None) -> str | None:
    """Format a naive-UTC datetime for display, or ``None``.

    Templates print this verbatim, so formatting happens here. The stored times
    are naive UTC; they are shown in a compact, unambiguous form with a ``UTC``
    suffix.
    """
    if value is None:
        return None
    return value.strftime("%Y-%m-%d %H:%M UTC")


def _fmt_bytes(num: int) -> str:
    """Render a byte count as a short human-readable string (e.g. ``"1.4 MB"``)."""
    size = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            precision = 0 if unit == "B" else 1
            return f"{size:.{precision}f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def _fmt_duration(seconds: int) -> str:
    """Render an elapsed-seconds count compactly (e.g. ``"2d 3h"``, ``"5m 12s"``)."""
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


@dataclass(frozen=True)
class _ProjectView:
    """Display projection of a project row plus its live capture status."""

    id: int
    name: str
    operational_status: str
    lifecycle_state: str
    frame_count: int
    last_capture_at: str | None
    # Raw datetimes for timezone-aware display via the localdt template filter.
    last_capture_at_raw: datetime.datetime | None
    next_capture_at: str | None
    next_capture_at_raw: datetime.datetime | None
    # Capture-health signals from live supervisor state: ``is_overdue`` is true
    # when a running project with frames has not captured within ~2x its interval
    # (a silent-stall warning); the last-error pair surfaces the most recent
    # capture failure for the error state.
    is_overdue: bool
    last_error_message: str | None
    last_error_at_raw: datetime.datetime | None
    latest_frame_url: str | None
    # Identity of what the card's time ribbon draws (#70 F-P1), from
    # :func:`ribbon_key`. The card keys its ``hx-preserve`` ribbon slot on it,
    # so a dashboard poll keeps the loaded ribbon until the ribbon would look
    # different: new frames, changed campaign bounds, or the clock moving on.
    ribbon_key: str
    # On-disk footprint of the project's active frames, and how long the current
    # capture runner has been up (None when not running). Both are part of the
    # status surface; the displays are pre-rendered for the template.
    disk_used_bytes: int
    disk_used_display: str
    # Forward-looking storage projection over the campaign duration. The byte and
    # frames-remaining figures are ``None`` for an open-ended campaign (no end
    # date) or one with no usable capture interval; ``projected_open_ended`` flags
    # that case so the template can say so plainly. The displays are pre-rendered.
    projected_open_ended: bool
    projected_total_bytes: int | None
    projected_total_display: str | None
    projected_frames_remaining: int | None
    projected_span_days: int | None
    # Pre-rendered storage growth rate (e.g. ``"12.4 MB / day"``): the measured
    # average frame size times the captures the schedule plans per day (interval
    # ticks inside the windows plus exact-time anchors, #22). ``None`` when the
    # project has no frames to measure yet, plans no capture, or is not
    # capturing (``capture_planned`` false).
    growth_rate_display: str | None
    # Whether the project is set to capture: active and neither stopped nor
    # paused for low disk (#22 D12). An operator-paused, low-disk or stopped
    # project shows no growth rate. A project merely waiting for its schedule
    # window to open still plans captures, so it keeps its rate.
    capture_planned: bool
    uptime_seconds: int | None
    uptime_display: str | None
    # Configuration fields the edit form prefills; not needed by the status cards
    # but carried on the same view model so the edit template reads ``project.*``.
    camera_id: int
    camera_name: str
    capture_interval_seconds: int | None
    # The stored interval decomposed into the value+unit the edit form's paired
    # control prefills (largest whole unit that divides the seconds evenly).
    interval_value: int
    interval_unit: str
    storage_path: str | None
    # Campaign bounds, pre-formatted for the form: the datetimes as
    # ``YYYY-MM-DDTHH:MM`` (the value an ``<input type="datetime-local">`` reads),
    # ``""`` when unset; the frame cap as itself or ``None``.
    start_date_input: str
    end_date_input: str
    max_frame_count: int | None
    # The render settings the edit form's dropdowns prefill, read from the stored
    # ``render_schedule`` JSON with defaults filled in for any missing key (so an
    # existing project always shows sensible selections). Keys: enabled,
    # interval_seconds, encoder, container, fps, resolution.
    render_settings: dict[str, Any]
    # A few suggested playback frame rates derived from the capture cadence, shown
    # as one-click chips beside the frame-rate input. Carried on the view (not a
    # loose template kwarg) so the error re-render of the form keeps them.
    fps_suggestions: list[int]
    # The archive-schedule / post-render-action JSON, pre-serialized for the edit
    # form's textareas ("" when unset). Those two remain raw-JSON surfaces.
    archive_schedule_json: str
    post_render_actions_json: str


# The shortest time bucket of a card ribbon key: the "now" cursor and an
# open-ended campaign's scale move at most one bucket before the ribbon reloads.
RIBBON_MIN_BUCKET_SECONDS = 3600


def _ribbon_clock() -> datetime.datetime:
    """Return the current instant for :func:`ribbon_key` (patched in tests)."""
    return datetime.datetime.now(datetime.UTC)


def _ribbon_bucket_seconds(
    start_date: datetime.datetime | None,
    end_date: datetime.datetime | None,
    now: datetime.datetime,
) -> int:
    """Return the ribbon key's time bucket for a campaign, in seconds.

    One hour, or the ribbon's own resolution when that is coarser: the span
    over its ``VBW`` viewBox units, rounded up to a power-of-two number of
    hours so the bucket size itself rarely changes. The span runs from
    ``start_date`` to ``end_date`` (or now); without a start date it is not
    known here, and the hour is used.
    """
    from .. import ribbon as ribbon_svg

    if start_date is None:
        return RIBBON_MIN_BUCKET_SECONDS
    start = _as_utc(start_date)
    end = _as_utc(end_date) if end_date is not None else now
    per_unit = max((end - start).total_seconds(), 0.0) / ribbon_svg.VBW
    bucket = RIBBON_MIN_BUCKET_SECONDS
    while bucket < per_unit:
        bucket *= 2
    return bucket


def _as_utc(value: datetime.datetime) -> datetime.datetime:
    """Treat a stored naive datetime as UTC; convert an aware one to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC)


def ribbon_key(
    *,
    latest_frame_id: int | None,
    frame_count: int,
    start_date: datetime.datetime | None,
    end_date: datetime.datetime | None,
    now: datetime.datetime,
) -> str:
    """Return the identity of what a dashboard card's time ribbon draws.

    The card ribbon (``GET /partials/projects/{id}/ribbon?h=20``) draws the
    campaign's day bands, the active frames as ticks, the gaps between them
    (lapse markers, relative to the span) and a "now" cursor. It depends on:

    * the frame set -- the latest active frame id (a capture) and the frame
      count (a deletion or an import that leaves the latest frame alone);
    * the campaign bounds;
    * the time: the cursor moves, and an open-ended campaign's span (and so
      its whole scale and its gap threshold) grows. A time bucket
      (:func:`_ribbon_bucket_seconds`) bounds that drift without reloading on
      every poll. A campaign that has ended draws a fixed picture and gets no
      bucket.

    Render jobs and alerts are not drawn on the card ribbon, so they are not
    part of the key.

    The key starts with the latest frame id (0 with no frames), then a short
    digest of the rest: ``"<latest frame id>-<8 hex digits>"``.
    """
    now = _as_utc(now)
    ended = end_date is not None and _as_utc(end_date) <= now
    bucket = (
        None
        if ended
        else int(now.timestamp()) // _ribbon_bucket_seconds(start_date, end_date, now)
    )
    material = repr((frame_count, start_date, end_date, bucket)).encode("utf-8")
    return f"{latest_frame_id or 0}-{hashlib.sha256(material).hexdigest()[:8]}"


def _capture_is_overdue(
    operational_status: str,
    frame_count: int,
    last_capture_at: datetime.datetime | None,
    interval_seconds: int | None,
) -> bool:
    """True when a running project has silently stalled.

    A project that is running and has already captured frames, but whose last
    capture is older than ~2x its configured interval, is very likely stuck (the
    camera went unreachable, the runner wedged, etc.) without having flipped to a
    hard ``error`` state. Two intervals tolerates one missed tick + jitter before
    raising the flag. Naive timestamps are treated as UTC to match storage.
    """
    if operational_status != "running" or not frame_count:
        return False
    if last_capture_at is None or not interval_seconds:
        return False
    last = (
        last_capture_at
        if last_capture_at.tzinfo is not None
        else last_capture_at.replace(tzinfo=datetime.UTC)
    )
    elapsed = (datetime.datetime.now(datetime.UTC) - last).total_seconds()
    return elapsed > 2 * interval_seconds


def _project_operational_status(project: Project, state: Any) -> str:
    """Derive the template's operational-status word for a project.

    The live capture state (``running``/``idle``/``stopped``/``error`` plus a
    pause reason) is translated into the template vocabulary
    (``running``/``paused``/``lowdisk``/``stopped``/``error``). With no live
    state -- the supervisor is off, or this project is not being captured -- the
    project reads as ``stopped``.

    A project the operator has paused (``lifecycle_state == "paused"``) reads as
    ``paused`` regardless of live state: the supervisor has stopped its runner so
    there is no live state to consult, and without this it would otherwise read
    as ``stopped``. Checked first so the paused surface is authoritative.
    """
    if project.lifecycle_state == "paused":
        return "paused"
    if state is None:
        return "stopped"
    if state.state == "running":
        return "running"
    if state.state == "error":
        return "error"
    if state.state == "stopped":
        return "stopped"
    # Idle: distinguish a low-disk pause from a closed-window pause.
    if getattr(state, "pause_reason", None) == "low_disk":
        return "lowdisk"
    return "paused"


# Operational statuses that plan no capture, so they show no growth rate (#22
# D12). An operator pause is caught by the lifecycle check in
# :func:`_capture_planned`; "paused" alone also covers a closed schedule window,
# which still plans captures.
_NOT_CAPTURING_STATUSES = frozenset({"lowdisk", "stopped"})


def _capture_planned(project: Project, operational_status: str) -> bool:
    """Whether *project* is set to capture, so a growth rate is meaningful (D12).

    False for a project the operator paused (``lifecycle_state`` other than
    ``active``), one paused for low disk, and one stopped (no live runner). A
    project idle because its schedule window is closed reads ``paused`` on the
    card but still plans its next capture, so it stays True.
    """
    return (
        project.lifecycle_state == "active"
        and operational_status not in _NOT_CAPTURING_STATUSES
    )


def _project_view(
    db: DbSession,
    project: Project,
    *,
    frame_stats: FrameAggregates | None = None,
    cameras: Mapping[int, Camera] | None = None,
) -> _ProjectView:
    """Build a project view model, consulting the supervisor for live status.

    The frame figures (last capture, preview frame, disk usage) come from
    :func:`~timelapse_manager.storage.frames.project_frame_aggregates`, and the
    disk usage is reused by both storage estimates (#66 C5). A list builder
    passes *frame_stats* and *cameras* it fetched for every project at once;
    without them this reads them for the one project.
    """
    from ...capture import next_due
    from ...storage import estimator
    from ...storage import frames as frame_service

    supervisor = get_context().capture_supervisor
    state = supervisor.state_for_project(project.id) if supervisor is not None else None
    # Last capture comes from the frame table (#5): any app capture path
    # (interval, anchor, event), and it survives a restart. The runner's
    # in-memory ``last_capture_at`` is only the interval cadence anchor.
    if frame_stats is None:
        frame_stats = frame_service.project_frame_aggregates(db, [project.id])[
            project.id
        ]
    last_capture = next_due.as_utc(frame_stats.last_captured_at)

    # The 640-px thumbnail, not the full frame: the card and the project
    # header never show more than that (#70 F-P2).
    latest_frame_url = (
        f"/projects/{project.id}/frames/{frame_stats.latest_frame_id}"
        "/thumbnail?size=640"
        if frame_stats.latest_frame_id is not None
        else None
    )
    disk_used = frame_stats.disk_usage_bytes
    uptime_seconds = _runner_uptime_seconds(state)
    projected_bytes, projected_remaining = estimator.estimate_for_project(
        db, project, disk_usage=disk_used
    )
    projected_open_ended = projected_bytes is None
    projected_span_days = _projected_span_days(project)
    interval_value, interval_unit = decompose_seconds(project.capture_interval_seconds)
    camera = (
        cameras.get(project.camera_id)
        if cameras is not None
        else db.get(Camera, project.camera_id)
    )
    camera_name = camera.name if camera is not None else f"Camera {project.camera_id}"
    operational_status = _project_operational_status(project, state)
    latitude, longitude = (
        (camera.geolocation_latitude, camera.geolocation_longitude)
        if camera is not None
        else (None, None)
    )
    now = datetime.datetime.now(datetime.UTC)
    # Growth rate (#22): only for a project that is set to capture (D12), and
    # only once it has frames to measure -- checked first so a paused or empty
    # project never pays for the captures-per-day walk.
    capture_planned = _capture_planned(project, operational_status)
    growth_rate = (
        estimator.estimate_growth_rate_bytes_per_day(
            db,
            project,
            captures_per_day=estimator.project_captures_per_day(
                db, project, now=now, camera=camera
            ),
            disk_usage=disk_used,
        )
        if capture_planned and project.frame_count
        else None
    )
    active = project.lifecycle_state == "active"
    next_capture_raw = (
        next_due.next_capture_instant(
            now=now,
            interval_seconds=project.capture_interval_seconds,
            last_captured_at=last_capture,
            anchors_raw=project.exact_time_anchors,
            schedule_raw=project.schedule,
            latitude=latitude,
            longitude=longitude,
            start_date=project.start_date,
            end_date=project.end_date,
            finished=(
                project.max_frame_count is not None
                and project.frame_count >= project.max_frame_count
            ),
        )
        if active
        else None
    )
    # Anchor-aware overdue (#5): an anchor whose grace window passed with no
    # capture -- the silent-runner failure of #6 -- flags the project even
    # though it has no interval.
    missed_anchor = (
        next_due.missed_anchor_instant(
            now=now,
            anchors_raw=project.exact_time_anchors,
            schedule_raw=project.schedule,
            latitude=latitude,
            longitude=longitude,
            last_captured_at=last_capture,
            active_since=next_due.as_utc(project.created_at),
        )
        if active
        else None
    )
    return _ProjectView(
        id=project.id,
        name=project.name,
        operational_status=operational_status,
        lifecycle_state=project.lifecycle_state,
        frame_count=project.frame_count,
        last_capture_at=_fmt_dt(last_capture),
        last_capture_at_raw=last_capture,
        next_capture_at=_fmt_dt(next_capture_raw),
        next_capture_at_raw=next_capture_raw,
        is_overdue=missed_anchor is not None
        or _capture_is_overdue(
            operational_status,
            project.frame_count,
            last_capture,
            project.capture_interval_seconds,
        ),
        last_error_message=_redacted_error(
            state.last_error if state is not None else None
        ),
        last_error_at_raw=(state.last_error_at if state is not None else None),
        latest_frame_url=latest_frame_url,
        ribbon_key=ribbon_key(
            latest_frame_id=frame_stats.latest_frame_id,
            frame_count=project.frame_count,
            start_date=project.start_date,
            end_date=project.end_date,
            now=_ribbon_clock(),
        ),
        disk_used_bytes=disk_used,
        disk_used_display=_fmt_bytes(disk_used),
        projected_open_ended=projected_open_ended,
        projected_total_bytes=projected_bytes,
        projected_total_display=(
            _fmt_bytes(projected_bytes) if projected_bytes is not None else None
        ),
        projected_frames_remaining=projected_remaining,
        projected_span_days=projected_span_days,
        growth_rate_display=(
            f"{_fmt_bytes(growth_rate)} / day" if growth_rate is not None else None
        ),
        capture_planned=capture_planned,
        uptime_seconds=uptime_seconds,
        uptime_display=(
            _fmt_duration(uptime_seconds) if uptime_seconds is not None else None
        ),
        camera_id=project.camera_id,
        camera_name=camera_name,
        capture_interval_seconds=project.capture_interval_seconds,
        interval_value=interval_value,
        interval_unit=interval_unit,
        storage_path=project.storage_path,
        start_date_input=_dt_input(project.start_date),
        end_date_input=_dt_input(project.end_date),
        max_frame_count=project.max_frame_count,
        render_settings=render_settings.render_settings_view(project.render_schedule),
        fps_suggestions=(
            render_settings.suggested_fps(project.capture_interval_seconds)
            if project.capture_interval_seconds is not None
            else []
        ),
        archive_schedule_json=_json_or_empty(project.archive_schedule),
        post_render_actions_json=_json_or_empty(project.post_render_actions),
    )


def _json_or_empty(value: Any) -> str:
    """Pretty-print a JSON value for a form textarea, or ``""`` when unset/empty."""
    if not value:
        return ""
    return json.dumps(value, indent=2)


def _projected_span_days(project: Project) -> int | None:
    """Return the campaign span in whole days, or ``None`` when open-ended.

    The span runs from ``start_date`` (or now, when unset) to ``end_date``; a
    project with no end date is open-ended and has no finite span. A span that
    has already elapsed floors at zero rather than going negative. The stored
    dates are naive-UTC, so "now" is taken naive-UTC to match.
    """
    if project.end_date is None:
        return None
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    start = project.start_date if project.start_date is not None else now
    seconds = (project.end_date - start).total_seconds()
    return max(0, int(seconds // 86400))


def _dt_input(value: datetime.datetime | None) -> str:
    """Format a stored datetime for a ``datetime-local`` input, or ``""``.

    The stored value is naive UTC; the input shows and re-submits it as the same
    wall-clock value (the app treats form datetimes as UTC), so no offset is
    applied. The ``minute`` precision matches the widget's default granularity.
    """
    if value is None:
        return ""
    return value.strftime("%Y-%m-%dT%H:%M")


def _runner_uptime_seconds(state: Any) -> int | None:
    """Return how long the current capture runner has been up, in whole seconds.

    ``None`` when no runner is live or it carries no start time. The start instant
    is set by the supervisor when it launches the loop; a reconcile restart begins
    a fresh runner and therefore a fresh uptime.
    """
    started_at = getattr(state, "started_at", None) if state is not None else None
    if started_at is None:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=datetime.UTC)
    elapsed = (datetime.datetime.now(datetime.UTC) - started_at).total_seconds()
    return max(0, int(elapsed))


def _project_views(
    db: DbSession, *, lifecycle_state: str | None = None
) -> list[_ProjectView]:
    """Return view models for every project, ordered by id.

    *lifecycle_state* keeps only the projects in that state, filtered in SQL.
    The frame figures for all of them come from one batched read and their
    cameras from one query (#66 C5), instead of several queries per project.
    """
    from ...storage import frames as frame_service

    stmt = select(Project).order_by(Project.id)
    if lifecycle_state is not None:
        stmt = stmt.where(Project.lifecycle_state == lifecycle_state)
    projects = db.execute(stmt).scalars().all()
    if not projects:
        return []
    frame_stats = frame_service.project_frame_aggregates(db, [p.id for p in projects])
    # Held for the whole loop: the session's identity map is weak-referenced, and
    # these strong references keep the estimator's ``session.get(Camera, ...)``
    # an identity-map hit rather than a query.
    cameras = {
        camera.id: camera
        for camera in db.execute(
            select(Camera).where(Camera.id.in_(sorted({p.camera_id for p in projects})))
        ).scalars()
    }
    return [
        _project_view(db, p, frame_stats=frame_stats[p.id], cameras=cameras)
        for p in projects
    ]


# The ``_ProjectView`` fields the dashboard status card renders
# (``_partials/project_status_card.html``). The dashboard poll version digests
# exactly these, so a field that changes on every request but is not shown
# (``uptime_seconds``, ``projected_frames_remaining``) cannot defeat the 204.
# ``tests/web/test_dashboard_poll_version.py`` fails when the card template
# reads a ``project.*`` field missing from this list.
DASHBOARD_CARD_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "operational_status",
    "lifecycle_state",
    "frame_count",
    "last_capture_at",
    "last_capture_at_raw",
    "next_capture_at",
    "next_capture_at_raw",
    "is_overdue",
    "latest_frame_url",
    "ribbon_key",
    "disk_used_display",
    "capture_planned",
    "growth_rate_display",
    "projected_open_ended",
    "projected_total_display",
)


def _minute_precision(value: Any) -> Any:
    """Truncate a datetime to the minute; return anything else unchanged."""
    if isinstance(value, datetime.datetime):
        return value.replace(second=0, microsecond=0)
    return value


def dashboard_poll_version(
    views: Sequence[_ProjectView],
    *,
    role: str | None,
    viewer_timezone: str | None,
) -> str:
    """Return the version of the dashboard grid these card views render to.

    A 16-hex-digit sha256 prefix over the displayed card fields of *views* (in
    order), the viewer's role (it decides the action buttons) and timezone (it
    decides how the times read). ``GET /partials/projects`` answers 204 when
    the poller already holds this version (#70 F-P1).

    It is computed from the plain view values, never from the rendered HTML:
    that carries the CSRF token, which a later change masks per response.
    Datetimes count to the minute, the precision the card shows them at: a
    project that is due now has a "next capture" of *now*, which would
    otherwise change the version on every poll.
    """
    cards = tuple(
        tuple(
            _minute_precision(getattr(view, field)) for field in DASHBOARD_CARD_FIELDS
        )
        for view in views
    )
    material = repr((cards, role, viewer_timezone)).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


@dataclass(frozen=True)
class _CameraGeo:
    """Geolocation sub-object for the camera row template."""

    lat: float
    lon: float
    # How the coordinates were obtained: ``"manual"`` (operator-entered) or
    # ``"camera"`` (reported by the device). Drives the row's source indicator and
    # the edit form's source selector; defaults to ``"manual"`` when unrecorded.
    source: str


@dataclass(frozen=True)
class _CameraView:
    """Display projection of a camera row (never carries credentials)."""

    id: int
    name: str
    protocol: str | None
    address: str | None
    credentials_inherit_default: bool
    snapshot_uri: str | None
    stream_uri: str | None
    geolocation: _CameraGeo | None
    # The camera's network hostname and how it was obtained, for edit-form prefill.
    device_hostname: str | None
    device_hostname_source: str | None
    # The pinned certificate's SHA-256 fingerprint (colon form), or ``None`` when
    # the camera is not pinned. Never the certificate itself.
    tls_pinned_fingerprint: str | None = None


def _redacted_error(value: object) -> str | None:
    """Return a stored capture error with any URL login masked, or ``None``."""
    return redact_text(value) if isinstance(value, str) else None


def _without_userinfo(value: str | None) -> str | None:
    """Return a camera address or URI without any ``user:pass@`` part.

    Stripped rather than masked: the camera form pre-fills its fields from the
    view, and a masked value saved back would corrupt the URI. Startup moves
    such a login into the credential document; this is the defence for a row
    that is still waiting for that (#69 CAM-S2).
    """
    return split_userinfo(value)[0] if value else value


def _camera_view(camera: Camera) -> _CameraView:
    """Build a camera view model with no secret material."""
    geo: _CameraGeo | None = None
    lat = camera.geolocation_latitude
    lon = camera.geolocation_longitude
    if lat is not None and lon is not None:
        geo = _CameraGeo(lat=lat, lon=lon, source=camera.geolocation_source or "manual")
    return _CameraView(
        id=camera.id,
        name=camera.name,
        protocol=camera.protocol,
        address=_without_userinfo(camera.address),
        credentials_inherit_default=bool(camera.credentials_inherit_default),
        snapshot_uri=_without_userinfo(camera.snapshot_uri),
        stream_uri=_without_userinfo(camera.stream_uri),
        geolocation=geo,
        device_hostname=camera.device_hostname,
        device_hostname_source=camera.device_hostname_source,
        tls_pinned_fingerprint=pinned_fingerprint(camera),
    )


@dataclass(frozen=True)
class _PlaybackView:
    """What ``playability.js`` asks the viewer's browser about one render.

    Rendered as ``data-*`` attributes on a player slot (see
    ``_partials/_playback.html``). ``codec`` is the probed RFC 6381 string, or
    ``""`` when none is recorded (the page then checks the MIME type alone).
    The size, frame rate and bitrate feed ``mediaCapabilities.decodingInfo``;
    any of them may be ``None``, in which case the page uses ``canPlayType``.
    """

    codec: str
    mime: str
    # Viewer-facing codec and container for the "can't play" note
    # ("AV1 in MP4", "H.264 in MKV"): the container matters as much as the codec.
    label: str
    width: int | None
    height: int | None
    fps: float | None
    bitrate: int | None


def _playback_view(
    job: RenderJob, raw: dict[str, Any], size: int | None
) -> _PlaybackView:
    """Build the browser-check inputs for a finished render.

    ``size`` is the output file's size in bytes, when known; with the frame
    count and frame rate it gives the average bitrate.
    """
    fps = _as_number(raw.get("fps"))
    width, height = raw.get("width"), raw.get("height")
    mime = job.mime_type or (
        media_type_for_path(job.output_file_path)
        if job.output_file_path
        else media_type_for_container(raw.get("container"))
    )
    # The file on disk is the truth for the container; the requested settings
    # are the fallback when no output path is recorded.
    container = (
        PurePath(job.output_file_path).suffix if job.output_file_path else ""
    ) or str(raw.get("container") or "")
    bitrate: int | None = None
    if size and job.frame_count and fps:
        bitrate = int(size * 8 / (job.frame_count / fps))
    return _PlaybackView(
        codec=job.codec_string or "",
        mime=mime or "",
        label=playback_label(str(raw.get("codec") or "") or None, container or None),
        width=width if isinstance(width, int) and width > 0 else None,
        height=height if isinstance(height, int) and height > 0 else None,
        fps=fps if fps and fps > 0 else None,
        bitrate=bitrate if bitrate and bitrate > 0 else None,
    )


def _file_size(path: str | None) -> int | None:
    """Size of a render's output file, or ``None`` when it is missing.

    A cheap existence check for list rows; the stream and download routes
    still confine the path to the project's render root before serving it.
    """
    if not path:
        return None
    try:
        return os.stat(path).st_size if os.path.isfile(path) else None
    except OSError:
        return None


@dataclass(frozen=True)
class _RenderView:
    """Display projection of a render-job row (one line of a render list)."""

    id: int
    kind: str
    status: str
    created_at: str | None
    # Raw datetime for timezone-aware display via the localdt template filter.
    created_at_raw: datetime.datetime | None
    browser_streamable: bool | None
    download_url: str | None
    stream_url: str | None
    # The fragment the row's opener loads into the render card dialog.
    card_url: str
    # Shown on the global Renders page, where rows from every project mix; the
    # project's own Renders tab leaves it ``None``.
    project_name: str | None = None
    # The browser playability check's inputs; set whenever ``stream_url`` is.
    playback: _PlaybackView | None = None


def _render_view(job: RenderJob, *, project_name: str | None = None) -> _RenderView:
    """Build a render view model, translating status and deriving media URLs.

    Every finished render whose file exists gets a ``stream_url``: the
    viewer's browser decides whether it plays (``playability.js``), not the
    server. ``browser_streamable`` only picks the no-JS default.
    """
    display_status = _RENDER_STATUS_DISPLAY.get(job.status, job.status)
    is_done = job.status == "done"
    size = _file_size(job.output_file_path) if is_done else None
    playable = is_done and size is not None
    return _RenderView(
        id=job.id,
        kind=job.kind,
        status=display_status,
        created_at=_fmt_dt(job.created_at),
        created_at_raw=job.created_at,
        browser_streamable=job.browser_streamable,
        download_url=f"/renders/{job.id}/download" if is_done else None,
        stream_url=f"/renders/{job.id}/stream" if playable else None,
        card_url=f"/renders/{job.id}/card",
        project_name=project_name,
        playback=(
            _playback_view(job, dict(job.output_settings or {}), size)
            if playable
            else None
        ),
    )


def _export_view(job: RenderJob) -> _RenderView:
    """Build a row view model for a frame export (the Frames tab's Exports list)."""
    return _RenderView(
        id=job.id,
        kind=job.kind,
        status=_RENDER_STATUS_DISPLAY.get(job.status, job.status),
        created_at=_fmt_dt(job.created_at),
        created_at_raw=job.created_at,
        browser_streamable=False,
        download_url=(
            f"/frames/export/{job.id}/download" if job.status == "done" else None
        ),
        stream_url=None,
        card_url=f"/frames/export/{job.id}/card",
    )


@dataclass(frozen=True)
class _JobCardView:
    """Everything the render card (and the export card) shows about one job.

    Built at request time: the output file is stat-ed now, so a file removed
    behind the application's back shows as missing. Raw datetimes are carried for
    the ``localdt`` filter. A ``None`` in a "recorded" field means the job ran
    before that detail was stored -- the card says "not recorded".
    """

    id: int
    kind: str
    # Display status (queued/running/complete/error) and the stored one.
    status: str
    status_raw: str
    is_running: bool
    is_archive: bool
    project_id: int
    project_name: str
    created_at_raw: datetime.datetime | None
    started_at_raw: datetime.datetime | None
    completed_at_raw: datetime.datetime | None
    took_display: str | None
    # Output settings (renders only).
    codec: str | None
    container: str | None
    fps_display: str | None
    resolution_display: str | None
    # The frame window recorded by the worker.
    frame_count: int | None
    requested_frame_count: int | None
    first_frame_at_raw: datetime.datetime | None
    last_frame_at_raw: datetime.datetime | None
    video_length_display: str | None
    chapters: list[tuple[str, str]] | None
    overlay_display: str
    # The output file as found on disk now.
    file_name: str | None
    file_size_display: str | None
    file_missing: bool
    browser_streamable: bool
    # The probed RFC 6381 codec string (``None`` until probed).
    codec_string: str | None
    failure_reason: str | None
    triggered_by: str
    stream_url: str | None
    # The browser playability check's inputs; set whenever ``stream_url`` is.
    playback: _PlaybackView | None
    download_url: str | None
    cancel_url: str | None
    delete_url: str


def _job_card_view(
    db: DbSession, job: RenderJob, project: Project, settings: Any
) -> _JobCardView:
    """Build the card view for a render or export job."""
    from ...services.renders import output_file_info

    is_export = job.kind == "export"
    is_done = job.status == "done"
    raw: dict[str, Any] = dict(job.output_settings or {})
    path, size = output_file_info(job, project, settings)
    file_ok = is_done and path is not None
    fps = _as_number(raw.get("fps"))
    width, height = raw.get("width"), raw.get("height")
    if is_export:
        download_url = f"/frames/export/{job.id}/download" if file_ok else None
        stream_url = None
        delete_url = f"/frames/export/{job.id}/delete"
        cancel_url = None
    else:
        download_url = f"/renders/{job.id}/download" if file_ok else None
        stream_url = f"/renders/{job.id}/stream" if file_ok else None
        delete_url = f"/renders/{job.id}/delete"
        cancel_url = (
            f"/renders/{job.id}/cancel"
            if job.status in ("pending", "encoding")
            else None
        )
    return _JobCardView(
        id=job.id,
        kind=job.kind,
        status=_RENDER_STATUS_DISPLAY.get(job.status, job.status),
        status_raw=job.status,
        is_running=job.status in ("pending", "encoding"),
        is_archive=job.kind == "archive",
        project_id=project.id,
        project_name=project.name,
        created_at_raw=job.created_at,
        started_at_raw=job.started_at,
        completed_at_raw=job.completed_at,
        took_display=_took(job.started_at, job.completed_at),
        codec=None if is_export else str(raw.get("codec") or "h264"),
        container="zip" if is_export else str(raw.get("container") or "mp4"),
        fps_display=None if is_export or fps is None else f"{fps:g}",
        resolution_display=(
            None
            if is_export
            else (f"{width}\u00d7{height}" if width and height else "source size")
        ),
        frame_count=job.frame_count,
        requested_frame_count=(len(raw.get("frame_ids") or []) if is_export else None),
        first_frame_at_raw=job.first_frame_at,
        last_frame_at_raw=job.last_frame_at,
        video_length_display=(
            _video_length(job.frame_count, fps) if not is_export else None
        ),
        chapters=_chapter_rows(job.chapters),
        overlay_display=_overlay_display(job.overlay_config),
        file_name=path.name if path is not None else None,
        file_size_display=_fmt_bytes(size) if size is not None else None,
        file_missing=is_done and path is None,
        browser_streamable=bool(job.browser_streamable),
        codec_string=None if is_export else job.codec_string,
        failure_reason=(_failure_reason(db, job) if job.status == "failed" else None),
        triggered_by=_triggered_by(db, job),
        stream_url=stream_url,
        download_url=download_url,
        cancel_url=cancel_url,
        delete_url=delete_url,
        playback=(_playback_view(job, raw, size) if stream_url is not None else None),
    )


# How many of a project's newest finished renders the Latest Frame player
# looks through for one whose file is still on disk.
_LATEST_RENDER_SCAN = 20


@dataclass(frozen=True)
class _LatestRenderView:
    """The render the project page's Latest Frame plays inline (#24)."""

    id: int
    stream_url: str
    download_url: str
    card_url: str
    codec_string: str | None
    # Raw finish time for the ``localdt`` filter, and the video's length.
    completed_at_raw: datetime.datetime | None
    length_display: str | None
    playback: _PlaybackView


def _latest_render_view(
    db: DbSession, project: Project, settings: Any
) -> _LatestRenderView | None:
    """The project's newest finished render whose file exists, or ``None``.

    Renders only (frame exports share the table and are excluded), status
    ``done``, newest by finish time then id. A render whose file is missing or
    outside the render root is passed over for the next newest one. Whether the
    viewer's browser can play the chosen render is decided in the page; a
    render that fails that check does not fall back to an older one.
    """
    from ...services.renders import output_file_info

    jobs = (
        db.execute(
            select(RenderJob)
            .where(
                RenderJob.project_id == project.id,
                RenderJob.kind != "export",
                RenderJob.status == "done",
            )
            .order_by(RenderJob.completed_at.desc().nulls_last(), RenderJob.id.desc())
            .limit(_LATEST_RENDER_SCAN)
        )
        .scalars()
        .all()
    )
    for job in jobs:
        path, size = output_file_info(job, project, settings)
        if path is None:
            continue
        raw: dict[str, Any] = dict(job.output_settings or {})
        return _LatestRenderView(
            id=job.id,
            stream_url=f"/renders/{job.id}/stream",
            download_url=f"/renders/{job.id}/download",
            card_url=f"/renders/{job.id}/card",
            codec_string=job.codec_string,
            completed_at_raw=job.completed_at,
            length_display=_video_length(job.frame_count, _as_number(raw.get("fps"))),
            playback=_playback_view(job, raw, size),
        )
    return None


def _as_number(value: Any) -> float | None:
    """Coerce a stored numeric setting to ``float``, or ``None``."""
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _took(
    started: datetime.datetime | None, completed: datetime.datetime | None
) -> str | None:
    """How long the job ran, compactly, when both ends are recorded."""
    if started is None or completed is None:
        return None
    return _fmt_duration(max(0, int((completed - started).total_seconds())))


def _video_length(frame_count: int | None, fps: float | None) -> str | None:
    """The resulting video length for ``frame_count`` frames at ``fps``."""
    if frame_count is None or not fps:
        return None
    seconds = frame_count / fps
    if seconds < 60:
        return f"{seconds:.1f}s"
    return _fmt_duration(int(round(seconds)))


def _chapter_rows(chapters: Any) -> list[tuple[str, str]] | None:
    """Chapter list as ``(m:ss, label)`` pairs; ``None`` when never recorded."""
    if chapters is None:
        return None
    rows: list[tuple[str, str]] = []
    for item in chapters if isinstance(chapters, list) else []:
        if not isinstance(item, dict):
            continue
        seconds = _as_number(item.get("timecode_seconds")) or 0.0
        minutes, sec = divmod(int(seconds), 60)
        rows.append((f"{minutes}:{sec:02d}", str(item.get("label") or "")))
    return rows


def _overlay_display(overlay: Any) -> str:
    """Name the enabled overlays (``"Timestamp, Text"``), or ``"Off"``."""
    raw: dict[str, Any] = overlay if isinstance(overlay, dict) else {}
    names = [
        label
        for key, label in (
            ("timestamp_enabled", "Timestamp"),
            ("text_enabled", "Text"),
            ("image_enabled", "Image"),
        )
        if raw.get(key)
    ]
    return ", ".join(names) if names else "Off"


# See _not_before_job.
_EVENT_SLACK = datetime.timedelta(seconds=1)


def _not_before_job(job: RenderJob) -> Any:
    """Filter to events written no earlier than ``job`` was created.

    Job ids are never reused (AUTOINCREMENT, migration 028), but databases
    that reused ids before that migration still hold events naming an older,
    deleted job by the same id. Every event about a job is written after its
    row is inserted, so anything earlier belongs to another job. One second of
    slack: ``created_at`` is the database clock in whole seconds, and SQLite
    compares these columns as text, where an event stamped by the database in
    the same second (``...:12``) sorts before the bound ``...:12.000000``.
    """
    if job.created_at is None:
        return true()
    return Event.timestamp >= job.created_at - _EVENT_SLACK


def _failure_reason(db: DbSession, job: RenderJob) -> str | None:
    """The newest ``render.failed`` or ``render.cancelled`` reason, or ``None``.

    The worker records why a job failed only as an event whose metadata carries
    the job's id; the ``render N failed:`` prefix it adds is stripped. A job a
    user cancelled also ends ``failed``; its ``render N cancelled by <user>``
    event gives ``cancelled by <user>``. Events older than the job are ignored
    (see :func:`_not_before_job`).
    """
    from ...monitoring.events import EventType

    rows = (
        db.execute(
            select(Event)
            .where(Event.scope == "project", Event.scope_id == job.project_id)
            .where(_not_before_job(job))
            .where(Event.event_metadata["render_id"].as_integer() == job.id)
            .order_by(Event.id.desc())
        )
        .scalars()
        .all()
    )
    for event in rows:
        meta = event.event_metadata or {}
        event_type = meta.get("type")
        if event_type == EventType.RENDER_FAILED.value:
            prefix = f"render {job.id} failed: "
        elif event_type == EventType.RENDER_CANCELLED.value:
            # A user's cancel ends the job failed too; the reason names who.
            prefix = f"render {job.id} "
        else:
            continue
        message = event.message
        return message[len(prefix) :] if message.startswith(prefix) else message
    return None


def _triggered_by(db: DbSession, job: RenderJob) -> str:
    """Who asked for the job: a username, ``"schedule"``, or ``"not recorded"``.

    Scheduled and archive renders come from the render schedule. A manual render
    or an export is attributed through the audit event the service wrote when it
    was queued (matched exactly, so render 1 never matches render 12, and never
    older than the job itself).
    """
    if job.kind in ("scheduled", "archive"):
        return "schedule"
    message = (
        f"export {job.id} triggered"
        if job.kind == "export"
        else f"manual render {job.id} triggered"
    )
    event = db.execute(
        select(Event)
        .where(Event.scope == "project", Event.scope_id == job.project_id)
        .where(Event.message == message)
        .where(_not_before_job(job))
        .order_by(Event.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if event is None or event.actor_user_id is None:
        return "not recorded"
    user = db.get(User, event.actor_user_id)
    return user.username if user is not None else "not recorded"


@dataclass(frozen=True)
class AlertScopeLink:
    """What an alert is about, for the alerts panel: a label and a link.

    ``url`` is ``None`` for a system alert or an entity that no longer exists;
    the panel then shows the label as plain text.
    """

    label: str
    url: str | None


def alert_scope_links(
    db: DbSession, scopes: list[tuple[str, int | None]]
) -> dict[tuple[str, int | None], AlertScopeLink]:
    """Resolve alert ``(scope, scope_id)`` pairs to a display label and link.

    Camera health alerts are camera-scoped since #69 (CAM-R6), so the panel
    names the camera (linking to its row on the Cameras page) rather than
    showing a bare ``camera #3``. Project alerts name and link the project.
    Names are read in at most two queries, whatever the number of alerts. A
    deleted camera or project keeps the ``<scope> #<id>`` fallback, unlinked.
    """
    camera_ids = {sid for scope, sid in scopes if scope == "camera" and sid}
    project_ids = {sid for scope, sid in scopes if scope == "project" and sid}
    camera_names: dict[int, str] = {}
    project_names: dict[int, str] = {}
    if camera_ids:
        camera_names = dict(
            db.execute(select(Camera.id, Camera.name).where(Camera.id.in_(camera_ids)))
            .tuples()
            .all()
        )
    if project_ids:
        project_names = dict(
            db.execute(
                select(Project.id, Project.name).where(Project.id.in_(project_ids))
            )
            .tuples()
            .all()
        )
    links: dict[tuple[str, int | None], AlertScopeLink] = {}
    for scope, scope_id in scopes:
        if scope == "camera" and scope_id in camera_names:
            links[(scope, scope_id)] = AlertScopeLink(
                label=f"camera {camera_names[scope_id]}",
                url=f"/cameras#camera-row-{scope_id}",
            )
        elif scope == "project" and scope_id in project_names:
            links[(scope, scope_id)] = AlertScopeLink(
                label=f"project {project_names[scope_id]}",
                url=f"/projects/{scope_id}",
            )
        else:
            suffix = f" #{scope_id}" if scope_id is not None else ""
            links[(scope, scope_id)] = AlertScopeLink(
                label=f"{scope}{suffix}", url=None
            )
    return links
