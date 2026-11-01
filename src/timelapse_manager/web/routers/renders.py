"""Render routes: the renders page, render triggering, the render card (details,
cancel, delete), and download/stream of finished render artifacts."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
)
from sqlalchemy import exists, select
from sqlalchemy.orm import Session as DbSession
from starlette.concurrency import run_in_threadpool

from ...db.models import Project, RenderJob, User
from ...db.models.render_job import active_job_filter
from ...render import fps_suggest
from ...render import settings as render_settings
from ...runtime import get_context
from ...services import renders as render_service
from ...services._base import Actor, run_effects
from .. import dependencies as deps
from ..dependencies import (
    CurrentUser,
    DbDep,
    FormDep,
    OperatorUser,
    templates,
)
from ._jobs import card_response, delete_response
from ._shared import (
    _get_project_or_404,
    _settings,
)
from ._viewmodels import (
    _job_card_view,
    _render_view,
    _RenderView,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# The Renders page lists this many renders, then offers "Load more" (#67 P5).
_RENDERS_PER_PAGE = 50
# Upper bound on a caller-chosen ``limit``.
_MAX_RENDERS_PER_PAGE = 100
# A poll re-renders every row from the page's oldest shown render up (its
# ``since`` cursor), so rows a long-open page has gathered stay put. Capped so a
# crafted ``since`` cannot ask for the whole table in one response.
_MAX_POLLED_RENDERS = 500
# htmx stops an element's ``every`` trigger when a poll answers 286.
_HTMX_STOP_POLLING = 286
# The output-settings keys a list row reads (the Play check, ``_playback_view``).
# Only these are extracted in SQL: the list never loads the whole JSON column.
_ROW_SETTINGS_KEYS = ("fps", "width", "height", "codec", "container")


@dataclass(frozen=True)
class _RendersListView:
    """One window of the Renders list, with its polling and paging state.

    ``active`` is the poll marker: any render (not export) is pending or
    encoding. ``poll_url`` refreshes the same window (its ``since``/``before``
    bounds). ``next_before`` is the keyset cursor of the next older page, or
    ``None`` at the start of the queue. ``has_any`` tells an empty window from an
    empty queue (only the latter shows "No renders yet").
    """

    renders: list[_RenderView]
    active: bool
    poll_url: str
    next_before: int | None
    has_any: bool


# SQLite integers are signed 64-bit; a larger bound parameter would not bind.
_MAX_CURSOR = 2**63 - 1


def _clamp_cursor(value: int | None) -> int | None:
    """Bound a ``before``/``since`` cursor to what SQLite can compare against."""
    return None if value is None else max(0, min(value, _MAX_CURSOR))


def _clamp_limit(limit: int) -> int:
    """Bound a caller's page size to 1..``_MAX_RENDERS_PER_PAGE``."""
    return max(1, min(limit, _MAX_RENDERS_PER_PAGE))


def _render_rows(
    db: DbSession, *, before: int | None, since: int | None, limit: int
) -> list[_RenderView]:
    """Render rows with ``since <= id < before``, newest first, at most ``limit``.

    Exports share the ``render_job`` table but are listed on their project's
    Frames tab, so they are left out. The query selects explicit columns plus
    the few output-settings keys the row needs (never the whole JSON column),
    and the project name in the same query (rows from every project mix here).
    Each row becomes a transient, never-persisted ``RenderJob`` so the shared
    ``_render_view`` builds it exactly as it builds every other render row.
    """
    settings_cols = [
        RenderJob.output_settings[key].label(f"os_{key}") for key in _ROW_SETTINGS_KEYS
    ]
    stmt = (
        select(
            RenderJob.id,
            RenderJob.project_id,
            RenderJob.kind,
            RenderJob.status,
            RenderJob.created_at,
            RenderJob.browser_streamable,
            RenderJob.output_file_path,
            RenderJob.mime_type,
            RenderJob.codec_string,
            RenderJob.frame_count,
            Project.name.label("project_name"),
            *settings_cols,
        )
        .outerjoin(Project, Project.id == RenderJob.project_id)
        .where(RenderJob.kind != "export")
    )
    if before is not None:
        stmt = stmt.where(RenderJob.id < before)
    if since is not None:
        stmt = stmt.where(RenderJob.id >= since)
    rows = db.execute(stmt.order_by(RenderJob.id.desc()).limit(limit)).all()
    views: list[_RenderView] = []
    for row in rows:
        output_settings = {
            key: value
            for key in _ROW_SETTINGS_KEYS
            if (value := getattr(row, f"os_{key}")) is not None
        }
        job = RenderJob(
            id=row.id,
            project_id=row.project_id,
            kind=row.kind,
            status=row.status,
            created_at=row.created_at,
            browser_streamable=row.browser_streamable,
            output_file_path=row.output_file_path,
            mime_type=row.mime_type,
            codec_string=row.codec_string,
            frame_count=row.frame_count,
            output_settings=output_settings,
        )
        views.append(_render_view(job, project_name=row.project_name))
    return views


