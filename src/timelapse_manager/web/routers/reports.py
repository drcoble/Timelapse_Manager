""" "Report a problem" (#8): the reporter's form and the admin report list.

Any signed-in role may file a report from any page. The form is a drawer (with
a full-page fallback), prefilled from the page it was opened on: the area, and
the project and camera when that page was about one. Submitting stores the
report and its redacted snapshot, shows the report id, and hands delivery to a
background task so a slow mail server or Gitea cannot hang the submit.

Admins get Settings > Problem reports: list, download, resend, create a Gitea
issue, delete. Every admin action is audited.

Anonymized reports and the GitHub hand-off (#21): while
``reporting.github.enabled`` every report is anonymized and the result screen
offers "Open on GitHub" (a prefilled public issue the user submits from their
own account) plus the anonymized zip, which the reporter may download from
``/report/{key}/download``. With GitHub off the form offers an optional
"Anonymize private data" box instead. Either way the screenshot is left out
of an anonymized report.

The form posts ``multipart/form-data`` (for the optional image). With HTMX the
CSRF token rides in the ``X-CSRF-Token`` header; without JS it is a
``csrf_token`` part the CSRF middleware reads. Errors follow the repo's inline
convention: an HTMX request gets the form back at 200 with the message (HTMX
does not swap a 4xx), a plain request gets the real status (413/422/429).
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import re
import urllib.parse
import zoneinfo
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from ...db.models import Project, Report, User
from ...logging import recent_requests
from ...monitoring.events import EventType, log_event
from ...reports import (
    AREAS,
    SNAPSHOT_SUMMARY,
    FiledReport,
    Reporter,
    ReportTooLargeError,
    RequestContext,
    SnapshotSources,
    Submission,
    capture_live_diagnostics,
    configured_destinations,
    deliver_report,
    file_report,
    validate_attachment,
)
from ...reports.service import should_anonymize
from ...reports.store import (
    REPORT_KEY_PATTERN,
    delete_report_files,
    set_delivery,
    utcnow,
    zip_path_for,
)
from ...runtime import get_context
from ...security.authz import session_cookie_value
from ...services.errors import AppError, ValidationError
from .. import dependencies as deps
from ..dependencies import AdminUser, CurrentUser, DbDep, FormDep, templates
from ..multipart import (
    BodyTooLargeError,
    parse_multipart,
    read_body_limited,
    report_body_limit,
)
from ..safe_redirect import safe_local_path
from ._values import UserView

logger = logging.getLogger(__name__)

router = APIRouter()

# How many of their own reports a reporter sees on the form.
_OWN_REPORTS_SHOWN = 5

# How many recent request ids of the reporter go into the snapshot.
_RECENT_REQUEST_IDS = 10

# ``from`` must be a site-relative path: one leading slash, no scheme/host.
_MAX_FROM_LENGTH = 500
_PROJECT_PATH = re.compile(r"\A/projects/(\d+)(?:/|\Z)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_enabled() -> None:
    """404 while the feature is disabled, like the other optional surfaces."""
    if not get_context().settings.reporting.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


def _safe_from(raw: str | None) -> str | None:
    """Accept only a site-relative path; it is rendered as a Cancel/Back href.

    The shared :func:`..safe_redirect.safe_local_path` rule decides (no host, no
    backslash or control character, no encoded slash, no dot segment), with this
    form's shorter length cap.
    """
    return safe_local_path(raw, max_length=_MAX_FROM_LENGTH)


def _int_or_none(raw: str | None) -> int | None:
    """A positive integer from a form or query value, else ``None``."""
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _first(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    return values[0] if values else None


def derive_scope(db: DbSession, page: str | None) -> dict[str, Any]:
    """Infer the area, project and camera from the page the form was opened on."""
    scope: dict[str, Any] = {"area": "other", "project_id": None, "camera_id": None}
    if page is None:
        return scope
    parsed = urllib.parse.urlsplit(page)
    path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
    match = _PROJECT_PATH.match(path)
    if match:
        scope.update(area="project", project_id=int(match.group(1)))
    elif path.startswith("/frames"):
        scope.update(area="capture")
    elif path.startswith("/renders"):
        scope.update(area="renders")
    elif path.startswith("/cameras"):
        scope.update(area="cameras")
    elif path.startswith("/login"):
        scope.update(area="login")
    if scope["project_id"] is None:
        scope["project_id"] = _int_or_none(_first(query, "project"))
    scope["camera_id"] = _int_or_none(_first(query, "camera"))
    if scope["project_id"] is not None:
        project = db.get(Project, scope["project_id"])
        if project is None:
            scope["project_id"] = None
        elif scope["camera_id"] is None:
            scope["camera_id"] = project.camera_id
    return scope


def _viewer_tz(user: User | UserView) -> datetime.tzinfo:
    name = getattr(user, "viewer_timezone", None)
    try:
        return zoneinfo.ZoneInfo(name) if name else datetime.UTC
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return datetime.UTC


def _local_input_value(moment: datetime.datetime, user: User | UserView) -> str:
    """A naive-UTC instant as a ``datetime-local`` value in the viewer's zone."""
    local = moment.replace(tzinfo=datetime.UTC).astimezone(_viewer_tz(user))
    return local.strftime("%Y-%m-%dT%H:%M")


