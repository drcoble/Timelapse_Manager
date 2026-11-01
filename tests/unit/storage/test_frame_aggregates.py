"""Equivalence tests for the batched frame reads (#66 C5, C6).

* :func:`project_frame_aggregates` must give, for every project, exactly what the
  per-project helpers it replaces on the dashboard give: ``latest_captured_at``,
  ``sum_project_disk_usage`` and ``latest_active_frame``. The seed covers the
  awkward cases: no frames, only soft-deleted frames, only uploads, null sizes
  and null timestamps, and historic photos imported *after* newer captures
  (high ids, old timestamps, low sequence indexes), where the preview must
  still be the highest ``sequence_index`` and not the highest id.
* :func:`gather_frames` must return the same :class:`FrameRef` list, in the same
  order, as the OFFSET page loop over :func:`list_frames` it replaced, and must
  leave the session's identity map empty.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import insert
from sqlalchemy.orm import Session, sessionmaker

from timelapse_manager.config import Settings
from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.encode.encoder import FrameRef
from timelapse_manager.encode.frame_source import _as_utc, gather_frames
from timelapse_manager.storage import resolve_absolute
from timelapse_manager.storage.frames import (
    FrameAggregates,
    latest_active_frame,
    latest_captured_at,
    list_frames,
    project_frame_aggregates,
    sum_project_disk_usage,
)

_BASE = datetime(2026, 3, 1, 12, 0)


def _frame(project_id: int, seq: int, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "project_id": project_id,
        "sequence_index": seq,
        "capture_timestamp": _BASE + timedelta(hours=seq),
        "file_path": f"{seq:08d}.jpg",
        "width": 1920,
        "height": 1080,
        "file_size_bytes": 1000 + seq,
        "capture_status": "captured",
        "origin": "captured",
        "lifecycle_state": "active",
        "excluded_at": None,
    }
    row.update(overrides)
    return row


def _projects(session: Session, names: list[str]) -> dict[str, int]:
    camera = Camera(name="cam")
    session.add(camera)
    session.flush()
    projects = {name: Project(camera_id=camera.id, name=name) for name in names}
    session.add_all(projects.values())
    session.flush()
    return {name: project.id for name, project in projects.items()}


def _seed_mixed(factory: sessionmaker[Session]) -> dict[str, int]:
    """Seed one project per awkward case; return ``{case: project id}``."""
    with session_scope(factory) as session:
        ids = _projects(
            session,
            ["mixed", "empty", "only_deleted", "only_uploads", "late_import"],
        )
        mixed, deleted, uploads, late = (
            ids["mixed"],
            ids["only_deleted"],
            ids["only_uploads"],
            ids["late_import"],
        )
        rows = [
            _frame(
                mixed,
                seq,
                origin="uploaded" if seq % 4 == 0 else "captured",
                lifecycle_state="soft_deleted" if seq % 5 == 0 else "active",
                file_size_bytes=None if seq % 3 == 0 else 1000 + seq,
                capture_timestamp=None if seq == 7 else _BASE + timedelta(hours=seq),
                excluded_at=_BASE if seq % 6 == 0 else None,
            )
            for seq in range(1, 41)
        ]
        # The newest captured frame of "mixed" is soft-deleted: it must not count.
        rows.append(_frame(mixed, 50, lifecycle_state="soft_deleted"))
        rows += [_frame(deleted, seq, lifecycle_state="soft_deleted") for seq in (1, 2)]
        rows += [_frame(uploads, seq, origin="uploaded") for seq in (1, 2, 3)]
        # A sizeless upload: the sum must treat NULL as nothing.
        rows.append(_frame(uploads, 4, origin="uploaded", file_size_bytes=None))
        # Live captures first (low ids, recent times, high sequence indexes) ...
        rows += [
            _frame(late, seq, capture_timestamp=_BASE + timedelta(days=30, hours=seq))
            for seq in range(10, 16)
        ]
        session.execute(insert(Frame), rows)
        # ... then historic photos imported later: higher ids, older timestamps,
        # lower sequence indexes (the resequence places them before the captures).
        session.execute(
            insert(Frame),
            [
                _frame(
                    late,
                    seq,
                    origin="uploaded",
                    capture_timestamp=_BASE - timedelta(days=365 - seq),
                )
                for seq in range(1, 10)
            ],
        )
    return ids


def _legacy(session: Session, project_id: int) -> FrameAggregates:
    """The three per-project reads the dashboard made before #66 C5."""
    latest = latest_active_frame(session, project_id)
    return FrameAggregates(
        last_captured_at=latest_captured_at(session, project_id),
        disk_usage_bytes=sum_project_disk_usage(session, project_id),
        latest_frame_id=latest.id if latest is not None else None,
    )


