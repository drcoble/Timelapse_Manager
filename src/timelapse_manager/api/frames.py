"""Frame listing, lifecycle, and capture-status endpoints.

Lists a project's frames in capture order (paginated, optionally including
soft-deleted ones), reports a project's live capture state from the supervisor,
and exposes the operator-or-admin-gated lifecycle operations: soft-delete,
restore, permanent delete, upload, and capture-timestamp correction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from ..capture import CaptureSupervisor
from ..config import Settings
from ..db.models import Frame, Project
from ..db.session import DbSessionDep
from ..executors import run_in
from ..logging import redact_text
from ..runtime import AppContext, get_context
from ..security import Principal, require_operator_or_admin_principal
from ..security.sessions import reauthorize_after_body
from ..services import frames as frame_service
from ..services._base import Actor, Result
from ..services.errors import NotFoundError, ValidationError
from ..storage import frames as frame_storage
from ..storage.frames import ImportResult
from ..web.multipart import (
    BodyTooLargeError,
    InsufficientSpoolSpaceError,
    MultipartError,
    TooManyFilesError,
    UploadSpool,
    stream_body_to_file,
    stream_multipart,
    upload_spool,
)

router = APIRouter(prefix="/frames", tags=["frames"])

# Lifecycle mutations live under a project-scoped path so the owning project is
# explicit in the URL and verifiable against the frame. Kept on its own router
# (mounted alongside the read router) so the path shapes differ cleanly.
admin_router = APIRouter(prefix="/projects", tags=["frames"])

_MAX_LIMIT = 500


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
        dimension_mismatch=frame_storage.dimension_mismatch(frame, predominant),
    )


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
    session: DbSessionDep,
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
    frames = frame_storage.list_frames(
        session,
        project_id,
        limit=limit,
        offset=offset,
        include_deleted=include_deleted,
    )
    predominant = frame_storage.predominant_dimensions(session, project_id)
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
        last_error=(
            redact_text(state.last_error) if state.last_error is not None else None
        ),
        frames_captured=state.frames_captured,
    )


def _frame_out_in_project(session: Session, project_id: int, frame: Frame) -> FrameOut:
    """Serialize a frame after a mutation, with a freshly computed mismatch flag."""
    session.flush()
    predominant = frame_storage.predominant_dimensions(session, project_id)
    return _to_out(frame, predominant)


@admin_router.post("/{project_id}/frames/{frame_id}/soft-delete")
def soft_delete_frame(
    project_id: int,
    frame_id: int,
    session: DbSessionDep,
    principal: Annotated[Principal, Depends(require_operator_or_admin_principal)],
) -> FrameOut:
    """Soft-delete a frame: flip its lifecycle flag, keep its file on disk."""
    result = frame_service.soft_delete_frame(
        session, Actor.of(principal), project_id, frame_id
    )
    return _frame_out_in_project(session, project_id, result.value)


@admin_router.post("/{project_id}/frames/{frame_id}/restore")
def restore_frame(
    project_id: int,
    frame_id: int,
    session: DbSessionDep,
    principal: Annotated[Principal, Depends(require_operator_or_admin_principal)],
) -> FrameOut:
    """Restore a soft-deleted frame to the active set."""
    result = frame_service.restore_frame(
        session, Actor.of(principal), project_id, frame_id
    )
    return _frame_out_in_project(session, project_id, result.value)


@admin_router.post(
    "/{project_id}/frames/{frame_id}/permanent-delete",
    status_code=status.HTTP_204_NO_CONTENT,
)
def permanent_delete_frame(
    project_id: int,
    frame_id: int,
    session: DbSessionDep,
    principal: Annotated[Principal, Depends(require_operator_or_admin_principal)],
    confirm: Annotated[bool, Query(description="must be true to delete")] = False,
) -> None:
    """Irreversibly delete a frame's row and file. Requires ``confirm=true``."""
    frame_service.permanent_delete_frame(
        session,
        Actor.of(principal),
        project_id,
        frame_id,
        confirm=confirm,
        settings=get_context().settings,
    )


