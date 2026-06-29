"""Unit tests for chapter timecode computation from milestones and calendar
boundaries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from timelapse_manager.encode.chapters import Milestone, compute_chapters
from timelapse_manager.encode.encoder import Chapter, FrameRef, FrameSequence

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_ts(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


def _make_frame(
    sequence_index: int,
    capture_timestamp: datetime,
) -> FrameRef:
    return FrameRef(
        sequence_index=sequence_index,
        capture_timestamp=capture_timestamp,
        absolute_path=__file__,  # type: ignore[arg-type]  # not touched in chapters
        width=64,
        height=48,
    )


def _seq(frames: list[FrameRef]) -> FrameSequence:
    return FrameSequence(project_id=1, frames=frames)


# ---------------------------------------------------------------------------
# Empty / degenerate cases
# ---------------------------------------------------------------------------


class TestComputeChaptersDegenerate:
    def test_empty_sequence_returns_empty(self) -> None:
        seq = FrameSequence(project_id=1, frames=[])
        result = compute_chapters(seq, [], fps=24.0, auto=None)
        assert result == []

    def test_zero_fps_returns_empty(self) -> None:
        frames = [_make_frame(0, _make_ts(2024, 1, 1))]
        seq = _seq(frames)
        result = compute_chapters(seq, [], fps=0.0, auto=None)
        assert result == []

    def test_negative_fps_returns_empty(self) -> None:
        frames = [_make_frame(0, _make_ts(2024, 1, 1))]
        seq = _seq(frames)
        result = compute_chapters(seq, [], fps=-1.0, auto=None)
        assert result == []

    def test_no_auto_no_milestones_returns_empty(self) -> None:
        frames = [_make_frame(i, _make_ts(2024, 1, i + 1)) for i in range(5)]
        seq = _seq(frames)
        result = compute_chapters(seq, [], fps=24.0, auto=None)
        assert result == []


# ---------------------------------------------------------------------------
# Timecode arithmetic: fps=1 means timecode == ordinal index
# ---------------------------------------------------------------------------


class TestTimecodeArithmetic:
    def test_fps1_timecode_equals_frame_index(self) -> None:
        frames = [
            _make_frame(i, _make_ts(2024, 1, 1) + timedelta(hours=i)) for i in range(5)
        ]
        seq = _seq(frames)
        milestone = Milestone(label="marker", position_frame_index=3)
        result = compute_chapters(seq, [milestone], fps=1.0, auto=None)

        assert len(result) == 1
        # Frame ordinal 3 at fps=1 -> timecode = 3.0
        assert result[0].timecode_seconds == pytest.approx(3.0)

    def test_fps24_timecode_is_ordinal_over_fps(self) -> None:
        frames = [
            _make_frame(i, _make_ts(2024, 1, 1) + timedelta(hours=i)) for i in range(10)
        ]
        seq = _seq(frames)
        milestone = Milestone(label="mark", position_frame_index=6)
        result = compute_chapters(seq, [milestone], fps=24.0, auto=None)

        assert len(result) == 1
        assert result[0].timecode_seconds == pytest.approx(6 / 24.0)

    def test_chapters_sorted_by_timecode(self) -> None:
        frames = [
            _make_frame(i, _make_ts(2024, 1, 1) + timedelta(hours=i)) for i in range(10)
        ]
        seq = _seq(frames)
        milestones = [
            Milestone(label="late", position_frame_index=8),
            Milestone(label="early", position_frame_index=2),
        ]
        result = compute_chapters(seq, milestones, fps=1.0, auto=None)

        assert len(result) == 2
        assert result[0].timecode_seconds < result[1].timecode_seconds
        assert result[0].label == "early"
        assert result[1].label == "late"


# ---------------------------------------------------------------------------
# Manual milestone resolution
# ---------------------------------------------------------------------------


class TestMilestoneResolution:
    def test_milestone_at_frame3_creates_chapter_at_correct_timecode(self) -> None:
        frames = [
            _make_frame(i, _make_ts(2024, 1, 1) + timedelta(hours=i)) for i in range(6)
        ]
        seq = _seq(frames)
        milestone = Milestone(label="Day 3", position_frame_index=3)
        result = compute_chapters(seq, [milestone], fps=1.0, auto=None)

        assert len(result) == 1
        assert result[0] == Chapter(timecode_seconds=3.0, label="Day 3")

    def test_milestone_beyond_sequence_is_skipped(self) -> None:
        frames = [
            _make_frame(i, _make_ts(2024, 1, 1) + timedelta(hours=i)) for i in range(5)
        ]
        seq = _seq(frames)
        # Frame index 99 does not exist in the 5-frame sequence.
        milestone = Milestone(label="missing", position_frame_index=99)
        result = compute_chapters(seq, [milestone], fps=1.0, auto=None)
        assert result == []

    def test_milestone_with_no_position_is_skipped(self) -> None:
        frames = [
            _make_frame(i, _make_ts(2024, 1, 1) + timedelta(hours=i)) for i in range(5)
        ]
        seq = _seq(frames)
        milestone = Milestone(
            label="noop", position_frame_index=None, position_timestamp=None
        )
        result = compute_chapters(seq, [milestone], fps=1.0, auto=None)
        assert result == []

    def test_milestone_by_timestamp_matches_nearest_frame_at_or_after(self) -> None:
        base = _make_ts(2024, 3, 1, 0)
        frames = [_make_frame(i, base + timedelta(hours=i)) for i in range(6)]
        seq = _seq(frames)
        # Ask for the milestone to land at 2.5 hours in; nearest frame at-or-after
        # is frame 3.
        ts = base + timedelta(hours=2, minutes=30)
        milestone = Milestone(label="ts-marker", position_timestamp=ts)
        result = compute_chapters(seq, [milestone], fps=1.0, auto=None)

        assert len(result) == 1
        assert result[0].timecode_seconds == pytest.approx(3.0)

    def test_frame_index_takes_precedence_over_timestamp(self) -> None:
        base = _make_ts(2024, 3, 1, 0)
        frames = [_make_frame(i, base + timedelta(hours=i)) for i in range(6)]
        seq = _seq(frames)
        # Frame index 1 is ordinal 1; timestamp would point to frame 4 -- index wins.
        milestone = Milestone(
            label="index-wins",
            position_frame_index=1,
            position_timestamp=base + timedelta(hours=4),
        )
        result = compute_chapters(seq, [milestone], fps=1.0, auto=None)
        assert len(result) == 1
        assert result[0].timecode_seconds == pytest.approx(1.0)

    def test_milestone_on_deleted_frame_snaps_to_next_active(self) -> None:
        # Frames have sequence indices 0, 2, 4 (1 and 3 are "deleted" / not in
        # sequence).
        base = _make_ts(2024, 1, 1)
        frames = [
            _make_frame(0, base),
            _make_frame(2, base + timedelta(hours=2)),
            _make_frame(4, base + timedelta(hours=4)),
        ]
        seq = _seq(frames)
        # Milestone at sequence_index=1 (deleted) should snap to ordinal 1
        # (sequence_index=2).
        milestone = Milestone(label="snapped", position_frame_index=1)
        result = compute_chapters(seq, [milestone], fps=1.0, auto=None)

        assert len(result) == 1
        # Ordinal 1 (second frame in rendered sequence) at fps=1 -> timecode 1.0.
        assert result[0].timecode_seconds == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Auto monthly chapters
# ---------------------------------------------------------------------------


class TestAutoMonthlyChapters:
    def test_single_month_produces_one_chapter(self) -> None:
        frames = [_make_frame(i, _make_ts(2024, 3, i + 1)) for i in range(5)]
        seq = _seq(frames)
        result = compute_chapters(seq, [], fps=1.0, auto="monthly")

        assert len(result) == 1
        assert result[0].timecode_seconds == pytest.approx(0.0)
        assert "March" in result[0].label or "2024" in result[0].label

    def test_two_months_produces_two_chapters(self) -> None:
        jan_frames = [_make_frame(i, _make_ts(2024, 1, i + 1)) for i in range(3)]
        feb_frames = [_make_frame(3 + i, _make_ts(2024, 2, i + 1)) for i in range(3)]
        seq = _seq(jan_frames + feb_frames)
        result = compute_chapters(seq, [], fps=1.0, auto="monthly")

        assert len(result) == 2
        # First chapter at frame 0 (ordinal 0), second at frame 3 (ordinal 3).
        assert result[0].timecode_seconds == pytest.approx(0.0)
        assert result[1].timecode_seconds == pytest.approx(3.0)

    def test_monthly_chapter_label_format(self) -> None:
        frames = [_make_frame(i, _make_ts(2024, 6, i + 1)) for i in range(3)]
        seq = _seq(frames)
        result = compute_chapters(seq, [], fps=1.0, auto="monthly")

        assert len(result) == 1
        # Label should be "%B %Y" -> "June 2024"
        assert result[0].label == "June 2024"

    def test_monthly_off_by_one_first_frame_of_new_month(self) -> None:
        # The boundary chapter must be placed at the FIRST frame of the new month,
        # not the last frame of the old month.
        last_jan = _make_frame(0, _make_ts(2024, 1, 31))
        first_feb = _make_frame(1, _make_ts(2024, 2, 1))
        second_feb = _make_frame(2, _make_ts(2024, 2, 2))
        seq = _seq([last_jan, first_feb, second_feb])
        result = compute_chapters(seq, [], fps=1.0, auto="monthly")

        assert len(result) == 2
        jan_ch, feb_ch = result
        assert jan_ch.timecode_seconds == pytest.approx(0.0)
        assert feb_ch.timecode_seconds == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Auto weekly chapters
# ---------------------------------------------------------------------------


class TestAutoWeeklyChapters:
    def test_single_week_produces_one_chapter(self) -> None:
        # 2024-01-01 is Monday of ISO week 1.
        frames = [
            _make_frame(i, _make_ts(2024, 1, 1) + timedelta(days=i)) for i in range(5)
        ]
        seq = _seq(frames)
        result = compute_chapters(seq, [], fps=1.0, auto="weekly")

        assert len(result) == 1

    def test_two_weeks_produces_two_chapters(self) -> None:
        # 2024-01-01 Monday (week 1) + 2024-01-08 Monday (week 2).
        week1 = [
            _make_frame(i, _make_ts(2024, 1, 1) + timedelta(days=i)) for i in range(4)
        ]
        week2 = [
            _make_frame(4 + i, _make_ts(2024, 1, 8) + timedelta(days=i))
            for i in range(3)
        ]
        seq = _seq(week1 + week2)
        result = compute_chapters(seq, [], fps=1.0, auto="weekly")

        assert len(result) == 2
        assert result[0].timecode_seconds == pytest.approx(0.0)
        assert result[1].timecode_seconds == pytest.approx(4.0)

    def test_weekly_chapter_label_contains_date(self) -> None:
        frames = [
            _make_frame(i, _make_ts(2024, 1, 1) + timedelta(days=i)) for i in range(3)
        ]
        seq = _seq(frames)
        result = compute_chapters(seq, [], fps=1.0, auto="weekly")

        # Label format is "Week of %Y-%m-%d".
        assert result[0].label.startswith("Week of ")


# ---------------------------------------------------------------------------
# Milestone overrides auto marker at same frame
# ---------------------------------------------------------------------------


class TestMilestoneOverridesAutoMarker:
    def test_manual_milestone_label_overrides_auto_label_at_same_frame(self) -> None:
        # All frames in the same month; auto would produce "January 2024" at frame 0.
        # A manual milestone also at frame 0 should override the label.
        frames = [_make_frame(i, _make_ts(2024, 1, i + 1)) for i in range(5)]
        seq = _seq(frames)
        milestone = Milestone(label="Project Start", position_frame_index=0)
        result = compute_chapters(seq, [milestone], fps=1.0, auto="monthly")

        assert len(result) == 1
        assert result[0].label == "Project Start"
