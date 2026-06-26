"""Frame lifecycle operations: soft-delete, restore, permanent delete, upload,
timestamp correction, and ordered listing.

This is the single place a frame's lifecycle changes, and every change here is
audited. The design follows two rules consistently:

* **Keep-all.** Nothing in this module removes a frame's file or row except the
  explicit :func:`permanent_delete`. Soft-deleting only flips the lifecycle flag;
  the bytes stay on disk so the action is reversible by :func:`restore`.
* **Resolve before touching disk.** A frame row stores a path that may be
  relative (re-anchored from the project) or absolute (legacy or custom storage),
  so any filesystem access goes through the shared path resolver rather than
  interpreting the stored value directly.

Every mutation writes an :class:`~..db.models.Event` attributed to the acting
user, so the audit trail records who changed what. All work is synchronous,
matching the application's synchronous database engine; async callers invoke
these via a thread executor.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from ..cameras import CapturedFrame
from ..cameras._imageinfo import (
    detect_format,
    read_capture_timestamp,
    read_dimensions,
)
from ..db.models import Event, Frame, Project
from ..db.session import session_scope
from ..security.principal import ensure_sentinel_admin
from ..storage import frames_root, resolve_absolute

if TYPE_CHECKING:
    from ..capture import FrameWriter, WrittenFrame
    from ..config import Settings

logger = logging.getLogger(__name__)

# Image formats accepted on upload, mapped to the canonical name the dimension
# reader and the writer use. The request's declared format, when present, must
# agree with what the bytes actually are.
_ACCEPTED_FORMATS = {"jpeg": "jpeg", "jpg": "jpeg", "png": "png"}


class FrameNotFoundError(Exception):
    """Raised when a frame id does not exist (or not within a given project)."""


class ConfirmationRequiredError(Exception):
    """Raised when a destructive operation is attempted without confirmation."""


class InvalidImageError(Exception):
    """Raised when uploaded bytes are not a supported, well-formed image."""


def _to_naive_utc(value: datetime) -> datetime:
    """Return ``value`` as a naive UTC datetime for the naive timestamp column.

    Aware values are normalised to UTC and made naive; naive values are assumed
    to already be UTC and returned unchanged, mirroring the writer's convention.
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _get_frame(session: Session, frame_id: int) -> Frame:
    """Return a frame row or raise :class:`FrameNotFoundError`."""
    frame = session.get(Frame, frame_id)
    if frame is None:
        raise FrameNotFoundError(f"frame {frame_id} does not exist")
    return frame


def _record_event(
    session: Session,
    *,
    project_id: int,
    level: str,
    message: str,
    metadata: dict[str, Any],
    actor_user_id: int,
) -> None:
    """Append a project-scoped audit event attributed to ``actor_user_id``.

    The actor is a foreign key to a user row. Until real accounts are seeded the
    single chokepoint here ensures the sentinel administrator exists before the
    event is inserted, so the audit insert never fails its foreign key.
    """
    ensure_sentinel_admin(session)
    session.add(
        Event(
            scope="project",
            scope_id=project_id,
            level=level,
            message=message,
            event_metadata=metadata,
            actor_user_id=actor_user_id,
        )
    )


def soft_delete(session: Session, frame_id: int, actor_user_id: int) -> Frame:
    """Mark a frame soft-deleted, leaving its file on disk.

    Idempotent: soft-deleting an already soft-deleted frame is a no-op flip.
    Writes an audit event. Raises :class:`FrameNotFoundError` for an unknown id.
    """
    frame = _get_frame(session, frame_id)
    frame.lifecycle_state = "soft_deleted"
    _record_event(
        session,
        project_id=frame.project_id,
        level="info",
        message=f"frame {frame_id} soft-deleted",
        metadata={"frame_id": frame_id, "action": "soft_delete"},
        actor_user_id=actor_user_id,
    )
    return frame


def restore(session: Session, frame_id: int, actor_user_id: int) -> Frame:
    """Return a soft-deleted frame to the active set.

    Writes an audit event. Raises :class:`FrameNotFoundError` for an unknown id.
    """
    frame = _get_frame(session, frame_id)
    frame.lifecycle_state = "active"
    _record_event(
        session,
        project_id=frame.project_id,
        level="info",
        message=f"frame {frame_id} restored",
        metadata={"frame_id": frame_id, "action": "restore"},
        actor_user_id=actor_user_id,
    )
    return frame


def exclude(session: Session, frame_id: int, actor_user_id: int) -> Frame:
    """Exclude a frame from rendered output, leaving it visible in the browser.

    Stamps ``excluded_at`` with the current UTC time. This is orthogonal to
    soft-delete: an excluded frame is still listed and shown; only the encoder
    skips it. Idempotent -- re-excluding an already-excluded frame simply
    re-stamps the time. Writes an audit event. Raises
    :class:`FrameNotFoundError` for an unknown id.
    """
    frame = _get_frame(session, frame_id)
    frame.excluded_at = _to_naive_utc(datetime.now(UTC))
    _record_event(
        session,
        project_id=frame.project_id,
        level="info",
        message=f"frame {frame_id} excluded from render",
        metadata={"frame_id": frame_id, "action": "exclude"},
        actor_user_id=actor_user_id,
    )
    return frame


def include(session: Session, frame_id: int, actor_user_id: int) -> Frame:
    """Return an excluded frame to rendered output.

    Clears ``excluded_at`` back to ``NULL``. Idempotent -- including an
    already-included frame is a no-op clear. Writes an audit event. Raises
    :class:`FrameNotFoundError` for an unknown id.
    """
    frame = _get_frame(session, frame_id)
    frame.excluded_at = None
    _record_event(
        session,
        project_id=frame.project_id,
        level="info",
        message=f"frame {frame_id} included in render",
        metadata={"frame_id": frame_id, "action": "include"},
        actor_user_id=actor_user_id,
    )
    return frame


