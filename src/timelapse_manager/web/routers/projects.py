"""Project edit/clone/delete and lifecycle routes, including the project-
action catch-all."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
)
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ...cameras.base import PTZPresetsResult, StreamProfileResult
from ...db.models import Camera, Project, User
from ...render import settings as render_settings
from ...runtime import get_context
from .. import dependencies as deps
from ..dependencies import (
    DbDep,
    FormDep,
    OperatorUser,
    templates,
)
from ..interval import parse_interval_to_seconds
from ._shared import (
    COMMON_TIMEZONES,
    EVENT_TRIGGERS_MARKER,
    EXACT_TIME_MARKER,
    EventTopicsResult,
    _audit,
    _build_event_triggers_from_form,
    _build_exact_time_anchors_from_form,
    _build_schedule_from_form,
    _camera_has_geolocation,
    _campaign_bounds_error,
    _enumerate_event_topics,
    _enumerate_ptz_presets,
    _enumerate_stream_profiles,
    _event_triggers_to_form,
    _exact_time_anchors_to_form,
    _form_getlist,
    _get_project_or_404,
    _parse_optional_datetime,
    _parse_optional_json_field,
    _parse_optional_positive_int,
    _parse_ptz_fields,
    _parse_render_settings_field,
    _post_actions_field_error,
    _resolve_stream_label,
    _schedule_field_error,
    _schedule_to_form,
    _settings,
    build_solar_preview,
    build_solar_preview_from_rows,
    capture_mode_of,
)
from ._viewmodels import (
    _camera_view,
    _project_view,
)

logger = logging.getLogger(__name__)

router = APIRouter()


async def _settings_form_context(
    request: Request, db: DbSession, user: User, project: Project
) -> dict[str, object]:
    """Assemble the context the project-settings form needs.

    Shared by the standalone settings page and the in-page settings tab fragment
    so the two cannot drift. The stream-profile and PTZ pickers are server-
    rendered for the saved camera (preselected on load); enumeration is best-
    effort -- an unreachable camera renders the partial's inline notice rather
    than failing.
    """
    project_view = _project_view(db, project)
    cameras = db.execute(select(Camera).order_by(Camera.id)).scalars().all()

    # Each enumerator detaches (``db.expunge``) the camera it probes, so the saved
    # camera is re-fetched between the two probes.
    camera = db.get(Camera, project.camera_id)
    if camera is None:
        stream_result = StreamProfileResult(profiles=[], ok=False)
    else:
        stream_result = await _enumerate_stream_profiles(db, camera)

    camera = db.get(Camera, project.camera_id)
    if camera is None:
        ptz_result = PTZPresetsResult(presets=[], ptz_supported=False, ok=False)
    else:
        ptz_result = await _enumerate_ptz_presets(db, camera)

    # Each probe detaches the camera, so re-fetch before the event-topic probe.
    camera = db.get(Camera, project.camera_id)
    if camera is None:
        events_result = EventTopicsResult(events=[], ok=False)
    else:
        events_result = await _enumerate_event_topics(db, camera)

    # Expose the persisted auto-chapters choice so the schedule template selects
    # the saved option. Normalized to none/weekly/monthly so a stale or
    # hand-edited value still renders as a valid select option.
    current_render_settings = {
        "auto_chapters": render_settings.auto_chapters_choice(project.render_schedule)
    }

    return deps.base_context(
        request,
        db,
        user,
        project=project_view,
        cameras=[_camera_view(c) for c in cameras],
        stream_profiles=stream_result.profiles,
        stream_profiles_ok=stream_result.ok,
        stream_selected_id=project.stream_id,
        ptz_presets=ptz_result.presets,
        ptz_presets_ok=ptz_result.ok,
        ptz_supported=ptz_result.ptz_supported,
        ptz_selected_preset_id=project.ptz_preset,
        ptz_pan=project.ptz_pan,
        ptz_tilt=project.ptz_tilt,
        ptz_zoom=project.ptz_zoom,
        timezones=list(COMMON_TIMEZONES),
        current_schedule=_schedule_to_form(project.schedule),
        current_render_settings=current_render_settings,
        current_anchors=_exact_time_anchors_to_form(project.exact_time_anchors),
        camera_has_geolocation=_camera_has_geolocation(camera),
        solar_preview=build_solar_preview(camera, project.exact_time_anchors),
        # A null interval means the project captures only via its anchors -- the
        # "solar / scheduled-times only" mode. Drives the form's mode selector.
        capture_mode=(
            "solar" if project.capture_interval_seconds is None else "interval"
        ),
        discovered_events=events_result.events,
        events_discovery_ok=events_result.ok,
        events_discovery_message=events_result.message,
        current_triggers=_event_triggers_to_form(project.event_triggers),
    )


@router.get("/projects/{project_id}/settings", response_class=HTMLResponse)
async def edit_project_form(
    request: Request, db: DbDep, user: OperatorUser, project_id: int
) -> Response:
    """Render the standalone edit-project form for an existing project."""
    project = _get_project_or_404(db, project_id)
    context = await _settings_form_context(request, db, user, project)
    return templates.TemplateResponse(request, "edit_project.html", context)


@router.get("/projects/{project_id}/settings/form", response_class=HTMLResponse)
async def edit_project_form_fragment(
    request: Request, db: DbDep, user: OperatorUser, project_id: int
) -> Response:
    """Render just the settings form, for lazy-loading into the detail page tab."""
    project = _get_project_or_404(db, project_id)
    context = await _settings_form_context(request, db, user, project)
    return templates.TemplateResponse(
        request, "_partials/project_settings_form.html", context
    )


@router.get("/projects/{project_id}/solar-preview", response_class=HTMLResponse)
def solar_preview_fragment(
    request: Request, db: DbDep, user: OperatorUser, project_id: int
) -> Response:
    """Recompute the solar-capture preview from the current (unsaved) form rows.

    Driven by HTMX as the operator edits solar anchors: the exact-time fieldset
    inputs are sent as query parameters, the upcoming capture time is recomputed
    in the camera's local timezone, and the preview partial is swapped back in.
    A read-only computation, so it uses GET (no state change, no CSRF needed).
    """
    project = _get_project_or_404(db, project_id)
    camera = db.get(Camera, project.camera_id)
    params = request.query_params
    preview = build_solar_preview_from_rows(
        camera,
        params.getlist("anchor_kind"),
        params.getlist("anchor_offset"),
        params.getlist("anchor_id"),
        set(params.getlist("anchor_enabled")),
    )
    return templates.TemplateResponse(
        request, "_partials/_solar_preview.html", {"solar_preview": preview}
    )


@router.get("/projects/{project_id}/delete-confirm", response_class=HTMLResponse)
def delete_project_confirm(
    request: Request, db: DbDep, user: OperatorUser, project_id: int
) -> Response:
    """Inline confirmation row for permanently deleting a project."""
    _get_project_or_404(db, project_id)
    return templates.TemplateResponse(
        request,
        "_partials/inline_confirm.html",
        deps.base_context(
            request,
            db,
            user,
            confirm_action=f"/projects/{project_id}/delete",
            confirm_message=(
                "Delete this project? All frames and renders will be permanently "
                "removed. This cannot be undone."
            ),
            confirm_label="Yes, delete",
            confirm_cancel=f"/projects/{project_id}/confirm-cancel",
            confirm_danger=True,
        ),
    )


@router.get("/projects/{project_id}/archive-confirm", response_class=HTMLResponse)
def archive_project_confirm(
    request: Request, db: DbDep, user: OperatorUser, project_id: int
) -> Response:
    """Inline confirmation row for archiving a project."""
    _get_project_or_404(db, project_id)
    return templates.TemplateResponse(
        request,
        "_partials/inline_confirm.html",
        deps.base_context(
            request,
            db,
            user,
            confirm_action=f"/projects/{project_id}/archive",
            confirm_message=(
                "Archive this project? Capture will stop. You can reactivate it later."
            ),
            confirm_label="Yes, archive",
            confirm_cancel=f"/projects/{project_id}/confirm-cancel",
            confirm_danger=False,
        ),
    )


@router.get("/projects/{project_id}/confirm-cancel", response_class=HTMLResponse)
def project_confirm_cancel(
    request: Request, db: DbDep, user: OperatorUser, project_id: int
) -> Response:
    """Empty body that collapses an inline-confirm slot on Cancel."""
    return HTMLResponse("")


async def _edit_project_form_error(
    request: Request, db: DbSession, user: User, project_id: int, message: str
) -> Response:
    """Re-render the edit-project form with an error toast and a 400 status.

    Re-fetches the project so the form reflects committed values even after a
    rolled-back flush expired the in-session instance. Preserves the submitted
    capture mode (read from the already-parsed, cached form) so an error
    re-render does not flip a solar / scheduled-times choice back to interval.
    """
    project = _get_project_or_404(db, project_id)
    cameras = db.execute(select(Camera).order_by(Camera.id)).scalars().all()
    context = deps.base_context(
        request,
        db,
        user,
        project=_project_view(db, project),
        flash_messages=[{"type": "error", "message": message}],
        cameras=[_camera_view(c) for c in cameras],
    )
    # The handler stashes the submitted mode on request.state before any
    # validation, so every error re-render keeps the operator's choice.
    context["capture_mode"] = getattr(request.state, "capture_mode", "interval")
    return templates.TemplateResponse(
        request,
        "edit_project.html",
        context,
        status_code=status.HTTP_400_BAD_REQUEST,
    )


@router.post("/projects/{project_id}/settings")
async def edit_project(
    request: Request, db: DbDep, user: OperatorUser, project_id: int, form: FormDep
) -> Response:
    """Apply an edit to a project, then reconcile its live capture.

    Validation mirrors the create form (non-empty unique name, an existing camera
    configured with a protocol, a positive interval) and runs *before* the row is
    mutated, so a duplicate name re-renders the form with a 400 rather than
    surfacing as a 500. On success the project is committed and the supervisor is
    notified so an interval/camera/storage change takes effect without a restart.
    """
    from sqlalchemy.exc import IntegrityError

    project = _get_project_or_404(db, project_id)

    # Capture mode: "solar" means no recurring interval (anchor-only capture);
    # "interval" keeps the continuous cadence. Stashed on request.state up front
    # so every error re-render below preserves the operator's choice.
    capture_mode = capture_mode_of(form)
    request.state.capture_mode = capture_mode

    name = form.get("name", "").strip()
    if not name:
        return await _edit_project_form_error(
            request, db, user, project_id, "Project name is required."
        )
    try:
        camera_id = int(form.get("camera_id", ""))
    except ValueError:
        return await _edit_project_form_error(
            request, db, user, project_id, "Select a camera."
        )
    if capture_mode == "solar":
        capture_interval_seconds: int | None = None
    else:
        capture_interval_seconds, err = parse_interval_to_seconds(
            form.get("capture_interval_value", ""),
            form.get("capture_interval_unit", ""),
        )
        if err:
            return await _edit_project_form_error(request, db, user, project_id, err)
        assert capture_interval_seconds is not None  # err is None implies a value

    camera = db.get(Camera, camera_id)
    if camera is None:
        return await _edit_project_form_error(
            request, db, user, project_id, "Selected camera not found."
        )
    if camera.protocol is None:
        return await _edit_project_form_error(
            request,
            db,
            user,
            project_id,
            "The selected camera has no protocol configured; set one before "
            "assigning it to a project.",
        )

    # Uniqueness pre-check excludes this project's own row so saving an unchanged
    # name is not mistaken for a duplicate; the UNIQUE constraint + flush below is
    # the authoritative guard. Validate everything before mutating so the error
    # re-render's camera SELECT cannot autoflush a poisoned (duplicate) name.
    existing = db.execute(
        select(Project.id).where(Project.name == name).where(Project.id != project_id)
    ).scalar_one_or_none()
    if existing is not None:
        return await _edit_project_form_error(
            request, db, user, project_id, f"A project named {name!r} already exists."
        )

    # Capture-gating schedule from the preset/timezone/window/day controls. The
    # repeated ``capture_days`` checkbox is read from the raw body (the shared form
    # parser collapses repeated keys), then mapped to a stored schedule and
    # validated; a bad preset/window re-renders the form with a 400. Parsed before
    # any mutation so the error re-render's SELECT cannot autoflush a bad value.
    #
    # Only touch ``project.schedule`` when the schedule controls were submitted:
    # the marker preset field is always present when the schedule fieldset is on
    # the form, so a settings POST that omits the fieldset entirely leaves the
    # stored schedule untouched (mirroring how unrelated fields are preserved).
    schedule_present = form.get("capture_schedule_preset") is not None
    schedule = None
    if schedule_present:
        capture_days = await _form_getlist(request, "capture_days")
        schedule, err = _build_schedule_from_form(form, capture_days)
        if err:
            return await _edit_project_form_error(request, db, user, project_id, err)

    # Exact-time anchors from the repeatable rows (their own column, not the
    # schedule). Solar-noon anchors are validated against the *submitted* camera's
    # geolocation. Parsed before any mutation (and before the camera-detaching
    # stream-label resolution below) so the error re-render's SELECT cannot
    # autoflush a bad value and the camera row is still attached.
    exact_time_present = form.get(EXACT_TIME_MARKER) is not None
    exact_time_anchors = None
    if exact_time_present:
        exact_time_anchors, err = await _build_exact_time_anchors_from_form(
            request, camera
        )
        if err:
            return await _edit_project_form_error(request, db, user, project_id, err)

    # Solar / scheduled-times-only projects capture only from their anchors, so at
    # least one enabled anchor is required or the project would never capture.
    if capture_mode == "solar" and not any(
        a.get("enabled") for a in (exact_time_anchors or [])
    ):
        return await _edit_project_form_error(
            request,
            db,
            user,
            project_id,
            "Solar / scheduled-times mode needs at least one enabled capture time "
            "(e.g. solar noon). Add one or switch to interval capture.",
        )

    # Event triggers from the repeatable rows (their own column, not the
    # schedule). The builder discovers the submitted camera's events to enrich each
    # row's label/category, carrying a prior trigger's label forward on a probe
    # failure. The discovery detaches (expunges) the camera, so the camera is
    # re-fetched before the stream-label resolution below. Parsed before any
    # mutation so the error re-render's SELECT cannot autoflush a bad value.
    event_triggers_present = form.get(EVENT_TRIGGERS_MARKER) is not None
    event_triggers = None
    if event_triggers_present:
        event_triggers, err = await _build_event_triggers_from_form(
            request, camera, db, project.event_triggers
        )
        if err:
            return await _edit_project_form_error(request, db, user, project_id, err)
        # The event-topic probe detaches the camera; re-fetch so the stream-label
        # resolution and the mutations below have an attached row.
        camera = db.get(Camera, camera_id)
        if camera is None:
            return await _edit_project_form_error(
                request, db, user, project_id, "Selected camera not found."
            )

    # Render settings come from the structured dropdowns (encoder/container/fps/
    # resolution/frequency); the archive schedule and post-render actions remain
    # raw-JSON textareas.
    render_schedule, err = _parse_render_settings_field(form)
    if err:
        return await _edit_project_form_error(request, db, user, project_id, err)
    # The shared render-settings parser does not carry auto-chapters; fold the
    # form's choice into the stored schedule here (normalized to
    # none/weekly/monthly) so a scheduled render emits the same chapter request a
    # manual render does.
    render_schedule[render_settings.AUTO_CHAPTERS_KEY] = (
        render_settings.normalize_auto_chapters(form.get("auto_chapters"))
    )
    archive_schedule, err = _parse_optional_json_field(
        form.get("archive_schedule"), "Archive schedule"
    )
    err = err or _schedule_field_error(archive_schedule, "Archive schedule")
    if err:
        return await _edit_project_form_error(request, db, user, project_id, err)
    post_render_actions, err = _parse_optional_json_field(
        form.get("post_render_actions"), "Post-render actions"
    )
    err = err or _post_actions_field_error(post_render_actions)
    if err:
        return await _edit_project_form_error(request, db, user, project_id, err)

    # Campaign bounds, validated before any mutation (same autoflush-safety reason
    # as the name/camera checks above).
    start_date, err = _parse_optional_datetime(form.get("start_date"), "Start date")
    if err:
        return await _edit_project_form_error(request, db, user, project_id, err)
    end_date, err = _parse_optional_datetime(form.get("end_date"), "End date")
    if err:
        return await _edit_project_form_error(request, db, user, project_id, err)
    err = _campaign_bounds_error(start_date, end_date)
    if err:
        return await _edit_project_form_error(request, db, user, project_id, err)
    max_frame_count, err = _parse_optional_positive_int(
        form.get("max_frame_count"), "Maximum frame count"
    )
    if err:
        return await _edit_project_form_error(request, db, user, project_id, err)

    # PTZ position, parsed before any mutation (same autoflush-safety reason as the
    # validations above).
    ptz_preset, ptz_pan, ptz_tilt, ptz_zoom, err = _parse_ptz_fields(form)
    if err:
        return await _edit_project_form_error(request, db, user, project_id, err)

    # Stream/profile selection, resolved after all validation passes (so it does
    # not run on a request that will re-render the form) and before mutating the
    # row. Blank clears both columns; a chosen id stores the id plus a best-effort
    # label that falls back to the id when the camera is unreachable. Enumerating
    # profiles detaches the camera from the session, so it follows the camera
    # validation above and precedes the mutations below.
    stream_id_value = (form.get("stream_profile") or "").strip() or None
    stream_label_value: str | None = None
    if stream_id_value is not None:
        stream_label_value = await _resolve_stream_label(db, camera, stream_id_value)

    storage_path = form.get("storage_path", "").strip() or None
    project.name = name
    project.camera_id = camera_id
    project.capture_interval_seconds = capture_interval_seconds
    project.start_date = start_date
    project.end_date = end_date
    project.max_frame_count = max_frame_count
    project.storage_path = storage_path
    if schedule_present:
        project.schedule = schedule
    if exact_time_present:
        project.exact_time_anchors = exact_time_anchors
    if event_triggers_present:
        project.event_triggers = event_triggers
    project.stream_id = stream_id_value
    project.stream_label = stream_label_value
    project.render_schedule = render_schedule
    project.archive_schedule = archive_schedule
    project.post_render_actions = post_render_actions
    project.ptz_preset = ptz_preset
    project.ptz_pan = ptz_pan
    project.ptz_tilt = ptz_tilt
    project.ptz_zoom = ptz_zoom
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return await _edit_project_form_error(
            request, db, user, project_id, f"A project named {name!r} already exists."
        )

    _audit(
        db,
        scope="project",
        scope_id=project_id,
        actor_user_id=user.id,
        message=f"project {project_id} updated",
    )
    db.commit()
    supervisor = get_context().capture_supervisor
    if supervisor is not None:
        supervisor.notify_reconcile()
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)


def _suggested_clone_name(db: DbSession, base_name: str) -> str:
    """Return a unique "<name> (copy)" variant, bumping the counter on collision.

    Tries "<base> (copy)", then "<base> (copy 2)", "<base> (copy 3)", ... until
    one is free, so the clone form prefills a name that will not collide.
    """
    candidate = f"{base_name} (copy)"
    counter = 2
    while (
        db.execute(
            select(Project.id).where(Project.name == candidate)
        ).scalar_one_or_none()
        is not None
    ):
        candidate = f"{base_name} (copy {counter})"
        counter += 1
    return candidate


@router.get("/projects/{project_id}/clone", response_class=HTMLResponse)
def clone_project_form(
    request: Request, db: DbDep, user: OperatorUser, project_id: int
) -> Response:
    """Render the clone-project form prefilled with a unique suggested name."""
    source = _get_project_or_404(db, project_id)
    cameras = db.execute(select(Camera).order_by(Camera.id)).scalars().all()
    return templates.TemplateResponse(
        request,
        "clone_project.html",
        deps.base_context(
            request,
            db,
            user,
            source_project=_project_view(db, source),
            suggested_name=_suggested_clone_name(db, source.name),
            cameras=[_camera_view(c) for c in cameras],
        ),
    )


def _clone_project_form_error(
    request: Request, db: DbSession, user: User, project_id: int, message: str
) -> Response:
    """Re-render the clone-project form with an error toast and a 400 status."""
    source = _get_project_or_404(db, project_id)
    cameras = db.execute(select(Camera).order_by(Camera.id)).scalars().all()
    return templates.TemplateResponse(
        request,
        "clone_project.html",
        deps.base_context(
            request,
            db,
            user,
            source_project=_project_view(db, source),
            suggested_name=_suggested_clone_name(db, source.name),
            flash_messages=[{"type": "error", "message": message}],
            cameras=[_camera_view(c) for c in cameras],
        ),
        status_code=status.HTTP_400_BAD_REQUEST,
    )


@router.post("/projects/{project_id}/clone")
def clone_project(
    request: Request, db: DbDep, user: OperatorUser, project_id: int, form: FormDep
) -> Response:
    """Clone a project's capture configuration under a new unique name.

    Frames and renders are not copied. A non-empty, unique name is required; on
    success the new active project is committed, the supervisor is notified, and
    the browser is redirected to the clone's detail page.
    """
    from sqlalchemy.exc import IntegrityError

    from ...api.projects import _clone_from

    source = _get_project_or_404(db, project_id)

    name = form.get("name", "").strip()
    if not name:
        return _clone_project_form_error(
            request, db, user, project_id, "Project name is required."
        )
    existing = db.execute(
        select(Project.id).where(Project.name == name)
    ).scalar_one_or_none()
    if existing is not None:
        return _clone_project_form_error(
            request, db, user, project_id, f"A project named {name!r} already exists."
        )

    clone = _clone_from(source, name)
    db.add(clone)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return _clone_project_form_error(
            request, db, user, project_id, f"A project named {name!r} already exists."
        )

    clone_id = clone.id
    _audit(
        db,
        scope="project",
        scope_id=clone_id,
        actor_user_id=user.id,
        message=f"project {clone_id} cloned from {project_id}",
    )
    db.commit()
    supervisor = get_context().capture_supervisor
    if supervisor is not None:
        supervisor.notify_reconcile()
    return RedirectResponse(url=f"/projects/{clone_id}", status_code=303)


@router.post("/projects/{project_id}/delete")
def delete_project(
    request: Request, db: DbDep, user: OperatorUser, project_id: int
) -> Response:
    """Delete a project, its cascaded child rows, and its on-disk files.

    The cascade drops child rows; the shared cleanup helper unlinks the frame and
    render files. The delete is audited and the supervisor notified so the
    project's capture loop stops promptly, then the browser returns to the list.
    """
    from ...storage.projects import delete_project_with_files

    project = _get_project_or_404(db, project_id)
    delete_project_with_files(db, _settings(), project)
    _audit(
        db,
        scope="project",
        scope_id=project_id,
        actor_user_id=user.id,
        message=f"project {project_id} deleted",
    )
    db.commit()
    supervisor = get_context().capture_supervisor
    if supervisor is not None:
        supervisor.notify_reconcile()
    return RedirectResponse(url="/projects", status_code=303)


# Project lifecycle controls. Each action sets the persisted ``lifecycle_state``
# and then wakes the capture supervisor, whose reconcile loop converges the
# running capture tasks to the qualifying (``active``) set -- so pausing a project
# stops just that project's capture and resuming relaunches it, with no process
# restart. The routes enforce the security contract (operator-or-admin-gated,
# CSRF-protected) and record an audited intent.
#
# ``stop`` and ``pause`` collapse to the same ``paused`` state: with an always-on
# supervisor there is no separate "stopped but resumable" runtime state, so the
# two buttons differ only in label/context, not in effect. ``start`` and
# ``resume`` both return the project to ``active``.
#
# Registered AFTER the more specific ``/projects/{id}/renders`` (and the
# project-scoped frame routes, which have more path segments) because Starlette
# matches in registration order with no most-specific-wins rule: this 2-segment
# catch-all would otherwise shadow ``POST /projects/{id}/renders``.
_PROJECT_ACTIONS = ("start", "pause", "resume", "stop", "archive", "reactivate")

# The lifecycle_state each action sets. Every action is state-changing, so each
# one commits and notifies the supervisor.
_ACTION_LIFECYCLE_STATE = {
    "start": "active",
    "resume": "active",
    "reactivate": "active",
    "pause": "paused",
    "stop": "paused",
    "archive": "archived",
}


@router.post("/projects/{project_id}/{action}")
def project_action(
    request: Request,
    db: DbDep,
    user: OperatorUser,
    project_id: int,
    action: str,
) -> Response:
    """Apply a lifecycle action to a project and redirect to its page.

    Sets the persisted ``lifecycle_state`` (``start``/``resume``/``reactivate`` ->
    ``active``; ``pause``/``stop`` -> ``paused``; ``archive`` -> ``archived``),
    commits, then wakes the capture supervisor so its reconcile loop starts or
    stops this project's capture promptly without a process restart. Unknown
    actions are a 404.
    """
    if action not in _PROJECT_ACTIONS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    project = _get_project_or_404(db, project_id)

    project.lifecycle_state = _ACTION_LIFECYCLE_STATE[action]
    _audit(
        db,
        scope="project",
        scope_id=project_id,
        actor_user_id=user.id,
        message=f"project {action} requested",
    )
    # Commit before notifying: the supervisor reconciles in a fresh session on
    # another thread and would not see an uncommitted state change.
    db.commit()
    supervisor = get_context().capture_supervisor
    if supervisor is not None:
        supervisor.notify_reconcile()
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)
