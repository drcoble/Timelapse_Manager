"""Render and milestone endpoints.

Covers the render lifecycle (trigger a manual render, list/status, cancel,
delete, download the output, and range-stream it for inline playback) and
milestone CRUD
(the user-placed chapter markers a render turns into chapters).

Reads are token-gated (the parent router attaches the local-token dependency);
every mutation additionally requires an administrator principal and attributes
the actor on the audit event it writes. A manual render is validated through the
encoder up front, so an unsupported target is rejected before any job is created.
The download and stream endpoints serve only from inside the project's render
root, resolved through the shared path layer.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, BinaryIO

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..db.models import Milestone, Project, RenderJob
from ..db.session import DbSessionDep
from ..encode import EncoderCapabilityError, EncoderError, OutputSettings
from ..encode.codec_string import media_type_for_path
from ..encode.overlay import (
    DEFAULT_TIMESTAMP_FORMAT,
    TEXT_CONTENT_MAX_LENGTH,
    validate_timestamp_format,
)
from ..render import RenderQueue, fps_suggest, project_render_root
from ..runtime import get_context
from ..security import Principal, require_operator_or_admin_principal
from ..services import milestones as milestone_service
from ..services import renders as render_service
from ..services._base import Actor, Effect, run_effects
from ..services.errors import PermissionError_
from ..services.milestones import MilestoneSpec
from ..services.renders import OPERATING_ROLES, RUNNING_STATUSES, DeletedJob

logger = logging.getLogger(__name__)

# Two sibling routers: project-scoped collection routes and id-addressed routes.
router = APIRouter(prefix="/projects", tags=["renders"])
renders_router = APIRouter(prefix="/renders", tags=["renders"])

# Bytes read per chunk when range-streaming, so a large file is not buffered.
_STREAM_CHUNK = 64 * 1024

# How many times a delete re-issues the worker cancel before giving up. A job
# claimed by the worker an instant before the first cancel has no task yet, so
# that cancel is a no-op; the next attempt finds the task and stops it.
_DELETE_CANCEL_ATTEMPTS = 5
_DELETE_CANCEL_RETRY_SECONDS = 0.1

# Set on a trigger response that returned an already-queued identical job.
DEDUPLICATED_HEADER = "X-Render-Deduplicated"

# A single ``bytes=start-end`` range (the only form needed for video seeking).
# Each bound is capped at 19 digits (any file offset fits in a signed 64-bit
# integer): a longer number is treated as an invalid header, so a crafted
# multi-thousand-digit value never reaches ``int()`` (which refuses strings over
# 4300 digits and would turn the request into a 500).
_RANGE_RE = re.compile(r"^bytes=(\d{0,19})-(\d{0,19})$")


class OutputSettingsBody(BaseModel):
    """Encode target for a manual render."""

    # The encoder's own range (encode/allowlist.py); NaN and infinities fail
    # it too, so a bad fps is a 422 here rather than a failed job later (#97).
    fps: float | None = Field(default=None, ge=0.1, le=240.0)
    width: int = Field(default=1920, gt=0)
    height: int = Field(default=1080, gt=0)
    codec: str = "h264"
    container: str = "mp4"
    bitrate_kbps: int | None = Field(default=None, gt=0)
    crf: int | None = Field(default=None, ge=0)
    auto_chapters: str | None = None
    deflicker: bool = False


class OverlayBody(BaseModel):
    """Overlay configuration for a manual render."""

    timestamp_enabled: bool = False
    timestamp_format: str = DEFAULT_TIMESTAMP_FORMAT
    timestamp_timezone: str = "UTC"
    text_enabled: bool = False
    # Capped so a caption cannot overflow an ffmpeg argument (#139).
    text_content: str = Field(default="", max_length=TEXT_CONTENT_MAX_LENGTH)
    image_enabled: bool = False
    image_path: str | None = None
    placement: str = "top_left"

    @field_validator("timestamp_format")
    @classmethod
    def _check_timestamp_format(cls, value: str) -> str:
        """Hold the format to the overlay grammar, so a bad one is a 422 (#67 S4)."""
        return validate_timestamp_format(value)


class RenderCreate(BaseModel):
    """Request body to trigger a manual render."""

    output: OutputSettingsBody = Field(default_factory=OutputSettingsBody)
    overlay: OverlayBody = Field(default_factory=OverlayBody)


class SuggestedFpsOut(BaseModel):
    """The suggested render frame rate for a project's frames (#301)."""

    # ``None`` when there is no suggestion (fewer than two frames).
    fps: int | None
    interval_seconds: float | None
    frame_count: int
    expected_duration_seconds: float | None


class RenderOut(BaseModel):
    """A render job's state as returned to clients."""

    id: int
    project_id: int
    kind: str
    status: str
    output_file_path: str | None
    browser_streamable: bool | None
    # RFC 6381 codec string and MIME type probed from the output (``None``
    # until probed, or when the probe failed).
    codec_string: str | None = None
    mime_type: str | None = None
    started_at: str | None
    completed_at: str | None
    created_at: str | None


class MilestoneCreate(BaseModel):
    """Request body to place a milestone in a project's timeline."""

    label: str = Field(min_length=1)
    position_frame_index: int | None = Field(default=None, ge=0)
    position_timestamp: datetime | None = None


class MilestoneUpdate(BaseModel):
    """Request body to update a milestone's label and/or position.

    Every field is optional: only the supplied fields change. Supplying a
    position keeps the create-time invariant that a milestone has at least one of
    a frame index or a timestamp -- an update may not leave it with neither.
    """

    label: str | None = Field(default=None, min_length=1)
    position_frame_index: int | None = Field(default=None, ge=0)
    position_timestamp: datetime | None = None


class MilestoneOut(BaseModel):
    """A milestone as returned to clients."""

    id: int
    project_id: int
    label: str | None
    position_frame_index: int | None
    position_timestamp: str | None


def _queue() -> RenderQueue:
    """Return the running render worker or raise a 503 if absent."""
    queue = get_context().render_queue
    if queue is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="render engine is not available",
        )
    return queue