@dataclass(frozen=True)
class BulkResult:
    """Outcome of a bulk lifecycle operation over an id-set.

    ``succeeded`` holds the ids that were found and mutated (one audit event
    each); ``failed`` holds the ids that could not be applied -- today that is
    only ids with no matching row, since the per-frame work is a flag flip that
    cannot otherwise fail. The two lists partition the requested id-set, and the
    operation is **skip-not-raise**: a bad id is recorded in ``failed`` and the
    batch continues rather than aborting, so one vanished frame never voids the
    rest of a selection.
    """

    succeeded: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)


def _apply_many(
    session: Session,
    frame_ids: list[int],
    actor_user_id: int,
    *,
    mutate: Any,
    message: Any,
    action: str,
) -> BulkResult:
    """Apply a single-frame lifecycle mutation across an id-set, skip-not-raise.

    For each id: look it up with ``session.get`` and, if present, run ``mutate``
    on the row and write one per-frame audit event (the same event-type strings
    the single-frame operations use). A missing id is appended to ``failed`` and
    the loop continues -- the batch is never aborted by one bad id. Returns the
    succeeded/failed partition of the requested ids.

    This is the shared core of the four public ``*_many`` helpers; each passes
    the field mutation, the audit message builder, and the ``metadata.action``
    string so the audit trail matches the single-frame operations exactly.
    """
    result = BulkResult()
    for frame_id in frame_ids:
        frame = session.get(Frame, frame_id)
        if frame is None:
            result.failed.append(frame_id)
            continue
        mutate(frame)
        _record_event(
            session,
            project_id=frame.project_id,
            level="info",
            message=message(frame_id),
            metadata={"frame_id": frame_id, "action": action},
            actor_user_id=actor_user_id,
        )
        result.succeeded.append(frame_id)
    return result


def soft_delete_many(
    session: Session, frame_ids: list[int], actor_user_id: int
) -> BulkResult:
    """Soft-delete every frame in ``frame_ids`` (skip-not-raise), audited per id.

    Mirrors :func:`soft_delete` for each found frame -- the file stays on disk
    and the action is reversible via :func:`restore_many`. Unknown ids are
    reported in :attr:`BulkResult.failed`; the rest succeed.
    """
    return _apply_many(
        session,
        frame_ids,
        actor_user_id,
        mutate=lambda f: setattr(f, "lifecycle_state", "soft_deleted"),
        message=lambda fid: f"frame {fid} soft-deleted",
        action="soft_delete",
    )


def restore_many(
    session: Session, frame_ids: list[int], actor_user_id: int
) -> BulkResult:
    """Restore every frame in ``frame_ids`` to the active set (skip-not-raise)."""
    return _apply_many(
        session,
        frame_ids,
        actor_user_id,
        mutate=lambda f: setattr(f, "lifecycle_state", "active"),
        message=lambda fid: f"frame {fid} restored",
        action="restore",
    )


def exclude_many(
    session: Session, frame_ids: list[int], actor_user_id: int
) -> BulkResult:
    """Exclude every frame in ``frame_ids`` from rendered output (skip-not-raise).

    Each found frame is stamped ``excluded_at = now()`` -- it stays visible in
    the browser but the encoder skips it -- exactly as :func:`exclude` does.
    """
    now = _to_naive_utc(datetime.now(UTC))
    return _apply_many(
        session,
        frame_ids,
        actor_user_id,
        mutate=lambda f: setattr(f, "excluded_at", now),
        message=lambda fid: f"frame {fid} excluded from render",
        action="exclude",
    )


def include_many(
    session: Session, frame_ids: list[int], actor_user_id: int
) -> BulkResult:
    """Return every frame in ``frame_ids`` to rendered output (skip-not-raise)."""
    return _apply_many(
        session,
        frame_ids,
        actor_user_id,
        mutate=lambda f: setattr(f, "excluded_at", None),
        message=lambda fid: f"frame {fid} included in render",
        action="include",
    )


@dataclass(frozen=True)
class OffsetResult:
    """Outcome of a bulk timestamp-offset over an id-set.

    The requested ids partition into three lists, never overlapping:

    * ``shifted`` -- frames whose ``capture_timestamp`` was moved by the signed
      delta (one audit event each);
    * ``skipped_null`` -- frames with a null ``capture_timestamp``: they have no
      place on the time axis, so there is nothing to shift. They are reported
      separately (not a failure -- the frame exists and is untouched);
    * ``failed`` -- ids with no matching row (e.g. a frame that vanished between
      selection and apply).

    The operation is **skip-not-raise**: a null-timestamp frame or a missing id is
    recorded and the batch continues, so neither voids the rest of the selection.
    Only ``shifted`` is reversible: an inverse offset replays ``-delta`` over
    exactly those ids.
    """

    shifted: list[int] = field(default_factory=list)
    skipped_null: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)


def offset_timestamps_many(
    session: Session,
    frame_ids: list[int],
    seconds: int,
    actor_user_id: int,
) -> OffsetResult:
    """Shift the capture timestamp of every frame in ``frame_ids`` by ``seconds``.

    ``seconds`` is a signed offset (negative shifts earlier). For each id:

    * a missing row is recorded in :attr:`OffsetResult.failed` and skipped;
    * a frame whose ``capture_timestamp`` is null is recorded in
      :attr:`OffsetResult.skipped_null` and left untouched -- a frame off the time
      axis has no timestamp to move;
    * otherwise the timestamp is moved by ``timedelta(seconds=seconds)`` and one
      audit event is written recording the previous and new values plus the delta,
      mirroring :func:`edit_capture_timestamp`.

    The column stores naive UTC; adding a ``timedelta`` to a naive datetime keeps
    it naive, so no timezone normalisation is needed. The batch is
    **skip-not-raise** -- neither a missing id nor a null-timestamp frame aborts
    it. An inverse offset (``-seconds``) over the returned ``shifted`` ids
    round-trips them to their original times; this is how Undo is expressed -- it
    is not a "restore".
    """
    delta = timedelta(seconds=seconds)
    result = OffsetResult()
    for frame_id in frame_ids:
        frame = session.get(Frame, frame_id)
        if frame is None:
            result.failed.append(frame_id)
            continue
        if frame.capture_timestamp is None:
            result.skipped_null.append(frame_id)
            continue
        previous = frame.capture_timestamp.isoformat()
        frame.capture_timestamp = frame.capture_timestamp + delta
        _record_event(
            session,
            project_id=frame.project_id,
            level="info",
            message=f"frame {frame_id} capture timestamp edited",
            metadata={
                "frame_id": frame_id,
                "action": "edit_capture_timestamp",
                "previous": previous,
                "new": frame.capture_timestamp.isoformat(),
                "delta_seconds": seconds,
            },
            actor_user_id=actor_user_id,
        )
        result.shifted.append(frame_id)
    return result


