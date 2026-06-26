"""Unit tests for a project's per-day storage growth-rate estimate.

Exercises :func:`estimate_growth_rate_bytes_per_day` over a real migrated
project: the two "not enough data" sentinels (no interval, no frames) and the
arithmetic itself (average captured frame size x captures-per-day), including
that the average is taken over the *active* frame footprint so soft-deleted
frames never inflate the rate.

Fixtures local to this file by design, so this suite does not edit the shared
conftest.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.storage.estimator import (
    SECONDS_PER_DAY,
    estimate_growth_rate_bytes_per_day,
)


def _seed_camera_and_project(session: Session, *, interval: int | None) -> int:
    cam = Camera(
        name="gr-cam",
        address="127.0.0.1",
        protocol="vapix",
        snapshot_uri="http://127.0.0.1/snap",
    )
    session.add(cam)
    session.flush()
    proj = Project(
        camera_id=cam.id,
        name="gr-proj",
        capture_interval_seconds=interval,
        lifecycle_state="active",
        operational_status="idle",
        frame_count=0,
    )
    session.add(proj)
    session.flush()
    return proj.id


def _add_frame(
    session: Session, project_id: int, seq: int, size: int | None, state: str
) -> None:
    session.add(
        Frame(
            project_id=project_id,
            sequence_index=seq,
            capture_timestamp=datetime(2026, 1, 1, 0, seq, tzinfo=UTC).replace(
                tzinfo=None
            ),
            file_path=f"/frames/{seq:08d}.jpg",
            width=1,
            height=1,
            file_size_bytes=size,
            capture_status="captured",
            origin="captured",
            lifecycle_state=state,
        )
    )


class TestEstimateGrowthRate:
    def test_no_interval_returns_none(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        with session_scope(migrated_factory) as session:
            pid = _seed_camera_and_project(session, interval=None)
        with session_scope(migrated_factory) as session:
            proj = session.get(Project, pid)
            assert proj is not None
            assert estimate_growth_rate_bytes_per_day(session, proj) is None

    def test_non_positive_interval_returns_none(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        with session_scope(migrated_factory) as session:
            pid = _seed_camera_and_project(session, interval=0)
        with session_scope(migrated_factory) as session:
            proj = session.get(Project, pid)
            assert proj is not None
            assert estimate_growth_rate_bytes_per_day(session, proj) is None

    def test_zero_frame_count_returns_none(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        # A usable interval but nothing captured yet -> no measured rate.
        with session_scope(migrated_factory) as session:
            pid = _seed_camera_and_project(session, interval=60)
        with session_scope(migrated_factory) as session:
            proj = session.get(Project, pid)
            assert proj is not None
            assert proj.frame_count == 0
            assert estimate_growth_rate_bytes_per_day(session, proj) is None

    def test_rate_is_average_frame_size_times_captures_per_day(  # type: ignore[no-untyped-def]
        self, migrated_factory
    ) -> None:
        # 4 frames totalling 4000 bytes -> average 1000; interval 60s -> 1440
        # captures/day -> 1000 * 86400 / 60 == 1,440,000 bytes/day.
        with session_scope(migrated_factory) as session:
            pid = _seed_camera_and_project(session, interval=60)
            for seq in range(1, 5):
                _add_frame(session, pid, seq, 1000, "active")
            proj = session.get(Project, pid)
            assert proj is not None
            proj.frame_count = 4
        with session_scope(migrated_factory) as session:
            proj = session.get(Project, pid)
            assert proj is not None
            rate = estimate_growth_rate_bytes_per_day(session, proj)
        assert rate == 1000 * SECONDS_PER_DAY // 60
        assert rate == 1_440_000

    def test_excludes_soft_deleted_frames_from_average(  # type: ignore[no-untyped-def]
        self, migrated_factory
    ) -> None:
        # 4 active frames @ 1000 (total 4000) plus one large soft-deleted frame.
        # frame_count reflects the active frames only. If the soft-deleted frame
        # leaked into the usage sum, the average -- and the rate -- would change.
        with session_scope(migrated_factory) as session:
            pid = _seed_camera_and_project(session, interval=60)
            for seq in range(1, 5):
                _add_frame(session, pid, seq, 1000, "active")
            _add_frame(session, pid, 5, 999_999, "soft_deleted")
            proj = session.get(Project, pid)
            assert proj is not None
            proj.frame_count = 4
        with session_scope(migrated_factory) as session:
            proj = session.get(Project, pid)
            assert proj is not None
            rate = estimate_growth_rate_bytes_per_day(session, proj)
        # Average stays 1000 (the soft-deleted 999,999 is excluded), not inflated.
        assert rate == 1_440_000


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