@admin_router.patch("/{project_id}/frames/{frame_id}")
def edit_frame(
    project_id: int,
    frame_id: int,
    payload: TimestampUpdate,
    session: DbSessionDep,
    principal: Annotated[Principal, Depends(require_operator_or_admin_principal)],
) -> FrameOut:
    """Correct a frame's capture timestamp; any other field yields a 422."""
    result = frame_service.edit_frame_timestamp(
        session, Actor.of(principal), project_id, frame_id, payload.capture_timestamp
    )
    return _frame_out_in_project(session, project_id, result.value)


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

    The body is streamed to a spool file and refused with ``413`` the moment it
    passes ``capture.max_image_bytes`` (or ``507`` when the disk is short), so it
    is never buffered on the event loop. The write runs on the jobs executor.
    """
    context = get_context()
    settings = context.settings
    try:
        async with upload_spool(
            settings.paths.data_dir,
            expected_bytes=_declared_length(request),
            low_watermark_bytes=settings.storage.low_watermark_bytes,
        ) as spool:
            image_path = spool.path / "image"
            await stream_body_to_file(
                request,
                image_path,
                max_bytes=settings.capture.max_image_bytes,
                low_watermark_bytes=settings.storage.low_watermark_bytes,
            )
            # A revoke, disable or demotion can land while the body streams in.
            actor = await run_in_threadpool(_reauthorize, context, request, principal)
            # The worker owns the spool from here and removes it. The service
            # validates the bytes (an invalid body is a 422) and maps a missing
            # project to a 404; both surface as an AppError the handler renders.
            result = await run_in(
                context.jobs_executor,
                _upload_from_spool,
                context.session_factory,
                settings,
                actor,
                project_id,
                spool,
                image_format,
                capture_timestamp,
            )
    except BodyTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Request body exceeds the configured size limit.",
        ) from exc
    except InsufficientSpoolSpaceError as exc:
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE, detail=str(exc)
        ) from exc
    return await run_in_threadpool(
        _uploaded_frame_out, context.session_factory, project_id, result.value
    )


def _declared_length(request: Request) -> int:
    """The request's ``Content-Length`` as an int, or 0 when absent or invalid."""
    raw = request.headers.get("content-length", "").strip()
    return int(raw) if raw.isascii() and raw.isdigit() else 0


def _reauthorize(context: AppContext, request: Request, principal: Principal) -> Actor:
    """Re-check a cookie caller's access after the body, return the actor.

    Synchronous, for a threadpool hop. A bearer-token caller has no session to
    re-check and keeps the principal the dependency resolved; a cookie caller's
    live user row decides (:func:`~..security.sessions.reauthorize_after_body`).
    """
    with context.session_factory() as session:
        user = reauthorize_after_body(
            request,
            session,
            settings=context.settings.session,
            allowed_roles=_WRITE_ROLES,
        )
    return Actor.of(principal if user is None else user)


def _upload_from_spool(
    session_factory: sessionmaker[Session],
    settings: Settings,
    actor: Actor,
    project_id: int,
    spool: UploadSpool,
    image_format: str | None,
    capture_timestamp: datetime,
) -> Result[Frame]:
    """Read the spooled image and upload it; always removes the spool.

    Synchronous, for the jobs executor. Peak memory is the one image
    (``capture.max_image_bytes`` at most).
    """
    spool.claim_or_raise()
    try:
        image_bytes = (spool.path / "image").read_bytes()
        return frame_service.upload_frame(
            session_factory,
            settings,
            actor,
            project_id,
            image_bytes,
            image_format,
            capture_timestamp,
        )
    finally:
        spool.finish()


def _uploaded_frame_out(
    session_factory: sessionmaker[Session], project_id: int, frame: Frame
) -> FrameOut:
    """Serialize a just-uploaded frame in a short session of its own.

    Synchronous: the upload route awaits it through the threadpool so the
    predominant-dimensions query never runs on the event loop (#49).
    """
    with session_factory() as session:
        return _frame_out_in_project(session, project_id, frame)


def _require_project(session_factory: sessionmaker[Session], project_id: int) -> None:
    """Raise :class:`NotFoundError` when ``project_id`` names no project.

    Synchronous: the import route awaits it through the threadpool so the
    existence check never queries on the event loop (#49).
    """
    with session_factory() as session:
        if session.get(Project, project_id) is None:
            raise NotFoundError(f"project {project_id} does not exist")


