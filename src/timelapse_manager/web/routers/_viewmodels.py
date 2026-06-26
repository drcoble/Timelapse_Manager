"""Display projections for the web routers: view dataclasses and their
builders, plus the datetime/byte/duration formatters and status-vocabulary
translation the templates rely on."""

from __future__ import annotations

import datetime
import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ...db.models import Camera, Frame, Project, RenderJob
from ...render import settings as render_settings
from ...runtime import get_context
from ..interval import decompose_seconds

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
    # Pre-rendered storage growth rate (e.g. ``"12.4 MB / day"``); ``None`` when
    # the project has not captured enough to measure a rate yet, in which case the
    # template shows a "not enough data" placeholder.
    growth_rate_display: str | None
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


def _project_view(db: DbSession, project: Project) -> _ProjectView:
    """Build a project view model, consulting the supervisor for live status.

    The latest active frame is fetched once and reused for both the preview URL
    and the next-capture estimate. On the projects list this is one indexed
    lookup per project (an accepted N+1 for the modest project counts the UI
    targets); a batched single-query variant would be the optimisation if that
    ever grows.
    """
    from ...storage import estimator
    from ...storage import frames as frame_service

    supervisor = get_context().capture_supervisor
    state = supervisor.state_for_project(project.id) if supervisor is not None else None
    last_capture = state.last_capture_at if state is not None else None

    latest_frame = frame_service.latest_active_frame(db, project.id)
    latest_frame_url = (
        f"/projects/{project.id}/frames/{latest_frame.id}/image"
        if latest_frame is not None
        else None
    )
    disk_used = frame_service.sum_project_disk_usage(db, project.id)
    uptime_seconds = _runner_uptime_seconds(state)
    next_capture_raw = _next_capture_dt(project, latest_frame)
    projected_bytes, projected_remaining = estimator.estimate_for_project(db, project)
    projected_open_ended = projected_bytes is None
    projected_span_days = _projected_span_days(project)
    growth_rate = estimator.estimate_growth_rate_bytes_per_day(db, project)
    interval_value, interval_unit = decompose_seconds(project.capture_interval_seconds)
    camera = db.get(Camera, project.camera_id)
    camera_name = camera.name if camera is not None else f"Camera {project.camera_id}"
    operational_status = _project_operational_status(project, state)
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
        is_overdue=_capture_is_overdue(
            operational_status,
            project.frame_count,
            last_capture,
            project.capture_interval_seconds,
        ),
        last_error_message=(state.last_error if state is not None else None),
        last_error_at_raw=(state.last_error_at if state is not None else None),
        latest_frame_url=latest_frame_url,
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


def _next_capture_dt(
    project: Project, latest_frame: Frame | None
) -> datetime.datetime | None:
    """Raw estimated next-capture datetime (last capture + interval).

    Returns ``None`` when there is no frame, no timestamp, or no interval.
    """
    interval = project.capture_interval_seconds
    if latest_frame is None or latest_frame.capture_timestamp is None or not interval:
        return None
    return latest_frame.capture_timestamp + datetime.timedelta(seconds=interval)


def _next_capture_at(project: Project, latest_frame: Frame | None) -> str | None:
    """Estimate when the next still is due: last capture + the interval.

    A pure projection from the latest frame's capture time plus the project's
    configured interval -- not a scheduled instant. The template only surfaces it
    while the project reads as ``running``. Returns ``None`` when there is no
    frame yet, the latest frame has no timestamp, or no interval is configured.
    """
    raw = _next_capture_dt(project, latest_frame)
    return _fmt_dt(raw) if raw is not None else None


def _project_views(db: DbSession) -> list[_ProjectView]:
    """Return view models for every project, ordered by id."""
    projects = db.execute(select(Project).order_by(Project.id)).scalars().all()
    return [_project_view(db, p) for p in projects]


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
        address=camera.address,
        credentials_inherit_default=bool(camera.credentials_inherit_default),
        snapshot_uri=camera.snapshot_uri,
        stream_uri=camera.stream_uri,
        geolocation=geo,
        device_hostname=camera.device_hostname,
        device_hostname_source=camera.device_hostname_source,
    )


@dataclass(frozen=True)
class _RenderView:
    """Display projection of a render-job row."""

    id: int
    kind: str
    status: str
    created_at: str | None
    # Raw datetime for timezone-aware display via the localdt template filter.
    created_at_raw: datetime.datetime | None
    browser_streamable: bool | None
    download_url: str | None
    stream_url: str | None


def _render_view(job: RenderJob) -> _RenderView:
    """Build a render view model, translating status and deriving media URLs."""
    display_status = _RENDER_STATUS_DISPLAY.get(job.status, job.status)
    is_done = job.status == "done"
    return _RenderView(
        id=job.id,
        kind=job.kind,
        status=display_status,
        created_at=_fmt_dt(job.created_at),
        created_at_raw=job.created_at,
        browser_streamable=job.browser_streamable,
        download_url=f"/renders/{job.id}/download" if is_done else None,
        stream_url=(
            f"/renders/{job.id}/stream" if is_done and job.browser_streamable else None
        ),
    )
