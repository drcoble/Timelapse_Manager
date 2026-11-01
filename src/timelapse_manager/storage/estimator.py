"""Projected storage estimate for a capture campaign.

Given how many captures a project plans per day (its interval ticks inside the
schedule windows plus its exact-time anchors, worked out by
:func:`timelapse_manager.capture.cadence.captures_per_day`), its campaign
duration, and the average size of a captured frame, this module estimates how
many frames the campaign will ultimately produce and how much disk that will
consume. It powers the forward-looking figures shown on a project's status
surface (the API and the web detail page), alongside the *current* footprint
reported elsewhere.

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
* one that plans no captures (no interval and no enabled anchor that fires)
  likewise yields ``None``.

A finite campaign returns concrete integers, with the projected frame count
bounded by the optional frame cap when one is set.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ..db.models import Camera, Project

# Fallback average frame size used when a project has no frames yet (or no known
# on-disk usage) to derive a real average from. ~512 KB is a reasonable middle
# ground for a mid-resolution JPEG still; it only affects the estimate until the
# project has captured enough frames to measure its own typical size.
DEFAULT_AVERAGE_FRAME_SIZE_BYTES = 512 * 1024

# Whole seconds in a day: an always-open interval project captures
# ``SECONDS_PER_DAY / interval`` frames per day, and campaign durations are
# converted to days with it.
SECONDS_PER_DAY = 86400


class _Unset(enum.Enum):
    """Sentinel type: "work out captures per day yourself" (``None`` is a value)."""

    TOKEN = 0


_UNSET: Final = _Unset.TOKEN


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
    captures_per_day: float | None,
    average_frame_size_bytes: int = DEFAULT_AVERAGE_FRAME_SIZE_BYTES,
) -> int:
    """Estimate the daily growth rate of a not-yet-created project.

    *captures_per_day* comes from
    :func:`~timelapse_manager.capture.cadence.captures_per_day` over the create
    form's interval, schedule and anchors. At create time there are no frames
    to measure, so the default average frame size is used. Returns 0 when the
    form plans no captures (``None`` or a non-positive count).
    """
    if captures_per_day is None or captures_per_day <= 0:
        return 0
    return int(captures_per_day * max(0, average_frame_size_bytes))


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
    captures_per_day: float | None,
    duration_seconds: float | None,
    average_frame_size_bytes: int,
    max_frame_count: int | None = None,
) -> tuple[int | None, int | None]:
    """Estimate ``(projected_frame_count, projected_total_bytes)`` for a campaign.

    The projection is ``frames = duration in days * captures_per_day`` capped by
    ``max_frame_count`` when set, and ``bytes = frames * average_frame_size``.

    Returns ``(None, None)`` -- a deliberate "cannot project" sentinel rather
    than a bogus zero -- when the campaign is **open-ended**
    (``duration_seconds is None``) or plans no captures (``captures_per_day``
    missing or non-positive).

    A non-positive ``duration_seconds`` (an end already in the past) floors to a
    zero-frame projection rather than going negative.
    """
    if duration_seconds is None or captures_per_day is None or captures_per_day <= 0:
        return None, None

    duration_frames = max(0, int(duration_seconds / SECONDS_PER_DAY * captures_per_day))
    projected_frames = duration_frames
    if max_frame_count is not None:
        projected_frames = min(projected_frames, max_frame_count)

    projected_bytes = projected_frames * max(0, average_frame_size_bytes)
    return projected_frames, projected_bytes


def project_captures_per_day(
    session: Session,
    project: Project,
    *,
    now: datetime | None = None,
    camera: Camera | None = None,
) -> float | None:
    """Return how many captures *project* plans per day from now, or ``None``.

    A thin database-aware wrapper over
    :func:`~timelapse_manager.capture.cadence.captures_per_day`: it reads the
    camera's coordinates (for solar anchors and sun windows) and the project's
    interval, anchors, schedule, campaign bounds and frame cap. Pass *camera*
    when the caller already holds the row, to skip the lookup.
    """
    from ..capture import cadence
    from ..db.models import Camera

    if camera is None:
        camera = session.get(Camera, project.camera_id)
    latitude, longitude = (
        (camera.geolocation_latitude, camera.geolocation_longitude)
        if camera is not None
        else (None, None)
    )
    return cadence.captures_per_day(
        now=now if now is not None else datetime.now(UTC),
        interval_seconds=project.capture_interval_seconds,
        anchors_raw=project.exact_time_anchors,
        schedule_raw=project.schedule,
        latitude=latitude,
        longitude=longitude,
        start_date=project.start_date,
        end_date=project.end_date,
        finished=(
            project.max_frame_count is not None
            and project.frame_count >= project.max_frame_count
        ),
    )


def _campaign_captures_per_day(session: Session, project: Project) -> float | None:
    """Captures per day over the campaign itself, for the storage projection.

    The projection covers the whole campaign (from ``start_date``, or now when
    unset), so the rate is measured from the campaign's start rather than from
    now. Unlike the live rate it ignores the frame cap (the projection applies
    the cap itself) and an ``end_date`` already passed (the projection floors to
    zero frames rather than reading as "cannot project").
    """
    from ..capture import cadence
    from ..db.models import Camera

    camera = session.get(Camera, project.camera_id)
    latitude, longitude = (
        (camera.geolocation_latitude, camera.geolocation_longitude)
        if camera is not None
        else (None, None)
    )
    reference = (
        project.start_date if project.start_date is not None else _now_naive_utc()
    )
    end = project.end_date
    return cadence.captures_per_day(
        now=reference,
        interval_seconds=project.capture_interval_seconds,
        anchors_raw=project.exact_time_anchors,
        schedule_raw=project.schedule,
        latitude=latitude,
        longitude=longitude,
        end_date=end if end is not None and end > reference else None,
    )


def estimate_for_project(
    session: Session, project: Project, *, disk_usage: int | None = None
) -> tuple[int | None, int | None]:
    """Project a stored project's total storage and frames-remaining.

    Returns ``(projected_total_bytes, projected_frames_remaining)`` -- both
    ``None`` together for an open-ended campaign (no ``end_date``) or one that
    plans no captures. The capture rate is the campaign's captures per day
    (interval ticks inside the schedule windows plus exact-time anchors, see
    :mod:`timelapse_manager.capture.cadence`), measured from the campaign's
    start (see :func:`_campaign_captures_per_day`). The average frame size is
    derived from the project's own active-frame footprint, falling back to the
    module default before any frames exist. The duration runs from
    ``start_date`` (or now, when unset) to ``end_date``; "frames remaining" is
    the projected total minus the frames already captured, floored at zero (a
    campaign can already be over its projection when its start has passed).

    Pass *disk_usage* (the project's active-frame bytes, as
    :func:`~timelapse_manager.storage.frames.sum_project_disk_usage` returns it)
    when the caller already has it, to skip that aggregate (#66 C5).
    """
    if project.end_date is None:
        return None, None

    # Imported here, not at module load, to avoid a storage <-> storage cycle.
    from . import frames as frame_service

    rate = _campaign_captures_per_day(session, project)
    if rate is None:
        return None, None

    usage = (
        frame_service.sum_project_disk_usage(session, project.id)
        if disk_usage is None
        else disk_usage
    )
    avg = average_frame_size_bytes(usage, project.frame_count)

    start = project.start_date if project.start_date is not None else _now_naive_utc()
    duration_seconds = (project.end_date - start).total_seconds()

    projected_frames, projected_bytes = estimate_project_storage(
        rate,
        duration_seconds,
        avg,
        max_frame_count=project.max_frame_count,
    )
    if projected_frames is None:
        return None, None

    frames_remaining = max(0, projected_frames - project.frame_count)
    return projected_bytes, frames_remaining


def estimate_growth_rate_bytes_per_day(
    session: Session,
    project: Project,
    *,
    captures_per_day: float | None | _Unset = _UNSET,
    disk_usage: int | None = None,
) -> int | None:
    """Estimate how fast a project's footprint grows, in bytes per day.

    The rate is the project's *average captured frame size* multiplied by the
    number of captures it plans per day: its interval ticks while the schedule
    gate is open plus its exact-time anchors (clock or solar), as worked out by
    :func:`project_captures_per_day`. Pass *captures_per_day* to reuse a value
    the caller already has. The average is derived from the project's own
    active-frame footprint via :func:`average_frame_size_bytes`, so the figure
    reflects this project's measured frame sizes rather than a guess. Pass
    *disk_usage* (as :func:`estimate_for_project` takes it) to skip the
    footprint aggregate (#66 C5).

    Returns ``None`` -- a deliberate "no rate" signal for the caller to render,
    rather than a fabricated zero -- when the project plans no captures
    (finished, past its ``end_date``, or nothing enabled), or has not captured
    any real frames yet (so there is no measured average to project from).
    Whether a paused, low-disk or stopped project shows a rate at all is the
    caller's decision: the web view model does not ask for one (#22, D12).
    """
    if not project.frame_count:
        return None
    rate = (
        project_captures_per_day(session, project)
        if captures_per_day is _UNSET
        else captures_per_day
    )
    if rate is None or rate <= 0:
        return None

    # Imported here, not at module load, to avoid a storage <-> storage cycle.
    from . import frames as frame_service

    usage = (
        frame_service.sum_project_disk_usage(session, project.id)
        if disk_usage is None
        else disk_usage
    )
    avg = average_frame_size_bytes(usage, project.frame_count)
    return round(avg * rate)