def _renders_active(db: DbSession) -> bool:
    """Whether any render (not export) is pending or encoding."""
    return bool(
        db.scalar(
            select(
                exists().where(
                    RenderJob.kind != "export",
                    active_job_filter(),
                )
            )
        )
    )


def _any_renders(db: DbSession) -> bool:
    """Whether any render (not export) exists at all."""
    return bool(db.scalar(select(exists().where(RenderJob.kind != "export"))))


def _poll_url(*, before: int | None, since: int | None) -> str:
    """The list's poll URL: the same window, refreshed."""
    params: dict[str, int] = {}
    if before is not None:
        params["before"] = before
    if since is not None:
        params["since"] = since
    query = f"&{urlencode(params)}" if params else ""
    return f"/partials/renders/list?poll=1{query}"


def _renders_list_view(
    db: DbSession,
    *,
    before: int | None,
    since: int | None,
    limit: int = _RENDERS_PER_PAGE,
) -> _RendersListView:
    """The polled list: one page below ``before``, or everything from ``since``.

    Without ``since`` this is a page: the ``limit`` newest renders below
    ``before`` (one extra is read to tell whether an older page exists), and the
    oldest one shown becomes ``since`` for the poll URL. With ``since`` (a poll
    of a page already on screen) every render from ``since`` up is listed, so a
    new render is added at the top without pushing a shown one off the bottom.
    """
    if since is None:
        rows = _render_rows(db, before=before, since=None, limit=limit + 1)
        has_more = len(rows) > limit
        rows = rows[:limit]
        since = rows[-1].id if rows else None
        next_before = since if has_more else None
    else:
        rows = _render_rows(db, before=before, since=since, limit=_MAX_POLLED_RENDERS)
        next_before = None  # the page's own "Load more" is outside the poll
    return _RendersListView(
        renders=rows,
        active=_renders_active(db),
        poll_url=_poll_url(before=before, since=since),
        next_before=next_before,
        has_any=bool(rows) or _any_renders(db),
    )


@router.get("/renders", response_class=HTMLResponse)
def renders_page(
    request: Request, db: DbDep, user: CurrentUser, before: int | None = None
) -> Response:
    """Render the global render queue, newest first, one page at a time.

    The first ``_RENDERS_PER_PAGE`` renders are listed; "Load more" fetches the
    next older page in place (``?before=`` is its no-JS fallback). The list
    polls every 10 s only while a render is pending or encoding (#67 P5).
    """
    renders_list = _renders_list_view(db, before=_clamp_cursor(before), since=None)
    return templates.TemplateResponse(
        request,
        "renders.html",
        deps.base_context(request, db, user, renders_list=renders_list),
    )


@router.get("/partials/renders/list", response_class=HTMLResponse)
def partial_renders_list(
    request: Request,
    db: DbDep,
    user: CurrentUser,
    poll: bool = False,
    before: int | None = None,
    since: int | None = None,
    limit: int = _RENDERS_PER_PAGE,
) -> Response:
    """The Renders list fragment: a poll of the list, or the next older page.

    * ``poll=1``: the whole ``#renders-list`` element for the given window. With
      no render pending or encoding it answers **286** (htmx stops polling) and
      the element it returns carries no ``every`` trigger, so polling stays off
      until the page is reloaded or the tab is shown again.
    * ``before=N`` without ``poll``: the next ``limit`` older rows plus a fresh
      "Load more" link (or none at the start of the queue), swapped in place of
      the link that asked for them.
    * neither: the list element for the first page.
    """
    before, since = _clamp_cursor(before), _clamp_cursor(since)
    if before is not None and not poll:
        older = _renders_list_view(
            db, before=before, since=None, limit=_clamp_limit(limit)
        )
        return templates.TemplateResponse(
            request,
            "_partials/renders_list.html",
            deps.base_context(
                request, db, user, renders_list=older, renders_part="older"
            ),
        )
    view = _renders_list_view(db, before=before, since=since)
    return templates.TemplateResponse(
        request,
        "_partials/renders_list.html",
        deps.base_context(request, db, user, renders_list=view),
        status_code=200 if view.active or not poll else _HTMX_STOP_POLLING,
    )