def _parse_when(raw: str | None, user: User | UserView) -> datetime.datetime | None:
    """Parse a ``datetime-local`` value (viewer's zone) to naive UTC."""
    if not raw:
        return None
    try:
        local = datetime.datetime.strptime(raw[:16], "%Y-%m-%dT%H:%M")
    except ValueError:
        raise ValidationError(
            "That date and time is not valid.", field="when"
        ) from None
    aware = local.replace(tzinfo=_viewer_tz(user))
    return aware.astimezone(datetime.UTC).replace(tzinfo=None)


def _own_reports(db: DbSession, user: User) -> list[Report]:
    return list(
        db.execute(
            select(Report)
            .where(Report.user_id == user.id)
            .order_by(Report.id.desc())
            .limit(_OWN_REPORTS_SHOWN)
        ).scalars()
    )


def _form_context(
    request: Request,
    db: DbSession,
    user: User,
    *,
    values: dict[str, Any],
    error: str | None = None,
) -> dict[str, Any]:
    reporting = get_context().settings.reporting
    github = reporting.github
    project_name = None
    if values.get("project_id"):
        project = db.get(Project, values["project_id"])
        project_name = project.name if project is not None else None
    return deps.base_context(
        request,
        db,
        user,
        values=values,
        project_name=project_name,
        areas=AREAS,
        snapshot_summary=_snapshot_summary(user),
        own_reports=_own_reports(db, user),
        max_upload_mb=reporting.max_upload_bytes // (1024 * 1024),
        form_error=error,
        in_drawer=bool(values.get("in_drawer")),
        github_enabled=github.enabled,
        github_repo=github.repo,
    )


def _snapshot_allowed(user: User | UserView) -> bool:
    """Whether this reporter's reports may carry the system snapshot (S12).

    A viewer cannot see logs, events or the live dumps anywhere else in the
    app, so a viewer's report holds only their own words and request context.
    Operators and admins keep the choice; any other role is refused.
    """
    return user.role in (deps.ADMIN_ROLE, deps.OPERATOR_ROLE)


# Shown to a viewer under "What the snapshot contains" in place of the
# snapshot's contents: the box is there, but a viewer's report never carries
# it (S12). Kept in the router so report_form.html needs no change.
_VIEWER_SNAPSHOT_SUMMARY: tuple[str, ...] = (
    "Diagnostics are attached only to reports filed by operators and "
    "administrators. Your report carries your title, description and the time "
    "you give.",
    "It also records this page's address, your browser name, your username and "
    "role, and the ids of your last few requests.",
)


def _snapshot_summary(user: User) -> tuple[str, ...]:
    """The snapshot description for this reporter's form."""
    return SNAPSHOT_SUMMARY if _snapshot_allowed(user) else _VIEWER_SNAPSHOT_SUMMARY


def _initial_values(db: DbSession, user: User, page: str | None) -> dict[str, Any]:
    scope = derive_scope(db, page)
    return {
        "title": "",
        "description": "",
        "area": scope["area"],
        "project_id": scope["project_id"],
        "camera_id": scope["camera_id"],
        "when": _local_input_value(utcnow(), user),
        "include_snapshot": _snapshot_allowed(user),
        "anonymize": False,
        "page_url": page or "",
    }


def _is_htmx(request: Request) -> bool:
    return bool(request.headers.get("HX-Request"))


# ---------------------------------------------------------------------------
# Reporter: form and submit
# ---------------------------------------------------------------------------


@router.get("/report", response_class=HTMLResponse)
def report_page(
    request: Request, db: DbDep, user: CurrentUser, page: str | None = None
) -> Response:
    """The full-page form (no-JS / direct-URL fallback for the drawer)."""
    _require_enabled()
    values = _initial_values(db, user, _safe_from(page))
    return templates.TemplateResponse(
        request, "report_problem.html", _form_context(request, db, user, values=values)
    )