class TestProjectFrameAggregates:
    def test_matches_the_per_project_helpers_for_every_case(
        self, migrated_factory: sessionmaker[Session]
    ) -> None:
        ids = _seed_mixed(migrated_factory)

        with session_scope(migrated_factory) as session:
            batched = project_frame_aggregates(session, list(ids.values()))
            legacy = {pid: _legacy(session, pid) for pid in ids.values()}

        assert batched == legacy
        assert set(batched) == set(ids.values())

    def test_types_match_the_helpers(
        self, migrated_factory: sessionmaker[Session]
    ) -> None:
        """The CASE keeps the column's DateTime type: no string leaks out."""
        ids = _seed_mixed(migrated_factory)

        with session_scope(migrated_factory) as session:
            stats = project_frame_aggregates(session, [ids["mixed"]])[ids["mixed"]]

        assert isinstance(stats.last_captured_at, datetime)
        assert isinstance(stats.disk_usage_bytes, int)
        assert isinstance(stats.latest_frame_id, int)

    def test_awkward_cases_have_the_expected_values(
        self, migrated_factory: sessionmaker[Session]
    ) -> None:
        ids = _seed_mixed(migrated_factory)

        with session_scope(migrated_factory) as session:
            stats = project_frame_aggregates(session, list(ids.values()))
            late_preview = latest_active_frame(session, ids["late_import"])
            assert late_preview is not None
            late_max_id = max(
                frame.id
                for frame in session.query(Frame).filter_by(
                    project_id=ids["late_import"]
                )
            )

        assert stats[ids["empty"]] == FrameAggregates()
        assert stats[ids["only_deleted"]] == FrameAggregates()
        uploads = stats[ids["only_uploads"]]
        assert uploads.last_captured_at is None
        assert uploads.disk_usage_bytes == 1001 + 1002 + 1003
        assert uploads.latest_frame_id is not None
        # The preview is the highest sequence index, not the highest id.
        late = stats[ids["late_import"]]
        assert late.latest_frame_id == late_preview.id
        assert late_preview.sequence_index == 15
        assert late.latest_frame_id != late_max_id
        assert late.last_captured_at == _BASE + timedelta(days=30, hours=15)

    def test_no_ids_issue_no_query(
        self, migrated_factory: sessionmaker[Session]
    ) -> None:
        with session_scope(migrated_factory) as session:
            assert project_frame_aggregates(session, []) == {}

    def test_unknown_and_duplicate_ids(
        self, migrated_factory: sessionmaker[Session]
    ) -> None:
        ids = _seed_mixed(migrated_factory)
        mixed = ids["mixed"]

        with session_scope(migrated_factory) as session:
            stats = project_frame_aggregates(session, [mixed, mixed, 9999])
            expected = _legacy(session, mixed)

        assert stats == {mixed: expected, 9999: FrameAggregates()}


# ---------------------------------------------------------------------------
# gather_frames (C6)
# ---------------------------------------------------------------------------

_OLD_PAGE_SIZE = 1000


