"""Projected storage estimate for a capture campaign.

Given a project's capture interval, its campaign duration, and the average size
of a captured frame, this module estimates how many frames the campaign will
ultimately produce and how much disk that will consume. It powers the
forward-looking figures shown on a project's status surface (the API and the
web detail page), alongside the *current* footprint reported elsewhere.

The math is deliberately split from the database:

* :func:`average_frame_size_bytes` and :func:`estimate_project_storage` are pure
  functions over plain numbers, so the estimate logic -- including the default
  fallback, the open-ended sentinel, and the frame-cap bound -- is unit-testable
  without a session.
* :func:`estimate_for_project` is the thin database-aware wrapper the request
  handlers call; it reads the project's existing footprint and bounds, then
  defers to the pure core.

Two kinds of campaign cannot be projected to a finite number:

* one with no ``end_date`` is **open-ended** -- there is no horizon to project
  to, so both projected values are returned as ``None`` (a clear "unknown"
  rather than a fabricated number);
* one with no usable capture interval likewise yields ``None``.

A finite campaign returns concrete integers, with the projected frame count
bounded by the optional frame cap when one is set.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ..db.models import Project

# Fallback average frame size used when a project has no frames yet (or no known
# on-disk usage) to derive a real average from. ~512 KB is a reasonable middle
# ground for a mid-resolution JPEG still; it only affects the estimate until the
# project has captured enough frames to measure its own typical size.
DEFAULT_AVERAGE_FRAME_SIZE_BYTES = 512 * 1024

# Whole seconds in a day, used to convert a per-capture frame size into the
# per-day growth rate (a project captures ``SECONDS_PER_DAY / interval`` frames
# per day).
SECONDS_PER_DAY = 86400


def _now_naive_utc() -> datetime:
    """Return the current time as a naive UTC datetime.

    The project's ``start_date`` / ``end_date`` columns are stored naive-UTC, so
    "now" is normalised to the same shape before any subtraction -- mixing aware
    and naive datetimes would raise at runtime.
    """
    return datetime.now(UTC).replace(tzinfo=None)


def average_frame_size_bytes(total_usage_bytes: int, frame_count: int) -> int:
    """Return the average size of a frame in bytes, with a sane fallback.

    The average is derived as ``total_usage_bytes // frame_count`` when both are
    positive. When the project has no frames yet (``frame_count <= 0``) or no
    measurable usage (``total_usage_bytes <= 0``), there is nothing to measure,
    so :data:`DEFAULT_AVERAGE_FRAME_SIZE_BYTES` is returned instead.
    """
    if frame_count <= 0 or total_usage_bytes <= 0:
        return DEFAULT_AVERAGE_FRAME_SIZE_BYTES
    return total_usage_bytes // frame_count


def estimate_create_time_bytes_per_day(
    interval_seconds: int,
    average_frame_size_bytes: int = DEFAULT_AVERAGE_FRAME_SIZE_BYTES,
) -> int:
    """Estimate the daily growth rate of a not-yet-created project.

    At create time there are no frames to measure, so the default average frame
    size is used. ``frames_per_day = SECONDS_PER_DAY / interval_seconds``.
    Returns 0 for a non-positive interval.
    """
    if interval_seconds <= 0:
        return 0
    frames_per_day = SECONDS_PER_DAY / interval_seconds
    return int(frames_per_day * max(0, average_frame_size_bytes))


def preflight_level(bytes_per_day: int, free_bytes: int) -> str:
    """Classify storage headroom for a pre-flight estimate.

    ``ok`` when the disk lasts >= 90 days at this rate (or there is no growth),
    ``caution`` for 21-90 days, ``danger`` for < 21 days.
    """
    if bytes_per_day <= 0:
        return "ok"
    days = free_bytes / bytes_per_day
    if days >= 90:
        return "ok"
    if days >= 21:
        return "caution"
    return "danger"


def estimate_project_storage(
    interval_seconds: int | None,
    duration_seconds: float | None,
    average_frame_size_bytes: int,
    max_frame_count: int | None = None,
) -> tuple[int | None, int | None]:
    """Estimate ``(projected_frame_count, projected_total_bytes)`` for a campaign.

    The projection is ``frames = duration / interval`` capped by
    ``max_frame_count`` when set, and ``bytes = frames * average_frame_size``.

    Returns ``(None, None)`` -- a deliberate "cannot project" sentinel rather
    than a bogus zero -- when the campaign is **open-ended**
    (``duration_seconds is None``) or has no usable capture cadence
    (``interval_seconds`` missing or non-positive).

    A non-positive ``duration_seconds`` (an end already in the past) floors to a
    zero-frame projection rather than going negative.
    """
    if duration_seconds is None or interval_seconds is None or interval_seconds <= 0:
        return None, None

    duration_frames = max(0, int(duration_seconds // interval_seconds))
    projected_frames = duration_frames
    if max_frame_count is not None:
        projected_frames = min(projected_frames, max_frame_count)

    projected_bytes = projected_frames * max(0, average_frame_size_bytes)
    return projected_frames, projected_bytes


def estimate_for_project(
    session: Session, project: Project
) -> tuple[int | None, int | None]:
    """Project a stored project's total storage and frames-remaining.

    Returns ``(projected_total_bytes, projected_frames_remaining)`` -- both
    ``None`` together for an open-ended campaign (no ``end_date``) or one with no
    usable interval. The average frame size is derived from the project's own
    active-frame footprint, falling back to the module default before any frames
    exist. The duration runs from ``start_date`` (or now, when unset) to
    ``end_date``; "frames remaining" is the projected total minus the frames
    already captured, floored at zero (a campaign can already be over its
    projection when its start has passed).
    """
    if project.end_date is None:
        return None, None

    # Imported here, not at module load, to avoid a storage <-> storage cycle.
    from . import frames as frame_service

    usage = frame_service.sum_project_disk_usage(session, project.id)
    avg = average_frame_size_bytes(usage, project.frame_count)

    start = project.start_date if project.start_date is not None else _now_naive_utc()
    duration_seconds = (project.end_date - start).total_seconds()

    projected_frames, projected_bytes = estimate_project_storage(
        project.capture_interval_seconds,
        duration_seconds,
        avg,
        max_frame_count=project.max_frame_count,
    )
    if projected_frames is None:
        return None, None

    frames_remaining = max(0, projected_frames - project.frame_count)
    return projected_bytes, frames_remaining


def estimate_growth_rate_bytes_per_day(
    session: Session, project: Project
) -> int | None:
    """Estimate how fast a project's footprint grows, in bytes per day.

    The rate is the project's *average captured frame size* multiplied by the
    number of captures it makes per day
    (``SECONDS_PER_DAY / capture_interval_seconds``). The average is derived from
    the project's own active-frame footprint via
    :func:`average_frame_size_bytes`, so the figure reflects this project's
    measured frame sizes rather than a guess.

    Returns ``None`` -- a deliberate "not enough data yet" signal for the caller
    to render, rather than a fabricated zero -- when the project has no usable
    capture interval, or has not captured any real frames yet (so there is no
    measured average to project from).
    """
    interval = project.capture_interval_seconds
    if not interval or interval <= 0:
        return None
    if not project.frame_count:
        return None

    # Imported here, not at module load, to avoid a storage <-> storage cycle.
    from . import frames as frame_service

    usage = frame_service.sum_project_disk_usage(session, project.id)
    avg = average_frame_size_bytes(usage, project.frame_count)
    return round(avg * SECONDS_PER_DAY / interval)
