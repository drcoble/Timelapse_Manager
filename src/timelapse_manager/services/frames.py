"""Frame write use-cases -- the single implementation of the domain rules.

Each public function performs one frame lifecycle write and returns a
:class:`~timelapse_manager.services._base.Result`. The single-frame mutations run
against a caller-supplied request session; upload and import run against a session
factory the storage writer owns. The web and API layers are thin adapters over
these functions: they parse their own transport input, call the service, and let
the request dependency commit.

Validation and the project-scoped lookup live here, once. A rule violation raises
a transport-neutral :class:`~timelapse_manager.services.errors.AppError`
(``NotFoundError`` for a missing or cross-project frame, ``ValidationError`` for
an unconfirmed permanent delete or an unreadable upload body) that the web/API
error handler maps to the same status the endpoints returned before services
existed.

Two properties distinguish this from the project service:

* **No post-commit effects.** A frame lifecycle change does not signal the capture
  supervisor or the render worker, so every :class:`Result` carries an empty
  effect tuple.
* **Storage owns the transactions.** The heavy lifting -- atomic file+row writes,
  audit events, chronological re-sequencing -- stays in ``storage.frames``; these
  functions are the thin domain-rule + orchestration layer that both adapters
  call. The storage transaction behaviour is preserved exactly: single-session
  mutations flush through the caller's request session (committed by the request
  dependency), while upload/import go through the storage writer's own session
  scope. Nothing here commits.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from ..db.models import Frame
from ..storage import frames as _storage
from ._base import Actor, Result
from .errors import NotFoundError, ValidationError

if TYPE_CHECKING:
    import datetime
    from collections.abc import Sequence

    from sqlalchemy.orm import sessionmaker

    from ..config import Settings
    from ..storage.frames import ImportResult

# Re-exported so an adapter that routes an import through this service can catch
# the batch-size guard without also importing the storage module. The status it
# maps to is deliberately left to each adapter (the API returns 422, the web an
# inline 200 fragment), so it is passed through here, never translated.
ImportBatchTooLargeError = _storage.ImportBatchTooLargeError
MAX_IMPORT_BATCH = _storage.MAX_IMPORT_BATCH

# Magic-byte prefixes for the only image formats accepted on upload; mirrors the
# storage dimension reader's detection so an invalid body is rejected with a clear
# message before any writer work.
_JPEG_MAGIC = b"\xff\xd8"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _get_frame_in_project(session: Session, project_id: int, frame_id: int) -> Frame:
    """Return a frame that belongs to ``project_id`` or raise :class:`NotFoundError`.

    A frame addressed through the wrong project's path is treated as not found, so
    one project's URL can never mutate another's frame (anti-IDOR). Mapped to 404.
    """
    frame = session.get(Frame, frame_id)
    if frame is None or frame.project_id != project_id:
        raise NotFoundError(f"frame {frame_id} not found in project {project_id}")
    return frame


def soft_delete_frame(
    session: Session, actor: Actor, project_id: int, frame_id: int
) -> Result[Frame]:
    """Soft-delete a frame (flip its lifecycle flag, keep its file on disk).

    The frame must belong to ``project_id`` (else ``404``). Idempotent; audited by
    the storage layer. No commit, no post-commit effect.
    """
    _get_frame_in_project(session, project_id, frame_id)
    frame = _storage.soft_delete(session, frame_id, actor.user_id)
    return Result(frame)


def restore_frame(
    session: Session, actor: Actor, project_id: int, frame_id: int
) -> Result[Frame]:
    """Restore a soft-deleted frame to the active set.

    The frame must belong to ``project_id`` (else ``404``). Audited by the storage
    layer. No commit, no post-commit effect.
    """
    _get_frame_in_project(session, project_id, frame_id)
    frame = _storage.restore(session, frame_id, actor.user_id)
    return Result(frame)


def exclude_frame(
    session: Session, actor: Actor, project_id: int, frame_id: int
) -> Result[Frame]:
    """Exclude a frame from rendered output, leaving it visible in the browser.

    The frame must belong to ``project_id`` (else ``404``). Idempotent; audited by
    the storage layer. No commit, no post-commit effect.
    """
    _get_frame_in_project(session, project_id, frame_id)
    frame = _storage.exclude(session, frame_id, actor.user_id)
    return Result(frame)


def include_frame(
    session: Session, actor: Actor, project_id: int, frame_id: int
) -> Result[Frame]:
    """Return an excluded frame to rendered output.

    The frame must belong to ``project_id`` (else ``404``). Idempotent; audited by
    the storage layer. No commit, no post-commit effect.
    """
    _get_frame_in_project(session, project_id, frame_id)
    frame = _storage.include(session, frame_id, actor.user_id)
    return Result(frame)


def edit_frame_timestamp(
    session: Session,
    actor: Actor,
    project_id: int,
    frame_id: int,
    new_timestamp: datetime.datetime,
) -> Result[Frame]:
    """Correct a frame's capture timestamp, leaving all else untouched.

    The frame must belong to ``project_id`` (else ``404``). The caller supplies an
    already-parsed datetime (the API via Pydantic, the web via its form parser);
    the storage layer normalises it to naive UTC and audits the change. No commit,
    no post-commit effect.
    """
    _get_frame_in_project(session, project_id, frame_id)
    frame = _storage.edit_capture_timestamp(
        session, frame_id, new_timestamp, actor.user_id
    )
    return Result(frame)


def permanent_delete_frame(
    session: Session,
    actor: Actor,
    project_id: int,
    frame_id: int,
    *,
    confirm: bool,
    settings: Settings,
) -> Result[None]:
    """Irreversibly delete a frame's row and unlink its file.

    Requires explicit confirmation: without ``confirm`` nothing is touched and a
    ``422`` is raised, before the frame is even looked up (so a missing-confirm
    request is rejected the same way whether or not the frame exists). The frame
    must then belong to ``project_id`` (else ``404``). The storage layer deletes
    the row, writes the audit event, and unlinks the file. No commit, no
    post-commit effect.
    """
    if not confirm:
        raise ValidationError("permanent deletion requires confirm=true")
    _get_frame_in_project(session, project_id, frame_id)
    _storage.permanent_delete(
        session, frame_id, actor.user_id, confirm=confirm, settings=settings
    )
    return Result(None)


def upload_frame(
    session_factory: sessionmaker[Session],
    settings: Settings,
    actor: Actor,
    project_id: int,
    image_bytes: bytes,
    image_format: str | None,
    capture_timestamp: datetime.datetime,
) -> Result[Frame]:
    """Import a raw image body as an uploaded frame for the project.

    The bytes must be a valid JPEG or PNG (verified by magic bytes here, then by
    the storage layer's header read); an optional declared ``image_format`` must
    agree with the bytes. A body that is not a recognised image is a ``422``; a
    reference to a project that does not exist is a ``404`` (raised by the storage
    writer). The frame is written through the shared atomic writer with
    ``origin="uploaded"`` and audited.

    Synchronous: the storage writer owns its own session scope, so this runs off
    the event loop via a thread executor in the async adapter. No post-commit
    effect.
    """
    if not (image_bytes.startswith(_JPEG_MAGIC) or image_bytes.startswith(_PNG_MAGIC)):
        raise ValidationError("request body is not a valid JPEG or PNG image")
    try:
        frame = _storage.upload_frame(
            session_factory,
            settings,
            project_id,
            image_bytes,
            image_format,
            capture_timestamp,
            actor.user_id,
        )
    except _storage.InvalidImageError as exc:
        raise ValidationError(str(exc)) from exc
    except ValueError as exc:
        # Raised by the writer when the project does not exist.
        raise NotFoundError(str(exc)) from exc
    return Result(frame)


def import_frames(
    session_factory: sessionmaker[Session],
    settings: Settings,
    actor: Actor,
    project_id: int,
    files: Sequence[tuple[str, _storage.ImportSource]],
    fallback_timestamp: datetime.datetime,
    exif_timezone: str | None = None,
) -> Result[ImportResult]:
    """Import a batch of image files as uploaded frames, chronologically ordered.

    A thin orchestration seam over the storage importer: each file's bytes are
    validated and its Exif capture time read (falling back to ``fallback_timestamp``
    and flagged inferred), the frames are written through the shared atomic writer,
    and the project is re-sequenced once. Skip-not-raise per file -- a bad file is
    reported in the result, not fatal.

    A naive Exif time is read in ``exif_timezone`` (an IANA name) when given, else
    in the project's schedule timezone; an image's own UTC offset always wins. An
    unknown zone name is a :class:`ValidationError`, raised before any file is
    written.

    :class:`ImportBatchTooLargeError` (a batch exceeding the per-request file cap)
    is left to propagate: the adapters map it to their own surface's status (the
    API a ``422``, the web an inline ``200`` fragment), so it is never translated
    here. Synchronous -- the storage importer owns its session scope and runs off
    the event loop via a thread executor. No post-commit effect.
    """
    try:
        result = _storage.import_frames(
            session_factory,
            settings,
            project_id,
            files,
            fallback_timestamp,
            actor.user_id,
            exif_timezone=exif_timezone,
        )
    except _storage.InvalidExifTimezoneError as exc:
        raise ValidationError(str(exc)) from exc
    return Result(result)