def _get_project_or_404(session: Session, project_id: int) -> Project:
    """Return a project row or raise a 404."""
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"project {project_id} not found",
        )
    return project


def _get_render_or_404(session: Session, render_id: int) -> RenderJob:
    """Return a render-job row or raise a 404.

    A frame export shares the table but is not a render: its id is a 404 here,
    so the render routes (status, cancel, download, stream) never hand out an
    export's zip (#67 S13). Exports have their own operator-gated routes.
    """
    job = session.get(RenderJob, render_id)
    if job is None or job.kind == "export":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"render {render_id} not found",
        )
    return job


def _iso(value: datetime | None) -> str | None:
    """Return an ISO-8601 string for a datetime, or ``None``."""
    return value.isoformat() if value is not None else None


def _render_out(job: RenderJob) -> RenderOut:
    """Project a render-job row onto its public representation."""
    return RenderOut(
        id=job.id,
        project_id=job.project_id,
        kind=job.kind,
        status=job.status,
        output_file_path=job.output_file_path,
        browser_streamable=job.browser_streamable,
        codec_string=job.codec_string,
        mime_type=job.mime_type,
        started_at=_iso(job.started_at),
        completed_at=_iso(job.completed_at),
        created_at=_iso(job.created_at),
    )


def _milestone_out(row: Milestone) -> MilestoneOut:
    """Project a milestone row onto its public representation."""
    return MilestoneOut(
        id=row.id,
        project_id=row.project_id,
        label=row.label,
        position_frame_index=row.position_frame_index,
        position_timestamp=_iso(row.position_timestamp),
    )


def _trigger_precheck(session: Session, project_id: int) -> bool:
    """Confirm the project exists (404) and report whether it has milestones.

    Runs the trigger route's pre-worker synchronous reads in one offloaded call:
    the 404 check must precede the worker's 503 check, and the milestone presence
    drives the chapters-in-container validation.
    """
    _get_project_or_404(session, project_id)
    return _has_milestones(session, project_id)


