"""Frame listing, lifecycle, and capture-status endpoints.

Lists a project's frames in capture order (paginated, optionally including
soft-deleted ones), reports a project's live capture state from the supervisor,
and exposes the operator-or-admin-gated lifecycle operations: soft-delete,
restore, permanent delete, upload, and capture-timestamp correction.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session, sessionmaker

from ..capture import CaptureSupervisor
from ..config import Settings
from ..db.models import Frame, Project
from ..db.session import get_session
from ..runtime import get_context
from ..security import Principal, require_operator_or_admin_principal
from ..storage import frames as frame_service

router = APIRouter(prefix="/frames", tags=["frames"])

# Lifecycle mutations live under a project-scoped path so the owning project is
# explicit in the URL and verifiable against the frame. Kept on its own router
# (mounted alongside the read router) so the path shapes differ cleanly.
admin_router = APIRouter(prefix="/projects", tags=["frames"])

_MAX_LIMIT = 500

# Magic-byte prefixes for the only image formats accepted on upload; mirrors the
# dimension reader's detection so an invalid body is rejected before any work.
_JPEG_MAGIC = b"\xff\xd8"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class FrameOut(BaseModel):
    """A frame's metadata as returned to clients."""

    id: int
    project_id: int
    sequence_index: int
    capture_timestamp: str | None
    file_path: str | None
    width: int | None
    height: int | None
    file_size_bytes: int | None
    capture_status: str
    origin: str
    lifecycle_state: str
    dimension_mismatch: bool


class TimestampUpdate(BaseModel):
    """Body for a capture-timestamp correction; no other field is accepted."""

    model_config = ConfigDict(extra="forbid")

    capture_timestamp: datetime


class ImportSkippedOut(BaseModel):
    """A single file rejected during a batch import, with the reason."""

    filename: str
    reason: str | None


class ImportResultOut(BaseModel):
    """Outcome of a batch frame import: counts plus the per-file skip list."""

    imported_count: int
    skipped_count: int
    skipped: list[ImportSkippedOut]


class CaptureStatusOut(BaseModel):
    """Live capture state for a single project."""

    project_id: int
    camera_id: int | None
    state: str
    last_success_at: str | None
    last_error_at: str | None
    last_error: str | None
    frames_captured: int


def _to_out(frame: Frame, predominant: tuple[int, int] | None) -> FrameOut:
    """Project a frame row onto its public representation.

    ``dimension_mismatch`` is computed against the project's predominant frame
    dimensions (``predominant``) at serialization time; it is never stored. The
    persisted ``file_path`` is echoed as-is (relative for new frames); callers
    that open the file resolve it through the shared path layer instead.
    """
    return FrameOut(
        id=frame.id,
        project_id=frame.project_id,
        sequence_index=frame.sequence_index,
        capture_timestamp=(
            frame.capture_timestamp.isoformat()
            if frame.capture_timestamp is not None
            else None
        ),
        file_path=frame.file_path,
        width=frame.width,
        height=frame.height,
        file_size_bytes=frame.file_size_bytes,
        capture_status=frame.capture_status,
        origin=frame.origin,
        lifecycle_state=frame.lifecycle_state,
        dimension_mismatch=frame_service.dimension_mismatch(frame, predominant),
    )


def _get_frame_in_project(session: Session, project_id: int, frame_id: int) -> Frame:
    """Return a frame that belongs to ``project_id`` or raise a 404.

    A frame addressed through the wrong project's path is treated as not found,
    so one project's URL can never mutate another's frame.
    """
    frame = session.get(Frame, frame_id)
    if frame is None or frame.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"frame {frame_id} not found in project {project_id}",
        )
    return frame


def _supervisor() -> CaptureSupervisor:
    """Return the running capture supervisor or raise a 503 if absent."""
    supervisor = get_context().capture_supervisor
    if supervisor is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="capture engine is not available",
        )
    return supervisor


@router.get("", response_model=list[FrameOut])
def list_frames(
    session: Annotated[Session, Depends(get_session)],
    project_id: Annotated[int, Query(description="project to list frames for")],
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    include_deleted: Annotated[
        bool, Query(description="include soft-deleted frames")
    ] = False,
) -> list[FrameOut]:
    """List a project's frames in capture order, paginated.

    Ordered by capture timestamp ascending (with sequence index as a stable
    tie-break). Soft-deleted frames are excluded unless ``include_deleted`` is
    set. Each frame's ``dimension_mismatch`` is computed against the project's
    predominant frame dimensions, evaluated once over all active frames so the
    flag is consistent across pages.
    """
    frames = frame_service.list_frames(
        session,
        project_id,
        limit=limit,
        offset=offset,
        include_deleted=include_deleted,
    )
    predominant = frame_service.predominant_dimensions(session, project_id)
    return [_to_out(frame, predominant) for frame in frames]


