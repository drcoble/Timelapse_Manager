"""Unit tests for the jump-control storage helpers.

These pin the leaf contracts the frames-browser jump controls lean on: the
oldest-sequence resolver (jump to start), the capture-gap finder and the
nearest-gap selector (jump to next/prev gap), and the window-centre resolver
(the nearest-frame note). The gap finder uses the same threshold rule as the
ribbon's marker detector, so the buttons land on exactly the lapses the ribbon
draws.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.storage.frames import (
    CaptureGap,
    find_capture_gaps,
    nearest_gap,
    oldest_active_seq,
    resolve_window_center,
)

_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_STEP = timedelta(minutes=5)


def _seed(session, times: list[datetime | None], *, name: str = "jmp") -> int:
    """Seed a project with one frame per supplied (naive-or-aware) timestamp."""
    cam = Camera(name=f"{name}-cam", address="127.0.0.1", protocol="vapix")
    session.add(cam)
    session.flush()
    proj = Project(camera_id=cam.id, name=name, lifecycle_state="active")
    session.add(proj)
    session.flush()
    for i, t in enumerate(times):
        ts = None
        if t is not None:
            ts = t.replace(tzinfo=None) if t.tzinfo is not None else t
        session.add(
            Frame(
                project_id=proj.id,
                sequence_index=i,
                capture_timestamp=ts,
                file_path=f"/frames/{i:08d}.jpg",
                capture_status="captured",
                origin="captured",
                lifecycle_state="active",
            )
        )
    session.flush()
    return proj.id


class TestOldestActiveSeq:
    def test_returns_minimum_sequence(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid = _seed(s, [_T0 + _STEP * i for i in range(10)])
            assert oldest_active_seq(s, pid) == 0

    def test_empty_project_returns_none(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid = _seed(s, [])
            assert oldest_active_seq(s, pid) is None

    def test_excludes_soft_deleted_by_default(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid = _seed(s, [_T0 + _STEP * i for i in range(5)])
            # Soft-delete the two oldest (seq 0, 1) -> oldest active is now 2.
            for seq in (0, 1):
                fr = next(
                    f
                    for f in s.query(Frame).all()
                    if f.project_id == pid and f.sequence_index == seq
                )
                fr.lifecycle_state = "soft_deleted"
            s.flush()
            assert oldest_active_seq(s, pid) == 2
            # ...but it is reachable with include_deleted.
            assert oldest_active_seq(s, pid, include_deleted=True) == 0


class TestFindCaptureGaps:
    def test_finds_a_gap_with_bounding_timestamps(self) -> None:
        # Dense run, a 2-hour lapse, then dense again. Span is the full range.
        a = _T0
        b = _T0 + _STEP  # contiguous
        c = b + timedelta(hours=2)  # the lapse
        d = c + _STEP
        span_start, span_end = a, d
        gaps = find_capture_gaps([a, b, c, d], span_start, span_end)
        assert len(gaps) == 1
        assert gaps[0].before == b
        assert gaps[0].after == c
        assert gaps[0].duration == timedelta(hours=2)

    def test_threshold_matches_ribbon_rule(self) -> None:
        # A delta just over 4% of the span is a gap; just under is not.
        span_start = _T0
        span_end = _T0 + timedelta(hours=100)  # span = 100h; 4% = 4h
        small = [_T0, _T0 + timedelta(hours=3, minutes=59)]
        big = [_T0, _T0 + timedelta(hours=4, minutes=1)]
        assert find_capture_gaps(small, span_start, span_end) == []
        assert len(find_capture_gaps(big, span_start, span_end)) == 1

    def test_multiple_gaps_ordered_ascending(self) -> None:
        a = _T0
        b = a + timedelta(hours=2)
        c = b + _STEP
        d = c + timedelta(hours=3)
        gaps = find_capture_gaps([a, b, c, d], a, d)
        assert [g.before for g in gaps] == [a, c]

    def test_fewer_than_two_frames_no_gap(self) -> None:
        assert find_capture_gaps([_T0], _T0, _T0 + timedelta(hours=1)) == []
        assert find_capture_gaps([], _T0, _T0 + timedelta(hours=1)) == []

    def test_degenerate_span_no_gap(self) -> None:
        assert find_capture_gaps([_T0, _T0 + _STEP], _T0, _T0) == []


class TestNearestGap:
    def _gaps(self) -> list[CaptureGap]:
        # before-timestamps at t=10, t=20, t=30 (minutes).
        return [
            CaptureGap(_T0 + timedelta(minutes=10), _T0 + timedelta(minutes=15)),
            CaptureGap(_T0 + timedelta(minutes=20), _T0 + timedelta(minutes=25)),
            CaptureGap(_T0 + timedelta(minutes=30), _T0 + timedelta(minutes=35)),
        ]

    def test_next_picks_first_strictly_after(self) -> None:
        g = nearest_gap(self._gaps(), _T0 + timedelta(minutes=15), direction="next")
        assert g is not None and g.before == _T0 + timedelta(minutes=20)

    def test_prev_picks_first_strictly_before(self) -> None:
        g = nearest_gap(self._gaps(), _T0 + timedelta(minutes=25), direction="prev")
        assert g is not None and g.before == _T0 + timedelta(minutes=20)

    def test_strict_comparison_steps_off_current_gap(self) -> None:
        # Anchored exactly on a gap's before, next must skip to the following one.
        on = _T0 + timedelta(minutes=20)
        assert nearest_gap(
            self._gaps(), on, direction="next"
        ).before == _T0 + timedelta(minutes=30)
        assert nearest_gap(
            self._gaps(), on, direction="prev"
        ).before == _T0 + timedelta(minutes=10)

    def test_no_gap_in_direction_returns_none(self) -> None:
        gaps = self._gaps()
        assert nearest_gap(gaps, _T0 + timedelta(minutes=40), direction="next") is None
        assert nearest_gap(gaps, _T0, direction="prev") is None

    def test_unknown_direction_returns_none(self) -> None:
        assert nearest_gap(self._gaps(), _T0, direction="sideways") is None


class TestResolveWindowCenter:
    def test_exact_anchor_is_exact(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid = _seed(s, [_T0 + _STEP * i for i in range(10)])
            center, exact = resolve_window_center(s, pid, _T0 + _STEP * 3)
            assert center == (_T0 + _STEP * 3).replace(tzinfo=None)
            assert exact is True

    def test_between_frames_returns_nearest_at_or_after(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid = _seed(s, [_T0 + _STEP * i for i in range(10)])
            anchor = _T0 + _STEP * 3 + timedelta(seconds=30)
            center, exact = resolve_window_center(s, pid, anchor)
            # The grid centres on frame 4 (first at-or-after); not an exact hit.
            assert center == (_T0 + _STEP * 4).replace(tzinfo=None)
            assert exact is False

    def test_past_last_frame_no_center(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid = _seed(s, [_T0 + _STEP * i for i in range(10)])
            center, exact = resolve_window_center(s, pid, _T0 + _STEP * 1000)
            assert center is None
            assert exact is False