@router.get("/projects/{project_id}/renders/suggested-fps", response_class=HTMLResponse)
def suggested_fps_fragment(
    request: Request,
    db: DbDep,
    user: OperatorUser,
    project_id: int,
    start: datetime | None = None,
    end: datetime | None = None,
) -> Response:
    """Return the render form's "Suggested: N fps" fragment (#301).

    Reads only the timestamps of the frames a render would use, in the optional
    ``start``..``end`` range. The route is synchronous, so Starlette runs it on
    the threadpool and the query never blocks the event loop.
    """
    _get_project_or_404(db, project_id)
    if (
        start is not None
        and end is not None
        and fps_suggest.naive_utc(start) > fps_suggest.naive_utc(end)
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="start must not be after end",
        )
    suggestion = fps_suggest.suggest_for_project(db, project_id, start, end)
    return templates.TemplateResponse(
        request,
        "_partials/render_fps_suggestion.html",
        deps.base_context(
            request,
            db,
            user,
            suggestion=suggestion,
            suggestion_label=fps_suggest.suggestion_label(suggestion),
            few_frames=(
                suggestion.expected_duration_seconds is not None
                and suggestion.expected_duration_seconds
                < fps_suggest.SHORT_VIDEO_SECONDS
            ),
        ),
    )


@router.post("/projects/{project_id}/renders")
def trigger_render(
    request: Request, db: DbDep, user: OperatorUser, project_id: int, form: FormDep
) -> Response:
    """Queue a manual render for a project and redirect back to its detail page.

    A pending ``manual`` job is inserted. The render-trigger panel may submit
    inline ``render_encoder``/``render_container``/``render_fps``/
    ``render_resolution`` overrides for this one render; any provided fields are
    merged onto the project's stored render settings and validated (invalid
    tokens fall back to defaults). With no overrides the stored schedule is used,
    falling back to a safe default (the configured fps, 1080p H.264 MP4).
    """
    project = _get_project_or_404(db, project_id)
    settings = _settings()

    overrides: dict[str, object] = {}
    for key, field in (
        ("encoder", "render_encoder"),
        ("container", "render_container"),
        ("resolution", "render_resolution"),
    ):
        value = (form.get(field) or "").strip()
        if value:
            overrides[key] = value
    fps_raw = (form.get("render_fps") or "").strip()
    if fps_raw.isdigit():
        overrides["fps"] = int(fps_raw)

    if "fps" not in overrides and render_settings.fps_is_auto(project.render_schedule):
        # The schedule is on "Auto (suggested)" and the form sent no number:
        # resolve it from the frames, as a scheduled render would.
        suggested = fps_suggest.suggest_for_project(db, project.id).fps
        if suggested is not None:
            overrides["fps"] = suggested

    if overrides:
        base = (
            dict(project.render_schedule)
            if isinstance(project.render_schedule, dict)
            else {}
        )
        output_settings = render_settings.output_settings_from_schedule(
            {**base, **overrides}
        )
    else:
        output_settings = render_settings.output_settings_from_schedule(
            project.render_schedule
        )
    if output_settings is None:
        output_settings = {
            "fps": settings.render.default_fps,
            "width": 1920,
            "height": 1080,
            "codec": "h264",
            "container": "mp4",
        }
    # The output settings are fully resolved above (overrides merged onto the
    # stored schedule, falling back to a safe default), so the service just
    # inserts the pending job -- or returns an identical one still queued, so a
    # double submit queues one render (#67 R14). Commit before running effects
    # so the woken worker sees the committed row.
    result = render_service.enqueue_render(
        db,
        Actor.of(user),
        project.id,
        output_settings=output_settings,
        overlay_config={},
        dedupe=True,
    )
    db.commit()
    if render_service.is_deduplicated(result):
        return _already_queued_response(request, db, user, result.value)
    run_effects(result.effects, get_context())
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)