@router.get("/capture-status", response_model=CaptureStatusOut)
def capture_status(
    project_id: Annotated[int, Query(description="project to report status for")],
) -> CaptureStatusOut:
    """Return the live capture state for a project."""
    supervisor = _supervisor()
    state = supervisor.state_for_project(project_id)
    if state is None:
        return CaptureStatusOut(
            project_id=project_id,
            camera_id=None,
            state="idle",
            last_success_at=None,
            last_error_at=None,
            last_error=None,
            frames_captured=0,
        )
    return CaptureStatusOut(
        project_id=state.project_id,
        camera_id=state.camera_id,
        state=state.state,
        last_success_at=(
            state.last_success_at.isoformat()
            if state.last_success_at is not None
            else None
        ),
        last_error_at=(
            state.last_error_at.isoformat() if state.last_error_at is not None else None
        ),
        last_error=state.last_error,
        frames_captured=state.frames_captured,
    )


def _frame_out_in_project(session: Session, project_id: int, frame: Frame) -> FrameOut:
    """Serialize a frame after a mutation, with a freshly computed mismatch flag."""
    session.flush()
    predominant = frame_service.predominant_dimensions(session, project_id)
    return _to_out(frame, predominant)


@admin_router.post("/{project_id}/frames/{frame_id}/soft-delete")
def soft_delete_frame(
    project_id: int,
    frame_id: int,
    session: Annotated[Session, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_operator_or_admin_principal)],
) -> FrameOut:
    """Soft-delete a frame: flip its lifecycle flag, keep its file on disk."""
    _get_frame_in_project(session, project_id, frame_id)
    frame = frame_service.soft_delete(session, frame_id, principal.user_id)
    return _frame_out_in_project(session, project_id, frame)


@admin_router.post("/{project_id}/frames/{frame_id}/restore")
def restore_frame(
    project_id: int,
    frame_id: int,
    session: Annotated[Session, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_operator_or_admin_principal)],
) -> FrameOut:
    """Restore a soft-deleted frame to the active set."""
    _get_frame_in_project(session, project_id, frame_id)
    frame = frame_service.restore(session, frame_id, principal.user_id)
    return _frame_out_in_project(session, project_id, frame)


@admin_router.post(
    "/{project_id}/frames/{frame_id}/permanent-delete",
    status_code=status.HTTP_204_NO_CONTENT,
)
def permanent_delete_frame(
    project_id: int,
    frame_id: int,
    session: Annotated[Session, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_operator_or_admin_principal)],
    confirm: Annotated[bool, Query(description="must be true to delete")] = False,
) -> None:
    """Irreversibly delete a frame's row and file. Requires ``confirm=true``."""
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="permanent deletion requires confirm=true",
        )
    _get_frame_in_project(session, project_id, frame_id)
    frame_service.permanent_delete(
        session,
        frame_id,
        principal.user_id,
        confirm=confirm,
        settings=get_context().settings,
    )


@admin_router.patch("/{project_id}/frames/{frame_id}")
def edit_frame(
    project_id: int,
    frame_id: int,
    payload: TimestampUpdate,
    session: Annotated[Session, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_operator_or_admin_principal)],
) -> FrameOut:
    """Correct a frame's capture timestamp; any other field yields a 422."""
    _get_frame_in_project(session, project_id, frame_id)
    frame = frame_service.edit_capture_timestamp(
        session, frame_id, payload.capture_timestamp, principal.user_id
    )
    return _frame_out_in_project(session, project_id, frame)


@admin_router.post("/{project_id}/frames/upload")
async def upload_frame(
    project_id: int,
    request: Request,
    principal: Annotated[Principal, Depends(require_operator_or_admin_principal)],
    capture_timestamp: Annotated[
        datetime, Query(description="ISO-8601 time the image was taken")
    ],
    image_format: Annotated[
        str | None,
        Query(alias="format", description="declared image format: jpeg or png"),
    ] = None,
) -> FrameOut:
    """Import a raw image body as an uploaded frame for the project.

    The image is sent as the raw request body (no multipart). Its bytes must be a
    valid JPEG or PNG, verified by magic bytes; an optional ``format`` query must
    agree with the bytes. The frame is written through the shared atomic writer
    with ``origin="uploaded"`` and the supplied capture time, then audited.
    """
    image_bytes = await request.body()
    if not _looks_like_image(image_bytes):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="request body is not a valid JPEG or PNG image",
        )
    context = get_context()
    try:
        frame = await _run_upload(
            context.session_factory,
            context.settings,
            project_id,
            image_bytes,
            image_format,
            capture_timestamp,
            principal.user_id,
        )
    except frame_service.InvalidImageError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except ValueError as exc:
        # Raised by the writer when the project does not exist.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    with context.session_factory() as session:
        return _frame_out_in_project(session, project_id, frame)


def _looks_like_image(data: bytes) -> bool:
    """Return whether ``data`` begins with a JPEG or PNG magic-byte signature."""
    return data.startswith(_JPEG_MAGIC) or data.startswith(_PNG_MAGIC)


