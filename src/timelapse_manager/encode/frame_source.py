"""Gather a project's renderable frames into an ordered :class:`FrameSequence`.

The encoder works from resolved, on-disk paths, so this bridges the database to
that view: it reads the project's *active*, non-excluded frames in capture order
(the same filters and order as the shared listing
:func:`~timelapse_manager.storage.frames.list_frames`) and resolves each stored
path to an absolute location through the storage resolver.

Frames without a usable capture timestamp or file path cannot be placed on the
timeline and are skipped; the result is exactly the set of frames the encoder can
render, in the order it will render them.

Every path is checked against the project's frame directories before it reaches
the encoder (#67 S16): a stored path that resolves elsewhere (a traversal, an
absolute escape, a symlink out of the tree) is left out with a WARNING, and the
real path of each kept frame is what the encoder opens.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from ..db.models import Frame, Project
from ..storage.paths import (
    PathContainmentError,
    ProjectRef,
    frame_boundaries,
    resolve_contained_frame_path,
)
from .encoder import FrameRef, FrameSequence

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ..config import Settings

# Rows fetched per batch while streaming the gather. A months-long project can
# hold tens of thousands of frames; one query streams them in batches (#66 C6),
# instead of an OFFSET page loop that re-walks every skipped row per page.
_PAGE_SIZE = 1000

logger = logging.getLogger(__name__)


def _as_utc(value: datetime) -> datetime:
    """Return a naive (assumed-UTC) or aware timestamp as tz-aware UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def gather_frames(
    session: Session, settings: Settings, project_id: int
) -> FrameSequence:
    """Return the ordered, active, render-ready frames for a project.

    Only ``active`` frames are included (soft-deleted frames are left out).
    Frames the user has excluded from rendering are also omitted here -- this is
    the single query that honours that flag (``excluded_at IS NULL``);
    everywhere else excluded frames stay visible.
    Frames are ordered by capture timestamp, and each stored path is resolved to
    its real filesystem path. Frames missing a capture timestamp or file path
    are skipped, since neither the timeline nor the input can be built without
    them. A frame whose path resolves outside the project's frame directories is
    skipped too, with one WARNING per gather giving the count and the first
    example (#67 S16).

    One column query, streamed in batches, with no ORM instances, so the
    session's identity map stays empty however long the project is (#66 C6).
    ``ix_frame_project_ts`` matches its ORDER BY, so SQLite does not sort.
    """
    stmt = (
        select(
            Frame.sequence_index,
            Frame.capture_timestamp,
            Frame.file_path,
            Frame.width,
            Frame.height,
        )
        .where(Frame.project_id == project_id)
        .where(Frame.lifecycle_state == "active")
        .where(Frame.excluded_at.is_(None))
        .order_by(Frame.capture_timestamp.asc(), Frame.sequence_index.asc())
        .execution_options(yield_per=_PAGE_SIZE)
    )
    storage_path = session.execute(
        select(Project.storage_path).where(Project.id == project_id)
    ).scalar_one_or_none()
    project = ProjectRef(id=project_id, storage_path=storage_path)
    boundaries = frame_boundaries(settings, project)

    refs: list[FrameRef] = []
    escaped = 0
    first_escaped: tuple[int, str] | None = None
    for sequence_index, captured_at, file_path, width, height in session.execute(stmt):
        if captured_at is None or file_path is None:
            continue
        try:
            real_path = resolve_contained_frame_path(
                settings, project, file_path, boundaries=boundaries
            )
        except PathContainmentError:
            escaped += 1
            if first_escaped is None:
                first_escaped = (sequence_index, file_path)
            continue
        refs.append(
            FrameRef(
                sequence_index=sequence_index,
                capture_timestamp=_as_utc(captured_at),
                absolute_path=real_path,
                width=width,
                height=height,
            )
        )
    if first_escaped is not None:
        logger.warning(
            "render of project %s: skipped %d frame(s) whose path is outside the "
            "project's frame directory (first: sequence %s, stored path %r)",
            project_id,
            escaped,
            first_escaped[0],
            first_escaped[1],
        )
    return FrameSequence(project_id=project_id, frames=refs)