def permanent_delete(
    session: Session,
    frame_id: int,
    actor_user_id: int,
    *,
    confirm: bool,
    settings: Settings,
) -> None:
    """Irreversibly delete a frame's row and unlink its file.

    This is the only operation that removes anything. It requires explicit
    confirmation; without it nothing is touched and
    :class:`ConfirmationRequiredError` is raised.

    The row is deleted and the audit event written first; the file is unlinked
    last (tolerating an already-missing file). A failure mid-way therefore leaves
    at worst a harmless orphan file, never a row pointing at a deleted file --
    the same ordering philosophy the writer uses in reverse.

    Raises :class:`FrameNotFoundError` for an unknown id.
    """
    if not confirm:
        raise ConfirmationRequiredError(
            "permanent deletion requires explicit confirmation"
        )
    frame = _get_frame(session, frame_id)
    project_id = frame.project_id
    stored_path = frame.file_path
    absolute_path = (
        resolve_absolute(settings, project_id, stored_path)
        if stored_path is not None
        else None
    )

    project = session.get(Project, project_id)
    if project is not None and project.frame_count > 0:
        project.frame_count -= 1
    session.delete(frame)
    _record_event(
        session,
        project_id=project_id,
        level="warning",
        message=f"frame {frame_id} permanently deleted",
        metadata={
            "frame_id": frame_id,
            "action": "permanent_delete",
            "file_path": str(absolute_path) if absolute_path is not None else None,
        },
        actor_user_id=actor_user_id,
    )
    session.flush()

    if absolute_path is not None:
        with contextlib.suppress(FileNotFoundError):
            absolute_path.unlink()
    logger.info("permanently deleted frame project=%s id=%s", project_id, frame_id)


def edit_capture_timestamp(
    session: Session,
    frame_id: int,
    new_timestamp: datetime,
    actor_user_id: int,
) -> Frame:
    """Correct only a frame's capture timestamp, leaving all else untouched.

    Writes an audit event recording the previous and new values. Raises
    :class:`FrameNotFoundError` for an unknown id.
    """
    frame = _get_frame(session, frame_id)
    previous = (
        frame.capture_timestamp.isoformat()
        if frame.capture_timestamp is not None
        else None
    )
    frame.capture_timestamp = _to_naive_utc(new_timestamp)
    _record_event(
        session,
        project_id=frame.project_id,
        level="info",
        message=f"frame {frame_id} capture timestamp edited",
        metadata={
            "frame_id": frame_id,
            "action": "edit_capture_timestamp",
            "previous": previous,
            "new": frame.capture_timestamp.isoformat(),
        },
        actor_user_id=actor_user_id,
    )
    return frame


def upload_frame(
    session_factory: sessionmaker[Session],
    settings: Settings,
    project_id: int,
    image_bytes: bytes,
    fmt: str | None,
    capture_timestamp: datetime,
    actor_user_id: int,
) -> Frame:
    """Import an externally supplied image as an uploaded frame.

    The bytes are validated to be a real JPEG or PNG by their magic bytes; the
    detected format is authoritative, and a declared ``fmt`` that disagrees is
    rejected. Dimensions are read from the image header. The frame is persisted
    through the shared atomic writer with ``origin="uploaded"`` and the caller's
    stated capture time, so it shares the same never-overwriting sequence and
    relocatable-path storage as captured frames.

    The write and its audit event are committed in separate transactions (the
    writer owns its own scope); a crash between them leaves a recorded frame
    without an audit row, consistent with the keep-all guarantee.

    :raises ValueError: if the project does not exist (from the writer).
    :raises InvalidImageError: if the bytes are not a supported, readable image,
        or a declared format contradicts the bytes.
    """
    detected = detect_format(image_bytes)
    if detected is None:
        raise InvalidImageError("uploaded bytes are not a recognised JPEG or PNG")
    if fmt is not None:
        declared = _ACCEPTED_FORMATS.get(fmt.lower())
        if declared is None:
            raise InvalidImageError(f"unsupported declared format: {fmt!r}")
        if declared != detected:
            raise InvalidImageError(
                f"declared format {declared!r} does not match image bytes "
                f"({detected!r})"
            )
    dimensions = read_dimensions(image_bytes)
    if dimensions is None:
        raise InvalidImageError("could not read image dimensions")
    width, height = dimensions

    captured = CapturedFrame(
        image_bytes=image_bytes,
        width=width,
        height=height,
        format=detected,
        captured_at=capture_timestamp,
    )
    # Imported here (not at module load) to avoid a capture<->storage import cycle.
    from ..capture import FrameWriter

    writer = FrameWriter(session_factory, frames_root(settings))
    written: WrittenFrame = writer.write(
        project_id,
        captured,
        origin="uploaded",
        capture_timestamp=capture_timestamp,
    )

    with session_scope(session_factory) as session:
        _record_event(
            session,
            project_id=project_id,
            level="info",
            message=f"frame {written.frame_id} uploaded",
            metadata={
                "frame_id": written.frame_id,
                "action": "upload",
                "sequence_index": written.sequence_index,
                "width": width,
                "height": height,
            },
            actor_user_id=actor_user_id,
        )
        frame = session.get(Frame, written.frame_id)
        assert frame is not None  # just written in the writer's transaction
        session.expunge(frame)
    logger.info(
        "uploaded frame project=%s id=%s seq=%s",
        project_id,
        written.frame_id,
        written.sequence_index,
    )
    return frame