@router.get("/drawers/report", response_class=HTMLResponse)
def report_drawer(
    request: Request, db: DbDep, user: CurrentUser, page: str | None = None
) -> Response:
    """The form as a drawer fragment for HTMX, else the full page."""
    _require_enabled()
    values = _initial_values(db, user, _safe_from(page))
    template = (
        "_partials/drawer_report.html" if _is_htmx(request) else "report_problem.html"
    )
    return templates.TemplateResponse(
        request, template, _form_context(request, db, user, values=values)
    )


async def _read_submission(
    request: Request, form_fields: dict[str, str]
) -> tuple[dict[str, str], tuple[str, bytes] | None]:
    """Return the text fields and the optional ``(content_type, bytes)`` upload.

    The body is read with the cap enforced on the bytes actually received (a
    chunked or understated ``Content-Length`` cannot slip past); when the CSRF
    middleware already did that bounded read, its bytes are reused.
    """
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/form-data"):
        return form_fields, None
    try:
        body = await read_body_limited(request, report_body_limit())
    except BodyTooLargeError:
        raise ReportTooLargeError(
            "The upload is too large.", field="attachment"
        ) from None
    parsed = parse_multipart(body, content_type)
    upload = next((part for part in parsed.files if part.name == "attachment"), None)
    return parsed.fields, ((upload.content_type, upload.data) if upload else None)


def _live_diagnostics() -> dict[str, Any]:
    """The #7 dumps, taken here on the event loop's own thread."""
    context = get_context()
    return capture_live_diagnostics(
        asyncio.get_running_loop(),
        context.capture_supervisor,
        context.loop_lag_monitor,
    )


def _sources(live: dict[str, Any] | None) -> SnapshotSources:
    context = get_context()
    return SnapshotSources(
        supervisor=context.capture_supervisor,
        live_diagnostics=live,
        ffmpeg_version=context.ffmpeg_version,
    )


def _recent_request_ids(user_id: int) -> tuple[dict[str, Any], ...]:
    entries = recent_requests.for_user(user_id)[-_RECENT_REQUEST_IDS:]
    return tuple(
        {
            "request_id": entry.request_id,
            "method": entry.method,
            "path": entry.path,
            "at": datetime.datetime.fromtimestamp(entry.at, datetime.UTC).isoformat(),
        }
        for entry in entries
    )


@router.post("/report", response_class=HTMLResponse)
async def report_submit(
    request: Request, db: DbDep, user: CurrentUser, form: FormDep
) -> Response:
    """File a report: validate, snapshot, store, then deliver in the background.

    ``async`` because the body is read and the #7 dumps are taken on the event
    loop. Every Session use (the commits, the audit, the form and result
    contexts) runs in the worker pool, so a held write lock never stalls the
    loop (#48).
    """
    _require_enabled()
    settings = get_context().settings
    fields: dict[str, str] = dict(form)
    values: dict[str, Any] = {}
    in_drawer = False
    # Copy the viewer's columns once, in the pool; the loop reads only the copy.
    viewer = await run_in_threadpool(UserView.of, user)
    try:
        fields, upload = await _read_submission(request, form)
        in_drawer = fields.get("in_drawer") == "1"
        page = _safe_from(fields.get("page_url"))
        values = {
            "title": fields.get("title", ""),
            "description": fields.get("description", ""),
            "area": fields.get("area", "other"),
            "project_id": _int_or_none(fields.get("project_id")),
            "camera_id": _int_or_none(fields.get("camera_id")),
            "when": fields.get("when", ""),
            # A viewer's report never carries the snapshot, whatever was posted.
            "include_snapshot": (
                _snapshot_allowed(viewer) and fields.get("include_snapshot") is not None
            ),
            "anonymize": fields.get("anonymize") is not None,
            "page_url": page or "",
        }
        # An anonymized report never carries the screenshot: an image cannot
        # be scrubbed. The form says so; an upload sent anyway is ignored.
        attachment = (
            validate_attachment(
                upload[0], upload[1], max_bytes=settings.reporting.max_upload_bytes
            )
            if upload is not None
            and not should_anonymize(settings, values["anonymize"])
            else None
        )
        submission = Submission(
            title=values["title"],
            description=values["description"],
            area=values["area"],
            reported_at=_parse_when(values["when"], viewer),
            project_id=values["project_id"],
            camera_id=values["camera_id"],
            include_snapshot=values["include_snapshot"],
            attachment=attachment,
            source="web",
            anonymize=values["anonymize"],
        )
        live = _live_diagnostics() if submission.include_snapshot else None
        reporter = Reporter(
            user_id=viewer.id, username=viewer.username, role=viewer.role
        )
        context = RequestContext(
            page_url=page,
            user_agent=(request.headers.get("user-agent") or "")[:300] or None,
            recent_requests=_recent_request_ids(viewer.id),
        )
        cookie = session_cookie_value(request, settings.session)
        # Release this request's transaction before the filing writes from a
        # worker thread on its own connection (SQLite allows one writer).
        await run_in_threadpool(db.commit)
        filed = await run_in_threadpool(
            file_report,
            get_context().session_factory,
            settings,
            submission,
            reporter,
            context=context,
            sources=_sources(live),
            extra_secrets=[cookie] if cookie else [],
        )
    except AppError as exc:
        return await run_in_threadpool(
            _submit_error_response, request, db, user, values, in_drawer, exc
        )

    task = (
        BackgroundTask(
            deliver_report,
            get_context().session_factory,
            settings,
            filed.report_id,
            filed.pending_destinations,
        )
        if filed.pending_destinations
        else None
    )
    return await run_in_threadpool(
        _submit_result_response, request, db, user, filed, values, in_drawer, task
    )


