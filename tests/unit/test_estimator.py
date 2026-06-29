"""Unit tests for the projected storage estimator.

Covers the pure math (average-size fallback vs derivation, finite vs open-ended
projection, the frame-cap bound, degenerate inputs) and the database-aware
wrapper over a real migrated project (zero-frames default, with-frames average,
open-ended sentinel, frames-remaining floor).

Fixtures local to this file by design, so this suite does not edit the shared
conftest.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.storage.estimator import (
    DEFAULT_AVERAGE_FRAME_SIZE_BYTES,
    average_frame_size_bytes,
    estimate_for_project,
    estimate_project_storage,
)

_DAY = 86400


# ---------------------------------------------------------------------------
# average_frame_size_bytes -- pure fallback vs derivation
# ---------------------------------------------------------------------------


class TestAverageFrameSize:
    def test_zero_frames_uses_default(self) -> None:
        assert average_frame_size_bytes(0, 0) == DEFAULT_AVERAGE_FRAME_SIZE_BYTES

    def test_zero_usage_uses_default(self) -> None:
        # Frames exist but no measurable bytes -- fall back rather than average 0.
        assert average_frame_size_bytes(0, 10) == DEFAULT_AVERAGE_FRAME_SIZE_BYTES

    def test_negative_count_uses_default(self) -> None:
        assert average_frame_size_bytes(1000, -1) == DEFAULT_AVERAGE_FRAME_SIZE_BYTES

    def test_derives_average_from_usage_and_count(self) -> None:
        assert average_frame_size_bytes(1000, 4) == 250

    def test_average_floors_to_int(self) -> None:
        assert average_frame_size_bytes(10, 3) == 3


# ---------------------------------------------------------------------------
# estimate_project_storage -- pure projection core
# ---------------------------------------------------------------------------


class TestEstimateProjectStorage:
    def test_finite_duration_projects_finite_values(self) -> None:
        # 1 day / 60s interval = 1440 frames; * 1000 bytes each.
        frames, total = estimate_project_storage(
            interval_seconds=60,
            duration_seconds=_DAY,
            average_frame_size_bytes=1000,
        )
        assert frames == 1440
        assert total == 1_440_000

    def test_open_ended_duration_returns_sentinel(self) -> None:
        assert estimate_project_storage(60, None, 1000) == (None, None)

    def test_missing_interval_returns_sentinel(self) -> None:
        assert estimate_project_storage(None, _DAY, 1000) == (None, None)

    def test_non_positive_interval_returns_sentinel(self) -> None:
        assert estimate_project_storage(0, _DAY, 1000) == (None, None)

    def test_negative_duration_floors_to_zero(self) -> None:
        # An end already in the past must not project a negative frame count.
        frames, total = estimate_project_storage(60, -_DAY, 1000)
        assert frames == 0
        assert total == 0

    def test_frame_cap_bounds_the_projection(self) -> None:
        # Duration alone would yield 1440 frames; the cap of 100 wins.
        frames, total = estimate_project_storage(
            interval_seconds=60,
            duration_seconds=_DAY,
            average_frame_size_bytes=1000,
            max_frame_count=100,
        )
        assert frames == 100
        assert total == 100_000

    def test_frame_cap_above_duration_does_not_raise_projection(self) -> None:
        # A cap larger than the duration-based count leaves the projection alone.
        frames, _ = estimate_project_storage(
            interval_seconds=60,
            duration_seconds=_DAY,
            average_frame_size_bytes=1000,
            max_frame_count=100_000,
        )
        assert frames == 1440


# ---------------------------------------------------------------------------
# estimate_for_project -- database-aware wrapper
# ---------------------------------------------------------------------------


def _seed_project(
    factory,  # type: ignore[no-untyped-def]
    *,
    name: str,
    interval: int | None = 60,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    max_frame_count: int | None = None,
) -> int:
    with session_scope(factory) as session:
        cam = Camera(name=f"{name}-cam", address="127.0.0.1", protocol="vapix")
        session.add(cam)
        session.flush()
        proj = Project(
            camera_id=cam.id,
            name=name,
            capture_interval_seconds=interval,
            lifecycle_state="active",
            start_date=start_date,
            end_date=end_date,
            max_frame_count=max_frame_count,
        )
        session.add(proj)
        session.flush()
        return proj.id


def _add_frames(
    factory,  # type: ignore[no-untyped-def]
    project_id: int,
    sizes: list[int],
) -> None:
    with session_scope(factory) as session:
        proj = session.get(Project, project_id)
        for index, size in enumerate(sizes):
            session.add(
                Frame(
                    project_id=project_id,
                    sequence_index=index,
                    file_path=f"{project_id}/{index}.jpg",
                    file_size_bytes=size,
                    lifecycle_state="active",
                    origin="captured",
                )
            )
        proj.frame_count = len(sizes)


class TestEstimateForProject:
    def test_open_ended_project_returns_sentinel(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        pid = _seed_project(migrated_factory, name="open-ended", end_date=None)
        with session_scope(migrated_factory) as session:
            result = estimate_for_project(session, session.get(Project, pid))
        assert result == (None, None)

    def test_zero_frames_uses_default_average(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        start = datetime(2026, 7, 1, 0, 0)
        end = start + timedelta(days=1)
        pid = _seed_project(
            migrated_factory,
            name="zero-frames",
            interval=60,
            start_date=start,
            end_date=end,
        )
        with session_scope(migrated_factory) as session:
            total, remaining = estimate_for_project(session, session.get(Project, pid))
        # 1440 frames over the day, each at the default average; none captured yet.
        assert total == 1440 * DEFAULT_AVERAGE_FRAME_SIZE_BYTES
        assert remaining == 1440

    def test_with_frames_derives_average_from_usage(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        start = datetime(2026, 7, 1, 0, 0)
        end = start + timedelta(days=1)
        pid = _seed_project(
            migrated_factory,
            name="with-frames",
            interval=60,
            start_date=start,
            end_date=end,
        )
        _add_frames(migrated_factory, pid, [1000, 1000, 1000, 1000])  # avg 1000
        with session_scope(migrated_factory) as session:
            total, remaining = estimate_for_project(session, session.get(Project, pid))
        assert total == 1440 * 1000
        assert remaining == 1440 - 4

    def test_frame_cap_bounds_projection(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        start = datetime(2026, 7, 1, 0, 0)
        end = start + timedelta(days=1)
        pid = _seed_project(
            migrated_factory,
            name="capped",
            interval=60,
            start_date=start,
            end_date=end,
            max_frame_count=100,
        )
        with session_scope(migrated_factory) as session:
            total, remaining = estimate_for_project(session, session.get(Project, pid))
        assert total == 100 * DEFAULT_AVERAGE_FRAME_SIZE_BYTES
        assert remaining == 100

    def test_frames_remaining_floors_at_zero(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        # Already captured more than the projection allows -> remaining floors at 0.
        start = datetime(2026, 7, 1, 0, 0)
        end = start + timedelta(days=1)
        pid = _seed_project(
            migrated_factory,
            name="over-projection",
            interval=60,
            start_date=start,
            end_date=end,
            max_frame_count=10,
        )
        _add_frames(migrated_factory, pid, [500] * 25)  # well past the cap of 10
        with session_scope(migrated_factory) as session:
            _total, remaining = estimate_for_project(session, session.get(Project, pid))
        assert remaining == 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