# The largest number of files accepted in a single import batch. Files are read
# fully into memory for header/Exif parsing, so the cap bounds peak memory and
# request time; a larger import is issued as several batches, each ending with
# its own single re-sequence.
MAX_IMPORT_BATCH = 200


class ImportBatchTooLargeError(Exception):
    """Raised when an import batch exceeds :data:`MAX_IMPORT_BATCH` files."""


def resequence_project(session: Session, project_id: int) -> int:
    """Densely renumber a project's ``sequence_index`` into chronological order.

    Realigns ``sequence_index`` so its order equals
    ``(capture_timestamp, id)`` order across **all** of the project's rows
    (active and soft-deleted alike), which is what an interleaved import requires:
    the browser's keyset/window cursor navigates in sequence space and assumes
    sequence order tracks time order. Render output already sorts by
    ``capture_timestamp`` and is unaffected, so a partial/failed run only
    temporarily de-orders the browser and is safely re-runnable.

    Two phases inside the caller's transaction, because SQLite enforces UNIQUE
    per row, immediately -- a single "shift into final position" statement would
    transiently duplicate a value and fail:

    * **Park.** Shift every row up by ``offset = max + 1``. Values only move
      upward into empty space, so no row lands on a still-occupied slot.
    * **Assign.** Renumber all rows to a dense ``1..K`` in chronological order.
      The dense targets ``[1, K]`` are disjoint from the parked range, so every
      per-row write lands on a free slot.

    Ordering: ``capture_timestamp`` ascending (true chronological interleave of
    uploaded and captured frames), tie-broken by ``id`` ascending (stable, never
    null); rows with a null ``capture_timestamp`` are parked last. The caller is
    responsible for holding the per-project write lock so a live capture cannot
    interleave between the two phases. Returns ``K``, the number of rows
    renumbered.
    """
    current_max = session.execute(
        select(func.max(Frame.sequence_index)).where(Frame.project_id == project_id)
    ).scalar_one_or_none()
    if current_max is None:
        return 0
    offset = current_max + 1

    # Phase 1 -- park every row up into a disjoint high range.
    session.execute(
        update(Frame)
        .where(Frame.project_id == project_id)
        .values(sequence_index=Frame.sequence_index + offset)
    )

    # Phase 2 -- dense assign 1..K in chronological order.
    ids = (
        session.execute(
            select(Frame.id)
            .where(Frame.project_id == project_id)
            .order_by(
                Frame.capture_timestamp.is_(None),  # null timestamps parked last
                Frame.capture_timestamp.asc(),
                Frame.id.asc(),  # stable tie-break for equal/duplicate timestamps
            )
        )
        .scalars()
        .all()
    )
    if ids:
        session.execute(
            update(Frame),
            [{"id": fid, "sequence_index": i} for i, fid in enumerate(ids, start=1)],
        )
    return len(ids)


@dataclass
class ImportedFile:
    """Per-file outcome of a batch import.

    ``name`` is the caller-supplied label for the file (e.g. the upload
    filename). An imported file carries the ``frame_id`` it became and whether
    its capture timestamp was ``inferred`` (no readable Exif time, fell back to
    the caller's supplied instant). A skipped file carries the ``reason`` it was
    rejected and leaves the other fields ``None``/``False``.
    """

    name: str
    status: str  # "imported" or "skipped"
    frame_id: int | None = None
    inferred: bool = False
    reason: str | None = None


@dataclass
class ImportResult:
    """Outcome of a batch import: per-file records plus convenience counts.

    The whole batch goes through the shared atomic writer (one committed
    transaction per file, ``origin="uploaded"``), then a single re-sequence
    realigns the project's sequence to chronological order. Like the other bulk
    helpers this is **skip-not-raise**: a file that fails validation is recorded
    in :attr:`skipped` and the batch continues. ``imported`` and ``skipped``
    partition the input files in order.
    """

    imported: list[ImportedFile] = field(default_factory=list)
    skipped: list[ImportedFile] = field(default_factory=list)

    @property
    def imported_count(self) -> int:
        """Number of files persisted as frames."""
        return len(self.imported)

    @property
    def skipped_count(self) -> int:
        """Number of files rejected and not persisted."""
        return len(self.skipped)