def _persist_render(
    session: Session,
    principal: Principal,
    project_id: int,
    output_settings: dict[str, Any],
    overlay_config: dict[str, Any],
) -> tuple[RenderOut, tuple[Effect, ...], bool]:
    """Enqueue the render through the service, commit, and return the response.

    Synchronous; called via ``run_in_threadpool``. Builds the response model
    while the job is still attached, commits the row so the woken worker sees it,
    and hands back the effects for the caller to run on the event loop, plus
    whether an identical queued job was returned instead of a new one (#67 R14).
    """
    result = render_service.enqueue_render(
        session,
        Actor.of(principal),
        project_id,
        output_settings=output_settings,
        overlay_config=overlay_config,
        dedupe=True,
    )
    out_model = _render_out(result.value)
    session.commit()
    return out_model, result.effects, render_service.is_deduplicated(result)


@router.post(
    "/{project_id}/renders",
    response_model=RenderOut,
    status_code=status.HTTP_201_CREATED,
)
async def trigger_render(
    project_id: int,
    payload: RenderCreate,
    session: DbSessionDep,
    principal: Annotated[Principal, Depends(require_operator_or_admin_principal)],
    response: Response,
) -> RenderOut:
    """Trigger a manual render: validate the target, queue it, return the job.

    The encoder validates the target up front (codec/container/parameters and the
    chapters-in-container rule), so an unsupported request is rejected before any
    job row is created. On success a pending ``manual`` job is inserted and the
    worker is woken to pick it up.

    A repeated submit is not queued twice (#67 R14): while a manual job of the
    project with the same output and overlay settings is still ``pending`` or
    ``encoding``, that job is returned instead, with the same ``201`` and an
    ``X-Render-Deduplicated: true`` header.

    This route awaits the encoder's async capability probe, so it stays ``async``;
    its synchronous database work (the project existence check, the insert, the
    audit, and the commit) is pushed off the event loop onto the request
    threadpool (``run_in_threadpool``) so a slow query never stalls the loop.
    """
    # Enforce 404-before-503 ordering: confirm the project exists (and read
    # whether it carries any milestone, which drives the chapters-in-container
    # rule) before the worker is consulted. Offloaded so the reads never block
    # the event loop.
    has_milestones = await run_in_threadpool(_trigger_precheck, session, project_id)
    queue = _queue()

    out = payload.output
    fps = out.fps if out.fps is not None else get_context().settings.render.default_fps
    target = OutputSettings(
        fps=fps,
        width=out.width,
        height=out.height,
        codec=out.codec,
        container=out.container,
        bitrate_kbps=out.bitrate_kbps,
        crf=out.crf,
    )
    has_chapters = out.auto_chapters in ("monthly", "weekly") or has_milestones
    try:
        await queue.encoder.validate(target, has_chapters=has_chapters)
    except EncoderCapabilityError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unsupported render setting ({exc.option}): {exc}",
        ) from exc
    except EncoderError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    out_model, effects, deduplicated = await run_in_threadpool(
        _persist_render,
        session,
        principal,
        project_id,
        _output_json(out, fps),
        payload.overlay.model_dump(),
    )
    if deduplicated:
        response.headers[DEDUPLICATED_HEADER] = "true"
    # Wake the worker only after the row is durably committed.
    run_effects(effects, get_context())
    return out_model


def _suggest_fps(
    session: Session,
    project_id: int,
    start: datetime | None,
    end: datetime | None,
) -> SuggestedFpsOut:
    """Return the suggestion for a project's frames. Synchronous (threadpool)."""
    _get_project_or_404(session, project_id)
    found = fps_suggest.suggest_for_project(session, project_id, start, end)
    return SuggestedFpsOut(
        fps=found.fps,
        interval_seconds=found.interval_seconds,
        frame_count=found.frame_count,
        expected_duration_seconds=found.expected_duration_seconds,
    )


@router.get("/{project_id}/renders/suggested-fps", response_model=SuggestedFpsOut)
async def suggested_fps(
    project_id: int,
    session: DbSessionDep,
    start: datetime | None = None,
    end: datetime | None = None,
) -> SuggestedFpsOut:
    """Suggest a render frame rate from the interval between a project's frames.

    The suggestion uses the median gap of the frames a render would use, in the
    optional ``start``..``end`` range (inclusive; a bound without a zone is UTC).
    ``fps`` is ``null`` with fewer than two frames. A trigger without ``fps``
    still uses ``render.default_fps``; this endpoint only informs the caller.
    Reads frame timestamps only, off the event loop.
    """
    if (
        start is not None
        and end is not None
        and fps_suggest.naive_utc(start) > fps_suggest.naive_utc(end)
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="start must not be after end",
        )
    return await run_in_threadpool(_suggest_fps, session, project_id, start, end)


