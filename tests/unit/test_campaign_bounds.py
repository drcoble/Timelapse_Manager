"""Unit tests for project campaign bounds (start/end date + frame cap).

Covers the model + migration round-trip for the three new columns, and the
supervisor's runtime enforcement under a deterministic fake clock:

- a project whose ``start_date`` is in the future captures nothing;
- a project that reaches ``max_frame_count`` stops at *exactly* the cap, ends
  its runner, and is archived;
- a project past its ``end_date`` is dropped from the qualifying set and archived
  via the reconcile seam (end-date enforcement lives in ``_load_targets`` /
  reconcile, not in the per-project capture loop).

The fake clock follows the project's existing test shape (see the soak test): its
``sleep`` advances ``now`` and a generous safety cap raises ``CancelledError`` so
a broken loop fails fast instead of hanging.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.conftest import FakeAdapter
from timelapse_manager.capture.supervisor import (
    CaptureState,
    CaptureSupervisor,
    CaptureTarget,
    _campaign_end_reason,
)
from timelapse_manager.config.settings import (
    CaptureSettings,
    DatabaseSettings,
    LoggingSettings,
    PathsSettings,
    Settings,
)
from timelapse_manager.db.models import Camera, Event, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.storage.monitor import DiskSpaceMonitor

_UTC = UTC
_INTERVAL = 10
# Generous safety bound: a correct frame-cap / start-date loop ends (or never
# captures) well within this many fake cycles. If a regression makes the loop run
# forever, the clock cancels it instead of hanging the test.
_MAX_CYCLES = 50


def _permissive_disk_monitor() -> DiskSpaceMonitor:
    return DiskSpaceMonitor(
        low_watermark_bytes=1,
        low_watermark_percent=0.001,
        resume_watermark_bytes=1,
        resume_watermark_percent=0.001,
        check_interval_seconds=0.0,
        get_free_bytes=lambda _p: 10**15,
        get_total_bytes=lambda _p: 10**15,
    )


class FakeClock:
    """Deterministic clock: ``sleep`` advances ``now`` and counts cycles."""

    def __init__(self, start: datetime) -> None:
        self._now = start
        self.cycle_count = 0

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self._now += timedelta(seconds=max(0.0, seconds))
        self.cycle_count += 1
        if self.cycle_count >= _MAX_CYCLES:
            raise asyncio.CancelledError


def _make_settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    db_path = tmp_path / "test.db"
    return Settings(
        database=DatabaseSettings(url=f"sqlite:///{db_path}"),
        logging=LoggingSettings(level="WARNING", format="text"),
        paths=PathsSettings(
            data_dir=data_dir,
            frames_root=data_dir / "frames",
            token_file=data_dir / ".local-token",
        ),
        capture=CaptureSettings(
            autostart=False,
            timeout_seconds=5.0,
            max_idle_sleep_seconds=float(_INTERVAL),
            frozen_frame_enabled=False,
        ),
    )


def _seed_camera_and_project(
    migrated_factory,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    *,
    name: str,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    max_frame_count: int | None = None,
) -> dict:
    storage = tmp_path / "frames" / name
    storage.mkdir(parents=True, exist_ok=True)
    with session_scope(migrated_factory) as session:
        cam = Camera(
            name=f"{name}-cam",
            address="127.0.0.1",
            protocol="vapix",
            snapshot_uri="http://127.0.0.1/snap",
        )
        session.add(cam)
        session.flush()
        cam_id = cam.id
        proj = Project(
            camera_id=cam_id,
            name=name,
            capture_interval_seconds=_INTERVAL,
            lifecycle_state="active",
            operational_status="idle",
            storage_path=str(storage),
            start_date=start_date,
            end_date=end_date,
            max_frame_count=max_frame_count,
        )
        session.add(proj)
        session.flush()
        project_id = proj.id
    return {"camera_id": cam_id, "project_id": project_id, "storage_path": storage}


async def _run_one_loop(
    supervisor: CaptureSupervisor, target: CaptureTarget, state: CaptureState
) -> None:
    """Run ``_run_project`` with a fake camera until it ends or is cancelled."""
    with (
        patch.object(supervisor, "_load_camera", return_value=MagicMock()),
        patch(
            "timelapse_manager.capture.supervisor.build_adapter",
            return_value=FakeAdapter(),
        ),
        contextlib.suppress(asyncio.CancelledError),
    ):
        await supervisor._run_project(target, state)


def _frame_count(migrated_factory, project_id: int) -> int:  # type: ignore[no-untyped-def]
    with session_scope(migrated_factory) as session:
        return session.get(Project, project_id).frame_count


def _lifecycle(migrated_factory, project_id: int) -> str:  # type: ignore[no-untyped-def]
    with session_scope(migrated_factory) as session:
        return session.get(Project, project_id).lifecycle_state


# ---------------------------------------------------------------------------
# Model + migration round-trip
# ---------------------------------------------------------------------------


class TestCampaignBoundsRoundTrip:
    def test_columns_persist(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        start = datetime(2026, 7, 1, 8, 0)
        end = datetime(2026, 7, 10, 18, 0)
        with session_scope(migrated_factory) as session:
            cam = Camera(name="rt-cam", address="10.0.0.1", protocol="vapix")
            session.add(cam)
            session.flush()
            proj = Project(
                camera_id=cam.id,
                name="rt-proj",
                capture_interval_seconds=30,
                lifecycle_state="active",
                start_date=start,
                end_date=end,
                max_frame_count=500,
            )
            session.add(proj)
            session.flush()
            pid = proj.id

        with session_scope(migrated_factory) as session:
            reloaded = session.get(Project, pid)
            assert reloaded.start_date == start
            assert reloaded.end_date == end
            assert reloaded.max_frame_count == 500

    def test_columns_default_to_null(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        with session_scope(migrated_factory) as session:
            cam = Camera(name="rt-cam2", address="10.0.0.2", protocol="vapix")
            session.add(cam)
            session.flush()
            proj = Project(
                camera_id=cam.id,
                name="rt-proj2",
                capture_interval_seconds=30,
                lifecycle_state="active",
            )
            session.add(proj)
            session.flush()
            pid = proj.id
        with session_scope(migrated_factory) as session:
            reloaded = session.get(Project, pid)
            assert reloaded.start_date is None
            assert reloaded.end_date is None
            assert reloaded.max_frame_count is None


# ---------------------------------------------------------------------------
# _campaign_end_reason pure helper (frame cap reason)
# ---------------------------------------------------------------------------


class TestCampaignEndReason:
    def test_no_bounds_means_not_ended(self) -> None:
        now = datetime(2026, 7, 1, tzinfo=_UTC)
        assert (
            _campaign_end_reason(
                now=now, end_date=None, frame_count=10, max_frame_count=None
            )
            is None
        )

    def test_end_date_reached(self) -> None:
        now = datetime(2026, 7, 1, tzinfo=_UTC)
        reason = _campaign_end_reason(
            now=now,
            end_date=datetime(2026, 6, 30, tzinfo=_UTC),
            frame_count=0,
            max_frame_count=None,
        )
        assert reason == "end_date"

    def test_frame_cap_reached(self) -> None:
        now = datetime(2026, 7, 1, tzinfo=_UTC)
        reason = _campaign_end_reason(
            now=now, end_date=None, frame_count=5, max_frame_count=5
        )
        assert reason == "frame_count"

    def test_frame_cap_not_yet_reached(self) -> None:
        now = datetime(2026, 7, 1, tzinfo=_UTC)
        assert (
            _campaign_end_reason(
                now=now, end_date=None, frame_count=4, max_frame_count=5
            )
            is None
        )


# ---------------------------------------------------------------------------
# Start-date gating (drives _run_project under the fake clock)
# ---------------------------------------------------------------------------


class TestStartDateGate:
    async def test_no_capture_before_start_date(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        start = datetime(2026, 7, 1, 12, 0, tzinfo=_UTC)
        clock = FakeClock(start=datetime(2026, 7, 1, 8, 0, tzinfo=_UTC))
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings,
            migrated_factory,
            clock=clock,
            disk_monitor=_permissive_disk_monitor(),
        )
        ctx = _seed_camera_and_project(
            migrated_factory, tmp_path, name="future-start", start_date=start
        )
        target = CaptureTarget(
            project_id=ctx["project_id"],
            project_name="future-start",
            camera_id=ctx["camera_id"],
            interval_seconds=_INTERVAL,
            storage_path=str(ctx["storage_path"]),
            start_date=start,
        )
        state = CaptureState(project_id=ctx["project_id"], camera_id=ctx["camera_id"])

        await _run_one_loop(supervisor, target, state)

        # The clock never reached the start instant, so no frame was ever written
        # and the loop reported a "window" pause throughout.
        assert _frame_count(migrated_factory, ctx["project_id"]) == 0
        assert state.frames_captured == 0
        assert state.pause_reason == "window"
        await supervisor.stop()

    async def test_capture_runs_once_start_date_passes(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        # Start instant is only one interval ahead, so once the fake clock crosses
        # it the loop must begin capturing.
        clock = FakeClock(start=datetime(2026, 7, 1, 8, 0, tzinfo=_UTC))
        start = clock.now() + timedelta(seconds=_INTERVAL)
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings,
            migrated_factory,
            clock=clock,
            disk_monitor=_permissive_disk_monitor(),
        )
        ctx = _seed_camera_and_project(
            migrated_factory, tmp_path, name="soon-start", start_date=start
        )
        target = CaptureTarget(
            project_id=ctx["project_id"],
            project_name="soon-start",
            camera_id=ctx["camera_id"],
            interval_seconds=_INTERVAL,
            storage_path=str(ctx["storage_path"]),
            start_date=start,
        )
        state = CaptureState(project_id=ctx["project_id"], camera_id=ctx["camera_id"])

        await _run_one_loop(supervisor, target, state)

        assert _frame_count(migrated_factory, ctx["project_id"]) > 0
        await supervisor.stop()


# ---------------------------------------------------------------------------
# Frame-cap enforcement (exactly N, runner ends, project archived)
# ---------------------------------------------------------------------------


class TestFrameCap:
    async def test_stops_at_exactly_cap_and_archives(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        cap = 3
        clock = FakeClock(start=datetime(2026, 7, 1, 8, 0, tzinfo=_UTC))
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings,
            migrated_factory,
            clock=clock,
            disk_monitor=_permissive_disk_monitor(),
        )
        ctx = _seed_camera_and_project(
            migrated_factory, tmp_path, name="capped", max_frame_count=cap
        )
        target = CaptureTarget(
            project_id=ctx["project_id"],
            project_name="capped",
            camera_id=ctx["camera_id"],
            interval_seconds=_INTERVAL,
            storage_path=str(ctx["storage_path"]),
            max_frame_count=cap,
        )
        state = CaptureState(project_id=ctx["project_id"], camera_id=ctx["camera_id"])

        await _run_one_loop(supervisor, target, state)

        # The loop must have ended itself (no overshoot, did not hit the safety
        # cycle bound) at exactly the cap, and archived the project.
        assert clock.cycle_count < _MAX_CYCLES
        assert _frame_count(migrated_factory, ctx["project_id"]) == cap
        with session_scope(migrated_factory) as session:
            rows = (
                session.query(Frame)
                .filter(Frame.project_id == ctx["project_id"])
                .count()
            )
        assert rows == cap
        assert _lifecycle(migrated_factory, ctx["project_id"]) == "archived"
        assert state.state == "stopped"
        await supervisor.stop()

    async def test_does_not_start_when_already_at_cap(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        # A project seeded already at its cap must capture nothing and archive on
        # the very first eligible cycle (the no-overshoot pre-capture guard).
        cap = 2
        clock = FakeClock(start=datetime(2026, 7, 1, 8, 0, tzinfo=_UTC))
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings,
            migrated_factory,
            clock=clock,
            disk_monitor=_permissive_disk_monitor(),
        )
        ctx = _seed_camera_and_project(
            migrated_factory, tmp_path, name="atcap", max_frame_count=cap
        )
        # Bump the stored frame_count to the cap without writing frame files.
        with session_scope(migrated_factory) as session:
            session.get(Project, ctx["project_id"]).frame_count = cap

        target = CaptureTarget(
            project_id=ctx["project_id"],
            project_name="atcap",
            camera_id=ctx["camera_id"],
            interval_seconds=_INTERVAL,
            storage_path=str(ctx["storage_path"]),
            max_frame_count=cap,
        )
        state = CaptureState(project_id=ctx["project_id"], camera_id=ctx["camera_id"])

        await _run_one_loop(supervisor, target, state)

        # No new frames captured; project archived; loop ended promptly.
        with session_scope(migrated_factory) as session:
            rows = (
                session.query(Frame)
                .filter(Frame.project_id == ctx["project_id"])
                .count()
            )
        assert rows == 0
        assert _lifecycle(migrated_factory, ctx["project_id"]) == "archived"
        assert state.state == "stopped"
        await supervisor.stop()


# ---------------------------------------------------------------------------
# End-date enforcement via the reconcile / _load_targets seam
# ---------------------------------------------------------------------------


class TestEndDate:
    def test_load_targets_excludes_and_archives_ended_project(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        # End-date enforcement lives in _load_targets (the authoritative
        # time-based enforcer), not in the per-project loop.
        now = datetime(2026, 7, 1, 12, 0, tzinfo=_UTC)
        end = now - timedelta(hours=1)
        clock = FakeClock(start=now)
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings,
            migrated_factory,
            clock=clock,
            disk_monitor=_permissive_disk_monitor(),
        )
        ctx = _seed_camera_and_project(
            migrated_factory, tmp_path, name="ended", end_date=end
        )

        targets = supervisor._load_targets()

        assert all(t.project_id != ctx["project_id"] for t in targets)
        assert _lifecycle(migrated_factory, ctx["project_id"]) == "archived"
        # An informational event recording the reason was written.
        with session_scope(migrated_factory) as session:
            events = (
                session.query(Event).filter(Event.scope_id == ctx["project_id"]).all()
            )
        assert any((e.event_metadata or {}).get("reason") == "end_date" for e in events)

    def test_load_targets_excludes_and_archives_frame_capped_project(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        # The reconcile path must recognise the frame-cap reason too, so a project
        # already at its cap on restart is reaped by reconcile (not only by the
        # live loop) -- proving "reconcile + runtime agree".
        now = datetime(2026, 7, 1, 12, 0, tzinfo=_UTC)
        clock = FakeClock(start=now)
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings,
            migrated_factory,
            clock=clock,
            disk_monitor=_permissive_disk_monitor(),
        )
        ctx = _seed_camera_and_project(
            migrated_factory, tmp_path, name="reconcile-capped", max_frame_count=3
        )
        with session_scope(migrated_factory) as session:
            session.get(Project, ctx["project_id"]).frame_count = 3

        targets = supervisor._load_targets()

        assert all(t.project_id != ctx["project_id"] for t in targets)
        assert _lifecycle(migrated_factory, ctx["project_id"]) == "archived"
        with session_scope(migrated_factory) as session:
            events = (
                session.query(Event).filter(Event.scope_id == ctx["project_id"]).all()
            )
        assert any(
            (e.event_metadata or {}).get("reason") == "frame_count" for e in events
        )

    def test_load_targets_includes_project_before_end_date(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        now = datetime(2026, 7, 1, 12, 0, tzinfo=_UTC)
        end = now + timedelta(days=1)
        clock = FakeClock(start=now)
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings,
            migrated_factory,
            clock=clock,
            disk_monitor=_permissive_disk_monitor(),
        )
        ctx = _seed_camera_and_project(
            migrated_factory, tmp_path, name="not-ended", end_date=end
        )

        targets = supervisor._load_targets()

        assert any(t.project_id == ctx["project_id"] for t in targets)
        assert _lifecycle(migrated_factory, ctx["project_id"]) == "active"

    async def test_reconcile_stops_runner_past_end_date(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        # Start a project, then advance the clock past its end date and reconcile;
        # the runner must be cancelled and the project archived.
        now = datetime(2026, 7, 1, 12, 0, tzinfo=_UTC)
        end = now + timedelta(seconds=_INTERVAL * 3)
        clock = FakeClock(start=now)
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings,
            migrated_factory,
            clock=clock,
            disk_monitor=_permissive_disk_monitor(),
        )
        ctx = _seed_camera_and_project(
            migrated_factory, tmp_path, name="endsoon", end_date=end
        )
        await supervisor.start()
        assert ctx["project_id"] in supervisor._runners

        # Jump the clock past the end date and run one reconcile pass.
        clock._now = end + timedelta(seconds=1)
        await supervisor._reconcile_once()

        assert ctx["project_id"] not in supervisor._runners
        assert _lifecycle(migrated_factory, ctx["project_id"]) == "archived"
        await supervisor.stop()