def import_frames(
    session_factory: sessionmaker[Session],
    settings: Settings,
    project_id: int,
    files: list[tuple[str, bytes]],
    fallback_timestamp: datetime,
    actor_user_id: int,
) -> ImportResult:
    """Import a batch of image files as uploaded frames, chronologically ordered.

    Each ``(name, image_bytes)`` is processed serially: the bytes are validated
    to be a real JPEG or PNG and their dimensions read; the capture time is taken
    from the image's Exif ``DateTimeOriginal`` when present, else it falls back to
    ``fallback_timestamp`` and the frame is flagged ``inferred`` (uploaded bytes
    have no filesystem mtime, so the caller supplies the fallback -- typically the
    upload instant or a client-stated time). The frame is persisted through the
    shared atomic writer with ``origin="uploaded"``.

    A single re-sequence runs once at the end (never per file), realigning the
    whole project's ``sequence_index`` to ``(capture_timestamp, id)`` order so
    imported frames interleave chronologically with captured ones in the browser.
    The re-sequence is idempotent and re-runnable, so even a crash mid-import only
    temporarily de-orders the browser -- never loses data or mis-orders renders.

    The batch is **skip-not-raise**: a file that is not a readable supported image
    is recorded in :attr:`ImportResult.skipped` with a reason and the batch
    continues. The whole import (every write plus the single re-sequence) runs
    under the shared per-project write lock so a concurrent live capture cannot
    interleave with the re-sequence's two phases.

    :raises ImportBatchTooLargeError: if ``files`` exceeds
        :data:`MAX_IMPORT_BATCH`; this is a batch-level guard, distinct from the
        per-file skip behaviour.
    """
    if len(files) > MAX_IMPORT_BATCH:
        raise ImportBatchTooLargeError(
            f"import batch of {len(files)} files exceeds the maximum of "
            f"{MAX_IMPORT_BATCH}"
        )

    # Imported here (not at module load) to avoid a capture<->storage import
    # cycle, mirroring upload_frame's lazy import.
    from ..capture import FrameWriter
    from ..capture.frame_writer import project_write_lock

    writer = FrameWriter(session_factory, frames_root(settings))
    result = ImportResult()

    with project_write_lock(project_id):
        for name, image_bytes in files:
            record = _import_one_file(
                writer, project_id, name, image_bytes, fallback_timestamp
            )
            if record.status == "imported":
                result.imported.append(record)
            else:
                result.skipped.append(record)

        # One transaction for the per-frame audit events and the single
        # re-sequence over whatever was accepted. The frame writes already
        # committed (one transaction each); a crash before this point leaves
        # recorded frames without audit rows, consistent with keep-all, and the
        # re-sequence is re-runnable.
        if result.imported:
            with session_scope(session_factory) as session:
                for record in result.imported:
                    _record_event(
                        session,
                        project_id=project_id,
                        level="info",
                        message=f"frame {record.frame_id} imported",
                        metadata={
                            "frame_id": record.frame_id,
                            "action": "import",
                            "name": record.name,
                            "inferred": record.inferred,
                        },
                        actor_user_id=actor_user_id,
                    )
                resequence_project(session, project_id)

    logger.info(
        "imported frames project=%s imported=%s skipped=%s",
        project_id,
        result.imported_count,
        result.skipped_count,
    )
    return result


def _import_one_file(
    writer: FrameWriter,
    project_id: int,
    name: str,
    image_bytes: bytes,
    fallback_timestamp: datetime,
) -> ImportedFile:
    """Validate and persist one import file, returning its per-file record.

    Skip-not-raise: a file that is not a readable supported image, or whose write
    fails, yields a ``skipped`` record with a reason rather than aborting the
    batch. The Exif capture time is preferred; its absence flags ``inferred`` and
    falls back to ``fallback_timestamp``.
    """
    detected = detect_format(image_bytes)
    if detected is None:
        return ImportedFile(
            name=name, status="skipped", reason="not a recognised JPEG or PNG"
        )
    dimensions = read_dimensions(image_bytes)
    if dimensions is None:
        return ImportedFile(
            name=name, status="skipped", reason="could not read image dimensions"
        )
    width, height = dimensions

    exif_timestamp = read_capture_timestamp(image_bytes)
    inferred = exif_timestamp is None
    capture_timestamp = fallback_timestamp if exif_timestamp is None else exif_timestamp

    captured = CapturedFrame(
        image_bytes=image_bytes,
        width=width,
        height=height,
        format=detected,
        captured_at=capture_timestamp,
    )
    try:
        written = writer.write(
            project_id,
            captured,
            origin="uploaded",
            capture_timestamp=capture_timestamp,
            capture_timestamp_inferred=inferred,
        )
    except ValueError as exc:
        return ImportedFile(name=name, status="skipped", reason=str(exc))

    return ImportedFile(
        name=name,
        status="imported",
        frame_id=written.frame_id,
        inferred=inferred,
    )


def list_frames(
    session: Session,
    project_id: int,
    *,
    limit: int,
    offset: int,
    include_deleted: bool = False,
    include_excluded: bool = True,
) -> list[Frame]:
    """Return a project's frames in capture order, paginated.

    Frames are ordered by ``capture_timestamp`` ascending, then
    ``sequence_index`` ascending as a stable tie-break (so rows with a null or
    equal timestamp keep a deterministic order). Soft-deleted frames are excluded
    unless ``include_deleted`` is set.

    Note the two visibility flags default in *opposite* directions, which is
    deliberate: ``include_deleted`` defaults ``False`` (soft-deleted frames are
    hidden from browse), while ``include_excluded`` defaults ``True`` (frames
    excluded from rendering stay fully visible in the browser). A reader who
    "normalises" these defaults to match would reintroduce a bug. Only the
    encoder gather passes ``include_excluded=False`` to add
    ``Frame.excluded_at IS NULL``; excluding a frame changes exactly that one
    query and a tile badge -- it touches nothing in keyset/window/count/the
    "new frames" pill/neighbour navigation/disk usage/dimensions.
    """
    stmt = select(Frame).where(Frame.project_id == project_id)
    if not include_deleted:
        stmt = stmt.where(Frame.lifecycle_state == "active")
    if not include_excluded:
        stmt = stmt.where(Frame.excluded_at.is_(None))
    stmt = (
        stmt.order_by(Frame.capture_timestamp.asc(), Frame.sequence_index.asc())
        .limit(limit)
        .offset(offset)
    )
    return list(session.execute(stmt).scalars().all())


def latest_active_frame(session: Session, project_id: int) -> Frame | None:
    """Return a project's most recent active frame, or ``None`` if it has none.

    Recency is judged by ``sequence_index`` descending: the writer assigns it
    monotonically per project, so the highest index is always the newest capture.
    It is preferred over ``capture_timestamp`` here because the timestamp column
    is nullable and user-editable, whereas the sequence is stable and never null.
    Only active (not soft-deleted) frames are considered. One cheap, indexed
    lookup -- callers rendering many projects issue one of these per project.
    """
    return session.execute(
        select(Frame)
        .where(Frame.project_id == project_id)
        .where(Frame.lifecycle_state == "active")
        .order_by(Frame.sequence_index.desc())
        .limit(1)
    ).scalar_one_or_none()