@router.get("/{project_id}/renders", response_model=list[RenderOut])
def list_renders(
    project_id: int,
    session: DbSessionDep,
) -> list[RenderOut]:
    """List a project's render jobs, newest first."""
    _get_project_or_404(session, project_id)
    jobs = (
        session.execute(
            select(RenderJob)
            .where(RenderJob.project_id == project_id)
            .order_by(RenderJob.id.desc())
        )
        .scalars()
        .all()
    )
    return [_render_out(job) for job in jobs]


@renders_router.get("/{render_id}", response_model=RenderOut)
def get_render(
    render_id: int,
    session: DbSessionDep,
) -> RenderOut:
    """Return a single render job's status."""
    return _render_out(_get_render_or_404(session, render_id))


async def _cancel_through_worker(
    queue: RenderQueue, session: Session, job_id: int
) -> bool:
    """Have the worker stop a job, after ending this request's transaction.

    The worker settles the job (``failed``, partial output removed) in its own
    session. A request authenticated by a session cookie has already touched
    its session row, so its transaction holds SQLite's write lock; the worker's
    write would wait on that lock and fail with "database is locked". The
    request's transaction is therefore committed first. Used by the cancel
    route and by :func:`delete_job`; both are a user's request, so the worker
    writes no failure event and the caller records the cancel (``info``).
    Returns whether the worker changed the job (see ``RenderQueue.cancel_job``):
    a running, pending or orphaned job is settled; an already terminal one is
    not.
    """
    await run_in_threadpool(session.commit)
    return await queue.cancel_job(job_id, user_requested=True)


def _cancel_load(session: Session, render_id: int) -> RenderOut | None:
    """Load a render (404) and return its response if already terminal, else ``None``.

    Synchronous; called via ``run_in_threadpool``. A ``done``/``failed`` job is
    returned unchanged so the caller short-circuits before touching the worker.
    """
    job = _get_render_or_404(session, render_id)
    if job.status in ("done", "failed"):
        return _render_out(job)
    return None


def _cancel_finalize(
    session: Session, principal: Principal, render_id: int, *, changed: bool
) -> RenderOut:
    """Audit the cancellation, commit, and return the worker's settled state.

    Synchronous; called via ``run_in_threadpool`` after the worker has torn
    down the job in its own session. The audit event is written only when the
    worker really changed the job, so the log never says "cancelled" about a
    row the cancel did not touch. Expires the request session so the re-read
    reflects the worker's terminal write rather than this session's stale row.
    """
    if changed:
        render_service.cancel_render(session, Actor.of(principal), render_id)
        session.commit()
    session.expire_all()
    return _render_out(_get_render_or_404(session, render_id))


@renders_router.post("/{render_id}/cancel", response_model=RenderOut)
async def cancel_render(
    render_id: int,
    session: DbSessionDep,
    principal: Annotated[Principal, Depends(require_operator_or_admin_principal)],
) -> RenderOut:
    """Cancel a pending or in-flight render.

    Routes through the worker: a pending job flips straight to ``failed``; an
    in-flight render's task is cancelled and awaited so the ffmpeg child is
    killed, the partial output removed, and the job recorded ``failed`` before
    this returns. A render already in a terminal state is returned unchanged --
    that check runs before the worker is consulted, so a settled job still
    returns ``200`` even when the worker is absent.

    This route awaits the worker's async teardown, so it stays ``async``; its
    synchronous database work (the load, the audit, the commit, and the re-read)
    is pushed off the event loop onto the request threadpool.
    """
    terminal_out = await run_in_threadpool(_cancel_load, session, render_id)
    if terminal_out is not None:
        return terminal_out

    changed = await _cancel_through_worker(_queue(), session, render_id)
    return await run_in_threadpool(
        _cancel_finalize, session, principal, render_id, changed=changed
    )


