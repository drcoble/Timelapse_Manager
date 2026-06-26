"""Compute output-timeline chapters from milestones and calendar boundaries.

A timelapse plays one source frame per output frame, so a frame's playback
offset is purely its ordinal position over the frame rate: ``index / fps``
seconds. Every chapter timecode here is derived from that relationship -- both
the manual milestones a user pins and the automatic markers placed at month or
week boundaries crossed by the capture timestamps.

The functions take an ordered :class:`~.encoder.FrameSequence` and never touch
the database; the caller resolves milestones into the lightweight
:class:`Milestone` shape below.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from .encoder import Chapter, FrameSequence


@dataclass(frozen=True)
class Milestone:
    """A user-pinned marker, positioned by frame index and/or timestamp.

    At least one of the two positions should be set. A frame index is used
    directly; otherwise the timestamp is matched to the nearest frame at or
    after it. A milestone that matches no frame is skipped.
    """

    label: str
    position_frame_index: int | None = None
    position_timestamp: datetime | None = None


def _as_utc(value: datetime) -> datetime:
    """Return ``value`` as tz-aware UTC (naive values are assumed UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _timecode_for_index(index: int, fps: float) -> float:
    """Return the playback offset in seconds for a zero-based frame index."""
    return index / fps


def _index_for_frame_position(frames: FrameSequence, position: int) -> int | None:
    """Map a milestone's stored frame position to an ordinal in the sequence.

    Milestones are positioned by ``sequence_index`` (the project-wide capture
    ordinal), but chapter timecodes need the position *within the rendered
    sequence* (active frames only, in order). This finds the first rendered
    frame whose ``sequence_index`` is at least ``position`` and returns its
    ordinal, so a milestone on a since-deleted frame snaps forward to the next
    surviving frame. Returns ``None`` if no frame qualifies.
    """
    for ordinal, frame in enumerate(frames.frames):
        if frame.sequence_index >= position:
            return ordinal
    return None


def _index_for_timestamp(frames: FrameSequence, when: datetime) -> int | None:
    """Return the ordinal of the first frame captured at or after ``when``."""
    target = _as_utc(when)
    for ordinal, frame in enumerate(frames.frames):
        if _as_utc(frame.capture_timestamp) >= target:
            return ordinal
    return None


def _milestone_ordinal(frames: FrameSequence, milestone: Milestone) -> int | None:
    """Resolve a milestone to an ordinal in the rendered sequence, or ``None``.

    A frame index takes precedence over a timestamp when both are present.
    """
    if milestone.position_frame_index is not None:
        return _index_for_frame_position(frames, milestone.position_frame_index)
    if milestone.position_timestamp is not None:
        return _index_for_timestamp(frames, milestone.position_timestamp)
    return None


def _month_key(value: datetime) -> tuple[int, int]:
    """Return the ``(year, month)`` a timestamp falls in (UTC)."""
    utc = _as_utc(value)
    return (utc.year, utc.month)


def _week_key(value: datetime) -> tuple[int, int]:
    """Return the ISO ``(year, week)`` a timestamp falls in (UTC)."""
    utc = _as_utc(value)
    iso = utc.isocalendar()
    return (iso.year, iso.week)


def _auto_boundary_ordinals(
    frames: FrameSequence, granularity: str
) -> list[tuple[int, str]]:
    """Return ``(ordinal, label)`` for each calendar boundary the frames cross.

    Walks the frames in order and emits a marker at the first frame of each new
    period (month or ISO week), including the very first frame. The label is the
    period the frame opens, formatted from its capture timestamp.
    """
    if granularity == "monthly":
        key = _month_key
        label_fmt = "%B %Y"
    else:  # "weekly"
        key = _week_key
        label_fmt = "Week of %Y-%m-%d"

    markers: list[tuple[int, str]] = []
    last_key: tuple[int, int] | None = None
    for ordinal, frame in enumerate(frames.frames):
        current = key(frame.capture_timestamp)
        if current != last_key:
            label = _as_utc(frame.capture_timestamp).strftime(label_fmt)
            markers.append((ordinal, label))
            last_key = current
    return markers


def compute_chapters(
    frames: FrameSequence,
    milestones: Iterable[Milestone],
    fps: float,
    *,
    auto: str | None,
) -> list[Chapter]:
    """Compute the ordered, de-duplicated chapter list for a render.

    Combines automatic calendar markers (when ``auto`` is ``"monthly"`` or
    ``"weekly"``) with manual ``milestones``, mapping each to an output-timeline
    timecode (``ordinal / fps``). Results are sorted by timecode; when an
    automatic marker and a milestone (or two markers) land on the same frame, the
    first one encountered wins -- manual milestones are applied after automatic
    ones, so a user label overrides a generic calendar label at the same frame.

    An empty sequence, a non-positive ``fps``, or ``auto`` of ``None`` with no
    milestones all yield an empty list.
    """
    if len(frames) == 0 or fps <= 0:
        return []

    # ordinal -> label, last write wins, so milestones (applied second) override.
    by_ordinal: dict[int, str] = {}

    if auto in ("monthly", "weekly"):
        for ordinal, label in _auto_boundary_ordinals(frames, auto):
            by_ordinal[ordinal] = label

    for milestone in milestones:
        matched = _milestone_ordinal(frames, milestone)
        if matched is None:
            continue
        label = milestone.label if milestone.label else f"Milestone @ {matched}"
        by_ordinal[matched] = label

    return [
        Chapter(timecode_seconds=_timecode_for_index(ordinal, fps), label=label)
        for ordinal, label in sorted(by_ordinal.items())
    ]