def oldest_active_seq(
    session: Session,
    project_id: int,
    *,
    include_deleted: bool = False,
) -> int | None:
    """Return a project's lowest ``sequence_index``, or ``None`` if it has none.

    The mirror of the keyset cursor's newest-first ordering: the lowest sequence
    is the oldest capture (the writer assigns the index monotonically per
    project). A "jump to start" resolves this to anchor the grid at the very
    first frame so the end-cap renders. Soft-deleted frames are excluded unless
    ``include_deleted``. One cheap, indexed lookup.
    """
    stmt = select(func.min(Frame.sequence_index)).where(Frame.project_id == project_id)
    if not include_deleted:
        stmt = stmt.where(Frame.lifecycle_state == "active")
    return session.execute(stmt).scalar_one_or_none()


def list_active_frame_times(
    session: Session,
    project_id: int,
    *,
    include_deleted: bool = False,
    limit: int = 5000,
) -> list[datetime]:
    """Return a project's capture timestamps (ascending) for gap detection.

    Only frames that carry a timestamp participate in a time-axis view, so null
    timestamps are filtered out and the rest are ordered ascending -- the input
    shape the ribbon and the gap finder expect. Soft-deleted frames are excluded
    unless ``include_deleted``. Capped at ``limit`` rows to bound the scan; the
    cap matches the ribbon's own draw cap so the buttons and the ribbon markers
    are computed from the same frame population.
    """
    stmt = select(Frame.capture_timestamp).where(
        Frame.project_id == project_id,
        Frame.capture_timestamp.is_not(None),
    )
    if not include_deleted:
        stmt = stmt.where(Frame.lifecycle_state == "active")
    stmt = stmt.order_by(Frame.capture_timestamp.asc()).limit(limit)
    rows = session.execute(stmt).scalars().all()
    return [t for t in rows if t is not None]


@dataclass(frozen=True)
class CaptureGap:
    """A capture lapse: the last frame before it, the first after, and its span.

    ``before`` is the timestamp of the frame immediately preceding the lapse (the
    anchor a "jump to gap" lands on, so the grid shows the last frame captured
    before capture stopped); ``after`` is the first frame once capture resumed.
    ``duration`` is ``after - before``. Timestamps carry whatever tzinfo the
    caller supplied -- the finder does not normalise them.
    """

    before: datetime
    after: datetime

    @property
    def duration(self) -> timedelta:
        """The wall-clock length of the lapse (``after - before``)."""
        return self.after - self.before


def find_capture_gaps(
    frame_times: list[datetime],
    span_start: datetime,
    span_end: datetime,
    *,
    min_gap_fraction: float = 0.04,
) -> list[CaptureGap]:
    """Find capture gaps in timestamp space, bounded by their adjacent frames.

    A gap is any interval between two consecutive (timestamp-ordered) frames
    longer than ``min_gap_fraction`` of the whole ``[span_start, span_end]`` span
    -- the same threshold rule the ribbon's marker detector uses, so the
    jump-to-gap targets are exactly the lapses the ribbon draws. Returns the gaps
    in ascending order with their bounding frame timestamps (unlike the ribbon's
    fraction-only markers, which are lossy for navigation). Returns an empty list
    when there are fewer than two frames or the span is degenerate.
    """
    if len(frame_times) < 2 or span_end <= span_start:
        return []
    ordered = sorted(frame_times)
    threshold = (span_end - span_start).total_seconds() * min_gap_fraction
    gaps: list[CaptureGap] = []
    for prev, nxt in zip(ordered, ordered[1:], strict=False):
        if (nxt - prev).total_seconds() > threshold:
            gaps.append(CaptureGap(before=prev, after=nxt))
    return gaps


def nearest_gap(
    gaps: list[CaptureGap],
    anchor: datetime,
    *,
    direction: str,
) -> CaptureGap | None:
    """Return the gap adjacent to ``anchor`` in ``direction`` (``next``/``prev``).

    A gap is identified by its ``before`` timestamp (the last frame before the
    lapse -- the point a jump lands on). For ``next`` the nearest gap whose
    ``before`` is strictly after ``anchor`` is returned; for ``prev`` the nearest
    whose ``before`` is strictly before it. The strict comparison is deliberate:
    repeated presses step through successive gaps instead of sticking on the one
    the grid is already showing. Returns ``None`` when no gap lies in that
    direction (the caller then leaves the grid where it is).
    """
    if direction == "next":
        candidates = [g for g in gaps if g.before > anchor]
        return min(candidates, key=lambda g: g.before) if candidates else None
    if direction == "prev":
        candidates = [g for g in gaps if g.before < anchor]
        return max(candidates, key=lambda g: g.before) if candidates else None
    return None


def list_frames_keyset(
    session: Session,
    project_id: int,
    *,
    before_seq: int | None = None,
    limit: int,
    include_deleted: bool = False,
) -> list[Frame]:
    """Return a project's frames newest-first for keyset (cursor) pagination.

    Ordered by ``sequence_index`` descending (the newest capture first); with
    ``before_seq`` only frames strictly older than that cursor are returned
    (``sequence_index < before_seq``). The sequence is per-project, monotonic and
    never null, so this cursor is stable even when soft-delete/restore shifts the
    frame population mid-scroll -- unlike an ``offset``, which would skip or
    duplicate rows. Soft-deleted frames are excluded unless ``include_deleted``.
    """
    stmt = select(Frame).where(Frame.project_id == project_id)
    if not include_deleted:
        stmt = stmt.where(Frame.lifecycle_state == "active")
    if before_seq is not None:
        stmt = stmt.where(Frame.sequence_index < before_seq)
    stmt = stmt.order_by(Frame.sequence_index.desc()).limit(limit)
    return list(session.execute(stmt).scalars().all())


