"""Dashboard and project-overview routes: the home/dashboard, project list,
project creation, and project detail pages."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
)
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ...db.models import Camera, Event, Frame, Project, RenderJob, User
from ...render import settings as render_settings
from ...runtime import get_context
from ...storage import estimator, paths
from ...storage.monitor import _default_free_bytes
from .. import dependencies as deps
from .. import ribbon as ribbon_svg
from ..dependencies import (
    CurrentUser,
    DbDep,
    FormDep,
    OperatorUser,
    templates,
)
from ..interval import parse_interval_to_seconds
from ._shared import (
    COMMON_TIMEZONES,
    EXACT_TIME_MARKER,
    _audit,
    _build_exact_time_anchors_from_form,
    _build_schedule_from_form,
    _campaign_bounds_error,
    _form_getlist,
    _get_project_or_404,
    _parse_optional_datetime,
    _parse_optional_positive_int,
    _parse_ptz_fields,
    _parse_render_settings_field,
    _resolve_stream_label,
    capture_mode_of,
)
from ._viewmodels import (
    _camera_view,
    _fmt_bytes,
    _project_view,
    _project_views,
    _render_view,
)
from .milestones import milestone_views

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: DbDep, user: CurrentUser) -> Response:
    """Render the dashboard: project status cards for every project."""
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        deps.base_context(request, db, user, projects=_project_views(db)),
    )


@router.get("/partials/projects", response_class=HTMLResponse)
def partial_projects(request: Request, db: DbDep, user: CurrentUser) -> Response:
    """Return the dashboard project grid fragment for HTMX polling.

    Returns the full ``project-grid`` wrapper (or the empty state) so the layout
    class survives the swap, matching the dashboard's inner content. Only active
    projects are shown, mirroring the dashboard's polled region. The grid is
    assembled from the per-card template so card markup stays single-sourced.
    """
    active = [p for p in _project_views(db) if p.lifecycle_state == "active"]
    if not active:
        body = (
            '<div class="empty-state">'
            '<div class="empty-state-title">No active projects</div>'
            "</div>"
        )
    else:
        env = templates.env
        card = env.get_template("_partials/project_status_card.html")
        context = deps.base_context(request, db, user)
        cards = "".join(
            card.render({**context, "project": project}) for project in active
        )
        body = f'<div class="project-grid">{cards}</div>'
    return HTMLResponse(body)


@router.get("/partials/projects/{project_id}/status", response_class=HTMLResponse)
def partial_project_status(
    request: Request, db: DbDep, user: CurrentUser, project_id: int
) -> Response:
    """Return a single project's status card fragment for HTMX polling."""
    project = _get_project_or_404(db, project_id)
    return templates.TemplateResponse(
        request,
        "_partials/project_status_card.html",
        deps.base_context(request, db, user, project=_project_view(db, project)),
    )


