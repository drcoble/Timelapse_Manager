"""Suggested render frame rate (#301): the pure function, the query and the schedule."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session, sessionmaker

from timelapse_manager.config.settings import Settings
from timelapse_manager.db.models import Camera, Frame, Project, RenderJob
from timelapse_manager.db.session import session_scope
from timelapse_manager.render import RenderQueue, RenderScheduler
from timelapse_manager.render import fps_suggest as fs
from timelapse_manager.render import settings as render_settings

MIN = 60
HOUR = 3600
DAY = 86400


def _series(interval: float, n: int = 40) -> list[float]:
    return [1_700_000_000 + i * interval for i in range(n)]


class TestSuggestFps:
    @pytest.mark.parametrize(
        ("interval", "expected"),
        [
            (1 * MIN, 30),
            (2 * MIN, 30),
            (5 * MIN, 30),
            (10 * MIN, 30),
            (15 * MIN, 30),
            (30 * MIN, 24),
            (1 * HOUR, 24),
            (2 * HOUR, 20),
            (3 * HOUR, 20),
            (6 * HOUR, 15),
            (12 * HOUR, 15),
            (1 * DAY, 12),
            (2 * DAY, 10),
            (3 * DAY, 10),
            (7 * DAY, 8),
        ],
    )
    def test_table_from_the_requirement(self, interval: int, expected: int) -> None:
        assert fs.suggest_fps(_series(interval)) == expected

    def test_clamps_outside_the_anchors(self) -> None:
        assert fs.suggest_fps(_series(5)) == 30
        assert fs.suggest_fps(_series(60 * DAY)) == 8

    def test_non_increasing_as_the_interval_grows(self) -> None:
        rates = [fs.fps_for_interval(s) for s in range(1, 20 * DAY, 197)]
        assert rates == sorted(rates, reverse=True)

    def test_always_within_8_and_30(self) -> None:
        for seconds in (0.5, 1, 59, 61, 599, 3601, 40000, DAY, 9 * DAY, 400 * DAY):
            assert 8 <= fs.fps_for_interval(seconds) <= 30

    def test_tie_goes_to_the_higher_rate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A base of exactly 22 sits between 20 and 24.
        monkeypatch.setattr(fs, "ANCHORS", ((60, 22.0), (600, 22.0)))
        assert fs.fps_for_interval(100) == 24

    def test_an_outage_does_not_change_the_result(self) -> None:
        times = _series(5 * MIN, 60)
        gapped = times[:30] + [t + 3 * DAY for t in times[30:]]
        assert fs.suggest_fps(gapped) == fs.suggest_fps(times) == 30

    def test_weekday_mask_daily_series_is_12(self) -> None:
        # Mon-Fri at noon: four 1-day gaps and one 3-day gap per week.
        start = datetime(2026, 1, 5, 12, tzinfo=UTC)
        days = [start + timedelta(days=d) for d in range(60) if (d % 7) < 5]
        assert fs.suggest_fps([d.timestamp() for d in days]) == 12

    @pytest.mark.parametrize("count", [0, 1])
    def test_fewer_than_two_frames_is_none(self, count: int) -> None:
        assert fs.suggest_fps(_series(60, count)) is None

    def test_identical_timestamps_are_none(self) -> None:
        assert fs.suggest_fps([5.0, 5.0, 5.0]) is None


class TestLabels:
    def test_interval_phrases(self) -> None:
        assert fs.describe_interval(30) == "30 seconds"
        assert fs.describe_interval(60) == "1 minute"
        assert fs.describe_interval(300) == "5 minutes"
        assert fs.describe_interval(3600) == "1 hour"
        assert fs.describe_interval(DAY) == "1 day"
        assert fs.describe_interval(3 * DAY) == "3 days"

    def test_durations(self) -> None:
        assert fs.describe_duration(42) == "42 s"
        assert fs.describe_duration(125) == "2 min 5 s"
        assert fs.describe_duration(120) == "2 min"

    def test_label(self) -> None:
        suggestion = fs.FpsSuggestion(12, DAY, 504, 42.0)
        assert fs.suggestion_label(suggestion) == (
            "Suggested: 12 fps (captures about every 1 day) · about 42 s"
        )
        assert fs.suggestion_label(fs.FpsSuggestion(None, None, 1, None)) == ""


def _seed(
    factory: sessionmaker[Session],
    interval: timedelta,
    count: int,
    *,
    excluded: int = 0,
    first: datetime = datetime(2026, 1, 1, 12),
) -> int:
    with session_scope(factory) as session:
        cam = Camera(name="fps-cam", address="10.0.0.5", protocol="vapix")
        session.add(cam)
        session.flush()
        project = Project(
            camera_id=cam.id,
            name="fps",
            lifecycle_state="active",
            operational_status="idle",
        )
        session.add(project)
        session.flush()
        for i in range(count):
            session.add(
                Frame(
                    project_id=project.id,
                    sequence_index=i,
                    capture_timestamp=first + interval * i,
                    file_path=f"{i:08d}.jpg",
                    capture_status="captured",
                    origin="captured",
                    lifecycle_state="active",
                    excluded_at=first if i < excluded else None,
                )
            )
        return project.id


class TestSuggestForProject:
    def test_five_minute_project_suggests_30(
        self, migrated_factory: sessionmaker[Session]
    ) -> None:
        pid = _seed(migrated_factory, timedelta(minutes=5), 100)
        with session_scope(migrated_factory) as session:
            found = fs.suggest_for_project(session, pid)
        assert found.fps == 30
        assert found.interval_seconds == 300
        assert found.frame_count == 100
        assert found.expected_duration_seconds == pytest.approx(100 / 30)

    def test_noon_anchor_project_suggests_12(
        self, migrated_factory: sessionmaker[Session]
    ) -> None:
        pid = _seed(migrated_factory, timedelta(days=1), 100)
        with session_scope(migrated_factory) as session:
            assert fs.suggest_for_project(session, pid).fps == 12

    def test_range_bounds_and_excluded_frames(
        self, migrated_factory: sessionmaker[Session]
    ) -> None:
        pid = _seed(migrated_factory, timedelta(hours=1), 48, excluded=8)
        with session_scope(migrated_factory) as session:
            everything = fs.suggest_for_project(session, pid)
            ranged = fs.suggest_for_project(
                session,
                pid,
                start=datetime(2026, 1, 2, 12, tzinfo=UTC),
                end=datetime(2026, 1, 2, 20, 30, tzinfo=UTC),
            )
        assert everything.frame_count == 40  # excluded frames are not rendered
        assert ranged.frame_count == 9
        assert ranged.fps == 24

    def test_too_few_frames_is_none(
        self, migrated_factory: sessionmaker[Session]
    ) -> None:
        pid = _seed(migrated_factory, timedelta(minutes=5), 1)
        with session_scope(migrated_factory) as session:
            found = fs.suggest_for_project(session, pid)
        assert found == fs.FpsSuggestion(None, None, 1, None)

    def test_reads_only_the_timestamp_column(
        self, migrated_factory: sessionmaker[Session]
    ) -> None:
        pid = _seed(migrated_factory, timedelta(minutes=5), 5)
        statements: list[str] = []
        from sqlalchemy import event

        engine = migrated_factory.kw["bind"]

        def _capture(conn: Any, cursor: Any, statement: str, *_: Any) -> None:
            statements.append(" ".join(statement.split()))

        event.listen(engine, "before_cursor_execute", _capture)
        try:
            with session_scope(migrated_factory) as session:
                fs.suggest_for_project(session, pid)
        finally:
            event.remove(engine, "before_cursor_execute", _capture)
        [sql] = [s for s in statements if "FROM frame" in s]
        assert sql.startswith("SELECT frame.capture_timestamp FROM frame"), sql
        assert "file_path" not in sql


class TestScheduleAuto:
    def test_old_schedule_is_unchanged(self) -> None:
        old = {"enabled": True, "interval_seconds": 86400, "fps": 24}
        assert render_settings.fps_is_auto(old) is False
        view = render_settings.render_settings_view(old)
        assert view["fps"] == 24 and view["fps_auto"] is False
        assert render_settings.output_settings_from_schedule(old)["fps"] == 24  # type: ignore[index]
        assert render_settings.fps_is_auto(None) is False

    def test_auto_reads_back_and_never_leaks_into_a_job(self) -> None:
        auto = {"enabled": True, "interval_seconds": 86400, "fps": "auto"}
        assert render_settings.fps_is_auto(auto) is True
        view = render_settings.render_settings_view(auto)
        assert view["fps_auto"] is True
        assert isinstance(view["fps"], int)
        out = render_settings.output_settings_from_schedule(auto)
        assert out is not None and isinstance(out["fps"], int)

    @staticmethod
    def _scheduler(
        factory: sessionmaker[Session], settings: Settings
    ) -> RenderScheduler:
        queue = RenderQueue(settings, factory, encoder=MagicMock())
        return RenderScheduler(settings, factory, queue)

    def _set_schedule(self, factory: sessionmaker[Session], pid: int, fps: Any) -> None:
        with session_scope(factory) as session:
            project = session.get(Project, pid)
            assert project is not None
            project.render_schedule = {
                "enabled": True,
                "interval_seconds": 3600,
                "encoder": "libx264",
                "container": "mp4",
                "fps": fps,
                "resolution": "1920x1080",
            }

    def _fire(
        self,
        factory: sessionmaker[Session],
        settings: Settings,
    ) -> dict[str, Any]:
        created = self._scheduler(factory, settings)._enqueue_due_jobs(  # noqa: SLF001
            datetime(2030, 1, 1, tzinfo=UTC)
        )
        assert len(created) == 1
        with session_scope(factory) as session:
            job = session.get(RenderJob, created[0])
            assert job is not None
            return dict(job.output_settings or {})

    def test_auto_resolves_from_the_frames_when_it_fires(
        self,
        migrated_factory: sessionmaker[Session],
        settings_no_autostart: Settings,
    ) -> None:
        pid = _seed(migrated_factory, timedelta(days=1), 30)
        self._set_schedule(migrated_factory, pid, "auto")
        assert self._fire(migrated_factory, settings_no_autostart)["fps"] == 12

    def test_auto_with_no_suggestion_uses_the_default(
        self,
        migrated_factory: sessionmaker[Session],
        settings_no_autostart: Settings,
    ) -> None:
        pid = _seed(migrated_factory, timedelta(days=1), 1)
        self._set_schedule(migrated_factory, pid, "auto")
        got = self._fire(migrated_factory, settings_no_autostart)
        assert got["fps"] == settings_no_autostart.render.default_fps

    def test_fixed_rate_schedule_ignores_the_frames(
        self,
        migrated_factory: sessionmaker[Session],
        settings_no_autostart: Settings,
    ) -> None:
        pid = _seed(migrated_factory, timedelta(days=1), 30)
        self._set_schedule(migrated_factory, pid, 24)
        assert self._fire(migrated_factory, settings_no_autostart)["fps"] == 24