def _job_status_or_404(session: Session, job_id: int, kind: str | None) -> str:
    """Return a job's current status (a fresh read) or raise ``404``.

    ``kind`` restricts the lookup: ``"render"`` rejects export jobs, ``"export"``
    rejects everything else, ``None`` accepts any job. Synchronous.
    """
    session.expire_all()
    job = session.get(RenderJob, job_id)
    if job is None or (kind == "render" and job.kind == "export"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"render {job_id} not found"
        )
    if kind == "export" and job.kind != "export":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"export {job_id} not found"
        )
    return job.status


def _delete_commit(
    session: Session, subject: Any, job_id: int, kind: str | None
) -> DeletedJob:
    """Delete the (stopped) job through the service and commit. Synchronous."""
    session.expire_all()
    actor = Actor.of(subject)
    settings = get_context().settings
    job = session.get(RenderJob, job_id)
    is_export = job is not None and job.kind == "export"
    if kind == "export" or (kind is None and is_export):
        result = render_service.delete_export(session, actor, job_id, settings=settings)
    else:
        result = render_service.delete_render(session, actor, job_id, settings=settings)
    session.commit()
    return result.value


async def delete_job(
    session: Session, subject: Any, job_id: int, *, kind: str | None
) -> DeletedJob:
    """Stop a job if it is still running, then delete it (file and row).

    Shared by ``DELETE /api/v1/renders/{id}`` and the web delete routes (owner
    decision: delete on a pending or encoding job cancels it first). A running
    job goes through the worker's ``cancel_job``, which kills ffmpeg and removes
    the partial output -- or waits out an export's zip thread -- before
    returning; the status is then re-read, and the cancel retried briefly to
    cover a job claimed by the worker a moment before its task was registered.
    Only a job seen in a terminal state is handed to the service, which itself
    refuses a running one, so a row is never removed under a live writer. An
    orphaned ``encoding`` row (no live task) is settled by the worker's cancel
    too, so it deletes without a restart (#67 R1).

    Raises ``404`` for an unknown id (or the wrong kind), ``503`` when a running
    job cannot be stopped because the worker is absent, ``409`` when it does not
    stop; the service's own errors (``403``) propagate to the app's handler.
    """
    # The routes gate on role already; checked here too so no caller can stop
    # a render it is not allowed to delete.
    # ``subject`` is a web ``User`` row on the web routes: read it in the pool.
    actor = await run_in_threadpool(Actor.of, subject)
    if actor.role not in OPERATING_ROLES:
        raise PermissionError_("only operators and admins may delete a render")
    job_status = await run_in_threadpool(_job_status_or_404, session, job_id, kind)
    attempts = 0
    cancelled = False
    while job_status in RUNNING_STATUSES:
        if attempts >= _DELETE_CANCEL_ATTEMPTS:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"render {job_id} could not be stopped; try again",
            )
        if attempts:
            await asyncio.sleep(_DELETE_CANCEL_RETRY_SECONDS)
        cancelled = await _cancel_through_worker(_queue(), session, job_id) or cancelled
        attempts += 1
        job_status = await run_in_threadpool(_job_status_or_404, session, job_id, kind)
    if cancelled:
        await run_in_threadpool(_audit_cancel, session, subject, job_id)
    return await run_in_threadpool(_delete_commit, session, subject, job_id, kind)


def _audit_cancel(session: Session, subject: Any, job_id: int) -> None:
    """Record the cancellation a delete performed, like the cancel route does."""
    render_service.cancel_render(session, Actor.of(subject), job_id)
    session.commit()


@renders_router.delete("/{render_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_render(
    render_id: int,
    session: DbSessionDep,
    principal: Annotated[Principal, Depends(require_operator_or_admin_principal)],
) -> None:
    """Delete a render (or export) job and its output file.

    A pending or encoding job is cancelled first (see :func:`delete_job`). The
    file is removed only when it resolves inside the project's render root; a
    file that is already gone is not an error. ``204`` on success, ``404`` for
    an unknown id, ``403`` for a viewer.
    """
    await delete_job(session, principal, render_id, kind=None)


