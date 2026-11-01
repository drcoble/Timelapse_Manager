"""Suggested render frame rate from a project's capture interval (#301).

Frames minutes apart change little, so 24-30 fps plays smoothly. Frames hours
or days apart change light, shadow and weather from one frame to the next, and
at 24-30 fps that becomes strobing flicker; a lower rate gives the eye time to
read each frame. Below about 8 fps the video turns into a slideshow, so 8 is
the floor.

The suggestion is derived from the **median gap** between consecutive frames
(the median ignores outages, restarts and weekday masks), interpolated
linearly in ``log(interval)`` between :data:`ANCHORS` and snapped to the
nearest rate in :data:`LADDER` (a tie goes to the higher rate). Both are
constants here, so tuning is a one-line change.

:func:`suggest_fps` is pure. :func:`suggest_for_project` reads only the
``capture_timestamp`` column of the frames a render would use, through
``ix_frame_project_ts``, and reads no frame files. It is synchronous: web and
API callers run it off the event loop.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Frame

# (interval seconds, fps), ascending by interval. Clamped outside the ends.
ANCHORS: tuple[tuple[float, float], ...] = (
    (60, 30.0),
    (600, 30.0),
    (3600, 24.0),
    (21600, 15.0),
    (86400, 12.0),
    (604800, 8.0),
)
# The standard rates a suggestion snaps to.
LADDER: tuple[int, ...] = (8, 10, 12, 15, 20, 24, 30)

# A video shorter than this many seconds gets a "few frames" hint.
SHORT_VIDEO_SECONDS = 10.0

# Timestamps are streamed in pages so a huge project is never one big fetch.
_PAGE_SIZE = 10_000


@dataclass(frozen=True)
class FpsSuggestion:
    """A suggestion for a frame range.

    ``fps`` and ``interval_seconds`` are ``None`` when there is no suggestion
    (fewer than two frames, or no positive gap). ``expected_duration_seconds``
    is ``frame_count / fps`` when there is a suggestion.
    """

    fps: int | None
    interval_seconds: float | None
    frame_count: int
    expected_duration_seconds: float | None


def median_interval(timestamps: list[float]) -> float | None:
    """Return the median positive gap between consecutive timestamps, or ``None``.

    ``timestamps`` are epoch seconds in ascending order. Zero gaps (duplicate
    timestamps) are ignored; with no positive gap there is no interval.
    """
    gaps = sorted(
        b - a for a, b in zip(timestamps, timestamps[1:], strict=False) if b > a
    )
    if not gaps:
        return None
    n = len(gaps)
    return gaps[n // 2] if n % 2 else (gaps[n // 2 - 1] + gaps[n // 2]) / 2


def fps_for_interval(interval: float) -> int:
    """Return the ladder rate for a representative interval in seconds."""
    if interval <= ANCHORS[0][0]:
        base = ANCHORS[0][1]
    elif interval >= ANCHORS[-1][0]:
        base = ANCHORS[-1][1]
    else:
        base = ANCHORS[-1][1]
        for (i0, f0), (i1, f1) in zip(ANCHORS, ANCHORS[1:], strict=False):
            if i0 <= interval <= i1:
                base = f0 + math.log(interval / i0) / math.log(i1 / i0) * (f1 - f0)
                break
    return min(LADDER, key=lambda rate: (abs(rate - base), -rate))


def suggest_fps(timestamps: list[float]) -> int | None:
    """Suggested render fps for frames at these epoch-second timestamps (sorted).

    ``None`` when there is no suggestion: fewer than two frames, or a median
    gap of zero. The caller then falls back to ``render.default_fps``.
    """
    interval = median_interval(timestamps)
    if interval is None:
        return None
    return fps_for_interval(interval)


def naive_utc(value: datetime) -> datetime:
    """Return ``value`` as the naive UTC datetime the frame table stores."""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def frame_timestamps(
    session: Session,
    project_id: int,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[float]:
    """Return the epoch-second capture times of the frames a render would use.

    The same frames :func:`~timelapse_manager.encode.gather_frames` selects:
    active, not excluded from rendering, with a capture timestamp, in capture
    order. ``start`` and ``end`` bound the range inclusively (a naive bound is
    read as UTC). Only the timestamp column is read, ordered to match
    ``ix_frame_project_ts``.
    """
    stmt = (
        select(Frame.capture_timestamp)
        .where(Frame.project_id == project_id)
        .where(Frame.lifecycle_state == "active")
        .where(Frame.excluded_at.is_(None))
        .where(Frame.capture_timestamp.is_not(None))
    )
    if start is not None:
        stmt = stmt.where(Frame.capture_timestamp >= naive_utc(start))
    if end is not None:
        stmt = stmt.where(Frame.capture_timestamp <= naive_utc(end))
    stmt = stmt.order_by(Frame.capture_timestamp.asc(), Frame.sequence_index.asc())
    stmt = stmt.execution_options(yield_per=_PAGE_SIZE)
    return [
        captured.replace(tzinfo=UTC).timestamp()
        for (captured,) in session.execute(stmt)
        if captured is not None
    ]


def suggest_for_project(
    session: Session,
    project_id: int,
    start: datetime | None = None,
    end: datetime | None = None,
) -> FpsSuggestion:
    """Return the suggestion for a project's frames in an optional range."""
    timestamps = frame_timestamps(session, project_id, start, end)
    interval = median_interval(timestamps)
    if interval is None:
        return FpsSuggestion(None, None, len(timestamps), None)
    fps = fps_for_interval(interval)
    return FpsSuggestion(fps, interval, len(timestamps), len(timestamps) / fps)


def resolve_auto_fps(session: Session, project_id: int, default: float) -> float:
    """Return the suggested fps for the project's frames now, else ``default``."""
    suggestion = suggest_for_project(session, project_id)
    return float(suggestion.fps) if suggestion.fps is not None else default


def _plural(value: float, unit: str) -> str:
    """Return ``value`` and ``unit`` with a trailing ``s`` unless it is exactly 1."""
    text = f"{value:.1f}".rstrip("0").rstrip(".") if value % 1 else f"{int(value)}"
    return f"{text} {unit}" if text == "1" else f"{text} {unit}s"


def describe_interval(seconds: float) -> str:
    """Return a short phrase for a capture interval, like ``5 minutes`` or ``1 day``."""
    if seconds < 60:
        return _plural(round(seconds), "second")
    if seconds < 3300:
        return _plural(round(seconds / 60), "minute")
    if seconds < 79200:
        return _plural(round(seconds / 3600, 1), "hour")
    return _plural(round(seconds / 86400, 1), "day")


def describe_duration(seconds: float) -> str:
    """Return a video length like ``42 s`` or ``2 min 5 s``."""
    whole = max(0, round(seconds))
    if whole < 60:
        return f"{whole} s"
    minutes, rest = divmod(whole, 60)
    return f"{minutes} min {rest} s" if rest else f"{minutes} min"


def suggestion_label(suggestion: FpsSuggestion) -> str:
    """Return the form's label for a suggestion.

    For example ``Suggested: 12 fps (captures about every 1 day) · about 42 s``.
    Empty when there is no suggestion.
    """
    if (
        suggestion.fps is None
        or suggestion.interval_seconds is None
        or suggestion.expected_duration_seconds is None
    ):
        return ""
    return (
        f"Suggested: {suggestion.fps} fps "
        f"(captures about every {describe_interval(suggestion.interval_seconds)})"
        f" · about {describe_duration(suggestion.expected_duration_seconds)}"
    )
