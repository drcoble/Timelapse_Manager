"""Render and milestone endpoints.

Covers the render lifecycle (trigger a manual render, list/status, cancel,
download the output, and range-stream it for inline playback) and milestone CRUD
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
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Event, Milestone, Project, RenderJob
from ..db.session import get_session
from ..encode import EncoderCapabilityError, EncoderError, OutputSettings
from ..render import RenderQueue, project_render_root
from ..runtime import get_context
from ..security import Principal, require_operator_or_admin_principal
from ..security.principal import ensure_sentinel_admin

logger = logging.getLogger(__name__)

# Two sibling routers: project-scoped collection routes and id-addressed routes.
router = APIRouter(prefix="/projects", tags=["renders"])
renders_router = APIRouter(prefix="/renders", tags=["renders"])

# Bytes read per chunk when range-streaming, so a large file is not buffered.
_STREAM_CHUNK = 64 * 1024

# A single ``bytes=start-end`` range (the only form needed for video seeking).
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")

_MEDIA_TYPES: dict[str, str] = {
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
}


class OutputSettingsBody(BaseModel):
    """Encode target for a manual render."""

    fps: float | None = None
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
    timestamp_format: str = "%Y-%m-%d %H:%M:%S"
    timestamp_timezone: str = "UTC"
    text_enabled: bool = False
    text_content: str = ""
    image_enabled: bool = False
    image_path: str | None = None
    placement: str = "top_left"


class RenderCreate(BaseModel):
    """Request body to trigger a manual render."""

    output: OutputSettingsBody = Field(default_factory=OutputSettingsBody)
    overlay: OverlayBody = Field(default_factory=OverlayBody)


class RenderOut(BaseModel):
    """A render job's state as returned to clients."""

    id: int
    project_id: int
    kind: str
    status: str
    output_file_path: str | None
    browser_streamable: bool | None
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
    """Return a render-job row or raise a 404."""
    job = session.get(RenderJob, render_id)
    if job is None:
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


def _audit(
    session: Session, *, project_id: int, actor_user_id: int, message: str
) -> None:
    """Write a project-scoped audit event attributed to the actor.

    The actor is a foreign key to a user row; until real accounts are seeded the
    sentinel administrator is materialised here so the audit insert (and any
    actor-attributed row) never fails its foreign key.
    """
    ensure_sentinel_admin(session)
    session.add(
        Event(
            scope="project",
            scope_id=project_id,
            level="info",
            message=message,
            actor_user_id=actor_user_id,
            timestamp=datetime.now(UTC).replace(tzinfo=None),
        )
    )


@router.post(
    "/{project_id}/renders",
    response_model=RenderOut,
    status_code=status.HTTP_201_CREATED,
)
async def trigger_render(
    project_id: int,
    payload: RenderCreate,
    session: Annotated[Session, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_operator_or_admin_principal)],
) -> RenderOut:
    """Trigger a manual render: validate the target, queue it, return the job.

    The encoder validates the target up front (codec/container/parameters and the
    chapters-in-container rule), so an unsupported request is rejected before any
    job row is created. On success a pending ``manual`` job is inserted and the
    worker is woken to pick it up.
    """
    _get_project_or_404(session, project_id)
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
    has_chapters = out.auto_chapters in ("monthly", "weekly") or _has_milestones(
        session, project_id
    )
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

    job = RenderJob(
        project_id=project_id,
        kind="manual",
        status="pending",
        output_settings=_output_json(out, fps),
        overlay_config=payload.overlay.model_dump(),
    )
    session.add(job)
    session.flush()
    _audit(
        session,
        project_id=project_id,
        actor_user_id=principal.user_id,
        message=f"manual render {job.id} triggered",
    )
    out_model = _render_out(job)
    # Wake the worker only after the row is durably committed by the dependency.
    session.commit()
    queue.notify()
    return out_model


@router.get("/{project_id}/renders", response_model=list[RenderOut])
def list_renders(
    project_id: int,
    session: Annotated[Session, Depends(get_session)],
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
    session: Annotated[Session, Depends(get_session)],
) -> RenderOut:
    """Return a single render job's status."""
    return _render_out(_get_render_or_404(session, render_id))


@renders_router.post("/{render_id}/cancel", response_model=RenderOut)
async def cancel_render(
    render_id: int,
    session: Annotated[Session, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_operator_or_admin_principal)],
) -> RenderOut:
    """Cancel a pending or in-flight render.

    Routes through the worker: a pending job flips straight to ``failed``; an
    in-flight render's task is cancelled and awaited so the ffmpeg child is
    killed, the partial output removed, and the job recorded ``failed`` before
    this returns. A render already in a terminal state is returned unchanged.
    """
    job = _get_render_or_404(session, render_id)
    if job.status in ("done", "failed"):
        return _render_out(job)

    project_id = job.project_id
    queue = _queue()
    await queue.cancel_job(render_id)

    _audit(
        session,
        project_id=project_id,
        actor_user_id=principal.user_id,
        message=f"render {render_id} cancelled",
    )
    session.commit()
    # Re-read so the response reflects the worker's terminal write.
    session.expire_all()
    return _render_out(_get_render_or_404(session, render_id))