@renders_router.get("/{render_id}/download")
def download_render(
    render_id: int,
    session: DbSessionDep,
) -> FileResponse:
    """Download a completed render's output file."""
    job = _get_render_or_404(session, render_id)
    path = _output_path_or_404(session, job)
    return FileResponse(path, media_type=_media_type(path), filename=path.name)


@renders_router.get("/{render_id}/stream")
def stream_render(
    render_id: int,
    request: Request,
    session: DbSessionDep,
) -> Response:
    """Stream a render for inline playback, honouring HTTP ``Range`` requests.

    Every finished render is served inline with its real MIME type, whatever
    its codec: the viewer's browser decides whether it can play it (see
    ``static/js/playability.js``); ``browser_streamable`` is informational only.
    When the client sends a satisfiable ``Range`` header, responds ``206
    Partial Content`` with the requested byte range and a ``Content-Range``
    header (a range past EOF is clamped). A request with no/invalid range gets
    the full file (``200``).
    """
    job = _get_render_or_404(session, render_id)
    path = _output_path_or_404(session, job)
    media_type = _media_type(path)

    file_size = path.stat().st_size
    range_header = request.headers.get("range")
    byte_range = _parse_range(range_header, file_size)
    if byte_range is None:
        return FileResponse(path, media_type=media_type)

    start, end = byte_range
    length = end - start + 1
    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
    }
    return StreamingResponse(
        _iter_file_range(path, start, length),
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        media_type=media_type,
        headers=headers,
    )


@router.post(
    "/{project_id}/milestones",
    response_model=MilestoneOut,
    status_code=status.HTTP_201_CREATED,
)
def create_milestone(
    project_id: int,
    payload: MilestoneCreate,
    session: DbSessionDep,
    principal: Annotated[Principal, Depends(require_operator_or_admin_principal)],
) -> MilestoneOut:
    """Place a milestone in a project's timeline, attributed to the principal."""
    spec = MilestoneSpec(
        label=payload.label,
        position_frame_index=payload.position_frame_index,
        position_timestamp=_naive_utc(payload.position_timestamp),
        fields_set=frozenset({"label", "position_frame_index", "position_timestamp"}),
    )
    result = milestone_service.create_milestone(
        session, Actor.of(principal), project_id, spec
    )
    out = _milestone_out(result.value)
    session.commit()
    run_effects(result.effects, get_context())
    return out


@router.get("/{project_id}/milestones", response_model=list[MilestoneOut])
def list_milestones(
    project_id: int,
    session: DbSessionDep,
) -> list[MilestoneOut]:
    """List a project's milestones in creation order."""
    _get_project_or_404(session, project_id)
    rows = (
        session.execute(
            select(Milestone)
            .where(Milestone.project_id == project_id)
            .order_by(Milestone.id)
        )
        .scalars()
        .all()
    )
    return [_milestone_out(row) for row in rows]


@router.patch(
    "/{project_id}/milestones/{milestone_id}",
    response_model=MilestoneOut,
)
def update_milestone(
    project_id: int,
    milestone_id: int,
    payload: MilestoneUpdate,
    session: DbSessionDep,
    principal: Annotated[Principal, Depends(require_operator_or_admin_principal)],
) -> MilestoneOut:
    """Update a milestone's label and/or position, attributed to the principal.

    Only the fields present in the request change (a partial update). A position
    field is applied only when the client actually sent it, so omitting both
    leaves the existing position untouched; setting one replaces it. The
    create-time invariant is preserved: an update may not leave the milestone
    with neither a frame index nor a timestamp.
    """
    spec = MilestoneSpec(
        label=payload.label,
        position_frame_index=payload.position_frame_index,
        position_timestamp=_naive_utc(payload.position_timestamp),
        fields_set=frozenset(payload.model_fields_set),
    )
    result = milestone_service.update_milestone(
        session, Actor.of(principal), project_id, milestone_id, spec
    )
    out = _milestone_out(result.value)
    session.commit()
    run_effects(result.effects, get_context())
    return out


