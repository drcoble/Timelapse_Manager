"""Gather a project's renderable frames into an ordered :class:`FrameSequence`.

The encoder works from resolved, on-disk paths, so this bridges the database to
that view: it reads the project's *active* frames in capture order (reusing the
shared frame listing, which already excludes soft-deleted rows and orders by
capture timestamp) and resolves each stored path to an absolute location through
the storage resolver.

Frames without a usable capture timestamp or file path cannot be placed on the
timeline and are skipped; the result is exactly the set of frames the encoder can
render, in the order it will render them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ..storage import resolve_absolute
from ..storage.frames import list_frames
from .encoder import FrameRef, FrameSequence

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ..config import Settings

# Page size for walking the frame table. A months-long project can hold tens of
# thousands of frames; they are pulled in batches rather than one unbounded query
# so memory stays bounded and the existing paginated listing is reused as-is.
_PAGE_SIZE = 1000


def _as_utc(value: datetime) -> datetime:
    """Return a naive (assumed-UTC) or aware timestamp as tz-aware UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def gather_frames(
    session: Session, settings: Settings, project_id: int
) -> FrameSequence:
    """Return the ordered, active, render-ready frames for a project.

    Only ``active`` frames are included (soft-deleted frames are excluded by the
    underlying listing). Frames the user has excluded from rendering are also
    omitted here -- this is the single query that honours that flag, via
    ``include_excluded=False``; everywhere else excluded frames stay visible.
    Frames are ordered by capture timestamp, and each stored path is resolved to
    an absolute filesystem path. Frames missing a capture timestamp or file path
    are skipped, since neither the timeline nor the input can be built without
    them.
    """
    refs: list[FrameRef] = []
    offset = 0
    while True:
        batch = list_frames(
            session,
            project_id,
            limit=_PAGE_SIZE,
            offset=offset,
            include_deleted=False,
            include_excluded=False,
        )
        if not batch:
            break
        for frame in batch:
            if frame.capture_timestamp is None or frame.file_path is None:
                continue
            absolute_path = resolve_absolute(settings, project_id, frame.file_path)
            refs.append(
                FrameRef(
                    sequence_index=frame.sequence_index,
                    capture_timestamp=_as_utc(frame.capture_timestamp),
                    absolute_path=absolute_path,
                    width=frame.width,
                    height=frame.height,
                )
            )
        if len(batch) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE

    return FrameSequence(project_id=project_id, frames=refs)