def resolve_seq_at_timestamp(
    session: Session,
    project_id: int,
    anchor: datetime,
    *,
    include_deleted: bool = False,
) -> int | None:
    """Resolve a timestamp anchor to a project's ``sequence_index`` boundary.

    Returns the ``sequence_index`` of the first frame whose capture timestamp is
    at-or-after ``anchor`` (ordered by ``capture_timestamp`` ascending, with
    ``sequence_index`` ascending as a stable tie-break for equal/duplicate
    timestamps). Frames with a null capture timestamp are ignored -- they have no
    place on a time axis. Returns ``None`` when no frame is at-or-after the anchor
    (the anchor is past the last timed frame) or the project has no timed frames
    at all, so callers can clamp to the newest page.

    The conversion is the reusable ``timestamp -> cursor`` convention: a future
    time-ordered view (e.g. an events page) resolves an anchor the same way.
    """
    stmt = select(Frame.sequence_index).where(
        Frame.project_id == project_id,
        Frame.capture_timestamp.is_not(None),
        Frame.capture_timestamp >= _to_naive_utc(anchor),
    )
    if not include_deleted:
        stmt = stmt.where(Frame.lifecycle_state == "active")
    stmt = stmt.order_by(
        Frame.capture_timestamp.asc(), Frame.sequence_index.asc()
    ).limit(1)
    return session.execute(stmt).scalar_one_or_none()


def resolve_window_center(
    session: Session,
    project_id: int,
    anchor: datetime,
    *,
    include_deleted: bool = False,
) -> tuple[datetime | None, bool]:
    """Return the timestamp the window will centre on, and whether it is exact.

    The window centres on the first frame at-or-after ``anchor`` (the same frame
    :func:`resolve_seq_at_timestamp` resolves the cursor to). This returns that
    frame's timestamp together with an ``exact`` flag that is ``True`` only when
    the anchor equals that timestamp. When the anchor falls between two frames the
    flag is ``False`` and the returned timestamp is the nearest captured frame the
    grid actually lands on -- the value to surface as a "nearest frame" note, so
    the user sees which real capture the jump resolved to. ``(None, False)`` means
    no timed frame is at-or-after the anchor (the grid clamps to the newest page),
    in which case there is no nearest-frame note to show.
    """
    center = session.execute(
        select(Frame.capture_timestamp)
        .where(
            Frame.project_id == project_id,
            Frame.capture_timestamp.is_not(None),
            Frame.capture_timestamp >= _to_naive_utc(anchor),
            *(() if include_deleted else (Frame.lifecycle_state == "active",)),
        )
        .order_by(Frame.capture_timestamp.asc(), Frame.sequence_index.asc())
        .limit(1)
    ).scalar_one_or_none()
    if center is None:
        return None, False
    return center, center == _to_naive_utc(anchor)


def list_frames_window(
    session: Session,
    project_id: int,
    anchor: datetime,
    *,
    side_limit: int = 30,
    include_deleted: bool = False,
) -> tuple[list[Frame], int | None]:
    """Return a bidirectional window of frames centered on a timestamp anchor.

    Resolves ``anchor`` to a ``sequence_index`` boundary, then gathers up to
    ``side_limit`` frames at-or-after it (the boundary and newer) and up to
    ``side_limit`` strictly before it (older). The combined list is returned
    newest-first (descending ``sequence_index``), matching the grid's ordering
    and the keyset batch contract, so the existing sentinel continues the
    downward scroll seamlessly.

    The window is built entirely in ``sequence_index`` space once the anchor is
    resolved. Centering is "by timestamp" only at the single resolved boundary;
    past that the two halves count off in sequence order. This stays exact under
    the keyset cursor even when a backdated or uploaded frame's capture time and
    sequence disagree -- splitting the halves on raw timestamp instead would make
    the oldest-in-window cursor ambiguous and could skip or re-show frames on the
    next batch.

    Returns ``(frames, next_before)`` where ``next_before`` is the
    ``sequence_index`` to pass as the next/older batch's cursor (the oldest frame
    in the window), or ``None`` when the window already reaches the start of the
    series. Series ends are handled without error:

    * anchor past the last timed frame (or no timed frames at all) -> the
      boundary is unresolved and the window is the newest ``side_limit`` frames
      (the tail), so a too-late or all-null-timestamp anchor degrades to the
      newest page rather than an empty grid;
    * anchor at/before the first frame -> the before-half is empty and
      ``next_before`` reflects the true series start.
    """
    anchor_seq = resolve_seq_at_timestamp(
        session, project_id, anchor, include_deleted=include_deleted
    )

    if anchor_seq is None:
        # Anchor is past the newest timed frame (or none are timed): clamp to the
        # newest page so the grid lands on the tail instead of going blank.
        newest = list_frames_keyset(
            session,
            project_id,
            before_seq=None,
            limit=side_limit + 1,
            include_deleted=include_deleted,
        )
        has_more = len(newest) > side_limit
        newest = newest[:side_limit]
        next_before = newest[-1].sequence_index if (newest and has_more) else None
        return newest, next_before

    at_or_after = _frames_at_or_after_seq(
        session,
        project_id,
        anchor_seq,
        limit=side_limit,
        include_deleted=include_deleted,
    )
    before = list_frames_keyset(
        session,
        project_id,
        before_seq=anchor_seq,
        limit=side_limit + 1,
        include_deleted=include_deleted,
    )
    before_has_more = len(before) > side_limit
    before = before[:side_limit]

    # Combined newest-first: the at-or-after half (descending) then the older
    # half (already descending from the keyset helper).
    frames = at_or_after + before
    if not frames:
        return [], None
    # The oldest frame in the window is the cursor for the next batch -- but only
    # if older frames remain beyond it.
    next_before = frames[-1].sequence_index if before_has_more else None
    return frames, next_before