@router.delete(
    "/{project_id}/milestones/{milestone_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_milestone(
    project_id: int,
    milestone_id: int,
    session: DbSessionDep,
    principal: Annotated[Principal, Depends(require_operator_or_admin_principal)],
) -> None:
    """Delete a milestone from a project."""
    result = milestone_service.delete_milestone(
        session, Actor.of(principal), project_id, milestone_id
    )
    session.commit()
    run_effects(result.effects, get_context())


def _has_milestones(session: Session, project_id: int) -> bool:
    """Return whether the project has any milestone (which becomes a chapter)."""
    row = session.execute(
        select(Milestone.id).where(Milestone.project_id == project_id).limit(1)
    ).first()
    return row is not None


def _output_json(out: OutputSettingsBody, fps: float) -> dict[str, Any]:
    """Build the JSON output settings stored on the job (resolved fps included)."""
    data: dict[str, Any] = {
        "fps": fps,
        "width": out.width,
        "height": out.height,
        "codec": out.codec,
        "container": out.container,
        "deflicker": out.deflicker,
    }
    if out.bitrate_kbps is not None:
        data["bitrate_kbps"] = out.bitrate_kbps
    if out.crf is not None:
        data["crf"] = out.crf
    if out.auto_chapters in ("monthly", "weekly"):
        data["auto_chapters"] = out.auto_chapters
    return data


def _output_path_or_404(session: Session, job: RenderJob) -> Path:
    """Return the confined, on-disk output path for a finished render or 404.

    A render that has not produced a file (not done, or no path) is a 404. The
    stored path must resolve inside the project's render root before it is served,
    so a tampered row can never read a file elsewhere on disk.
    """
    if job.status != "done" or not job.output_file_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"render {job.id} has no output file",
        )
    project = _get_project_or_404(session, job.project_id)
    root = project_render_root(get_context().settings, project).resolve()
    resolved = Path(job.output_file_path).resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"render {job.id} output is unavailable",
        )
    return resolved


def _media_type(path: Path) -> str:
    """Return the video media type for an output path's extension."""
    return media_type_for_path(path)


def _parse_range(header: str | None, file_size: int) -> tuple[int, int] | None:
    """Parse a single ``bytes=start-end`` range, clamped to the file.

    Returns ``(start, end)`` inclusive, or ``None`` to signal "serve the whole
    file": an absent header, an unparseable header, or a start at/after EOF all
    fall back to the full body rather than a 416, so playback never breaks on a
    malformed range. A suffix range (``bytes=-N``) returns the last ``N`` bytes.
    """
    if not header or file_size == 0:
        return None
    match = _RANGE_RE.match(header.strip())
    if match is None:
        return None
    start_text, end_text = match.group(1), match.group(2)
    if start_text == "" and end_text == "":
        return None
    if start_text == "":
        # Suffix range: the last N bytes.
        suffix = int(end_text)
        if suffix == 0:
            return None
        start = max(0, file_size - suffix)
        return start, file_size - 1
    start = int(start_text)
    if start >= file_size:
        return None
    end = int(end_text) if end_text != "" else file_size - 1
    end = min(end, file_size - 1)
    if end < start:
        return None
    return start, end


def _open_at(path: Path, start: int) -> BinaryIO:
    """Open ``path`` for reading, positioned at ``start``. Synchronous."""
    handle = path.open("rb")
    try:
        handle.seek(start)
    except BaseException:
        handle.close()
        raise
    return handle


async def _iter_file_range(path: Path, start: int, length: int) -> Any:
    """Yield a byte range of a file in bounded chunks for streaming.

    The open, the seek and every chunk read run on the request threadpool
    (``run_in_threadpool``), never on the event loop and never on the loop's
    default executor, where a slow disk under playback would compete with
    capture and the background loops (#66 C3, gap G10).
    """
    remaining = length
    handle = await run_in_threadpool(_open_at, path, start)
    try:
        while remaining > 0:
            chunk = await run_in_threadpool(handle.read, min(_STREAM_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        # Closing a read-only handle does no I/O worth a hop, and a generator
        # closed during cancellation must not await.
        handle.close()


def _naive_utc(value: datetime | None) -> datetime | None:
    """Normalise an aware/naive datetime to naive UTC for storage, or ``None``."""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value