@renders_router.get("/{render_id}/download")
def download_render(
    render_id: int,
    session: Annotated[Session, Depends(get_session)],
) -> FileResponse:
    """Download a completed render's output file."""
    job = _get_render_or_404(session, render_id)
    path = _output_path_or_404(session, job)
    return FileResponse(path, media_type=_media_type(path), filename=path.name)


@renders_router.get("/{render_id}/stream")
def stream_render(
    render_id: int,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
) -> Response:
    """Stream a render for inline playback, honouring HTTP ``Range`` requests.

    When the render is browser-streamable and the client sends a satisfiable
    ``Range`` header, responds ``206 Partial Content`` with the requested byte
    range and a ``Content-Range`` header (a range past EOF is clamped). A render
    that is not browser-streamable, or a request with no/invalid range, falls
    back to the full file (``200``) so playback still works.
    """
    job = _get_render_or_404(session, render_id)
    path = _output_path_or_404(session, job)
    media_type = _media_type(path)

    if not job.browser_streamable:
        return FileResponse(path, media_type=media_type, filename=path.name)

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
    session: Annotated[Session, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_operator_or_admin_principal)],
) -> MilestoneOut:
    """Place a milestone in a project's timeline, attributed to the principal."""
    _get_project_or_404(session, project_id)
    if payload.position_frame_index is None and payload.position_timestamp is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="a milestone needs a position_frame_index or position_timestamp",
        )
    # A milestone references the placing user; materialise the sentinel admin so
    # the foreign key holds until real accounts are seeded.
    ensure_sentinel_admin(session)
    milestone = Milestone(
        project_id=project_id,
        user_id=principal.user_id,
        label=payload.label,
        position_frame_index=payload.position_frame_index,
        position_timestamp=_naive_utc(payload.position_timestamp),
    )
    session.add(milestone)
    session.flush()
    _audit(
        session,
        project_id=project_id,
        actor_user_id=principal.user_id,
        message=f"milestone {milestone.id} created",
    )
    return _milestone_out(milestone)


@router.get("/{project_id}/milestones", response_model=list[MilestoneOut])
def list_milestones(
    project_id: int,
    session: Annotated[Session, Depends(get_session)],
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
    session: Annotated[Session, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_operator_or_admin_principal)],
) -> MilestoneOut:
    """Update a milestone's label and/or position, attributed to the principal.

    Only the fields present in the request change (a partial update). A position
    field is applied only when the client actually sent it, so omitting both
    leaves the existing position untouched; setting one replaces it. The
    create-time invariant is preserved: an update may not leave the milestone
    with neither a frame index nor a timestamp.
    """
    milestone = session.get(Milestone, milestone_id)
    if milestone is None or milestone.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"milestone {milestone_id} not found in project {project_id}",
        )

    fields_set = payload.model_fields_set
    if "label" in fields_set and payload.label is not None:
        milestone.label = payload.label
    new_frame = (
        payload.position_frame_index
        if "position_frame_index" in fields_set
        else milestone.position_frame_index
    )
    new_timestamp = (
        _naive_utc(payload.position_timestamp)
        if "position_timestamp" in fields_set
        else milestone.position_timestamp
    )
    if new_frame is None and new_timestamp is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="a milestone needs a position_frame_index or position_timestamp",
        )
    milestone.position_frame_index = new_frame
    milestone.position_timestamp = new_timestamp

    _audit(
        session,
        project_id=project_id,
        actor_user_id=principal.user_id,
        message=f"milestone {milestone_id} updated",
    )
    session.flush()
    return _milestone_out(milestone)


@router.delete(
    "/{project_id}/milestones/{milestone_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_milestone(
    project_id: int,
    milestone_id: int,
    session: Annotated[Session, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_operator_or_admin_principal)],
) -> None:
    """Delete a milestone from a project."""
    milestone = session.get(Milestone, milestone_id)
    if milestone is None or milestone.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"milestone {milestone_id} not found in project {project_id}",
        )
    session.delete(milestone)
    _audit(
        session,
        project_id=project_id,
        actor_user_id=principal.user_id,
        message=f"milestone {milestone_id} deleted",
    )


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
    return _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


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


async def _iter_file_range(path: Path, start: int, length: int) -> Any:
    """Yield a byte range of a file in bounded chunks for streaming."""
    remaining = length
    with path.open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            chunk = await asyncio.to_thread(handle.read, min(_STREAM_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def _naive_utc(value: datetime | None) -> datetime | None:
    """Normalise an aware/naive datetime to naive UTC for storage, or ``None``."""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value