def _aware(value: datetime) -> datetime:
    """Coerce a stored (naive UTC) datetime to an aware UTC datetime."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@router.get("/partials/projects/{project_id}/ribbon", response_class=HTMLResponse)
def partial_project_ribbon(
    request: Request,
    db: DbDep,
    user: CurrentUser,
    project_id: int,
    h: int = 20,
    decorative: bool = False,
    window_start: int | None = None,
    window_end: int | None = None,
) -> Response:
    """Return the time-ribbon SVG fragment for a project.

    Lazy-loaded by the dashboard card and the project detail header so its DB
    work stays off the card-render / poll critical path. ``h`` selects the
    height variant (12 compact / 20 card / 36 detail). ``decorative`` hides the
    SVG from assistive tech — set only when it loads into a labelled control
    (the frames scrubber's ``role=slider`` wrapper).

    ``window_start``/``window_end`` (epoch seconds) request a **zoom strip**: a
    finer ribbon clamped to that sub-range of the campaign, used by the frames
    scrubber to magnify a long campaign around the loaded window. Supplying both
    forces the interactive + zoom variant regardless of ``h`` (so a click still
    drives a jump) and suppresses the now-cursor when the window is wholly past.
    """
    project = _get_project_or_404(db, project_id)
    now = datetime.now(UTC)

    # Full-campaign bounds — the envelope a zoom window is clamped within.
    camp_start_raw = project.start_date
    camp_end_raw = project.end_date or now

    rows = (
        db.execute(
            select(Frame.capture_timestamp)
            .where(
                Frame.project_id == project_id,
                Frame.capture_timestamp.is_not(None),
                Frame.lifecycle_state == "active",
            )
            .order_by(Frame.capture_timestamp)
            .limit(5000)
        )
        .scalars()
        .all()
    )
    frame_times = [_aware(t) for t in rows if t is not None]

    start = _aware(
        camp_start_raw or (frame_times[0] if frame_times else now - timedelta(hours=1))
    )
    end = _aware(camp_end_raw)
    if end <= start:
        end = start + timedelta(hours=1)

    # Zoom mode: clamp the requested window to the campaign and re-window the
    # frame ticks. Falls back to the full span if the window is empty/inverted.
    zoom = False
    draw_now = True
    if window_start is not None and window_end is not None:
        w_start: datetime | None = None
        w_end: datetime | None = None
        try:
            w_start = max(start, _aware(datetime.fromtimestamp(window_start, UTC)))
            w_end = min(end, _aware(datetime.fromtimestamp(window_end, UTC)))
        except (OverflowError, OSError, ValueError):
            # An out-of-range epoch is a malformed request, not a 500 — ignore
            # the window and fall back to the full-span ribbon.
            w_start = w_end = None
        if w_start is not None and w_end is not None and w_end > w_start:
            zoom = True
            start, end = w_start, w_end
            wrows = (
                db.execute(
                    select(Frame.capture_timestamp)
                    .where(
                        Frame.project_id == project_id,
                        Frame.capture_timestamp.is_not(None),
                        Frame.lifecycle_state == "active",
                        Frame.capture_timestamp >= start.replace(tzinfo=None),
                        Frame.capture_timestamp <= end.replace(tzinfo=None),
                    )
                    .order_by(Frame.capture_timestamp)
                    .limit(5000)
                )
                .scalars()
                .all()
            )
            frame_times = [_aware(t) for t in wrows if t is not None]
            draw_now = start <= now <= end

    camera = db.get(Camera, project.camera_id)
    lat = camera.geolocation_latitude if camera else None
    lon = camera.geolocation_longitude if camera else None

    height = 12 if h <= 12 else 36 if h >= 36 else 20
    variant = "zoom" if zoom else {12: "compact", 36: "detail"}.get(height, "")

    svg = ribbon_svg.build_svg(
        start=start,
        end=end,
        now=now,
        height=height,
        frame_times=frame_times,
        latitude=lat,
        longitude=lon,
        label=(
            f"Zoomed capture timeline for {project.name}"
            if zoom
            else f"Capture timeline for {project.name}"
        ),
        interactive=zoom or height >= 36,
        decorative=decorative,
        draw_now=draw_now,
    )
    cls = "time-ribbon" + (f" time-ribbon--{variant}" if variant else "")
    # Epoch bounds let the interactive variant map a click x-fraction back to a
    # timestamp (consumed by the frames browser in a later phase).
    return HTMLResponse(
        f'<div class="{cls}" data-start="{int(start.timestamp())}" '
        f'data-end="{int(end.timestamp())}">{svg}</div>'
    )


@router.get("/projects/preflight", response_class=HTMLResponse)
def project_preflight(
    request: Request,
    db: DbDep,
    user: OperatorUser,
    capture_interval_value: str = "",
    capture_interval_unit: str = "seconds",
) -> Response:
    """Storage pre-flight: estimate a new project's daily growth vs free disk.

    Lazy-loaded as the operator sets the capture interval in the create form, so
    they see the storage impact before committing. Registered before
    ``/projects/{project_id}`` so the literal path wins. Returns an empty body
    until a valid interval is entered.
    """
    seconds, _err = parse_interval_to_seconds(
        capture_interval_value, capture_interval_unit
    )
    if seconds is None:
        return HTMLResponse("")
    settings = get_context().settings
    try:
        free = _default_free_bytes(paths.frames_root(settings))
    except Exception:  # noqa: BLE001
        free = 0
    bytes_per_day = estimator.estimate_create_time_bytes_per_day(seconds)
    level = estimator.preflight_level(bytes_per_day, free)
    days = int(free / bytes_per_day) if bytes_per_day > 0 else None
    return templates.TemplateResponse(
        request,
        "_partials/preflight_banner.html",
        {
            "request": request,
            "level": level,
            "days": days,
            "per_day_display": _fmt_bytes(bytes_per_day),
            "free_display": _fmt_bytes(free),
        },
    )


@router.get("/projects", response_class=HTMLResponse)
def projects_page(request: Request, db: DbDep, user: CurrentUser) -> Response:
    """Render the full projects list."""
    return templates.TemplateResponse(
        request,
        "projects.html",
        deps.base_context(request, db, user, projects=_project_views(db)),
    )


# NOTE: ``GET /projects/new`` and ``POST /projects`` are registered here, BEFORE
# ``GET /projects/{project_id}``. ``project_id`` is typed ``int``, so a request
# for ``/projects/new`` would 422 (not silently 404) rather than fall through to
# the create form -- registration order is what routes ``new`` correctly.


def _new_project_schedule_context() -> dict[str, Any]:
    """Template context the create form's capture-schedule partial consumes.

    A brand-new project has no persisted schedule, so ``current_schedule`` is
    ``None`` (the partial then defaults its preset to *always*). ``timezones``
    populates the timezone picker. ``current_render_settings`` carries a safe
    default so a render-settings control (e.g. the auto-chapters select) renders
    without an undefined lookup. Shared by both create GET handlers and the
    create POST's error re-render so every render path supplies the same keys.
    """
    return {
        "timezones": list(COMMON_TIMEZONES),
        "current_schedule": None,
        "current_render_settings": {"auto_chapters": "none"},
        # A brand-new project has no anchors yet. The camera is chosen in the same
        # form, so solar-noon is offered (True) and the submit-time validation
        # rejects it with a clear message if the chosen camera lacks a location.
        "current_anchors": [],
        "camera_has_geolocation": True,
        # No camera bound yet, so there is nothing to preview; the partial renders
        # an empty (but present) swap target.
        "solar_preview": None,
        # New projects default to continuous interval capture.
        "capture_mode": "interval",
    }


@router.get("/projects/new", response_class=HTMLResponse)
def new_project_form(request: Request, db: DbDep, user: OperatorUser) -> Response:
    """Render the create-project form with the list of cameras to choose from."""
    cameras = db.execute(select(Camera).order_by(Camera.id)).scalars().all()
    return templates.TemplateResponse(
        request,
        "new_project.html",
        deps.base_context(
            request,
            db,
            user,
            cameras=[_camera_view(c) for c in cameras],
            **_new_project_schedule_context(),
        ),
    )


@router.get("/drawers/new-project", response_class=HTMLResponse)
def new_project_drawer(request: Request, db: DbDep, user: OperatorUser) -> Response:
    """Serve the create-project form as a drawer fragment, or the full page.

    An HTMX request gets just the form fragment to swap into the drawer body; a
    direct (no-JS) request gets the standalone page, so the create flow still
    works without the drawer.
    """
    cameras = db.execute(select(Camera).order_by(Camera.id)).scalars().all()
    context = deps.base_context(
        request,
        db,
        user,
        cameras=[_camera_view(c) for c in cameras],
        **_new_project_schedule_context(),
    )
    template = (
        "_partials/drawer_new_project.html"
        if request.headers.get("HX-Request")
        else "new_project.html"
    )
    return templates.TemplateResponse(request, template, context)


async def _new_project_form_error(
    request: Request, db: DbSession, user: User, message: str
) -> Response:
    """Re-render the create-project form with an error toast and a 400 status.

    Preserves the submitted capture mode (read from the already-parsed, cached
    form) so an error re-render does not silently flip a solar / scheduled-times
    project back to continuous interval capture.
    """
    cameras = db.execute(select(Camera).order_by(Camera.id)).scalars().all()
    context = deps.base_context(
        request,
        db,
        user,
        flash_messages=[{"type": "error", "message": message}],
        cameras=[_camera_view(c) for c in cameras],
        **_new_project_schedule_context(),
    )
    # The handler stashes the submitted mode on request.state before any
    # validation, so every error re-render keeps the operator's choice.
    context["capture_mode"] = getattr(request.state, "capture_mode", "interval")
    return templates.TemplateResponse(
        request,
        "new_project.html",
        context,
        status_code=status.HTTP_400_BAD_REQUEST,
    )


@router.post("/projects")
async def create_project(
    request: Request, db: DbDep, user: OperatorUser, form: FormDep
) -> Response:
    """Create a project from the submitted form, then start capturing it live.

    Validates the submitted name (non-empty and unique), camera (must exist and
    be configured with a protocol), and capture interval (a positive integer).
    Any validation failure re-renders the form with a clear message and a 400 --
    a duplicate name never surfaces as a 500. On success the project is committed
    and the running capture supervisor is notified so capture begins without a
    process restart, then the browser is redirected to the new project's page.
    """
    from sqlalchemy.exc import IntegrityError

    # Capture mode selects between continuous interval capture and "solar /
    # scheduled times only" (no recurring interval; captures fire solely from the
    # exact-time/solar anchors). Stashed on request.state up front so every error
    # re-render below preserves the operator's choice.
    capture_mode = capture_mode_of(form)
    request.state.capture_mode = capture_mode

    name = form.get("name", "").strip()
    if not name:
        return await _new_project_form_error(
            request, db, user, "Project name is required."
        )

    try:
        camera_id = int(form.get("camera_id", ""))
    except ValueError:
        return await _new_project_form_error(request, db, user, "Select a camera.")
    # Solar mode stores a null interval -- the runner already treats that as
    # anchor-only; interval mode requires a positive interval as before.
    if capture_mode == "solar":
        capture_interval_seconds: int | None = None
    else:
        capture_interval_seconds, err = parse_interval_to_seconds(
            form.get("capture_interval_value", ""),
            form.get("capture_interval_unit", ""),
        )
        if err:
            return await _new_project_form_error(request, db, user, err)
        assert capture_interval_seconds is not None  # err is None implies a value

    camera = db.get(Camera, camera_id)
    if camera is None:
        return await _new_project_form_error(
            request, db, user, "Selected camera not found."
        )
    if camera.protocol is None:
        return await _new_project_form_error(
            request,
            db,
            user,
            "The selected camera has no protocol configured; set one before "
            "creating a project for it.",
        )

    # Optional campaign bounds (separate from the daily capture window). Parse and
    # validate before touching the row so an error re-render's camera SELECT cannot
    # autoflush a half-built project.
    start_date, err = _parse_optional_datetime(form.get("start_date"), "Start date")
    if err:
        return await _new_project_form_error(request, db, user, err)
    end_date, err = _parse_optional_datetime(form.get("end_date"), "End date")
    if err:
        return await _new_project_form_error(request, db, user, err)
    err = _campaign_bounds_error(start_date, end_date)
    if err:
        return await _new_project_form_error(request, db, user, err)
    max_frame_count, err = _parse_optional_positive_int(
        form.get("max_frame_count"), "Maximum frame count"
    )
    if err:
        return await _new_project_form_error(request, db, user, err)

    # Pre-check uniqueness for a friendly message; the UNIQUE constraint plus the
    # explicit flush below is the authoritative guard against a concurrent insert.
    existing = db.execute(
        select(Project.id).where(Project.name == name)
    ).scalar_one_or_none()
    if existing is not None:
        return await _new_project_form_error(
            request, db, user, f"A project named {name!r} already exists."
        )

    # Stream/profile selection. Blank means "use the camera default" (both columns
    # null). A chosen id stores the id plus a best-effort human label; the label
    # lookup re-contacts the camera but never blocks the save (falls back to the
    # id). Resolved here -- after all validation passes -- so it does not run on a
    # request that will re-render the form. Note: enumerating profiles detaches the
    # camera row from the session, so it must follow the camera validation above.
    stream_id_value = (form.get("stream_profile") or "").strip() or None
    stream_label_value: str | None = None
    if stream_id_value is not None:
        stream_label_value = await _resolve_stream_label(db, camera, stream_id_value)

    # Render settings from the (optional) dropdowns. The create form has no render
    # UI today, so absent fields yield a safe disabled-default schedule; an
    # unsupported combination (only reachable when the fields are present) is
    # rejected with a 400 like every other field.
    render_schedule, err = _parse_render_settings_field(form)
    if err:
        return await _new_project_form_error(request, db, user, err)

    # PTZ position: an optional named preset and/or a raw pan/tilt/zoom. Parsed
    # before the row is built so a bad value re-renders the form rather than
    # constructing a half-valid project.
    ptz_preset, ptz_pan, ptz_tilt, ptz_zoom, err = _parse_ptz_fields(form)
    if err:
        return await _new_project_form_error(request, db, user, err)

    # Capture schedule (the daily window/day/timezone gate, distinct from the
    # campaign bounds above). Guard on the preset field's presence: a form without
    # the schedule fieldset (an older/no-schedule POST) yields ``schedule=None`` --
    # an always-open gate -- rather than the builder's "always" default, so such a
    # create still works. The repeated ``capture_days`` checkbox is read from the
    # raw body via ``_form_getlist`` (the form parser collapses repeated keys).
    if form.get("capture_schedule_preset") is not None:
        capture_days = await _form_getlist(request, "capture_days")
        schedule, err = _build_schedule_from_form(form, capture_days)
        if err:
            return await _new_project_form_error(request, db, user, err)
    else:
        schedule = None

    # Exact-time captures (once-per-day single-shot anchors, independent of the
    # interval cadence and the schedule window). Guarded on the presence marker
    # like the schedule above so a POST without the fieldset leaves a project with
    # no anchors rather than clearing them by accident.
    if form.get(EXACT_TIME_MARKER) is not None:
        exact_time_anchors, err = await _build_exact_time_anchors_from_form(
            request, camera
        )
        if err:
            return await _new_project_form_error(request, db, user, err)
    else:
        exact_time_anchors = None

    # Solar / scheduled-times-only projects capture solely from their anchors, so
    # at least one enabled anchor is required or the project would never capture.
    if capture_mode == "solar" and not any(
        a.get("enabled") for a in (exact_time_anchors or [])
    ):
        return await _new_project_form_error(
            request,
            db,
            user,
            "Solar / scheduled-times mode needs at least one enabled capture time "
            "(e.g. solar noon). Add one or switch to interval capture.",
        )

    project = Project(
        camera_id=camera_id,
        name=name,
        capture_interval_seconds=capture_interval_seconds,
        start_date=start_date,
        end_date=end_date,
        max_frame_count=max_frame_count,
        schedule=schedule,
        exact_time_anchors=exact_time_anchors,
        stream_id=stream_id_value,
        stream_label=stream_label_value,
        render_schedule=render_schedule,
        ptz_preset=ptz_preset,
        ptz_pan=ptz_pan,
        ptz_tilt=ptz_tilt,
        ptz_zoom=ptz_zoom,
        lifecycle_state="active",
    )
    db.add(project)
    try:
        db.flush()
    except IntegrityError:
        # Lost the uniqueness race against a concurrent create. Roll back so the
        # poisoned session can serve the form re-render, then report cleanly.
        db.rollback()
        return await _new_project_form_error(
            request, db, user, f"A project named {name!r} already exists."
        )

    _audit(
        db,
        scope="project",
        scope_id=project.id,
        actor_user_id=user.id,
        message=f"project {project.id} created",
    )
    # Commit explicitly before notifying: the supervisor reconciles in a fresh
    # session on another thread and would not see an uncommitted row.
    project_id = project.id
    db.commit()
    supervisor = get_context().capture_supervisor
    if supervisor is not None:
        supervisor.notify_reconcile()
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)


def _human_duration(seconds: float) -> str:
    """A coarse human duration ("45 seconds", "12 minutes", "2.5 hours")."""
    if seconds < 90:
        return f"{round(seconds)} seconds"
    if seconds < 5400:
        return f"{round(seconds / 60)} minutes"
    if seconds < 172800:
        return f"{round(seconds / 3600, 1)} hours"
    return f"{round(seconds / 86400, 1)} days"


def _fps_hint(frame_count: int, interval_seconds: int, fps: int) -> str:
    """One sentence relating capture span to playback length at a frame rate."""
    if not frame_count or not fps:
        return ""
    captured = _human_duration(frame_count * max(1, interval_seconds))
    played = _human_duration(frame_count / fps)
    return (
        f"At {fps} fps, {frame_count} frames ({captured} of capture) play in "
        f"about {played}."
    )


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_detail(
    request: Request, db: DbDep, user: CurrentUser, project_id: int
) -> Response:
    """Render a project's detail page with its recent renders + activity."""
    project = _get_project_or_404(db, project_id)
    project_view = _project_view(db, project)
    render_view = render_settings.render_settings_view(project.render_schedule)
    jobs = (
        db.execute(
            select(RenderJob)
            .where(RenderJob.project_id == project_id)
            .order_by(RenderJob.id.desc())
            .limit(10)
        )
        .scalars()
        .all()
    )
    # Recent activity for this project (newest first) for the Status mini-log.
    recent_events = (
        db.execute(
            select(Event)
            .where(Event.scope == "project", Event.scope_id == project_id)
            .order_by(Event.id.desc())
            .limit(5)
        )
        .scalars()
        .all()
    )
    return templates.TemplateResponse(
        request,
        "project_detail.html",
        deps.base_context(
            request,
            db,
            user,
            project=project_view,
            renders=[_render_view(j) for j in jobs],
            milestones=milestone_views(db, project_id),
            recent_events=recent_events,
            render_view=render_view,
            render_encoder_options=render_settings.ENCODER_OPTIONS,
            render_container_options=render_settings.CONTAINER_OPTIONS,
            render_resolution_options=render_settings.RESOLUTION_OPTIONS,
            render_fps_suggestions=render_settings.suggested_fps(
                project.capture_interval_seconds or 60
            ),
            fps_hint=_fps_hint(
                project_view.frame_count or 0,
                project.capture_interval_seconds or 60,
                int(render_view["fps"]),
            ),
        ),
    )