def _legacy_gather(
    session: Session, settings: Settings, project_id: int
) -> list[FrameRef]:
    """The OFFSET page loop ``gather_frames`` used before #66 C6."""
    refs: list[FrameRef] = []
    offset = 0
    while True:
        batch = list_frames(
            session,
            project_id,
            limit=_OLD_PAGE_SIZE,
            offset=offset,
            include_deleted=False,
            include_excluded=False,
        )
        if not batch:
            break
        for frame in batch:
            if frame.capture_timestamp is None or frame.file_path is None:
                continue
            refs.append(
                FrameRef(
                    sequence_index=frame.sequence_index,
                    capture_timestamp=_as_utc(frame.capture_timestamp),
                    absolute_path=resolve_absolute(
                        settings, project_id, frame.file_path
                    ),
                    width=frame.width,
                    height=frame.height,
                )
            )
        if len(batch) < _OLD_PAGE_SIZE:
            break
        offset += _OLD_PAGE_SIZE
    return refs


def _seed_gather(factory: sessionmaker[Session], count: int) -> tuple[int, int]:
    """Seed ``count`` frames with ties, gaps and filtered rows, plus a decoy project."""
    with session_scope(factory) as session:
        ids = _projects(session, ["target", "decoy"])
        target, decoy = ids["target"], ids["decoy"]
        rows = []
        for seq in range(1, count + 1):
            # Inserted in a scrambled time order; every block of 7 shares one
            # timestamp, so ordering leans on the sequence tie-break.
            hour = (seq * 37) % count // 7
            rows.append(
                _frame(
                    target,
                    seq,
                    capture_timestamp=(
                        None if seq % 97 == 0 else _BASE + timedelta(hours=hour)
                    ),
                    file_path=None if seq % 89 == 0 else f"{seq:08d}.jpg",
                    lifecycle_state="soft_deleted" if seq % 41 == 0 else "active",
                    excluded_at=_BASE if seq % 43 == 0 else None,
                    width=None if seq % 13 == 0 else 1920,
                    height=None if seq % 13 == 0 else 1080,
                )
            )
        rows += [_frame(decoy, seq) for seq in range(1, 50)]
        session.execute(insert(Frame), rows)
    return target, decoy


class TestGatherFrames:
    @pytest.mark.parametrize("count", [0, 1, 999, 1000, 1001, 2500])
    def test_matches_the_offset_loop(
        self,
        migrated_factory: sessionmaker[Session],
        settings: Settings,
        count: int,
    ) -> None:
        target, _decoy = _seed_gather(migrated_factory, count)

        with session_scope(migrated_factory) as session:
            expected = _legacy_gather(session, settings, target)
        with session_scope(migrated_factory) as session:
            sequence = gather_frames(session, settings, target)

        assert sequence.project_id == target
        assert list(sequence.frames) == expected

    def test_filters_and_tie_order(
        self, migrated_factory: sessionmaker[Session], settings: Settings
    ) -> None:
        """Spot-check the shape directly, independent of the oracle."""
        target, _decoy = _seed_gather(migrated_factory, 2500)

        with session_scope(migrated_factory) as session:
            frames = list(gather_frames(session, settings, target).frames)

        seqs = [ref.sequence_index for ref in frames]
        assert not any(
            seq % 97 == 0 or seq % 89 == 0 or seq % 41 == 0 or seq % 43 == 0
            for seq in seqs
        )
        keys = [(ref.capture_timestamp, ref.sequence_index) for ref in frames]
        assert keys == sorted(keys)
        assert len({ref.capture_timestamp for ref in frames}) < len(frames)
        assert all(ref.capture_timestamp.tzinfo is not None for ref in frames)

    def test_loads_no_orm_instances(
        self, migrated_factory: sessionmaker[Session], settings: Settings
    ) -> None:
        target, _decoy = _seed_gather(migrated_factory, 1500)

        with session_scope(migrated_factory) as session:
            sequence = gather_frames(session, settings, target)
            assert len(session.identity_map) == 0

        assert len(sequence) > 0
