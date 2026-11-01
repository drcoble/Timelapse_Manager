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
import re
import threading
import time
import weakref
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from sqlalchemy import (
    Engine,
    Table,
    bindparam,
    case,
    event,
    func,
    select,
    text,
    update,
)
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
from ..storage.paths import PathContainmentError, resolve_contained_frame_path
from ..storage.thumbnail_cache import remove_frame_thumbnails

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

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


def _project_event(
    *,
    project_id: int,
    level: str,
    message: str,
    metadata: dict[str, Any],
    actor_user_id: int,
) -> Event:
    """Build (not add) a project-scoped audit event attributed to ``actor_user_id``.

    Callers that add it must first call :func:`ensure_sentinel_admin` on the
    session, as :func:`_record_event` does; the bulk paths call it once per
    batch and add every event with one ``add_all``.
    """
    return Event(
        scope="project",
        scope_id=project_id,
        level=level,
        message=message,
        event_metadata=metadata,
        actor_user_id=actor_user_id,
    )


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
        _project_event(
            project_id=project_id,
            level=level,
            message=message,
            metadata=metadata,
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
    _mark_dimensions_dirty(session, frame.project_id)
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
    _mark_dimensions_dirty(session, frame.project_id)
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


# Ids per ``WHERE id IN (...)`` when loading a selection of frames: well under
# SQLite's bound-parameter limit, and the same bound as the bulk routes' sync
# cap, so a typical bulk action is one SELECT.
_LOAD_CHUNK = 500


def load_frames_by_id(session: Session, frame_ids: Iterable[int]) -> dict[int, Frame]:
    """Return ``{id: Frame}`` for the ids in ``frame_ids`` that exist (#66 C7).

    One ``SELECT ... WHERE id IN (...)`` per :data:`_LOAD_CHUNK` distinct ids,
    instead of a ``session.get`` per id. Unknown ids are simply absent from the
    result, and the rows are not filtered by project: callers that need a
    project check apply it themselves, so the skip/fail semantics of a
    per-id lookup are kept exactly. Every chunk is loaded before the caller
    mutates anything, so no autoflush takes the SQLite write lock part-way
    through the reads.
    """
    distinct = list(dict.fromkeys(frame_ids))
    found: dict[int, Frame] = {}
    for start in range(0, len(distinct), _LOAD_CHUNK):
        chunk = distinct[start : start + _LOAD_CHUNK]
        for frame in session.execute(
            select(Frame).where(Frame.id.in_(chunk))
        ).scalars():
            found[frame.id] = frame
    return found


def distinct_project_ids(
    session: Session, frame_ids: Iterable[int], *, limit: int = 2
) -> list[int]:
    """Return the distinct project ids of the frames in ``frame_ids`` (#96).

    Reads in :data:`_LOAD_CHUNK`-id ``IN`` queries, so an explicit selection of
    any length stays under SQLite's bound-variable limit (32,766), and stops as
    soon as ``limit`` distinct projects are seen: a caller that only needs to
    know "one project or several" never reads further. Ids that match no frame
    contribute nothing.
    """
    distinct = list(dict.fromkeys(frame_ids))
    projects: set[int] = set()
    for start in range(0, len(distinct), _LOAD_CHUNK):
        chunk = distinct[start : start + _LOAD_CHUNK]
        projects.update(
            session.execute(
                select(Frame.project_id).where(Frame.id.in_(chunk)).distinct()
            ).scalars()
        )
        if len(projects) >= limit:
            break
    return sorted(projects)


def _apply_many(
    session: Session,
    frame_ids: list[int],
    actor_user_id: int,
    *,
    mutate: Any,
    message: Any,
    action: str,
    changes_active_set: bool = False,
) -> BulkResult:
    """Apply a single-frame lifecycle mutation across an id-set, skip-not-raise.

    For each id, if the frame exists: run ``mutate`` on the row and write one
    per-frame audit event (the same event-type strings the single-frame
    operations use). A missing id is appended to ``failed`` and the loop
    continues -- the batch is never aborted by one bad id. Returns the
    succeeded/failed partition of the requested ids, in request order.

    The rows are loaded up front in chunked ``IN`` queries
    (:func:`load_frames_by_id`) and the events added with one ``add_all``
    (#66 C7), so the statement count no longer grows with the selection and the
    write lock is not taken until the caller's flush or commit.

    This is the shared core of the four public ``*_many`` helpers; each passes
    the field mutation, the audit message builder, and the ``metadata.action``
    string so the audit trail matches the single-frame operations exactly.
    ``changes_active_set`` marks the touched projects' cached predominant
    dimensions stale (soft-delete and restore change the active set).
    """
    result = BulkResult()
    frames = load_frames_by_id(session, frame_ids)
    if frames:
        ensure_sentinel_admin(session)
    events: list[Event] = []
    for frame_id in frame_ids:
        frame = frames.get(frame_id)
        if frame is None:
            result.failed.append(frame_id)
            continue
        mutate(frame)
        if changes_active_set:
            _mark_dimensions_dirty(session, frame.project_id)
        events.append(
            _project_event(
                project_id=frame.project_id,
                level="info",
                message=message(frame_id),
                metadata={"frame_id": frame_id, "action": action},
                actor_user_id=actor_user_id,
            )
        )
        result.succeeded.append(frame_id)
    session.add_all(events)
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
        changes_active_set=True,
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
        changes_active_set=True,
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
    # Chunked IN loads and one add_all, as in _apply_many (#66 C7).
    frames = load_frames_by_id(session, frame_ids)
    if frames:
        ensure_sentinel_admin(session)
    events: list[Event] = []
    for frame_id in frame_ids:
        frame = frames.get(frame_id)
        if frame is None:
            result.failed.append(frame_id)
            continue
        if frame.capture_timestamp is None:
            result.skipped_null.append(frame_id)
            continue
        previous = frame.capture_timestamp.isoformat()
        frame.capture_timestamp = frame.capture_timestamp + delta
        _mark_order_dirty(session, frame.project_id, frame.id)
        events.append(
            _project_event(
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
        )
        result.shifted.append(frame_id)
    session.add_all(events)
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
    # Only a file inside the project's frame directories is ever unlinked: a row
    # whose path escapes them (an old import, a hand edit, a symlink pointing
    # elsewhere) loses its row but its file is left alone (#170).
    unlink_path: Path | None = None
    if stored_path is not None and project is not None:
        try:
            unlink_path = resolve_contained_frame_path(settings, project, stored_path)
        except PathContainmentError:
            logger.warning(
                "not deleting file of frame %s (project %s): outside its frame "
                "directory",
                frame_id,
                project_id,
            )
    if project is not None and project.frame_count > 0:
        project.frame_count -= 1
    session.delete(frame)
    _mark_dimensions_dirty(session, project_id)
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

    if unlink_path is not None:
        with contextlib.suppress(FileNotFoundError):
            unlink_path.unlink()
    # The cached thumbnails are derived from the file just removed (#67 R12).
    remove_frame_thumbnails(settings, project_id, frame_id)
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
    _mark_order_dirty(session, frame.project_id, frame.id)
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
    without an audit row, consistent with the keep-all guarantee. If the capture
    time puts the frame out of order, the project's order is repaired (#144).

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
    max_dimension = settings.capture.max_image_dimension_px
    if width > max_dimension or height > max_dimension:
        raise InvalidImageError(
            f"image dimensions {width}x{height} exceed the maximum of "
            f"{max_dimension}px per axis"
        )

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

    # The writer gave the frame the next index; an upload with an older capture
    # time must not leave it out of order in the browser (#144). Done before the
    # frame is read back, so the returned row carries its final index.
    restore_project_order(session_factory, project_id, [written.frame_id])

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

    The full repair: :func:`resequence_project_from` with no lower bound, so
    every row of the project is renumbered to a dense ``1..K``. Imports no
    longer call this (they renumber only the affected suffix, #66 C2); it stays
    as the explicit repair path for a project whose order is suspect. Returns
    ``K``, the number of rows renumbered.
    """
    return resequence_project_from(session, project_id, None)


def _suffix_predicate(
    session: Session, project_id: int, from_ts: datetime | None
) -> tuple[Any, int]:
    """Return ``(in_suffix, start)`` for a renumber from ``from_ts``.

    ``in_suffix`` is the WHERE term selecting the rows to renumber: the whole
    project for ``from_ts=None``, else the rows at or after the bound plus the
    null-timestamp rows. ``start`` is the first index to hand out: one past the
    highest index of the untouched prefix (``1`` when the suffix is everything).
    """
    if from_ts is None:
        return Frame.project_id == project_id, 1
    bound = _to_naive_utc(from_ts)
    in_suffix = (Frame.project_id == project_id) & (
        (Frame.capture_timestamp >= bound) | Frame.capture_timestamp.is_(None)
    )
    prefix_max = session.execute(
        select(func.max(Frame.sequence_index)).where(
            Frame.project_id == project_id, Frame.capture_timestamp < bound
        )
    ).scalar_one_or_none()
    return in_suffix, (prefix_max or 0) + 1


def resequence_project_from(
    session: Session, project_id: int, from_ts: datetime | None
) -> int:
    """Renumber the suffix of a project that starts at ``from_ts`` (#66 C2).

    Realigns ``sequence_index`` so its order equals ``(capture_timestamp, id)``
    order across **all** of the project's rows (active and soft-deleted alike),
    rows with a null ``capture_timestamp`` last. The browser's keyset/window
    cursor navigates in sequence space and assumes sequence order tracks time
    order. Render output sorts by ``capture_timestamp`` and is unaffected, so a
    partial/failed run only temporarily de-orders the browser and is safely
    re-runnable.

    Only the **suffix** is written: rows with ``capture_timestamp >= from_ts``
    or a null timestamp. The prefix (every row strictly earlier than
    ``from_ts``) keeps its indexes, and the suffix is numbered from one past the
    prefix's highest index. So the invariant holds afterwards provided the
    prefix was already in order among itself -- which
    :func:`earliest_out_of_order_timestamp` checks for the import path. With
    ``from_ts=None`` the suffix is the whole project (a dense ``1..K``).

    Two phases inside the caller's transaction, because SQLite enforces UNIQUE
    per row, immediately -- a single "shift into final position" statement would
    transiently duplicate a value and fail:

    * **Park.** Shift the suffix up by ``offset = max + K + 1``, above both the
      project's current maximum and the highest target ``start + K - 1``, so no
      parked row lands on an occupied slot or a slot a later assignment needs.
    * **Assign.** Renumber the suffix to ``start..start+K-1`` in chronological
      order. Every target is above the prefix and below the parked range, so
      each per-row write lands on a free slot.

    Ordering: ``capture_timestamp`` ascending, tie-broken by ``id`` ascending
    (stable, never null); null timestamps last. The caller holds the per-project
    write lock so a live capture cannot interleave between the two phases.
    Returns ``K``, the number of suffix rows renumbered.
    """
    in_suffix, start = _suffix_predicate(session, project_id, from_ts)

    ids = (
        session.execute(
            select(Frame.id)
            .where(in_suffix)
            .order_by(
                Frame.capture_timestamp.is_(None),  # null timestamps parked last
                Frame.capture_timestamp.asc(),
                Frame.id.asc(),  # stable tie-break for equal/duplicate timestamps
            )
        )
        .scalars()
        .all()
    )
    if not ids:
        return 0
    current_max = session.execute(
        select(func.max(Frame.sequence_index)).where(Frame.project_id == project_id)
    ).scalar_one()
    offset = current_max + len(ids) + 1

    # Phase 1 -- park the suffix up into a range disjoint from every row and
    # every target.
    session.execute(
        update(Frame)
        .where(in_suffix)
        .values(sequence_index=Frame.sequence_index + offset)
    )

    # Phase 2 -- assign start..start+K-1 in chronological order.
    session.execute(
        update(Frame),
        [{"id": fid, "sequence_index": i} for i, fid in enumerate(ids, start=start)],
    )
    return len(ids)


def earliest_out_of_order_timestamp(
    session: Session, project_id: int
) -> datetime | None:
    """Return the earliest timestamp at which a project's order is broken.

    Walks the project's rows in ``sequence_index`` order and flags every row
    that sorts *before* its predecessor under the resequence ordering
    (``capture_timestamp`` with nulls last, then ``id``). Returns the smallest
    ``capture_timestamp`` among the flagged rows, or ``None`` when the
    sequence already follows that ordering.

    Passing the result to :func:`resequence_project_from` therefore restores the
    full invariant: any two rows that are out of order relative to each other
    both have a timestamp at or after the returned value, so both fall in the
    suffix. (Proof sketch: between an inverted pair ``a`` before ``b``, the row
    with the smallest key after ``a`` sorts below its predecessor, so it is
    flagged, and its timestamp is at most ``b``'s.) This is what lets an import
    keep the old full resequence's guarantee -- it repairs a disorder left by a
    timestamp edit, an offset, a single upload or an interrupted import -- while
    writing only the suffix.

    A read-only scan of the project's rows in index order; null-timestamp rows
    never contribute (they are always in the suffix).
    """
    ordered = select(
        Frame.capture_timestamp.label("ts"),
        Frame.id.label("id"),
        func.lag(Frame.capture_timestamp, type_=Frame.capture_timestamp.type)
        .over(order_by=Frame.sequence_index)
        .label("prev_ts"),
        func.lag(Frame.id).over(order_by=Frame.sequence_index).label("prev_id"),
    ).where(Frame.project_id == project_id)
    walk = ordered.subquery()
    return session.execute(
        select(func.min(walk.c.ts)).where(
            walk.c.ts.is_not(None),
            walk.c.prev_id.is_not(None),
            walk.c.prev_ts.is_(None)
            | (walk.c.ts < walk.c.prev_ts)
            | ((walk.c.ts == walk.c.prev_ts) & (walk.c.id < walk.c.prev_id)),
        )
    ).scalar_one_or_none()


def _renumber_bound(
    session: Session, project_id: int, imported_ids: Sequence[int]
) -> datetime | None:
    """Return the timestamp an import's renumber starts from, or ``None``.

    The earliest of the imported frames' stored timestamps (read back from the
    database, so it is in the column's own naive-UTC form) and
    :func:`earliest_out_of_order_timestamp`. The second term covers rows the
    import did not write: a live capture that landed between files with an
    earlier camera clock, or a disorder left by an earlier edit. ``None`` means
    the project is already in order and nothing needs writing.
    """
    imported_min = (
        session.execute(
            select(func.min(Frame.capture_timestamp)).where(
                Frame.id.in_(list(imported_ids))
            )
        ).scalar_one_or_none()
        if imported_ids
        else None
    )
    disorder = earliest_out_of_order_timestamp(session, project_id)
    bounds = [ts for ts in (imported_min, disorder) if ts is not None]
    return min(bounds) if bounds else None


def _resequence_after_import(
    session: Session, project_id: int, imported_ids: Sequence[int]
) -> int:
    """Renumber the suffix an import disturbed; return the rows renumbered.

    The bound comes from :func:`_renumber_bound`; when it is ``None`` the project
    is already in order and nothing is written. One transaction, the caller's:
    for a suffix too large for that, see :func:`resequence_project_chunked`.
    """
    bound = _renumber_bound(session, project_id, imported_ids)
    if bound is None:
        return 0
    return resequence_project_from(session, project_id, bound)


# ---------------------------------------------------------------------------
# Chunked renumber (#145) and order repair after edits (#144)
# ---------------------------------------------------------------------------

# Rows renumbered per write transaction. A suffix of up to this many rows is
# renumbered in the caller's single transaction exactly as before (0.08 s at 5,200
# frames); a larger one is split so the SQLite write lock is released between
# chunks and a live capture waits one chunk (tens of milliseconds), not seconds.
RENUMBER_CHUNK_ROWS = 5_000
# Pause between chunks. SQLite's busy handler polls a locked database, so a
# writer queued behind back-to-back chunk transactions could starve; the pause
# gives it a window to take the lock.
_RENUMBER_CHUNK_PAUSE_SECONDS = 0.005

_renumber_locks: dict[int, threading.RLock] = {}
_renumber_locks_guard = threading.Lock()


def _renumber_lock(project_id: int) -> threading.RLock:
    """Return the per-project lock that serialises whole chunked renumbers.

    Distinct from the project write lock, which a chunked renumber takes (and
    releases) once per chunk: this one is held for the whole run, so two
    renumbers of one project never interleave their phases. Reentrant, so an
    import that holds it for its own final transaction can go on to a chunked
    run.
    """
    with _renumber_locks_guard:
        lock = _renumber_locks.get(project_id)
        if lock is None:
            lock = _renumber_locks[project_id] = threading.RLock()
        return lock


def _claim_write_transaction(session: Session) -> None:
    """Open the SQLite write transaction before the reads that plan a renumber.

    pysqlite begins a transaction at the first DML statement, so reads issued
    before one each see their own snapshot. A no-op ``UPDATE`` (no row matches)
    takes the write lock at once, so everything read afterwards is one snapshot
    that no timestamp edit can change underneath the plan.
    """
    session.execute(text("UPDATE frame SET id = id WHERE id = -1"))


def resequence_project_chunked(
    session_factory: sessionmaker[Session],
    project_id: int,
    *,
    imported_ids: Sequence[int] = (),
    chunk_rows: int | None = None,
) -> int:
    """Renumber a project's out-of-order suffix without one long write lock (#145).

    Same result as :func:`resequence_project_from` over the bound from
    :func:`_renumber_bound` (the earliest of ``imported_ids``' timestamps and the
    first out-of-order row), but a suffix larger than ``chunk_rows`` (default
    :data:`RENUMBER_CHUNK_ROWS`) is written in chunks of that many rows, each its
    own transaction under the project write lock, with the lock released between
    chunks. A suffix that fits in one chunk
    is renumbered in a single transaction, as before. Returns the rows
    renumbered.

    **Why a reader never sees a broken order.** The browser pages in
    ``sequence_index`` space, so every committed intermediate state must still be
    a coherent order. The two phases are therefore ordered:

    * *Park*, highest old index first. Each chunk moves the top of the suffix
      above the project's current maximum, by one constant offset. The rows moved
      so far are always the highest ones and keep their relative order, so the
      project's order is identical to the original after every chunk.
    * *Assign*, earliest target first. Each chunk gives the next rows of the
      sorted suffix their final indexes, which lie below the parked range and
      above the untouched prefix. After every chunk the order is: the prefix, the
      rows already in final position, then the not-yet-assigned rows in their
      old relative order. That is the original disorder shrinking from the front,
      never a new one, and no row is missing or listed twice.
    * *Compact.* A capture that landed mid-run takes ``max + 1``, above the parked
      range, so it ends up above everything with a gap. Those rows are moved
      down, in order, to follow the suffix, so the gap (and the index growth
      repeated runs would cause) does not accumulate.

    The park argument relies on every untouched prefix row sitting below every
    suffix row, which the bound guarantees: it is at most the earliest
    out-of-order timestamp, so no prefix row is inverted against a suffix row.

    Both phases move rows by the ids read in one snapshot, so a concurrent timestamp
    edit cannot strand a row, and a run that stops part way (a crash) leaves a
    state of the kinds above, which the next
    :func:`earliest_out_of_order_timestamp` pass repairs. A per-project lock keeps
    two renumbers from overlapping.

    :raises sqlalchemy.exc.OperationalError: the database stays locked past the
        busy timeout.
    """
    from ..capture.frame_writer import project_write_lock

    chunk_rows = max(1, RENUMBER_CHUNK_ROWS if chunk_rows is None else chunk_rows)
    with _renumber_lock(project_id):
        with project_write_lock(project_id), session_scope(session_factory) as session:
            _claim_write_transaction(session)
            bound = _renumber_bound(session, project_id, imported_ids)
            if bound is None:
                return 0
            in_suffix, start = _suffix_predicate(session, project_id, bound)
            size = session.execute(
                select(func.count()).select_from(Frame).where(in_suffix)
            ).scalar_one()
            if size <= chunk_rows:
                return resequence_project_from(session, project_id, bound)
            # Target order, with each row's current index, from one statement.
            rows = session.execute(
                select(Frame.id, Frame.sequence_index)
                .where(in_suffix)
                .order_by(
                    Frame.capture_timestamp.is_(None),
                    Frame.capture_timestamp.asc(),
                    Frame.id.asc(),
                )
            ).all()
            current_max = session.execute(
                select(func.max(Frame.sequence_index)).where(
                    Frame.project_id == project_id
                )
            ).scalar_one()
            offset = current_max + len(rows) + 1
            parking = sorted(rows, key=lambda row: row.sequence_index, reverse=True)
            _park_rows(session, parking[:chunk_rows], offset)
        parked = parking[chunk_rows:]
        order = [row.id for row in rows]
        del rows, parking

        for begin in range(0, len(parked), chunk_rows):
            time.sleep(_RENUMBER_CHUNK_PAUSE_SECONDS)
            with (
                project_write_lock(project_id),
                session_scope(session_factory) as session,
            ):
                _park_rows(session, parked[begin : begin + chunk_rows], offset)
        for begin in range(0, len(order), chunk_rows):
            time.sleep(_RENUMBER_CHUNK_PAUSE_SECONDS)
            with (
                project_write_lock(project_id),
                session_scope(session_factory) as session,
            ):
                _set_sequence_indexes(
                    session,
                    [
                        (frame_id, index)
                        for index, frame_id in enumerate(
                            order[begin : begin + chunk_rows], start=start + begin
                        )
                    ],
                )
        _compact_tail(session_factory, project_id, start + len(order))
        return len(order)


def _set_sequence_indexes(session: Session, assignments: list[tuple[int, int]]) -> None:
    """Set ``sequence_index`` for each ``(frame_id, index)``, in list order.

    A Core ``executemany`` rather than the ORM's bulk update by primary key: a
    frame deleted since the renumber was planned (a permanent delete between
    chunks) simply matches no row, where the ORM path would raise on the
    mismatch and abort the run part way.
    """
    if not assignments:
        return
    table = cast(Table, Frame.__table__)
    session.connection().execute(
        update(table)
        .where(table.c.id == bindparam("b_id"))
        .values(sequence_index=bindparam("b_index")),
        [{"b_id": frame_id, "b_index": index} for frame_id, index in assignments],
    )


def _park_rows(session: Session, rows: Sequence[Any], offset: int) -> None:
    """Move ``rows`` (``(id, sequence_index)``) up by ``offset``, in one statement."""
    _set_sequence_indexes(
        session, [(row.id, row.sequence_index + offset) for row in rows]
    )


def _compact_tail(
    session_factory: sessionmaker[Session], project_id: int, next_index: int
) -> None:
    """Move rows left above the renumbered range down to follow it, in order.

    These are frames captured while the renumber ran (they took ``max + 1`` above
    the parked range). Ascending order keeps them in order and every target is
    at or below the row's current index, so no write lands on an occupied slot.
    """
    from ..capture.frame_writer import project_write_lock

    while True:
        with project_write_lock(project_id), session_scope(session_factory) as session:
            tail = session.execute(
                select(Frame.id, Frame.sequence_index)
                .where(
                    Frame.project_id == project_id,
                    Frame.sequence_index >= next_index,
                )
                .order_by(Frame.sequence_index.asc())
                .limit(RENUMBER_CHUNK_ROWS)
            ).all()
            _set_sequence_indexes(
                session,
                [
                    (row.id, target)
                    for target, row in enumerate(tail, start=next_index)
                    if target != row.sequence_index
                ],
            )
            next_index += len(tail)
        if len(tail) < RENUMBER_CHUNK_ROWS:
            return


_ORDER_DIRTY_KEY = "tlm_order_dirty_projects"

# A change that moved more frames than this is checked with the full scan: one
# neighbour lookup per frame would cost more than the single pass it replaces.
_TARGETED_CHECK_MAX_FRAMES = 64


def _mark_order_dirty(
    session: Session, project_id: int, frame_id: int | None = None
) -> None:
    """Record that ``session`` moved a frame of ``project_id`` in time (#144).

    ``frame_id`` names the frame that moved so the post-commit check can look at
    its neighbours only (#263). ``None`` means "unknown": the project gets the
    full scan, and a later mark with an id never narrows that back down.
    """
    dirty: dict[int, set[int] | None] = session.info.setdefault(_ORDER_DIRTY_KEY, {})
    if frame_id is None:
        dirty[project_id] = None
        return
    moved = dirty.setdefault(project_id, set())
    if moved is not None:
        moved.add(frame_id)


@event.listens_for(Session, "after_commit")
def _restore_order_after_commit(session: Session) -> None:
    """Repair the browse order of the projects a committed session re-timed (#144).

    A timestamp edit or bulk offset changes ``capture_timestamp`` but not
    ``sequence_index``. Once the change is committed (so this session no longer
    holds the SQLite write lock), each marked project is checked and, if its order
    is now broken, renumbered in transactions of their own. A failure is logged,
    never raised: the edit has already landed, and the next import or edit repairs
    the order.
    """
    dirty = session.info.pop(_ORDER_DIRTY_KEY, None)
    if not dirty:
        return
    factory = sessionmaker(bind=_session_engine(session), expire_on_commit=False)
    for project_id in sorted(dirty):
        try:
            restore_project_order(factory, project_id, dirty[project_id])
        except Exception:
            logger.exception(
                "could not restore the frame order of project %s after a "
                "timestamp change; the next import or edit repairs it",
                project_id,
            )


@event.listens_for(Session, "after_rollback")
def _forget_order_after_rollback(session: Session) -> None:
    """A rolled-back change never happened: drop the session's order marks."""
    session.info.pop(_ORDER_DIRTY_KEY, None)


def _breaks_order(prev: Any, cur: Any) -> bool:
    """Whether ``cur`` sorts before ``prev`` (rows with an ``id`` and a timestamp).

    The same predicate :func:`earliest_out_of_order_timestamp` applies to each
    adjacent pair: a row is flagged only when it has a timestamp and either its
    predecessor has none, or it sorts before it by ``(capture_timestamp, id)``.
    """
    if cur.capture_timestamp is None:
        return False
    if prev.capture_timestamp is None:
        return True
    return (cur.capture_timestamp, cur.id) < (prev.capture_timestamp, prev.id)


def _neighbours_out_of_order(
    session: Session, project_id: int, frame_ids: Sequence[int]
) -> bool:
    """Whether any of ``frame_ids`` sorts out of order against an adjacent row (#263).

    Compares each frame with the rows next to it by ``sequence_index``, two
    indexed lookups per frame instead of a scan of the project. Returns ``True``
    (so the caller falls back to the full scan) when a frame cannot be found, as
    it can no longer be reasoned about.

    Moving a frame in time only changes the adjacent pairs that include it, so
    for a project that was in order before the change this is exactly as
    conclusive as the full scan. It does not see a disorder the change did not
    cause, such as one left by an interrupted import; that is repaired by the
    next import, which still runs the full scan.
    """
    columns = (Frame.id, Frame.sequence_index, Frame.capture_timestamp)
    for frame_id in frame_ids:
        row = session.execute(
            select(*columns).where(Frame.id == frame_id, Frame.project_id == project_id)
        ).first()
        if row is None:
            return True
        before = session.execute(
            select(*columns)
            .where(
                Frame.project_id == project_id,
                Frame.sequence_index < row.sequence_index,
            )
            .order_by(Frame.sequence_index.desc())
            .limit(1)
        ).first()
        if before is not None and _breaks_order(before, row):
            return True
        after = session.execute(
            select(*columns)
            .where(
                Frame.project_id == project_id,
                Frame.sequence_index > row.sequence_index,
            )
            .order_by(Frame.sequence_index.asc())
            .limit(1)
        ).first()
        if after is not None and _breaks_order(row, after):
            return True
    return False


def restore_project_order(
    session_factory: sessionmaker[Session],
    project_id: int,
    frame_ids: Sequence[int] | None = None,
) -> int:
    """Repair a project's ``sequence_index`` order after a timestamp change (#144).

    A single upload, a timestamp edit or a bulk offset moves a frame in time
    without renumbering, which leaves the browse order wrong until the next
    import. This runs the import's own check (:func:`earliest_out_of_order_timestamp`)
    and, only when the order is broken, renumbers the suffix from there with
    :func:`resequence_project_chunked`. Run it after the change has committed,
    never inside the request's transaction: it takes the project write lock, and
    a connection already holding the SQLite write lock must not wait for it. A
    session that edits a timestamp or offsets frames gets this for free through
    :func:`_restore_order_after_commit`. Returns the rows renumbered (``0`` for a
    project already in order).

    ``frame_ids`` names the frames the change moved (#263). When there are few
    of them, a look at their neighbours decides whether anything is out of order,
    and the full scan (linear in the project) runs only when one is. ``None`` or
    a long list always takes the full scan.
    """
    # A read-only look first: most changes leave the order intact, and then
    # neither lock is taken. The locked run re-checks.
    targeted = frame_ids is not None and len(frame_ids) <= _TARGETED_CHECK_MAX_FRAMES
    with session_scope(session_factory) as session:
        if targeted and not _neighbours_out_of_order(
            session, project_id, list(frame_ids or ())
        ):
            return 0
        if earliest_out_of_order_timestamp(session, project_id) is None:
            return 0
    return resequence_project_chunked(session_factory, project_id)


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
    transaction per file, ``origin="uploaded"``), then a single suffix
    re-sequence realigns the project's sequence to chronological order. Like the
    other bulk helpers this is **skip-not-raise**: a file that fails validation
    is recorded in :attr:`skipped` and the batch continues. ``imported`` and ``skipped``
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


# One import file's content: its bytes, or the spooled file holding them. The
# web and API importers pass a ``Path`` (the upload was streamed to disk, #67 P1)
# so the batch is never held in memory; the file is read one at a time.
ImportSource = bytes | Path


def import_frames(
    session_factory: sessionmaker[Session],
    settings: Settings,
    project_id: int,
    files: Sequence[tuple[str, ImportSource]],
    fallback_timestamp: datetime,
    actor_user_id: int,
    *,
    exif_timezone: str | None = None,
) -> ImportResult:
    """Import a batch of image files as uploaded frames, chronologically ordered.

    Each ``(name, source)`` is processed serially, ``source`` being the image's
    bytes or the path of a spooled file that holds them (read here, one file at a
    time, so peak memory is one image). The bytes are validated
    to be a real JPEG or PNG and their dimensions read; the capture time is taken
    from the image's Exif ``DateTimeOriginal`` when present, else it falls back to
    ``fallback_timestamp`` and the frame is flagged ``inferred`` (uploaded bytes
    have no filesystem mtime, so the caller supplies the fallback -- typically the
    upload instant or a client-stated time). The frame is persisted through the
    shared atomic writer with ``origin="uploaded"``.

    Exif stores a local wall-clock time, so it is converted to the naive UTC the
    column holds (#67 R10): by the image's own UTC offset when it carries one,
    else in ``exif_timezone`` (an IANA name from the caller), else in the
    project's schedule timezone. Nothing is converted for a UTC project, which
    keeps the earlier behaviour. Existing frames are never rewritten.

    A single re-sequence runs once at the end (never per file), realigning the
    project's ``sequence_index`` to ``(capture_timestamp, id)`` order so
    imported frames interleave chronologically with captured ones in the browser.
    Only the affected suffix is renumbered -- from the earliest imported
    timestamp, or an earlier out-of-order row if there is one -- so importing a
    recent file into a large project rewrites a handful of rows, not every row
    (#66 C2). A suffix larger than :data:`RENUMBER_CHUNK_ROWS` is renumbered in
    chunks that release the write lock between them (#145, see
    :func:`resequence_project_chunked`). The re-sequence is idempotent and
    re-runnable, so even a crash mid-import only temporarily de-orders the
    browser -- never loses data or mis-orders renders.

    The batch is **skip-not-raise**: a file that is not a readable supported image
    is recorded in :attr:`ImportResult.skipped` with a reason and the batch
    continues.

    Locking (#66 C2): each file's write takes the shared per-project write lock
    for its own ``max + 1`` and insert only (inside :meth:`FrameWriter.write`),
    so a live capture of the same project can land between files instead of
    waiting for the whole batch, and each file is its own short SQLite write
    transaction. The lock is taken again around the final transaction (audit
    events plus the re-sequence), so no capture interleaves with the
    re-sequence's two phases (a chunked renumber takes it once per chunk, see
    :func:`resequence_project_chunked`); a capture that landed between files is
    newer than the import and is included in the renumbered suffix.

    :param exif_timezone: IANA zone name the naive Exif times were taken in;
        ``None`` uses the project's schedule timezone.
    :raises ImportBatchTooLargeError: if ``files`` exceeds
        :data:`MAX_IMPORT_BATCH`; this is a batch-level guard, distinct from the
        per-file skip behaviour.
    :raises InvalidExifTimezoneError: if ``exif_timezone`` is not a known zone.
    """
    if len(files) > MAX_IMPORT_BATCH:
        raise ImportBatchTooLargeError(
            f"import batch of {len(files)} files exceeds the maximum of "
            f"{MAX_IMPORT_BATCH}"
        )
    exif_zone: tzinfo | None = parse_exif_timezone(exif_timezone)
    if exif_zone is None:
        exif_zone = _project_schedule_zone(session_factory, project_id)

    # Imported here (not at module load) to avoid a capture<->storage import
    # cycle, mirroring upload_frame's lazy import.
    from ..capture import FrameWriter

    writer = FrameWriter(session_factory, frames_root(settings))
    result = ImportResult()

    for name, source in files:
        try:
            image_bytes = source if isinstance(source, bytes) else source.read_bytes()
        except OSError as exc:
            logger.warning("could not read spooled import file %r: %s", name, exc)
            result.skipped.append(
                ImportedFile(
                    name=name, status="skipped", reason="could not read the file"
                )
            )
            continue
        record = _import_one_file(
            writer,
            project_id,
            name,
            image_bytes,
            fallback_timestamp,
            settings.capture.max_image_dimension_px,
            exif_zone,
        )
        if record.status == "imported":
            result.imported.append(record)
        else:
            result.skipped.append(record)

    # The per-frame audit events and the single suffix re-sequence over whatever
    # was accepted: one transaction when the suffix is small, else the audit
    # events and then a chunked renumber (#145); see _record_import_and_resequence.
    if result.imported:
        imported_ids = [r.frame_id for r in result.imported if r.frame_id is not None]
        # The renumber lock is held across the final transaction and any chunked
        # run that follows, so no other renumber of this project interleaves.
        with _renumber_lock(project_id):
            chunked = _record_import_and_resequence(
                session_factory,
                project_id,
                result.imported,
                imported_ids,
                actor_user_id,
            )
            if chunked:
                # A suffix too large for one transaction (#145): the audit events
                # are already committed; the write lock is released between chunks.
                resequence_project_chunked(
                    session_factory, project_id, imported_ids=imported_ids
                )

    logger.info(
        "imported frames project=%s imported=%s skipped=%s",
        project_id,
        result.imported_count,
        result.skipped_count,
    )
    return result


def _record_import_and_resequence(
    session_factory: sessionmaker[Session],
    project_id: int,
    imported: Sequence[ImportedFile],
    imported_ids: Sequence[int],
    actor_user_id: int,
) -> bool:
    """Commit an import's audit events and, when small, its suffix renumber.

    One transaction under the project lock (taken before the session, the same
    order :meth:`FrameWriter.write` uses). The frame writes already committed (one
    transaction each); a crash before this point leaves recorded frames without
    audit rows, consistent with keep-all, and the next renumber repairs the order.

    The audit events are flushed first, on purpose: their INSERT opens the SQLite
    write transaction, so every read of the renumber (the order walk, the bounds,
    the suffix ids) runs inside it. A timestamp edit or offset (which do not take
    the project lock) then waits on the busy timeout instead of moving a row
    across the bound between the suffix read and the park, which would strand a
    parked row or collide on UNIQUE in the assign.

    Returns ``True`` when the suffix exceeds :data:`RENUMBER_CHUNK_ROWS` and was
    left for :func:`resequence_project_chunked`; otherwise it was renumbered here.
    """
    from ..capture.frame_writer import project_write_lock

    with project_write_lock(project_id), session_scope(session_factory) as session:
        ensure_sentinel_admin(session)
        session.add_all(
            [
                _project_event(
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
                for record in imported
            ]
        )
        session.flush()
        bound = _renumber_bound(session, project_id, imported_ids)
        if bound is None:
            return False
        in_suffix, _start = _suffix_predicate(session, project_id, bound)
        size = session.execute(
            select(func.count()).select_from(Frame).where(in_suffix)
        ).scalar_one()
        if size > RENUMBER_CHUNK_ROWS:
            return True
        resequence_project_from(session, project_id, bound)
        return False


# An IANA zone name as the import override accepts it: letters, digits and the
# few separators real names use. Checked before the name reaches ``ZoneInfo``,
# which resolves it against the tz database on disk.
_ZONE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+){0,2}$")
_MAX_ZONE_NAME_LENGTH = 64


class InvalidExifTimezoneError(ValueError):
    """The import's ``exif_timezone`` is not a known IANA timezone name."""


def parse_exif_timezone(name: str | None) -> ZoneInfo | None:
    """Return the zone for an import's ``exif_timezone``, or None when unset.

    A blank value counts as unset (an empty form field).

    :raises InvalidExifTimezoneError: the name is not a known IANA zone.
    """
    if name is None or not name.strip():
        return None
    text = name.strip()
    if len(text) > _MAX_ZONE_NAME_LENGTH or _ZONE_NAME_PATTERN.match(text) is None:
        raise InvalidExifTimezoneError(
            "exif_timezone must be an IANA timezone name such as America/New_York"
        )
    try:
        return ZoneInfo(text)
    except (KeyError, ValueError, OSError) as exc:
        raise InvalidExifTimezoneError(f"unknown timezone {text!r}") from exc


def _project_schedule_zone(
    session_factory: sessionmaker[Session], project_id: int
) -> tzinfo | None:
    """Return the project's schedule timezone, or None for none/unreadable.

    Read-only use of the schedule parser (it is never written here). A project
    that is missing or has a malformed schedule yields None, so its Exif times
    are taken as UTC exactly as before -- the per-file write still reports a
    missing project.
    """
    from ..capture.schedule import parse_schedule

    with session_scope(session_factory) as session:
        raw = session.execute(
            select(Project.schedule).where(Project.id == project_id)
        ).scalar_one_or_none()
    if raw is None:
        return None
    try:
        return parse_schedule(raw).tzinfo()
    except ValueError:
        return None


def resolve_exif_instant(exif: datetime, zone: tzinfo | None) -> datetime:
    """Return an Exif capture time as the naive UTC the frame column stores.

    An aware value (the image carried its own UTC offset) is converted. A naive
    one is a local wall-clock time: it is read in ``zone`` when given, and kept
    as it is (the earlier behaviour: taken as UTC) when not.

    :raises OverflowError: the shift leaves the representable date range.
    """
    if exif.tzinfo is None:
        if zone is None:
            return exif
        exif = exif.replace(tzinfo=zone)
    return exif.astimezone(UTC).replace(tzinfo=None)


def _import_one_file(
    writer: FrameWriter,
    project_id: int,
    name: str,
    image_bytes: bytes,
    fallback_timestamp: datetime,
    max_dimension_px: int,
    exif_zone: tzinfo | None = None,
) -> ImportedFile:
    """Validate and persist one import file, returning its per-file record.

    Skip-not-raise: a file that is not a readable supported image, or whose write
    fails, yields a ``skipped`` record with a reason rather than aborting the
    batch. The Exif capture time is preferred (resolved to naive UTC by
    :func:`resolve_exif_instant`); its absence flags ``inferred`` and falls back
    to ``fallback_timestamp``.
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
    if width > max_dimension_px or height > max_dimension_px:
        return ImportedFile(
            name=name,
            status="skipped",
            reason=(
                f"image dimensions {width}x{height} exceed the maximum of "
                f"{max_dimension_px}px per axis"
            ),
        )

    exif_timestamp = read_capture_timestamp(image_bytes)
    inferred = exif_timestamp is None
    if exif_timestamp is None:
        capture_timestamp = fallback_timestamp
    else:
        try:
            capture_timestamp = resolve_exif_instant(exif_timestamp, exif_zone)
        except (OverflowError, ValueError):
            # A date at the edge of the range that the zone shift pushes out of
            # it: treat it as unreadable, like a malformed Exif time.
            inferred = True
            capture_timestamp = fallback_timestamp

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
    encoder gather (:func:`~timelapse_manager.encode.frame_source.gather_frames`,
    which streams the same filters and order as a column query) adds
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


def latest_captured_at(session: Session, project_id: int) -> datetime | None:
    """Return when the project last captured a frame itself, or ``None``.

    The newest ``capture_timestamp`` among the project's active frames that the
    app captured (interval, exact-time anchor or event -- ``origin='captured'``).
    Imported/uploaded history does not count as a capture. Read from the
    database so it survives restarts (issue #5).
    """
    return session.execute(
        select(func.max(Frame.capture_timestamp))
        .where(Frame.project_id == project_id)
        .where(Frame.lifecycle_state == "active")
        .where(Frame.origin == "captured")
    ).scalar_one_or_none()


@dataclass(frozen=True)
class FrameAggregates:
    """One project's frame figures for a status card, from the batched reads.

    ``last_captured_at`` matches :func:`latest_captured_at`, ``disk_usage_bytes``
    matches :func:`sum_project_disk_usage` and ``latest_frame_id`` is the id of
    :func:`latest_active_frame`. A project with no active frames gets the
    defaults (``None``, ``0``, ``None``).
    """

    last_captured_at: datetime | None = None
    disk_usage_bytes: int = 0
    latest_frame_id: int | None = None


def project_frame_aggregates(
    session: Session, project_ids: Sequence[int]
) -> dict[int, FrameAggregates]:
    """Return :class:`FrameAggregates` for every id in ``project_ids``.

    Two statements in total, whatever the number of projects (#66 C5), instead
    of three per project:

    * one grouped aggregate over the active frames: the newest
      ``capture_timestamp`` among app captures (``origin='captured'``) and the
      summed ``file_size_bytes``. Every column it reads is in
      ``ix_frame_project_state_origin_ts_size``, so it never reads a table row.
    * one preview lookup: per project, the active frame with the highest
      ``sequence_index`` -- the same rule as :func:`latest_active_frame`, *not*
      the highest id, because an import of historic photos gets high ids with
      old timestamps. Each lookup walks ``uq_frame_project_seq`` backwards and
      stops at the first active row.

    Every requested id is present in the result; a project with no active
    frames maps to the defaults.
    """
    ids = sorted(set(project_ids))
    if not ids:
        return {}
    aggregate_rows = session.execute(
        select(
            Frame.project_id,
            func.max(case((Frame.origin == "captured", Frame.capture_timestamp))),
            func.coalesce(func.sum(Frame.file_size_bytes), 0),
        )
        .where(Frame.project_id.in_(ids))
        .where(Frame.lifecycle_state == "active")
        .group_by(Frame.project_id)
    ).all()
    latest = (
        select(Frame.id)
        .where(Frame.project_id == Project.id)
        .where(Frame.lifecycle_state == "active")
        .order_by(Frame.sequence_index.desc())
        .limit(1)
        .correlate(Project)
        .scalar_subquery()
    )
    preview_rows = (
        session.execute(select(Project.id, latest).where(Project.id.in_(ids)))
        .tuples()
        .all()
    )

    totals = {
        project_id: (last_captured, int(size or 0))
        for project_id, last_captured, size in aggregate_rows
    }
    previews: dict[int, int | None] = dict(preview_rows)
    result: dict[int, FrameAggregates] = {}
    for project_id in ids:
        last_captured, size = totals.get(project_id, (None, 0))
        result[project_id] = FrameAggregates(
            last_captured_at=last_captured,
            disk_usage_bytes=size,
            latest_frame_id=previews.get(project_id),
        )
    return result


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


# The gap finder and the ribbon (``web/routers/dashboard.py``) both look at the
# first this-many timed active frames, so the jump buttons and the ribbon's gap
# markers are computed from the same frame population.
GAP_SCAN_LIMIT = 5000

# Slack, in seconds, on the SQL pre-filter of :func:`list_capture_gaps`.
# ``julianday`` differences are floats (about 10 us of error); the exact
# comparison is redone in Python on the few rows that pass.
_GAP_PREFILTER_SLACK_SECONDS = 1.0


def _gap_scan_population(project_id: int, limit: int) -> Any:
    """The first ``limit`` timed active frames of a project, oldest first.

    Served by ``ix_frame_project_ts`` in index order (the state filter reads
    the row), with no sort step.
    """
    return (
        select(Frame.capture_timestamp.label("ts"))
        .where(
            Frame.project_id == project_id,
            Frame.capture_timestamp.is_not(None),
            Frame.lifecycle_state == "active",
        )
        .order_by(Frame.capture_timestamp.asc())
        .limit(limit)
        .subquery()
    )


@dataclass(frozen=True)
class FrameTimeBounds:
    """The first and last timestamps (and count) of the gap-scan population."""

    first: datetime
    last: datetime
    count: int


def active_frame_time_bounds(
    session: Session, project_id: int, *, limit: int = GAP_SCAN_LIMIT
) -> FrameTimeBounds | None:
    """Return the bounds of the frames the gap finder scans, or ``None``.

    The population is the first ``limit`` timed active frames (the ribbon's
    population). ``first`` is the span start for a project without a
    ``start_date``; ``last`` is the default reference for a gap step. One
    aggregate over an index range; no timestamps are returned to Python.
    """
    population = _gap_scan_population(project_id, limit)
    row = session.execute(
        select(
            func.min(population.c.ts),
            func.max(population.c.ts),
            func.count(),
        )
    ).one()
    first, last, count = row
    if first is None or last is None:
        return None
    return FrameTimeBounds(first=first, last=last, count=int(count))


def list_capture_gaps(
    session: Session,
    project_id: int,
    span_start: datetime,
    span_end: datetime,
    *,
    min_gap_fraction: float = 0.04,
    limit: int = GAP_SCAN_LIMIT,
) -> list[CaptureGap]:
    """Find a project's capture gaps in SQL (#66 C7).

    Equivalent to ``find_capture_gaps(<first limit timed active timestamps>,
    span_start, span_end, min_gap_fraction=...)``, but the consecutive-pair walk
    runs in SQLite as a ``LAG`` window over ``ix_frame_project_ts``, so only the
    gaps -- a handful of rows, since gaps above a fraction of the span cannot be
    many -- come back to Python instead of every timestamp of the project.

    The SQL filter is loose by :data:`_GAP_PREFILTER_SLACK_SECONDS`; the strict
    ``> threshold`` test is then applied in Python with exact datetime
    arithmetic, so the result matches :func:`find_capture_gaps` to the
    microsecond. Returned timestamps are the stored naive UTC values, in
    ascending order.
    """
    if span_end <= span_start:
        return []
    threshold = (span_end - span_start).total_seconds() * min_gap_fraction
    population = _gap_scan_population(project_id, limit)
    pairs = select(
        func.lag(population.c.ts, type_=Frame.capture_timestamp.type)
        .over(order_by=population.c.ts)
        .label("before"),
        population.c.ts.label("after"),
    ).subquery()
    elapsed = (func.julianday(pairs.c.after) - func.julianday(pairs.c.before)) * 86400.0
    # No outer ORDER BY: the few rows that pass are sorted here, which keeps
    # the statement free of a sort step.
    rows = session.execute(
        select(pairs.c.before, pairs.c.after)
        .where(pairs.c.before.is_not(None))
        .where(elapsed > threshold - _GAP_PREFILTER_SLACK_SECONDS)
    ).tuples()
    return sorted(
        (
            CaptureGap(before=before, after=after)
            for before, after in rows
            if (after - before).total_seconds() > threshold
        ),
        key=lambda gap: (gap.after, gap.before),
    )


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


# --- Predominant-dimensions cache (#66 C7) ---------------------------------
#
# ``predominant_dimensions`` is asked once per uploaded frame and once per API
# frame listing. Computing it is a GROUP BY over every active frame of the
# project, so a scripted upload of N frames used to cost O(N^2). The cache keeps
# each project's ``(width, height) -> count`` histogram of active frames,
# together with the key it was computed at: ``(MAX(frame.id), project.frame_count)``.
#
# * Same key: a hit, no frame rows read.
# * New frames only (the max id grew and ``frame_count`` grew by exactly the
#   number of rows above the cached max id): the histogram is extended with just
#   those rows, read by primary-key range. This is the upload and capture path.
# * Anything else (a permanent delete, rows inserted without the counter): a
#   full recompute.
#
# Soft-delete and restore change the active set without changing the key, so
# they mark the project dirty on their session; an ``after_commit`` hook then
# drops the entry and bumps the project's generation. A computation that
# started before the bump is not stored, so a stale histogram never outlives
# the commit that made it stale. Until that commit, the mutating session itself
# bypasses the cache and computes fresh, so it sees its own changes.
#
# The cache is per database engine (weakly keyed), so two databases in one
# process -- the test suite, or a migration next to the app -- never share
# project ids. It is a display hint only: a dimension mismatch badge.

_DIMS_CACHE_MAX_PROJECTS = 256
_DIMS_DIRTY_KEY = "tlm_dimensions_dirty_projects"


@dataclass
class _DimensionsEntry:
    """One project's cached active-frame size histogram and the key it is valid at."""

    max_frame_id: int | None
    frame_count: int
    histogram: Counter[tuple[int, int]]


@dataclass
class _EngineDimensionsCache:
    """The per-engine cache: LRU entries plus a generation counter per project."""

    entries: OrderedDict[int, _DimensionsEntry] = field(default_factory=OrderedDict)
    generations: dict[int, int] = field(default_factory=dict)


_dims_lock = threading.Lock()
_dims_caches: weakref.WeakKeyDictionary[Engine, _EngineDimensionsCache] = (
    weakref.WeakKeyDictionary()
)


def _dims_cache_for(engine: Engine) -> _EngineDimensionsCache:
    """Return ``engine``'s cache, creating it on first use. Call under the lock."""
    cache = _dims_caches.get(engine)
    if cache is None:
        cache = _EngineDimensionsCache()
        _dims_caches[engine] = cache
    return cache


def _session_engine(session: Session) -> Engine:
    """Return the engine behind ``session`` (its bind may be a connection)."""
    return session.get_bind().engine


def _mark_dimensions_dirty(session: Session, project_id: int) -> None:
    """Record that ``session`` changed ``project_id``'s active frame set."""
    dirty: set[int] = session.info.setdefault(_DIMS_DIRTY_KEY, set())
    dirty.add(project_id)


def invalidate_predominant_dimensions(engine: Engine, project_id: int) -> None:
    """Drop ``project_id``'s cached dimensions and fence off in-flight computes."""
    with _dims_lock:
        cache = _dims_cache_for(engine)
        cache.entries.pop(project_id, None)
        cache.generations[project_id] = cache.generations.get(project_id, 0) + 1


@event.listens_for(Session, "after_commit")
def _invalidate_dimensions_after_commit(session: Session) -> None:
    """Invalidate the projects a just-committed session marked dirty."""
    dirty = session.info.pop(_DIMS_DIRTY_KEY, None)
    if not dirty:
        return
    engine = _session_engine(session)
    for project_id in dirty:
        invalidate_predominant_dimensions(engine, project_id)


@event.listens_for(Session, "after_rollback")
def _forget_dimensions_after_rollback(session: Session) -> None:
    """A rolled-back change never happened: drop the session's dirty marks."""
    session.info.pop(_DIMS_DIRTY_KEY, None)


def _dimension_histogram(
    session: Session,
    project_id: int,
    *,
    after_id: int | None = None,
    up_to_id: int | None = None,
) -> Counter[tuple[int, int]]:
    """Count a project's active frames by ``(width, height)`` in an id range."""
    stmt = (
        select(Frame.width, Frame.height, func.count())
        .where(Frame.project_id == project_id)
        .where(Frame.lifecycle_state == "active")
        .where(Frame.width.is_not(None))
        .where(Frame.height.is_not(None))
        .group_by(Frame.width, Frame.height)
    )
    if after_id is not None:
        stmt = stmt.where(Frame.id > after_id)
    if up_to_id is not None:
        stmt = stmt.where(Frame.id <= up_to_id)
    histogram: Counter[tuple[int, int]] = Counter()
    for width, height, count in session.execute(stmt).tuples():
        if width is not None and height is not None:  # filtered in SQL
            histogram[(width, height)] = count
    return histogram


def _histogram_mode(histogram: Counter[tuple[int, int]]) -> tuple[int, int] | None:
    """Return the most common size; ties go to the larger width, then height."""
    counted = [(n, size) for size, n in histogram.items() if n > 0]
    if not counted:
        return None
    _, size = max(counted, key=lambda item: (item[0], item[1][0], item[1][1]))
    return size


def _extend_histogram(
    session: Session,
    project_id: int,
    entry: _DimensionsEntry,
    max_frame_id: int,
    frame_count: int,
) -> Counter[tuple[int, int]] | None:
    """Fold the frames added since ``entry`` into a copy of its histogram.

    Returns ``None`` when the change is not "new frames only": the counter did
    not grow by exactly the number of rows above the cached max id (a permanent
    delete happened, or rows were inserted without the counter). The caller
    then recomputes from scratch.
    """
    added = session.execute(
        select(func.count()).where(
            Frame.project_id == project_id,
            Frame.id > (entry.max_frame_id or 0),
            Frame.id <= max_frame_id,
        )
    ).scalar_one()
    if frame_count - entry.frame_count != added:
        return None
    histogram = Counter(entry.histogram)
    histogram.update(
        _dimension_histogram(
            session, project_id, after_id=entry.max_frame_id, up_to_id=max_frame_id
        )
    )
    return histogram


def predominant_dimensions(session: Session, project_id: int) -> tuple[int, int] | None:
    """Return the most common ``(width, height)`` among a project's active frames.

    This is the reference a frame's :func:`dimension_mismatch` is judged against:
    a render needs uniform input dimensions, so the baseline is whatever size the
    bulk of the project's frames share. Ties are broken by larger width then
    larger height, making the result deterministic. Returns ``None`` when the
    project has no active frame with known dimensions, in which case no frame can
    be considered mismatched.

    Cached per project (see the cache notes above, #66 C7): a repeat call with
    no new frames reads one key row and no frame rows, and a call after new
    frames reads only those frames. A session that has itself soft-deleted,
    restored or permanently deleted frames of the project computes fresh, so it
    sees its own uncommitted change.
    """
    if project_id in session.info.get(_DIMS_DIRTY_KEY, ()):
        return _histogram_mode(_dimension_histogram(session, project_id))

    key = session.execute(
        select(
            Project.frame_count,
            select(func.max(Frame.id))
            .where(Frame.project_id == project_id)
            .scalar_subquery(),
        ).where(Project.id == project_id)
    ).first()
    if key is None:
        return None
    frame_count = int(key[0] or 0)
    max_frame_id: int | None = key[1]

    engine = _session_engine(session)
    with _dims_lock:
        cache = _dims_cache_for(engine)
        generation = cache.generations.get(project_id, 0)
        entry = cache.entries.get(project_id)
        if entry is not None:
            cache.entries.move_to_end(project_id)
    if (
        entry is not None
        and entry.max_frame_id == max_frame_id
        and entry.frame_count == frame_count
    ):
        return _histogram_mode(entry.histogram)

    histogram: Counter[tuple[int, int]] | None = None
    if (
        entry is not None
        and max_frame_id is not None
        and (entry.max_frame_id is None or max_frame_id > entry.max_frame_id)
    ):
        histogram = _extend_histogram(
            session, project_id, entry, max_frame_id, frame_count
        )
    if histogram is None:
        histogram = (
            Counter()
            if max_frame_id is None
            else _dimension_histogram(session, project_id, up_to_id=max_frame_id)
        )

    with _dims_lock:
        cache = _dims_cache_for(engine)
        if cache.generations.get(project_id, 0) == generation:
            cache.entries[project_id] = _DimensionsEntry(
                max_frame_id=max_frame_id,
                frame_count=frame_count,
                histogram=histogram,
            )
            cache.entries.move_to_end(project_id)
            while len(cache.entries) > _DIMS_CACHE_MAX_PROJECTS:
                cache.entries.popitem(last=False)
    return _histogram_mode(histogram)


def dimension_mismatch(frame: Frame, predominant: tuple[int, int] | None) -> bool:
    """Return whether ``frame`` differs from the project's predominant dimensions.

    Computed at read time, never stored. A frame is mismatched only when it has
    known dimensions that differ from the supplied baseline. A frame with unknown
    dimensions, or any frame when there is no baseline, is not mismatched.
    """
    if predominant is None or frame.width is None or frame.height is None:
        return False
    return (frame.width, frame.height) != predominant