def list_frames_at_or_after_seq(
    session: Session,
    project_id: int,
    seq: int,
    *,
    limit: int,
    include_deleted: bool = False,
) -> list[Frame]:
    """Return up to ``limit`` frames with ``sequence_index >= seq``, newest-first.

    The public spelling of :func:`_frames_at_or_after_seq` with a default
    ``include_deleted``. A "jump to start" uses this with the series-minimum
    sequence to fetch the oldest page newest-first: nothing is older than the
    minimum, so the result is the first page of a downward scroll and the grid's
    end-cap renders. It counts off existing rows, so a sparse sequence (from
    soft-deletes) still yields a full page where one exists.
    """
    return _frames_at_or_after_seq(
        session, project_id, seq, limit=limit, include_deleted=include_deleted
    )


def _frames_at_or_after_seq(
    session: Session,
    project_id: int,
    seq: int,
    *,
    limit: int,
    include_deleted: bool,
) -> list[Frame]:
    """Return up to ``limit`` frames with ``sequence_index >= seq``, newest-first.

    Queried ascending from the boundary so the boundary frame itself is included
    and the nearest newer frames are taken, then reversed to newest-first so the
    result composes with the older half into a single descending list.
    """
    stmt = select(Frame).where(
        Frame.project_id == project_id,
        Frame.sequence_index >= seq,
    )
    if not include_deleted:
        stmt = stmt.where(Frame.lifecycle_state == "active")
    stmt = stmt.order_by(Frame.sequence_index.asc()).limit(limit)
    rows = list(session.execute(stmt).scalars().all())
    rows.reverse()
    return rows


def count_frames_newer_than(
    session: Session,
    *,
    after_id: int,
    project_id: int | None = None,
    include_deleted: bool = False,
) -> int:
    """Count frames inserted after the cursor frame ``id``.

    The cursor is the globally monotonic primary key ``id`` (the value the grid
    exposes as its newest item), so this counts ``Frame.id > after_id``. With a
    ``project_id`` the count is scoped to that project; without one it spans all
    projects -- mirroring the single-vs-global discriminator the browser uses.
    Soft-deleted frames are excluded unless ``include_deleted``. One cheap indexed
    aggregate; returns ``0`` when nothing is newer.
    """
    stmt = select(func.count()).select_from(Frame).where(Frame.id > after_id)
    if project_id is not None:
        stmt = stmt.where(Frame.project_id == project_id)
    if not include_deleted:
        stmt = stmt.where(Frame.lifecycle_state == "active")
    return int(session.execute(stmt).scalar_one() or 0)


def list_all_frames_keyset(
    session: Session,
    *,
    before_id: int | None = None,
    limit: int,
    include_deleted: bool = False,
) -> list[Frame]:
    """Return frames across ALL projects newest-first for keyset pagination.

    The cross-project ("All Projects") browser cannot page on ``sequence_index``
    (unique only within a project) or ``capture_timestamp`` (nullable). It pages
    on the frame's primary key ``id`` instead: an autoincrement integer that is
    globally monotonic across every project and never null, so it is a clean
    single-column keyset -- the same property the events log relies on. Ordered
    ``id`` descending (most recently added first); with ``before_id`` only frames
    strictly older than that cursor are returned (``id < before_id``). Because the
    id never changes, the cursor is stable under soft-delete/restore mid-scroll.
    (Ordering is by insertion id, so a backdated/uploaded frame sorts by when it
    was added, not its capture time.)
    """
    stmt = select(Frame)
    if not include_deleted:
        stmt = stmt.where(Frame.lifecycle_state == "active")
    if before_id is not None:
        stmt = stmt.where(Frame.id < before_id)
    stmt = stmt.order_by(Frame.id.desc()).limit(limit)
    return list(session.execute(stmt).scalars().all())


def sum_project_disk_usage(session: Session, project_id: int) -> int:
    """Return the total on-disk size of a project's active frames, in bytes.

    Sums ``file_size_bytes`` over the project's active (not soft-deleted) frames;
    a frame whose size is unknown (``NULL``) contributes nothing. Soft-deleted
    frames are excluded so the figure tracks the live footprint shown in status.
    One indexed aggregate per project. Returns ``0`` when the project has no
    active frames.
    """
    total = session.execute(
        select(func.coalesce(func.sum(Frame.file_size_bytes), 0))
        .where(Frame.project_id == project_id)
        .where(Frame.lifecycle_state == "active")
    ).scalar_one()
    return int(total or 0)


def predominant_dimensions(session: Session, project_id: int) -> tuple[int, int] | None:
    """Return the most common ``(width, height)`` among a project's active frames.

    This is the reference a frame's :func:`dimension_mismatch` is judged against:
    a render needs uniform input dimensions, so the baseline is whatever size the
    bulk of the project's frames share. Ties are broken by larger width then
    larger height, making the result deterministic. Returns ``None`` when the
    project has no active frame with known dimensions, in which case no frame can
    be considered mismatched.
    """
    row = session.execute(
        select(Frame.width, Frame.height, func.count().label("n"))
        .where(Frame.project_id == project_id)
        .where(Frame.lifecycle_state == "active")
        .where(Frame.width.is_not(None))
        .where(Frame.height.is_not(None))
        .group_by(Frame.width, Frame.height)
        .order_by(func.count().desc(), Frame.width.desc(), Frame.height.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return (row.width, row.height)


def dimension_mismatch(frame: Frame, predominant: tuple[int, int] | None) -> bool:
    """Return whether ``frame`` differs from the project's predominant dimensions.

    Computed at read time, never stored. A frame is mismatched only when it has
    known dimensions that differ from the supplied baseline. A frame with unknown
    dimensions, or any frame when there is no baseline, is not mismatched.
    """
    if predominant is None or frame.width is None or frame.height is None:
        return False
    return (frame.width, frame.height) != predominant