def _submit_error_response(
    request: Request,
    db: DbSession,
    user: User,
    values: dict[str, Any],
    in_drawer: bool,
    exc: AppError,
) -> Response:
    """The form again with the error (it reads the database: worker pool only)."""
    if not values:
        values = _initial_values(db, user, None)
    values["in_drawer"] = in_drawer
    response = templates.TemplateResponse(
        request,
        "_partials/report_form.html" if _is_htmx(request) else "report_problem.html",
        _form_context(request, db, user, values=values, error=exc.detail),
        status_code=status.HTTP_200_OK if _is_htmx(request) else exc.status,
    )
    return response


def _submit_result_response(
    request: Request,
    db: DbSession,
    user: User,
    filed: FiledReport,
    values: dict[str, Any],
    in_drawer: bool,
    task: BackgroundTask | None,
) -> Response:
    """Audit the GitHub hand-off and render the result (worker pool only)."""
    if filed.github_issue_url:
        # We cannot know whether they submit it on GitHub; record that the
        # hand-off was offered. The URL itself is not logged.
        _audit(
            db,
            user,
            f"GitHub issue prepared for problem report {filed.report_key}",
            "report.github_prepared",
            filed.report_key,
        )
        db.commit()
    result_context = deps.base_context(
        request,
        db,
        user,
        filed=filed,
        page_url=values.get("page_url") or "/",
        in_page=not in_drawer,
    )
    return templates.TemplateResponse(
        request,
        "_partials/report_result.html" if _is_htmx(request) else "report_filed.html",
        result_context,
        background=task,
    )


@router.get("/report/{key}/download")
def report_reporter_download(key: str, db: DbDep, user: CurrentUser) -> Response:
    """Download an anonymized report's zip: its reporter, or an admin (#21).

    404 unless the report exists and was filed anonymized (an un-anonymized
    zip stays admin-only, at ``/settings/reports/{key}/download``); 403 for
    anyone else. Report keys are sequential and guessable, so the check here
    is the access control.
    """
    _require_enabled()
    report = _report_or_404(db, key)
    if not report.anonymized:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if report.user_id != user.id and user.role != deps.ADMIN_ROLE:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return _zip_response(report)


# ---------------------------------------------------------------------------
# Admin: Settings > Problem reports
# ---------------------------------------------------------------------------


def _report_or_404(db: DbSession, key: str) -> Report:
    if not REPORT_KEY_PATTERN.match(key):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    report = db.execute(
        select(Report).where(Report.report_key == key)
    ).scalar_one_or_none()
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return report


def _table_context(
    request: Request, db: DbSession, user: User, **extra: Any
) -> dict[str, Any]:
    settings = get_context().settings
    reports = list(
        db.execute(select(Report).order_by(Report.created_at.desc(), Report.id.desc()))
        .scalars()
        .all()
    )
    return deps.base_context(
        request,
        db,
        user,
        reports=reports,
        destinations=configured_destinations(settings),
        gitea_configured=settings.reporting.gitea.configured,
        areas=AREAS,
        **extra,
    )


def _audit(db: DbSession, user: User, message: str, action: str, key: str) -> None:
    log_event(
        db,
        scope="system",
        scope_id=None,
        level="info",
        message=message,
        type=EventType.AUDIT_CONTROL_ACTION.value,
        metadata={"action": action, "report_id": key},
        actor_user_id=user.id,
    )


