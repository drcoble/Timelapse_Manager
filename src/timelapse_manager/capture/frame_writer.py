"""Atomic frame persistence: image bytes to disk, then a ``Frame`` row.

A single :class:`FrameWriter` is the one place that turns a
:class:`~timelapse_manager.cameras.CapturedFrame` into a durable, recorded frame.
Both the scheduled capture loop and the manual-capture endpoint go through it, so
the ordering and atomicity guarantees exist exactly once:

1. The image bytes are written to a temporary file in the *destination
   directory* (same filesystem as the final file), flushed and ``fsync``-ed, and
   atomically moved into place with :func:`os.replace`.
2. Only after the file is durably on disk is the ``Frame`` row inserted.

Writing the file before the row means a crash can at worst leave an orphan file
(harmless, reclaimable), never a row pointing at a missing file. The sequence
index is the project's current maximum plus one, computed in the same
transaction as the insert; the ``(project_id, sequence_index)`` uniqueness
constraint is the backstop that prevents two writers from clobbering a slot.

All database work here is synchronous (the application uses a synchronous
engine). Async callers must invoke :meth:`write` via a thread executor (e.g.
:func:`asyncio.to_thread`) so the event loop is never blocked.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ..cameras import CapturedFrame
from ..db.models import Frame, Project
from ..db.session import session_scope
from ..storage import paths

logger = logging.getLogger(__name__)

# Map an image format to a file extension; default to the format itself.
_EXTENSIONS = {"jpeg": "jpg", "jpg": "jpg", "png": "png"}

# Per-project write serialization. ``sequence_index`` is assigned as the
# project's current ``max + 1`` (see ``_next_sequence_index``); a re-sequence
# (frame import) lowers that max while it renumbers, so a live capture reading a
# stale max concurrently could collide or interleave between the re-sequence's
# two phases. Both the writer's max-read+insert and the import re-sequence
# acquire the same per-project lock, which fully serializes them because the
# application is a single process (the CLI talks to the server over the local
# API rather than opening its own database connection). The registry hands out
# one ``RLock`` per project id, created on first use under a guard lock; the
# ``RLock`` is reentrant so an importer may hold it across the whole batch while
# each nested ``write`` re-acquires it on the same thread.
_project_locks: dict[int, threading.RLock] = {}
_project_locks_guard = threading.Lock()


def project_write_lock(project_id: int) -> threading.RLock:
    """Return the shared per-project write lock, creating it on first use.

    The single point of coordination between :meth:`FrameWriter.write` and the
    import re-sequence. Reentrant so the import path can hold it across the batch
    while each :meth:`FrameWriter.write` it calls re-acquires it on the same
    thread without deadlocking.
    """
    with _project_locks_guard:
        lock = _project_locks.get(project_id)
        if lock is None:
            lock = threading.RLock()
            _project_locks[project_id] = lock
        return lock


@dataclass
class WrittenFrame:
    """The outcome of persisting one captured frame.

    :param frame_id: primary key of the inserted ``Frame`` row.
    :param project_id: the project the frame belongs to.
    :param sequence_index: the frame's position in the project sequence.
    :param file_path: absolute path to the durable image file. (The ``Frame``
        row persists a path relative to the project's frame directory for
        default-layout projects; this in-memory field stays absolute so callers
        can open the file directly.)
    :param width: pixel width recorded for the frame.
    :param height: pixel height recorded for the frame.
    :param file_size_bytes: size of the written file in bytes.
    :param captured_at: timezone-aware UTC instant the frame was captured.
    :param project_frame_count: the project's active frame count *after* this
        write (the just-incremented value), so a caller can enforce a frame-count
        cap without a second query.
    """

    frame_id: int
    project_id: int
    sequence_index: int
    file_path: str
    width: int
    height: int
    file_size_bytes: int
    captured_at: datetime
    project_frame_count: int = 0


class FrameWriter:
    """Persists captured frames atomically: file first, then row.

    Construction is cheap and does no I/O; the writer holds only the session
    factory and the fallback frames root used when a project has no configured
    storage path.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        frames_root: Path,
    ) -> None:
        """Create a writer.

        :param session_factory: factory for synchronous ORM sessions.
        :param frames_root: base directory used to derive a per-project
            destination when the project has no ``storage_path`` of its own.
        """
        self._session_factory = session_factory
        self._frames_root = frames_root

    def destination_dir(self, project: Project) -> Path:
        """Return the directory frames for ``project`` are written to.

        Honours the project's configured ``storage_path``; falls back to a
        per-project sub-directory under the configured frames root. Delegates to
        the shared path layer so the physical layout has one definition.
        """
        return paths.frame_dir_under_root(self._frames_root, project)

    def write(
        self,
        project_id: int,
        captured: CapturedFrame,
        origin: str = "captured",
        capture_timestamp: datetime | None = None,
        *,
        stream_id: str | None = None,
        capture_timestamp_inferred: bool = False,
        capture_reason: str | None = None,
    ) -> WrittenFrame:
        """Persist ``captured`` for ``project_id`` and return its record.

        Synchronous; call from async code via a thread executor. The file is
        written and fsynced before the row is inserted, and the sequence index
        is computed and the row inserted in a single transaction.

        The default arguments reproduce scheduled-capture behaviour exactly: the
        frame is recorded as ``origin="captured"`` with the camera's own
        ``captured.captured_at`` timestamp. An uploaded frame passes
        ``origin="uploaded"`` and a caller-supplied ``capture_timestamp`` (the
        user-stated time the image was taken). The sequence index is always the
        project's current maximum plus one regardless of origin, so uploads and
        captures share one monotonic, never-overwriting sequence.

        :param origin: ``"captured"`` for a live capture, ``"uploaded"`` for an
            imported image.
        :param capture_timestamp: the instant to record for the frame; defaults
            to the captured frame's own timestamp when not supplied.
        :param stream_id: the identifier of the camera stream this frame was
            captured from, recorded as the frame's provenance. Keyword-only and
            defaulting to ``None`` so the upload path (which has no stream) and
            existing callers are unaffected. The scene metadata carried on
            ``captured`` is persisted regardless of this argument.
        :param capture_timestamp_inferred: ``True`` when ``capture_timestamp`` was
            inferred (an import whose bytes carried no readable Exif capture time
            fell back to a supplied instant) rather than read from the image or a
            live capture. Keyword-only and defaulting to ``False`` so existing
            callers are unaffected.
        :param capture_reason: why this frame was captured, recorded as the
            frame's provenance on the unified one-shot path (e.g.
            ``"anchor:clock"`` or ``"event:<topic>"``). Keyword-only and
            defaulting to ``None`` (no recorded reason) so the interval and upload
            paths and existing callers are unaffected.
        :raises ValueError: if the project does not exist.

        The file is named by a collision-free token (a UUID) rather than by
        ``sequence_index`` so a later re-sequence -- which lowers the project's
        max -- can never make a subsequent write's filename collide with an
        existing file. The stored path is the basename, so reads are unaffected.
        The max-read and insert run under the shared per-project write lock so an
        import re-sequence cannot interleave with the ``max + 1`` assignment.
        """
        effective_timestamp = (
            capture_timestamp if capture_timestamp is not None else captured.captured_at
        )
        with (
            project_write_lock(project_id),
            session_scope(self._session_factory) as session,
        ):
            project = session.get(Project, project_id)
            if project is None:
                raise ValueError(f"project {project_id} does not exist")
            destination = self.destination_dir(project)
            sequence_index = self._next_sequence_index(session, project_id)

            destination.mkdir(parents=True, exist_ok=True)
            file_name = f"{uuid4().hex}.{_extension(captured.format)}"
            final_path = destination / file_name
            _atomic_write(final_path, captured.image_bytes)

            naive_captured_at = _to_naive_utc(effective_timestamp)
            stored_path = paths.to_stored(project, final_path)
            frame = Frame(
                project_id=project_id,
                sequence_index=sequence_index,
                capture_timestamp=naive_captured_at,
                file_path=stored_path,
                width=captured.width,
                height=captured.height,
                file_size_bytes=len(captured.image_bytes),
                capture_status="captured",
                origin=origin,
                lifecycle_state="active",
                stream_id=stream_id,
                capture_timestamp_inferred=capture_timestamp_inferred,
                capture_reason=capture_reason,
                scene_metadata=captured.scene_metadata,
            )
            session.add(frame)
            project.frame_count = (project.frame_count or 0) + 1
            session.flush()
            frame_id = frame.id
            project_frame_count = project.frame_count

        logger.info(
            "wrote frame project=%s seq=%s path=%s",
            project_id,
            sequence_index,
            final_path,
        )
        return WrittenFrame(
            frame_id=frame_id,
            project_id=project_id,
            sequence_index=sequence_index,
            file_path=str(final_path),
            width=captured.width,
            height=captured.height,
            file_size_bytes=len(captured.image_bytes),
            captured_at=effective_timestamp,
            project_frame_count=project_frame_count,
        )

    @staticmethod
    def _next_sequence_index(session: Session, project_id: int) -> int:
        """Return one past the project's current maximum sequence index."""
        current_max = session.execute(
            select(func.max(Frame.sequence_index)).where(Frame.project_id == project_id)
        ).scalar_one_or_none()
        return (current_max or 0) + 1


def _extension(image_format: str) -> str:
    """Return a filename extension for an image format string."""
    return _EXTENSIONS.get(image_format.lower(), image_format.lower())


def _to_naive_utc(value: datetime) -> datetime:
    """Return ``value`` as a naive UTC datetime for the naive ``DateTime`` column.

    The capture layer hands back timezone-aware UTC instants; the frame table
    stores naive datetimes, so the offset is normalised to UTC and dropped to
    avoid mixing aware and naive values on read-back.
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _atomic_write(final_path: Path, data: bytes) -> None:
    """Write ``data`` to ``final_path`` atomically and durably.

    Bytes go to a temporary file in the same directory, are flushed and
    ``fsync``-ed, then moved into place with :func:`os.replace` (atomic on the
    same filesystem). The temporary file is removed on any failure so a partial
    write never lingers.
    """
    directory = final_path.parent
    fd, tmp_name = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, final_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