async def _run_upload(
    session_factory: sessionmaker[Session],
    settings: Settings,
    project_id: int,
    image_bytes: bytes,
    image_format: str | None,
    capture_timestamp: datetime,
    actor_user_id: int,
) -> Frame:
    """Run the synchronous upload service off the event loop."""
    return await asyncio.to_thread(
        frame_service.upload_frame,
        session_factory,
        settings,
        project_id,
        image_bytes,
        image_format,
        capture_timestamp,
        actor_user_id,
    )


# Upper bound on the whole multipart request body for a batch import. The body is
# read fully into memory to parse it, so an absurd Content-Length is rejected up
# front; the per-file count cap is enforced separately by the storage importer.
_MAX_IMPORT_REQUEST_BYTES = 512 * 1024 * 1024


def _parse_multipart_files(
    body: bytes, content_type: str, field: str
) -> list[tuple[str, bytes]]:
    """Extract ``(filename, bytes)`` for every ``field`` part of a multipart body.

    A small multipart/form-data reader: the API does not depend on a full
    multipart parser (the optional dependency is intentionally absent), so this
    decodes the one upload body it needs. Only parts whose Content-Disposition
    name equals ``field`` and that carry a filename and non-empty bytes are
    returned; any other part is ignored. Returns an empty list when the boundary
    is missing or no matching part is present.
    """
    boundary = _multipart_boundary(content_type)
    if boundary is None:
        return []
    delimiter = b"--" + boundary
    files: list[tuple[str, bytes]] = []
    for segment in body.split(delimiter):
        if not segment or segment in (b"--\r\n", b"--", b"\r\n"):
            continue
        segment = segment[2:] if segment.startswith(b"\r\n") else segment
        header_blob, sep, content = segment.partition(b"\r\n\r\n")
        if not sep:
            continue
        disposition = _part_disposition(header_blob)
        if _disposition_param(disposition, "name") != field:
            continue
        filename = _disposition_param(disposition, "filename")
        if not filename:
            continue
        if content.endswith(b"\r\n"):
            content = content[:-2]
        if not content:
            continue
        files.append((filename, content))
    return files


def _multipart_boundary(content_type: str) -> bytes | None:
    """Return the boundary token from a multipart Content-Type, or ``None``."""
    if not content_type.startswith("multipart/form-data"):
        return None
    for part in content_type.split(";"):
        key, _, value = part.strip().partition("=")
        if key.strip().lower() == "boundary":
            return value.strip().strip('"').encode("latin-1")
    return None


def _part_disposition(header_blob: bytes) -> str:
    """Return a multipart part's ``Content-Disposition`` header value (or "")."""
    for line in header_blob.split(b"\r\n"):
        key, _, value = line.partition(b":")
        if key.decode("latin-1").strip().lower() == "content-disposition":
            return value.decode("latin-1").strip()
    return ""


def _disposition_param(disposition: str, param: str) -> str | None:
    """Return a ``Content-Disposition`` parameter value (e.g. ``name``), or None."""
    for piece in disposition.split(";"):
        key, _, value = piece.strip().partition("=")
        if key.strip().lower() == param:
            return value.strip().strip('"')
    return None


@admin_router.post("/{project_id}/frames/import", response_model=ImportResultOut)
async def import_frames(
    project_id: int,
    request: Request,
    principal: Annotated[Principal, Depends(require_operator_or_admin_principal)],
) -> ImportResultOut:
    """Import a batch of image files as uploaded frames for the project.

    The files are sent as a ``multipart/form-data`` body with one or more
    ``files`` parts (the field name is ``files``, repeated per file). Each file's
    bytes are validated and its Exif capture time read; a file with no readable
    time falls back to now (naive UTC) and is flagged inferred. The project is
    then re-sequenced chronologically. Skip-not-raise: a file that is not a
    readable supported image is reported in ``skipped`` with a reason and the rest
    still import.

    Operator-or-admin gated (bearer token). An absurd request body is rejected
    with a 413 before it is read; a batch exceeding the per-request file cap is a
    422 -- both distinct from the per-file skips, which are a normal 200 result.
    """
    context = get_context()
    with context.session_factory() as session:
        if session.get(Project, project_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"project {project_id} does not exist",
            )

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            too_large = int(content_length) > _MAX_IMPORT_REQUEST_BYTES
        except ValueError:
            too_large = False
        if too_large:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="import request body is too large",
            )

    body = await request.body()
    files = _parse_multipart_files(
        body, request.headers.get("content-type", ""), "files"
    )
    if not files:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="no files provided (send one or more 'files' parts)",
        )

    fallback = datetime.now(UTC).replace(tzinfo=None)
    try:
        result = await asyncio.to_thread(
            frame_service.import_frames,
            context.session_factory,
            context.settings,
            project_id,
            files,
            fallback,
            principal.user_id,
        )
    except frame_service.ImportBatchTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    return ImportResultOut(
        imported_count=result.imported_count,
        skipped_count=result.skipped_count,
        skipped=[
            ImportSkippedOut(filename=item.name, reason=item.reason)
            for item in result.skipped
        ],
    )