def _already_queued_response(
    request: Request, db: DbSession, user: User, job: RenderJob
) -> Response:
    """Show the identical render already queued, with an "already queued" toast.

    The card page (the same one a card link opens without JavaScript) with the
    flash message in the page's toast region, instead of the redirect a new
    render gets.
    """
    project = _get_project_or_404(db, job.project_id)
    card = _job_card_view(db, job, project, _settings())
    context = deps.base_context(
        request,
        db,
        user,
        flash_messages=[
            {
                "type": "info",
                "message": f"Render #{job.id} with these settings is already queued.",
            }
        ],
        card=card,
        card_template="_partials/render_card.html",
    )
    return templates.TemplateResponse(request, "job_card_page.html", context)


def _render_or_404(db: DbSession, render_id: int) -> RenderJob:
    """Return a render (never an export) job, or raise a 404."""
    job = db.get(RenderJob, render_id)
    if job is None or job.kind == "export":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return job


@router.get("/renders/{render_id}/card", response_class=HTMLResponse)
def render_card(
    request: Request, db: DbDep, user: CurrentUser, render_id: int
) -> Response:
    """The render card: details, Play/Download, and Cancel/Delete for operators.

    One route and one template for both screens that list renders (the project
    Renders tab and the global Renders page), so the two cannot drift. Every role
    may open it; the card leaves out the actions the viewer's role cannot use.
    """
    job = _render_or_404(db, render_id)
    return card_response(request, db, user, job, template="_partials/render_card.html")


@router.post("/renders/{render_id}/cancel")
async def cancel_render(
    request: Request, db: DbDep, user: OperatorUser, render_id: int
) -> Response:
    """Cancel a pending or encoding render from its card, then re-show the card.

    Reuses the API cancel (the worker kills ffmpeg and removes the partial file
    before it returns). A plain form post is redirected to the card page.
    """
    from ...api import renders as render_api

    await run_in_threadpool(_render_or_404, db, render_id)
    await render_api.cancel_render(render_id=render_id, session=db, principal=user)  # type: ignore[arg-type]
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(url=f"/renders/{render_id}/card", status_code=303)
    db.expire_all()
    job = await run_in_threadpool(_render_or_404, db, render_id)
    return await run_in_threadpool(
        card_response, request, db, user, job, template="_partials/render_card.html"
    )


@router.post("/renders/{render_id}/delete")
async def delete_render(
    request: Request, db: DbDep, user: OperatorUser, render_id: int, form: FormDep
) -> Response:
    """Delete a render (cancelling it first if it is still running).

    CSRF-protected like every web form. From the card (HTMX) the row is removed
    in place, a toast confirms, and the dialog closes; a plain post redirects
    back to ``next`` (the Renders page or the project page).
    """
    return await delete_response(
        request, db, user, render_id, kind="render", next_url=form.get("next")
    )


@router.get("/renders/{render_id}/download")
def download_render(
    request: Request, db: DbDep, user: CurrentUser, render_id: int
) -> Response:
    """Download a completed render's output (any authenticated role may)."""
    return _serve_render(db, render_id, request, stream=False)


@router.get("/renders/{render_id}/stream")
def stream_render(
    request: Request, db: DbDep, user: CurrentUser, render_id: int
) -> Response:
    """Stream a render for inline playback, honouring HTTP ``Range`` requests."""
    return _serve_render(db, render_id, request, stream=True)


def _serve_render(
    db: DbSession, render_id: int, request: Request, *, stream: bool
) -> Response:
    """Serve a render file via the shared API range-stream helpers.

    An export id is a 404, as on every render route: an export's zip is served
    only by the operator-gated export route, never to a viewer here (#67 S13).
    """
    from ...api import renders as render_api

    _render_or_404(db, render_id)
    if stream:
        return render_api.stream_render(
            render_id=render_id, request=request, session=db
        )
    return render_api.download_render(render_id=render_id, session=db)
