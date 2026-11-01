"""Unit tests for a project's per-day storage growth-rate estimate.

Exercises :func:`estimate_growth_rate_bytes_per_day` over a real migrated
project: the "no rate" sentinels (nothing planned, no frames, finished, past
end date) and the arithmetic itself (average captured frame size x captures
per day), including that the average is taken over the *active* frame
footprint so soft-deleted frames never inflate the rate. Since #22 the captures
per day come from the full schedule: exact-time anchors (clock and solar) plus
interval ticks inside the schedule windows.

Fixtures local to this file by design, so this suite does not edit the shared
conftest.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

import pytest
from sqlalchemy.orm import Session

from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.storage.estimator import (
    SECONDS_PER_DAY,
    estimate_growth_rate_bytes_per_day,
)

# Buford, GA -- the deployment's coordinates.
LAT, LON = 34.123245, -83.94052
NY_SCHEDULE = {"enabled": True, "timezone": "America/New_York"}


def _clock(time: str, *, anchor_id: str = "a", enabled: bool = True) -> dict[str, Any]:
    return {"id": anchor_id, "kind": "clock", "time": time, "enabled": enabled}


def _seed_camera_and_project(
    session: Session,
    *,
    interval: int | None,
    anchors: list[dict[str, Any]] | None = None,
    schedule: dict[str, Any] | None = None,
    coordinates: tuple[float, float] | None = None,
    end_date: datetime | None = None,
    max_frame_count: int | None = None,
) -> int:
    cam = Camera(
        name="gr-cam",
        address="127.0.0.1",
        protocol="vapix",
        snapshot_uri="http://127.0.0.1/snap",
        geolocation_latitude=coordinates[0] if coordinates else None,
        geolocation_longitude=coordinates[1] if coordinates else None,
    )
    session.add(cam)
    session.flush()
    proj = Project(
        camera_id=cam.id,
        name="gr-proj",
        capture_interval_seconds=interval,
        exact_time_anchors=anchors,
        schedule=schedule,
        end_date=end_date,
        max_frame_count=max_frame_count,
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
    def test_no_interval_and_no_anchor_returns_none(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
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


def _rate_with_frames(
    factory,  # type: ignore[no-untyped-def]
    *,
    frames: int = 4,
    size: int = 1000,
    **project: Any,
) -> int | None:
    """Seed a project with *frames* active frames of *size* bytes; return its rate."""
    with session_scope(factory) as session:
        pid = _seed_camera_and_project(session, **project)
        for seq in range(1, frames + 1):
            _add_frame(session, pid, seq, size, "active")
        proj = session.get(Project, pid)
        assert proj is not None
        proj.frame_count = frames
    with session_scope(factory) as session:
        proj = session.get(Project, pid)
        assert proj is not None
        return estimate_growth_rate_bytes_per_day(session, proj)


class _MidJune(datetime):
    """``datetime`` whose ``now()`` is a UTC midnight in a week with no DST change."""

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> _MidJune:  # type: ignore[override]
        fixed = cls(2026, 6, 1, 0, 0, tzinfo=UTC)
        return fixed if tz is None else fixed.astimezone(tz)


class TestGrowthRateFromSchedule:
    """#22: captures per day from the full schedule, not only the interval."""

    def test_anchor_only_is_average_frame_size_per_day(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        # The prod case: no interval, one 12:00 anchor -> one frame a day.
        rate = _rate_with_frames(
            migrated_factory,
            interval=None,
            anchors=[_clock("12:00")],
            schedule=NY_SCHEDULE,
        )
        assert rate == 1000

    def test_two_anchors_are_two_frames_a_day(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        rate = _rate_with_frames(
            migrated_factory,
            interval=None,
            anchors=[_clock("07:00", anchor_id="a"), _clock("19:00", anchor_id="b")],
            schedule=NY_SCHEDULE,
        )
        assert rate == 2000

    def test_interval_and_anchors_both_count(  # type: ignore[no-untyped-def]
        self, migrated_factory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Hourly interval (always open) = 24, plus one noon anchor = 25 a day.
        # The rate is measured over the 7 local days from now, so pin "now" to a
        # week with no daylight-saving change: a 25-hour fall-back day in the
        # window really adds an hourly tick (25,143 instead of 25,000).
        from timelapse_manager.storage import estimator

        monkeypatch.setattr(estimator, "datetime", _MidJune)
        rate = _rate_with_frames(
            migrated_factory,
            interval=3600,
            anchors=[_clock("12:00")],
            schedule=NY_SCHEDULE,
        )
        assert rate == 25_000

    def test_interval_inside_a_window_counts_open_hours(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        # 08:00-18:00 with a 1-minute interval: 10 h of ticks = 600 a day. The
        # horizon starts at the real now, so allow the part-open edges.
        schedule = {
            **NY_SCHEDULE,
            "windows": [{"start_time": "08:00", "end_time": "18:00"}],
        }
        rate = _rate_with_frames(migrated_factory, interval=60, schedule=schedule)
        assert rate == pytest.approx(600 * 1000, rel=0.02)

    def test_weekday_mask_reduces_interval_not_anchors(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        # Weekdays only: interval 24 -> 24 * 5/7; the anchor still fires daily.
        schedule = {**NY_SCHEDULE, "day_of_week_mask": 0b0011111}
        rate = _rate_with_frames(
            migrated_factory,
            interval=3600,
            anchors=[_clock("12:00")],
            schedule=schedule,
        )
        # Part-open horizon edges and a DST week move it by a tick at most.
        assert rate == pytest.approx((24 * 5 / 7 + 1) * 1000, rel=0.02)

    def test_solar_anchor_with_coordinates(  # type: ignore[no-untyped-def]
        self, migrated_factory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pinned to a week with no daylight-saving change: across the fall-back
        # the 7-day horizon holds an eighth solar noon (1,143 instead of 1,000).
        from timelapse_manager.storage import estimator

        monkeypatch.setattr(estimator, "datetime", _MidJune)
        rate = _rate_with_frames(
            migrated_factory,
            interval=None,
            anchors=[{"id": "s", "kind": "solar_noon", "enabled": True}],
            schedule=NY_SCHEDULE,
            coordinates=(LAT, LON),
        )
        assert rate == 1000

    def test_solar_anchor_without_coordinates_has_no_rate(
        self, migrated_factory
    ) -> None:  # type: ignore[no-untyped-def]
        # The runner cannot fire it, so nothing is planned.
        rate = _rate_with_frames(
            migrated_factory,
            interval=None,
            anchors=[{"id": "s", "kind": "solar_noon", "enabled": True}],
            schedule=NY_SCHEDULE,
        )
        assert rate is None

    def test_disabled_anchor_has_no_rate(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        rate = _rate_with_frames(
            migrated_factory,
            interval=None,
            anchors=[_clock("12:00", enabled=False)],
            schedule=NY_SCHEDULE,
        )
        assert rate is None

    def test_no_schedule_at_all_has_no_rate(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        assert _rate_with_frames(migrated_factory, interval=None) is None

    def test_end_date_inside_horizon_keeps_the_daily_rate(
        self, migrated_factory, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        # Two days left: the rate is per day of the shortened horizon. Pin "now"
        # to a week with no DST change: a fall-back day inside the two-day
        # window really adds an hourly tick (24,511 instead of 24,000), which
        # failed this test on the real clock on 2026-10-30.
        from timelapse_manager.storage import estimator

        monkeypatch.setattr(estimator, "datetime", _MidJune)
        end = _MidJune.now(UTC).replace(tzinfo=None) + timedelta(days=2)
        rate = _rate_with_frames(
            migrated_factory, interval=3600, schedule=NY_SCHEDULE, end_date=end
        )
        assert rate == pytest.approx(24_000, rel=0.01)

    def test_past_end_date_has_no_rate(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        end = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
        rate = _rate_with_frames(
            migrated_factory,
            interval=None,
            anchors=[_clock("12:00")],
            schedule=NY_SCHEDULE,
            end_date=end,
        )
        assert rate is None

    def test_finished_campaign_has_no_rate(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        rate = _rate_with_frames(
            migrated_factory,
            frames=4,
            interval=60,
            anchors=[_clock("12:00")],
            schedule=NY_SCHEDULE,
            max_frame_count=4,
        )
        assert rate is None

    def test_no_frames_has_no_rate(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        rate = _rate_with_frames(
            migrated_factory,
            frames=0,
            interval=None,
            anchors=[_clock("12:00")],
            schedule=NY_SCHEDULE,
        )
        assert rate is None

    def test_caller_supplied_captures_per_day_is_used(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        with session_scope(migrated_factory) as session:
            pid = _seed_camera_and_project(session, interval=None)
            _add_frame(session, pid, 1, 1000, "active")
            proj = session.get(Project, pid)
            assert proj is not None
            proj.frame_count = 1
        with session_scope(migrated_factory) as session:
            proj = session.get(Project, pid)
            assert proj is not None
            assert (
                estimate_growth_rate_bytes_per_day(session, proj, captures_per_day=3.0)
                == 3000
            )
            assert (
                estimate_growth_rate_bytes_per_day(session, proj, captures_per_day=None)
                is None
            )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