def _admin_done(
    request: Request,
    db: DbSession,
    user: User,
    message: str,
    *,
    background: BackgroundTask | None = None,
) -> Response:
    """HTMX: the refreshed table with a notice. Plain: back to the list."""
    if _is_htmx(request):
        return templates.TemplateResponse(
            request,
            "_partials/report_table.html",
            _table_context(request, db, user, notice=message),
            background=background,
        )
    return RedirectResponse(
        url="/settings/reports",
        status_code=status.HTTP_303_SEE_OTHER,
        background=background,
    )


@router.get("/settings/reports", response_class=HTMLResponse)
def reports_admin_page(request: Request, db: DbDep, user: AdminUser) -> Response:
    """The full Problem reports page."""
    return templates.TemplateResponse(
        request, "reports_admin.html", _table_context(request, db, user)
    )


@router.get("/settings/reports/table", response_class=HTMLResponse)
def reports_admin_table(request: Request, db: DbDep, user: AdminUser) -> Response:
    """The report table fragment (loaded into the Settings tab)."""
    return templates.TemplateResponse(
        request, "_partials/report_table.html", _table_context(request, db, user)
    )


@router.get("/settings/reports/{key}/download")
def report_download(key: str, db: DbDep, user: AdminUser) -> Response:
    """Download a report's zip. Admin-only; the key is validated first."""
    return _zip_response(_report_or_404(db, key))


def _zip_response(report: Report) -> Response:
    """The stored zip as an attachment named ``<key>.zip``; 404 when missing."""
    path = zip_path_for(get_context().settings, report)
    if path is None or not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return FileResponse(path, media_type="application/zip", filename=path.name)


def _schedule(report: Report, destinations: tuple[str, ...]) -> BackgroundTask | None:
    for destination in destinations:
        set_delivery(report, destination, "pending")
    if not destinations:
        return None
    context = get_context()
    return BackgroundTask(
        deliver_report,
        context.session_factory,
        context.settings,
        report.id,
        destinations,
    )


@router.post("/settings/reports/{key}/resend", response_class=HTMLResponse)
def report_resend(
    request: Request, key: str, db: DbDep, user: AdminUser, form: FormDep
) -> Response:
    """Resend to one configured destination, or to all of them."""
    report = _report_or_404(db, key)
    configured = configured_destinations(get_context().settings)
    choice = form.get("destination", "all")
    if choice == "all":
        destinations = configured
    elif choice in configured:
        destinations = (choice,)
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="That destination is not configured.",
        )
    task = _schedule(report, destinations)
    _audit(
        db,
        user,
        f"problem report {key} resent to {', '.join(destinations) or 'nothing'}",
        "report.resend",
        key,
    )
    db.commit()
    return _admin_done(request, db, user, f"Resending {key}.", background=task)


@router.post("/settings/reports/{key}/create-issue", response_class=HTMLResponse)
def report_create_issue(
    request: Request, key: str, db: DbDep, user: AdminUser
) -> Response:
    """File (or finish filing) the Gitea issue for a stored report."""
    report = _report_or_404(db, key)
    if not get_context().settings.reporting.gitea.configured:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Gitea delivery is not configured.",
        )
    task = _schedule(report, ("gitea",))
    _audit(
        db, user, f"Gitea issue requested for problem report {key}", "report.issue", key
    )
    db.commit()
    return _admin_done(
        request, db, user, f"Creating a Gitea issue for {key}.", background=task
    )


@router.get("/settings/reports/{key}/delete-confirm", response_class=HTMLResponse)
def report_delete_confirm(
    request: Request, key: str, db: DbDep, user: AdminUser
) -> Response:
    """The inline confirmation for deleting a report."""
    _report_or_404(db, key)
    return templates.TemplateResponse(
        request,
        "_partials/inline_confirm.html",
        deps.base_context(
            request,
            db,
            user,
            confirm_message=f"Delete problem report {key} and its snapshot?",
            confirm_action=f"/settings/reports/{key}/delete",
            confirm_label="Delete report",
            confirm_cancel="/settings/reports/confirm-cancel",
            confirm_danger=True,
        ),
    )


@router.get("/settings/reports/confirm-cancel", response_class=HTMLResponse)
def report_confirm_cancel(user: AdminUser) -> Response:
    """Clear the inline-confirm slot."""
    return HTMLResponse("")


@router.post("/settings/reports/{key}/delete", response_class=HTMLResponse)
def report_delete(request: Request, key: str, db: DbDep, user: AdminUser) -> Response:
    """Delete a report row and its zip."""
    report = _report_or_404(db, key)
    delete_report_files(get_context().settings, report)
    db.delete(report)
    _audit(db, user, f"problem report {key} deleted", "report.delete", key)
    db.commit()
    return _admin_done(request, db, user, f"Deleted {key}.")
