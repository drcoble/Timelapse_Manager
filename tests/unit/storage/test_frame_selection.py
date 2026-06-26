"""Unit tests for range-descriptor resolution.

These pin the two genuinely different scope predicates that bound the whole
descriptor feature:

* ``in_range`` adds ``capture_timestamp IS NOT NULL`` -- a frame with no capture
  time is off the time axis, so a time-range selection never includes it.
* ``in_project`` adds no time predicate, so it INCLUDES null-timestamp frames.

Also covered: open-ended range bounds, the ``deselected_ids`` subtraction (and
that ``count`` subtracts only the in-range ones so ``count == len(resolve)``),
the ``include_deleted`` filter, the empty range, and the body parser's validation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.storage.frame_selection import (
    DescriptorError,
    DescriptorFilters,
    RangeDescriptor,
    TimeRange,
    count,
    materialize,
    parse_descriptor,
    resolve,
)

_T0 = datetime(2026, 3, 1, tzinfo=UTC)
_STEP = timedelta(hours=1)


def _seed(
    session,
    *,
    timed: int,
    untimed: int = 0,
    deleted: int = 0,
    name: str = "sel",
) -> tuple[int, list[int], list[int], list[int]]:
    """Seed a project; return (project_id, timed_ids, untimed_ids, deleted_ids).

    ``timed`` active frames carry hourly capture timestamps from ``_T0``;
    ``untimed`` active frames carry a NULL ``capture_timestamp``; ``deleted``
    soft-deleted frames are timed and follow the active ones in sequence.
    """
    cam = Camera(name=f"{name}-cam", address="127.0.0.1", protocol="vapix")
    session.add(cam)
    session.flush()
    proj = Project(camera_id=cam.id, name=name, lifecycle_state="active")
    session.add(proj)
    session.flush()

    seq = 0
    timed_ids: list[int] = []
    for i in range(timed):
        f = Frame(
            project_id=proj.id,
            sequence_index=seq,
            capture_timestamp=(_T0 + _STEP * i).replace(tzinfo=None),
            file_path=f"/frames/{seq:08d}.jpg",
            capture_status="captured",
            origin="captured",
            lifecycle_state="active",
        )
        session.add(f)
        session.flush()
        timed_ids.append(f.id)
        seq += 1

    untimed_ids: list[int] = []
    for _ in range(untimed):
        f = Frame(
            project_id=proj.id,
            sequence_index=seq,
            capture_timestamp=None,
            file_path=f"/frames/{seq:08d}.jpg",
            capture_status="captured",
            origin="captured",
            lifecycle_state="active",
        )
        session.add(f)
        session.flush()
        untimed_ids.append(f.id)
        seq += 1

    deleted_ids: list[int] = []
    for i in range(deleted):
        f = Frame(
            project_id=proj.id,
            sequence_index=seq,
            capture_timestamp=(_T0 + _STEP * (timed + i)).replace(tzinfo=None),
            file_path=f"/frames/{seq:08d}.jpg",
            capture_status="captured",
            origin="captured",
            lifecycle_state="soft_deleted",
        )
        session.add(f)
        session.flush()
        deleted_ids.append(f.id)
        seq += 1

    session.flush()
    return proj.id, timed_ids, untimed_ids, deleted_ids


def _range_desc(pid: int, *, frm=None, to=None, deselected=None) -> RangeDescriptor:
    return RangeDescriptor(
        scope="in_range",
        project_id=pid,
        time_range=TimeRange(time_from=frm, time_to=to),
        deselected_ids=deselected or [],
    )


def _project_desc(
    pid: int, *, include_deleted=False, deselected=None
) -> RangeDescriptor:
    return RangeDescriptor(
        scope="in_project",
        project_id=pid,
        filters=DescriptorFilters(include_deleted=include_deleted),
        deselected_ids=deselected or [],
    )


class TestScopeWhereClauses:
    def test_in_range_excludes_null_timestamp_frames(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid, timed, untimed, _ = _seed(s, timed=3, untimed=2)
            got = resolve(s, _range_desc(pid))
        # Only the timed frames; the two null-timestamp frames are off the axis.
        assert got == set(timed)
        assert not (got & set(untimed))

    def test_in_project_includes_null_timestamp_frames(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid, timed, untimed, _ = _seed(s, timed=3, untimed=2)
            got = resolve(s, _project_desc(pid))
        # The whole campaign, null-timestamp frames included.
        assert got == set(timed) | set(untimed)

    def test_discriminating_frame(self, migrated_factory) -> None:
        """A single null-ts frame: in_project keeps it, in_range drops it."""
        with session_scope(migrated_factory) as s:
            pid, _timed, untimed, _ = _seed(s, timed=0, untimed=1)
            fid = untimed[0]
            in_range = resolve(s, _range_desc(pid))
            in_project = resolve(s, _project_desc(pid))
        assert fid not in in_range
        assert fid in in_project


class TestRangeBounds:
    def test_closed_range(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid, timed, _, _ = _seed(s, timed=5)
            # Inclusive bounds covering frames at index 1, 2, 3 (hours 1..3).
            frm = (_T0 + _STEP * 1).replace(tzinfo=None)
            to = (_T0 + _STEP * 3).replace(tzinfo=None)
            got = resolve(s, _range_desc(pid, frm=frm, to=to))
        assert got == {timed[1], timed[2], timed[3]}

    def test_open_ended_from(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid, timed, _, _ = _seed(s, timed=5)
            # to only -> everything at or before hour 2 (indexes 0,1,2).
            to = (_T0 + _STEP * 2).replace(tzinfo=None)
            got = resolve(s, _range_desc(pid, to=to))
        assert got == {timed[0], timed[1], timed[2]}

    def test_open_ended_to(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid, timed, _, _ = _seed(s, timed=5)
            # from only -> everything at or after hour 3 (indexes 3,4).
            frm = (_T0 + _STEP * 3).replace(tzinfo=None)
            got = resolve(s, _range_desc(pid, frm=frm))
        assert got == {timed[3], timed[4]}

    def test_both_open_is_all_timed(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid, timed, untimed, _ = _seed(s, timed=4, untimed=2)
            got = resolve(s, _range_desc(pid))
        # Both bounds null = all timed frames (still excludes null-ts frames).
        assert got == set(timed)
        assert not (got & set(untimed))

    def test_aware_bound_is_normalised_to_utc(self, migrated_factory) -> None:
        """A tz-aware bound (what the client sends as ...Z) matches the naive
        UTC column rather than missing every frame."""
        with session_scope(migrated_factory) as s:
            pid, timed, _, _ = _seed(s, timed=3)
            frm = _T0 + _STEP * 1  # aware (UTC)
            got = resolve(s, _range_desc(pid, frm=frm))
        assert got == {timed[1], timed[2]}

    def test_empty_range(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid, _timed, _, _ = _seed(s, timed=3)
            # A window entirely after the last frame resolves to nothing.
            frm = (_T0 + _STEP * 100).replace(tzinfo=None)
            got = resolve(s, _range_desc(pid, frm=frm))
            n = count(s, _range_desc(pid, frm=frm))
        assert got == set()
        assert n == 0


class TestDeselectedSubtraction:
    def test_resolve_subtracts_deselected(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid, timed, _, _ = _seed(s, timed=4)
            got = resolve(s, _range_desc(pid, deselected=[timed[0], timed[2]]))
        assert got == {timed[1], timed[3]}

    def test_count_matches_resolve_with_deselected(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid, timed, _, _ = _seed(s, timed=5)
            desc = _range_desc(pid, deselected=[timed[1], timed[3]])
            assert count(s, desc) == len(resolve(s, desc))
            assert count(s, desc) == 3

    def test_count_ignores_out_of_range_deselected(self, migrated_factory) -> None:
        """A deselected id that is not in range must not lower the count."""
        with session_scope(migrated_factory) as s:
            pid, timed, untimed, _ = _seed(s, timed=3, untimed=1)
            # Deselect an untimed frame (never in an in_range result) -> no effect.
            desc = _range_desc(pid, deselected=[untimed[0]])
            assert count(s, desc) == 3
            assert resolve(s, desc) == set(timed)


class TestIncludeDeleted:
    def test_excludes_soft_deleted_by_default(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid, _timed, untimed, deleted = _seed(s, timed=2, untimed=1, deleted=2)
            got = resolve(s, _project_desc(pid))
        assert not (got & set(deleted))

    def test_includes_soft_deleted_when_requested(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid, timed, untimed, deleted = _seed(s, timed=2, untimed=1, deleted=2)
            got = resolve(s, _project_desc(pid, include_deleted=True))
        assert set(deleted) <= got
        assert got == set(timed) | set(untimed) | set(deleted)

    def test_in_range_include_deleted(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid, timed, _, deleted = _seed(s, timed=2, deleted=2)
            with_deleted = resolve(
                s,
                RangeDescriptor(
                    scope="in_range",
                    project_id=pid,
                    time_range=TimeRange(),
                    filters=DescriptorFilters(include_deleted=True),
                ),
            )
        # Deleted frames are timed, so an include_deleted in_range keeps them.
        assert set(deleted) <= with_deleted
        assert with_deleted == set(timed) | set(deleted)


class TestMaterialize:
    def test_materialize_is_sorted_resolve(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as s:
            pid, timed, _, _ = _seed(s, timed=4)
            ids = materialize(s, _range_desc(pid, deselected=[timed[1]]))
        assert ids == sorted(set(timed) - {timed[1]})
        assert ids == sorted(ids)


class TestParseDescriptor:
    def test_parse_in_range(self) -> None:
        desc = parse_descriptor(
            {
                "scope": "in_range",
                "project_id": 7,
                "time_range": {
                    "from": "2026-03-01T00:00:00Z",
                    "to": "2026-03-31T23:59:59Z",
                },
                "filters": {"include_deleted": False},
                "deselected_ids": [101, 102],
            }
        )
        assert desc.scope == "in_range"
        assert desc.project_id == 7
        assert desc.time_range is not None
        assert desc.time_range.time_from is not None
        assert desc.deselected_ids == [101, 102]

    def test_parse_in_project_no_time_range(self) -> None:
        desc = parse_descriptor({"scope": "in_project", "project_id": 3})
        assert desc.scope == "in_project"
        assert desc.time_range is None
        assert desc.deselected_ids == []

    def test_in_range_requires_time_range(self) -> None:
        with pytest.raises(DescriptorError):
            parse_descriptor({"scope": "in_range", "project_id": 1})

    def test_from_after_to_rejected(self) -> None:
        with pytest.raises(DescriptorError):
            parse_descriptor(
                {
                    "scope": "in_range",
                    "project_id": 1,
                    "time_range": {
                        "from": "2026-03-31T00:00:00Z",
                        "to": "2026-03-01T00:00:00Z",
                    },
                }
            )

    def test_open_ended_bounds_allowed(self) -> None:
        desc = parse_descriptor(
            {
                "scope": "in_range",
                "project_id": 1,
                "time_range": {"from": None, "to": None},
            }
        )
        assert desc.time_range is not None
        assert desc.time_range.time_from is None
        assert desc.time_range.time_to is None

    def test_unknown_scope_rejected(self) -> None:
        with pytest.raises(DescriptorError):
            parse_descriptor({"scope": "all_the_things", "project_id": 1})

    def test_bad_project_id_rejected(self) -> None:
        with pytest.raises(DescriptorError):
            parse_descriptor({"scope": "in_project", "project_id": 0})

    def test_oversized_deselected_rejected(self) -> None:
        with pytest.raises(DescriptorError):
            parse_descriptor(
                {
                    "scope": "in_project",
                    "project_id": 1,
                    "deselected_ids": list(range(10_001)),
                }
            )

    def test_bad_iso_datetime_rejected(self) -> None:
        with pytest.raises(DescriptorError):
            parse_descriptor(
                {
                    "scope": "in_range",
                    "project_id": 1,
                    "time_range": {"from": "not-a-date", "to": None},
                }
            )
