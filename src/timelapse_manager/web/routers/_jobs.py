"""Render-card and export-card plumbing shared by the renders and frames routers.

A render and a frame export are both ``render_job`` rows, opened from a list into
the same centered dialog and deleted the same way. The two routers own their URLs
(``/renders/{id}/...`` and ``/frames/export/{id}/...``); this module holds the
one implementation of the card response and the delete response behind them.

* **Card.** An HTMX request (the dialog's opener) gets the bare card fragment; a
  plain request -- JavaScript off, or the link opened in a new tab -- gets the
  same fragment inside a full page.
* **Delete.** Runs the shared cancel-then-delete (:func:`api.renders.delete_job`).
  An HTMX request gets out-of-band swaps that remove the list row and append a
  toast, plus an ``HX-Trigger`` that closes the dialog; a plain form post is
  redirected back to the page it came from (``next``, restricted to the two list
  pages so it can never be an open redirect).

The delete response is ``async`` because stopping a running job awaits the
render worker, which must happen on the event loop. Every other touch of the
request's ``Session`` -- the rollback after a failure and the toast (whose
``base_context`` reads the session row for the CSRF token) -- runs in the
threadpool, so a write lock held elsewhere never stalls the loop (#47).
"""

from __future__ import annotations

import logging
import re

from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session as DbSession
from starlette.concurrency import run_in_threadpool

from ...db.models import RenderJob, User
from ...services.errors import AppError
from .. import dependencies as deps
from ..dependencies import templates
from ._shared import _get_project_or_404, _settings
from ._viewmodels import _job_card_view

logger = logging.getLogger(__name__)

# The event modal.js listens for to close the open dialog.
CLOSE_DIALOG_EVENT = "modal:close"

# The only pages a delete may send the browser back to.
_SAFE_NEXT = re.compile(r"^/(renders|projects/\d+)$")


def _is_htmx(request: Request) -> bool:
    """Return whether the request came from HTMX."""
    return request.headers.get("HX-Request") == "true"


def safe_next(raw: str | None, default: str) -> str:
    """Return ``raw`` when it is one of the list pages, else ``default``."""
    value = (raw or "").strip()
    return value if _SAFE_NEXT.match(value) else default


def card_response(
    request: Request, db: DbSession, user: User, job: RenderJob, *, template: str
) -> Response:
    """Render a job's card: the fragment for HTMX, else a full page around it."""
    project = _get_project_or_404(db, job.project_id)
    card = _job_card_view(db, job, project, _settings())
    context = deps.base_context(request, db, user, card=card, card_template=template)
    if _is_htmx(request):
        return templates.TemplateResponse(request, template, context)
    return templates.TemplateResponse(request, "job_card_page.html", context)


async def delete_response(
    request: Request,
    db: DbSession,
    user: User,
    job_id: int,
    *,
    kind: str,
    next_url: str | None,
) -> Response:
    """Cancel-if-running then delete a job; answer the dialog or redirect back.

    ``kind`` is ``"render"`` or ``"export"`` and also names the row id prefix
    (``render-row-N`` / ``export-row-N``) the HTMX response removes. A failure on
    an HTMX request comes back as an error toast at ``200`` (HTMX does not swap
    error responses) and leaves the dialog open; a plain post re-raises it.
    """
    from ...api.renders import delete_job

    noun = "Export" if kind == "export" else "Render"
    try:
        deleted = await delete_job(db, user, job_id, kind=kind)
    except (HTTPException, AppError) as exc:
        await run_in_threadpool(db.rollback)
        if not _is_htmx(request):
            raise
        detail = exc.detail if isinstance(exc.detail, str) else "delete failed"
        return await run_in_threadpool(
            _toast_response,
            request,
            db,
            user,
            toast_type="error",
            message=detail,
            row_id=None,
        )
    if not _is_htmx(request):
        default = "/renders" if kind == "render" else f"/projects/{deleted.project_id}"
        return RedirectResponse(
            url=safe_next(next_url, default), status_code=status.HTTP_303_SEE_OTHER
        )
    response = await run_in_threadpool(
        _toast_response,
        request,
        db,
        user,
        toast_type="success",
        message=f"{noun} #{deleted.id} deleted",
        row_id=f"{kind}-row-{deleted.id}",
    )
    response.headers["HX-Trigger"] = CLOSE_DIALOG_EVENT
    return response


def _toast_response(
    request: Request,
    db: DbSession,
    user: User,
    *,
    toast_type: str,
    message: str,
    row_id: str | None,
) -> Response:
    """Render the out-of-band toast (and optional row removal) fragment.

    Synchronous (``base_context`` queries); :func:`delete_response` calls it via
    :func:`run_in_threadpool`.
    """
    return templates.TemplateResponse(
        request,
        "_partials/job_deleted.html",
        deps.base_context(
            request,
            db,
            user,
            msg={"type": toast_type, "message": message},
            row_id=row_id,
        ),
    )
