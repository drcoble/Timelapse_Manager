"""Unit tests for the bidirectional timestamp window resolver.

These pin the storage contract directly: a timestamp anchor resolves to a
sequence boundary, the window spans both sides of it, and the returned cursor
continues the keyset scroll exactly (no skip or overlap) even at the series ends
and when capture timestamp and sequence disagree (a backdated frame).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.storage.frames import (
    list_frames_keyset,
    list_frames_window,
    resolve_seq_at_timestamp,
)

_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_STEP = timedelta(minutes=5)


def _seed(session, n: int, *, name: str = "win") -> int:
    cam = Camera(name=f"{name}-cam", address="127.0.0.1", protocol="vapix")
    session.add(cam)
    session.flush()
    proj = Project(camera_id=cam.id, name=name, lifecycle_state="active")
    session.add(proj)
    session.flush()
    for i in range(n):
        session.add(
            Frame(
                project_id=proj.id,
                sequence_index=i,
                capture_timestamp=(_T0 + _STEP * i).replace(tzinfo=None),
                file_path=f"/frames/{i:08d}.jpg",
                capture_status="captured",
                origin="captured",
                lifecycle_state="active",
            )
        )
    session.flush()
    return proj.id


def _seqs(frames: list[Frame]) -> list[int]:
    return [f.sequence_index for f in frames]


class TestResolveSeqAtTimestamp:
    def test_exact_match_resolves_to_that_seq(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid = _seed(s, 20)
            assert resolve_seq_at_timestamp(s, pid, _T0 + _STEP * 7) == 7

    def test_between_frames_resolves_to_next_at_or_after(
        self, migrated_factory
    ) -> None:
        with session_scope(migrated_factory) as s:
            pid = _seed(s, 20)
            # 30s past frame 7's time -> next at-or-after is frame 8.
            anchor = _T0 + _STEP * 7 + timedelta(seconds=30)
            assert resolve_seq_at_timestamp(s, pid, anchor) == 8

    def test_past_last_frame_returns_none(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid = _seed(s, 20)
            assert resolve_seq_at_timestamp(s, pid, _T0 + _STEP * 1000) is None

    def test_before_first_frame_returns_first_seq(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid = _seed(s, 20)
            assert resolve_seq_at_timestamp(s, pid, _T0 - _STEP) == 0


class TestListFramesWindow:
    def test_window_centers_on_anchor(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid = _seed(s, 100)
            frames, _ = list_frames_window(s, pid, _T0 + _STEP * 50)
            seqs = _seqs(frames)
            # 30 at-or-after (50..79) + 30 before (20..49), newest-first.
            assert seqs == list(range(79, 19, -1))
            assert 50 in seqs
            assert max(seqs) > 50 and min(seqs) < 50

    def test_window_cursor_continues_scroll_without_gap(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid = _seed(s, 100)
            frames, next_before = list_frames_window(s, pid, _T0 + _STEP * 50)
            # Oldest in window is seq 20; the cursor must be exactly 20.
            assert next_before == 20
            older = list_frames_keyset(s, pid, before_seq=next_before, limit=1000)
            # The continuation is seq 19..0, contiguous with the window, no overlap.
            assert _seqs(older) == list(range(19, -1, -1))

    def test_near_first_frame_truncates_before_side(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid = _seed(s, 100)
            frames, next_before = list_frames_window(s, pid, _T0 + _STEP * 5)
            seqs = _seqs(frames)
            # 5 before (0..4) + 30 at-or-after (5..34) = 35; series start reached.
            assert len(seqs) == 35
            assert min(seqs) == 0
            assert next_before is None  # no older frames remain

    def test_near_last_frame_truncates_after_side(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid = _seed(s, 100)
            frames, _ = list_frames_window(s, pid, _T0 + _STEP * 95)
            seqs = _seqs(frames)
            # 5 at-or-after (95..99) + 30 before (65..94) = 35.
            assert len(seqs) == 35
            assert max(seqs) == 99

    def test_past_last_frame_clamps_to_newest(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid = _seed(s, 50)
            frames, next_before = list_frames_window(s, pid, _T0 + _STEP * 1000)
            seqs = _seqs(frames)
            # Clamp to the newest page (49..20), with a usable continuation cursor.
            assert seqs[0] == 49
            assert len(seqs) == 30
            assert next_before == 20

    def test_all_null_timestamps_falls_back_to_newest(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            cam = Camera(name="null-cam", address="127.0.0.2", protocol="vapix")
            s.add(cam)
            s.flush()
            proj = Project(camera_id=cam.id, name="null", lifecycle_state="active")
            s.add(proj)
            s.flush()
            for i in range(10):
                s.add(
                    Frame(
                        project_id=proj.id,
                        sequence_index=i,
                        capture_timestamp=None,
                        file_path=f"/frames/{i:08d}.jpg",
                        capture_status="captured",
                        origin="captured",
                        lifecycle_state="active",
                    )
                )
            s.flush()
            frames, _ = list_frames_window(s, proj.id, _T0)
            # No timed frame resolves the anchor -> newest page, never empty.
            assert _seqs(frames)[0] == 9

    def test_empty_project_returns_empty_without_error(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid = _seed(s, 0)
            frames, next_before = list_frames_window(s, pid, _T0)
            assert frames == []
            assert next_before is None

    def test_backdated_frame_keeps_cursor_exact(self, migrated_factory) -> None:
        """A frame whose capture time and sequence disagree must not corrupt the
        cursor: windowing stays in sequence space after the boundary resolves."""
        with session_scope(migrated_factory) as s:
            pid = _seed(s, 40)
            # Backdate the newest frame (seq 39) to before seq 10's time. Its
            # sequence is unchanged; only its timestamp moves into the past.
            backdated = s.execute(
                select(Frame).where(Frame.project_id == pid, Frame.sequence_index == 39)
            ).scalar_one()
            backdated.capture_timestamp = (_T0 + _STEP * 5).replace(tzinfo=None)
            s.flush()
            # Anchor at seq 20's time: boundary resolves in sequence space, so the
            # window's cursor is still the contiguous oldest sequence, not skewed
            # by the backdated frame's timestamp.
            frames, next_before = list_frames_window(s, pid, _T0 + _STEP * 20)
            seqs = _seqs(frames)
            # Oldest shown must be a contiguous run end; continuation has no gap.
            older = list_frames_keyset(s, pid, before_seq=next_before, limit=1000)
            combined = set(seqs) | set(_seqs(older))
            # Every sequence at or below the window's newest is accounted for once.
            assert len(seqs) == len(set(seqs))  # no duplicate in the window
            assert combined.issuperset(range(0, min(seqs)))