# Upper bound on the whole multipart request body for a batch import: a cheap
# up-front reject on ``Content-Length``. The streaming reader enforces the real
# caps (this, the per-file size and the file count) on the bytes received.
_MAX_IMPORT_REQUEST_BYTES = 512 * 1024 * 1024

# Optional text field naming the IANA zone the images' naive Exif times were
# taken in (#67 R10). Unknown names are a 422 from the service layer.
_EXIF_TIMEZONE_FIELD = "exif_timezone"

# Roles the write endpoints admit (the operator-or-admin gate).
_WRITE_ROLES = ("operator", "admin")


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
    time falls back to now (naive UTC) and is flagged inferred. A time without a
    UTC offset of its own is read in the optional ``exif_timezone`` text field (an
    IANA name; unknown names are a 422), else in the project's schedule timezone.
    The project is then re-sequenced chronologically. Skip-not-raise: a file that
    is not a readable supported image is reported in ``skipped`` with a reason and
    the rest still import.

    Operator-or-admin gated (bearer token). An absurd request body is rejected
    with a 413 before it is read; a batch exceeding the per-request file cap is a
    422 -- both distinct from the per-file skips, which are a normal 200 result.
    """
    context = get_context()
    settings = context.settings
    await run_in_threadpool(_require_project, context.session_factory, project_id)

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

    try:
        async with upload_spool(
            settings.paths.data_dir,
            expected_bytes=_declared_length(request),
            low_watermark_bytes=settings.storage.low_watermark_bytes,
        ) as spool:
            form = await stream_multipart(
                request,
                spool_dir=spool.path,
                file_field="files",
                text_fields=frozenset({"csrf_token", _EXIF_TIMEZONE_FIELD}),
                max_file_bytes=settings.capture.max_image_bytes,
                max_total_bytes=min(
                    _MAX_IMPORT_REQUEST_BYTES, settings.server.max_request_body_bytes
                ),
                max_files=frame_storage.MAX_IMPORT_BATCH,
                low_watermark_bytes=settings.storage.low_watermark_bytes,
            )
            files = [(part.filename, part.path) for part in form.files if part.path]
            if not files:
                raise ValidationError(
                    "no files provided (send one or more 'files' parts)"
                )
            # A revoke, disable or demotion can land while the body streams in.
            actor = await run_in_threadpool(_reauthorize, context, request, principal)
            fallback = datetime.now(UTC).replace(tzinfo=None)
            result = await run_in(
                context.jobs_executor,
                _import_from_spool,
                context.session_factory,
                settings,
                actor,
                project_id,
                spool,
                files,
                fallback,
                form.fields.get(_EXIF_TIMEZONE_FIELD),
            )
    except BodyTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Request body exceeds the configured size limit.",
        ) from exc
    except (TooManyFilesError, frame_service.ImportBatchTooLargeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"import batch exceeds the maximum of "
                f"{frame_storage.MAX_IMPORT_BATCH} files"
            ),
        ) from exc
    except MultipartError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except InsufficientSpoolSpaceError as exc:
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE, detail=str(exc)
        ) from exc

    import_result = result.value
    return ImportResultOut(
        imported_count=import_result.imported_count,
        skipped_count=import_result.skipped_count,
        skipped=[
            ImportSkippedOut(filename=item.name, reason=item.reason)
            for item in import_result.skipped
        ],
    )


def _import_from_spool(
    session_factory: sessionmaker[Session],
    settings: Settings,
    actor: Actor,
    project_id: int,
    spool: UploadSpool,
    files: list[tuple[str, Path]],
    fallback: datetime,
    exif_timezone: str | None = None,
) -> Result[ImportResult]:
    """Import the spooled files; always removes the spool directory.

    Synchronous, for the jobs executor. The worker owns the spool from the moment
    it starts (``claim``), so a client that disconnects mid-import neither loses
    the import nor leaves files behind. A job that never starts (the request was
    cancelled while it was queued, or the executor shut down) is cleaned up by
    the request instead.
    """
    spool.claim_or_raise()
    try:
        return frame_service.import_frames(
            session_factory,
            settings,
            actor,
            project_id,
            files,
            fallback,
            exif_timezone,
        )
    finally:
        spool.finish()
