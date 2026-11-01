"""``POST /api/v1/reports``: file a problem report from a script or monitor (#8).

The API twin of the web form, for scripted or monitoring use with the local
bearer token (the parent router enforces it; the caller acts as the sentinel
administrator). It builds the same snapshot and stores the same zip, and
delivery runs in the background exactly as for the form. There is no
attachment field: an image upload is a web-form feature.

Anonymizing (#21) applies here exactly as on the form: every report is
anonymized while ``reporting.github.enabled``, otherwise when ``anonymize`` is
set; a report that cannot be proved clean is refused with 422 and nothing is
stored. The response carries the prefilled GitHub issue URL when there is one.

The route is ``async`` on purpose: the asyncio task dump (#7) must be taken on
the event loop's own thread. All database work (the sentinel actor and the
filing itself) and the zip work then run in Starlette's worker pool rather than
asyncio's default executor, since a saturated default executor is one of the
stalls a report exists to capture. Nothing touches the Session on the loop (#48).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..db.models import User
from ..db.session import DbSessionDep
from ..reports import (
    Reporter,
    RequestContext,
    SnapshotSources,
    Submission,
    capture_live_diagnostics,
    deliver_report,
    file_report,
)
from ..runtime import get_context
from ..security.principal import ensure_sentinel_admin
from ..services.errors import AppError

router = APIRouter(prefix="/reports", tags=["reports"])


class ReportIn(BaseModel):
    """Body of ``POST /api/v1/reports``."""

    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    area: str = "other"
    reported_at: datetime | None = None
    project_id: int | None = Field(default=None, gt=0)
    camera_id: int | None = Field(default=None, gt=0)
    include_snapshot: bool = True
    page_url: str | None = Field(default=None, max_length=500)
    anonymize: bool = False


class ReportOut(BaseModel):
    """The filed report's id and where it is being delivered."""

    report_id: str
    saved: bool
    delivering_to: list[str]
    anonymized: bool = False
    github_issue_url: str | None = None


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value


def _sentinel_actor(db: Session) -> tuple[int, str | None]:
    """Materialise the sentinel admin; return its id and username (plain values).

    Runs in the worker pool: it may write the sentinel row and commits, which
    must never wait on SQLite's write lock on the event loop (#48).
    """
    user_id = ensure_sentinel_admin(db)
    actor = db.get(User, user_id)
    db.commit()
    return user_id, (actor.username if actor is not None else None)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ReportOut)
async def create_report(
    body: ReportIn,
    request: Request,
    db: DbSessionDep,
    background: BackgroundTasks,
) -> Any:
    """File a report; 429 past the hourly limit, 422 for bad input."""
    context = get_context()
    if not context.settings.reporting.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    user_id, username = await run_in_threadpool(_sentinel_actor, db)
    live = (
        capture_live_diagnostics(
            asyncio.get_running_loop(),
            context.capture_supervisor,
            context.loop_lag_monitor,
        )
        if body.include_snapshot
        else None
    )
    submission = Submission(
        title=body.title,
        description=body.description,
        area=body.area,
        reported_at=_naive_utc(body.reported_at),
        project_id=body.project_id,
        camera_id=body.camera_id,
        include_snapshot=body.include_snapshot,
        attachment=None,
        source="api",
        anonymize=body.anonymize,
    )
    try:
        filed = await run_in_threadpool(
            file_report,
            context.session_factory,
            context.settings,
            submission,
            Reporter(
                user_id=user_id,
                username=username,
                role="admin",
            ),
            context=RequestContext(
                page_url=body.page_url,
                user_agent=(request.headers.get("user-agent") or "")[:300] or None,
            ),
            sources=SnapshotSources(
                supervisor=context.capture_supervisor,
                live_diagnostics=live,
                ffmpeg_version=context.ffmpeg_version,
            ),
        )
    except AppError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    if filed.pending_destinations:
        background.add_task(
            deliver_report,
            context.session_factory,
            context.settings,
            filed.report_id,
            filed.pending_destinations,
        )
    return ReportOut(
        report_id=filed.report_key,
        saved=filed.saved,
        delivering_to=list(filed.pending_destinations),
        anonymized=filed.anonymized,
        github_issue_url=filed.github_issue_url,
    )
