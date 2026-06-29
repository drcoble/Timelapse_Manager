"""Render routes: the renders page, render triggering, and download/stream of
finished render artifacts."""

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

from ...db.models import RenderJob
from ...render import settings as render_settings
from ...runtime import get_context
from .. import dependencies as deps
from ..dependencies import (
    CurrentUser,
    DbDep,
    FormDep,
    OperatorUser,
    templates,
)
from ._shared import (
    _audit,
    _get_project_or_404,
    _settings,
)
from ._viewmodels import (
    _render_view,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/renders", response_class=HTMLResponse)
def renders_page(request: Request, db: DbDep, user: CurrentUser) -> Response:
    """Render the global render queue, newest first."""
    jobs = db.execute(select(RenderJob).order_by(RenderJob.id.desc())).scalars().all()
    return templates.TemplateResponse(
        request,
        "renders.html",
        deps.base_context(request, db, user, renders=[_render_view(j) for j in jobs]),
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
    job = RenderJob(
        project_id=project.id,
        kind="manual",
        status="pending",
        output_settings=output_settings,
        overlay_config={},
    )
    db.add(job)
    db.flush()
    _audit(
        db,
        scope="project",
        scope_id=project_id,
        actor_user_id=user.id,
        message=f"manual render {job.id} triggered",
    )
    db.commit()
    queue = get_context().render_queue
    if queue is not None:
        queue.notify()
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)


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
    """Serve a render file via the shared API range-stream helpers."""
    from ...api import renders as render_api

    job = db.get(RenderJob, render_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if stream:
        return render_api.stream_render(
            render_id=render_id, request=request, session=db
        )
    return render_api.download_render(render_id=render_id, session=db)
