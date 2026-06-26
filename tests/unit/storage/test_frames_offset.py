"""Unit tests for the bulk timestamp-offset helper and its summary builder.

Pins three contracts:

* ``offset_timestamps_many`` applies a signed delta across an id-set,
  **skip-not-raise**: a missing id lands in ``OffsetResult.failed``, a
  null-timestamp frame lands in ``skipped_null`` (it has no time to move), and the
  rest land in ``shifted`` with one audit event each recording before/after.
* An inverse offset (``-seconds``) over the shifted ids round-trips them to their
  original capture times -- the contract Undo relies on.
* The web summary-builder shapes an ``OffsetResult`` into the response contract
  and materialises the inverse offset (negated seconds) over the shifted ids only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from timelapse_manager.db.models import Camera, Event, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.storage.frames import (
    OffsetResult,
    offset_timestamps_many,
)
from timelapse_manager.web.routers.frames import _build_offset_summary

_T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_STEP = timedelta(minutes=5)
_ACTOR_USER_ID = 1  # sentinel admin


def _seed(
    session, n: int, *, name: str = "offset", null_ts_index: int | None = None
) -> tuple[int, list[int]]:
    """Seed ``n`` frames; the frame at ``null_ts_index`` gets a null timestamp."""
    cam = Camera(name=f"{name}-cam", address="127.0.0.1", protocol="vapix")
    session.add(cam)
    session.flush()
    proj = Project(camera_id=cam.id, name=name, lifecycle_state="active")
    session.add(proj)
    session.flush()
    ids: list[int] = []
    for i in range(n):
        ts = None if i == null_ts_index else (_T0 + _STEP * i).replace(tzinfo=None)
        frame = Frame(
            project_id=proj.id,
            sequence_index=i,
            capture_timestamp=ts,
            file_path=f"/frames/{i:08d}.jpg",
            capture_status="captured",
            origin="captured",
            lifecycle_state="active",
        )
        session.add(frame)
        session.flush()
        ids.append(frame.id)
    session.flush()
    return proj.id, ids


def _ts(session, frame_id: int) -> datetime | None:
    frame = session.get(Frame, frame_id)
    assert frame is not None
    return frame.capture_timestamp


class TestOffsetTimestampsMany:
    def test_applies_signed_positive_delta(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            _pid, ids = _seed(s, 3)
            before = {fid: _ts(s, fid) for fid in ids}
            result = offset_timestamps_many(s, ids, 3600, _ACTOR_USER_ID)
        assert isinstance(result, OffsetResult)
        assert result.shifted == ids
        assert result.skipped_null == []
        assert result.failed == []
        with session_scope(migrated_factory) as s:
            for fid in ids:
                assert _ts(s, fid) == before[fid] + timedelta(seconds=3600)

    def test_applies_signed_negative_delta(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            _pid, ids = _seed(s, 2)
            before = {fid: _ts(s, fid) for fid in ids}
            offset_timestamps_many(s, ids, -1800, _ACTOR_USER_ID)
        with session_scope(migrated_factory) as s:
            for fid in ids:
                assert _ts(s, fid) == before[fid] - timedelta(seconds=1800)

    def test_null_timestamp_frame_is_skipped_and_reported(
        self, migrated_factory
    ) -> None:
        with session_scope(migrated_factory) as s:
            _pid, ids = _seed(s, 3, null_ts_index=1)
            result = offset_timestamps_many(s, ids, 600, _ACTOR_USER_ID)
        assert result.shifted == [ids[0], ids[2]]
        assert result.skipped_null == [ids[1]]
        assert result.failed == []
        with session_scope(migrated_factory) as s:
            # The null-timestamp frame is untouched (still null).
            assert _ts(s, ids[1]) is None

    def test_missing_id_is_reported_failed(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            _pid, ids = _seed(s, 2)
            bad = 9_999_999
            result = offset_timestamps_many(
                s, [ids[0], bad, ids[1]], 60, _ACTOR_USER_ID
            )
        assert result.shifted == [ids[0], ids[1]]
        assert result.skipped_null == []
        assert result.failed == [bad]

    def test_inverse_offset_round_trips(self, migrated_factory) -> None:
        # A negative shift then its positive inverse must return the originals,
        # exercising both signs.
        with session_scope(migrated_factory) as s:
            _pid, ids = _seed(s, 3)
            original = {fid: _ts(s, fid) for fid in ids}
            forward = offset_timestamps_many(s, ids, -7200, _ACTOR_USER_ID)
        with session_scope(migrated_factory) as s:
            # Undo replays the inverse (-seconds) over exactly the shifted ids.
            offset_timestamps_many(s, forward.shifted, 7200, _ACTOR_USER_ID)
        with session_scope(migrated_factory) as s:
            for fid in ids:
                assert _ts(s, fid) == original[fid]

    def test_event_records_before_after_and_delta(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid, ids = _seed(s, 2, null_ts_index=0)
            before_ts = _ts(s, ids[1])
            offset_timestamps_many(s, ids, 900, _ACTOR_USER_ID)
        with session_scope(migrated_factory) as s:
            events = (
                s.query(Event)
                .filter(Event.scope == "project")
                .filter(Event.scope_id == pid)
                .filter(Event.actor_user_id == _ACTOR_USER_ID)
                .all()
            )
        edits = [
            e
            for e in events
            if e.event_metadata.get("action") == "edit_capture_timestamp"
        ]
        # One event for the shifted frame; the null-timestamp frame writes none.
        assert len(edits) == 1
        meta = edits[0].event_metadata
        assert meta["frame_id"] == ids[1]
        assert meta["delta_seconds"] == 900
        assert meta["previous"] == before_ts.isoformat()
        assert meta["new"] == (before_ts + timedelta(seconds=900)).isoformat()

    def test_zero_seconds_is_a_no_op_shift(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            _pid, ids = _seed(s, 2)
            before = {fid: _ts(s, fid) for fid in ids}
            result = offset_timestamps_many(s, ids, 0, _ACTOR_USER_ID)
        assert result.shifted == ids
        with session_scope(migrated_factory) as s:
            for fid in ids:
                assert _ts(s, fid) == before[fid]


class TestOffsetSummaryBuilder:
    def test_shape_and_counts(self) -> None:
        result = OffsetResult(shifted=[1, 2, 3], skipped_null=[4], failed=[9])
        summary = _build_offset_summary(3600, result)
        assert summary["operation"] == "offset"
        assert summary["seconds"] == 3600
        assert summary["shifted"] == 3
        assert summary["skipped_null"] == 1
        assert summary["failed"] == 1
        assert summary["shifted_ids"] == [1, 2, 3]
        assert summary["skipped_null_ids"] == [4]
        assert summary["failed_ids"] == [9]

    def test_undo_is_inverse_offset_over_shifted_only(self) -> None:
        result = OffsetResult(shifted=[5, 6], skipped_null=[7], failed=[8])
        summary = _build_offset_summary(1800, result)
        # Undo negates the seconds and acts only on the shifted ids -- never the
        # skipped-null or failed ids.
        assert summary["undo"]["operation"] == "offset"
        assert summary["undo"]["seconds"] == -1800
        assert summary["undo"]["frame_ids"] == [5, 6]

    def test_undo_negation_round_trips_sign(self) -> None:
        # A negative apply yields a positive undo, so the panel can replay it.
        summary = _build_offset_summary(-900, OffsetResult(shifted=[1], failed=[]))
        assert summary["undo"]["seconds"] == 900
