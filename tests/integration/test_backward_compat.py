"""Backward-compatibility tests: projects with no/empty schedule capture normally.

Verifies that a project with schedule=None or schedule={} (always-open gate)
continues to capture at its fixed interval. Drives _capture_once directly
and inspects DB rows and state — no real clock waits.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.conftest import FakeAdapter
from timelapse_manager.capture.schedule import is_within_window, parse_schedule
from timelapse_manager.capture.supervisor import (
    CaptureState,
    CaptureSupervisor,
    CaptureTarget,
)
from timelapse_manager.config.settings import (
    CaptureSettings,
    DatabaseSettings,
    LoggingSettings,
    PathsSettings,
    Settings,
)
from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.storage.monitor import DiskSpaceMonitor

_UTC = UTC


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
        capture=CaptureSettings(autostart=False, timeout_seconds=5.0),
    )


def _seed_project(
    migrated_factory,
    tmp_path: Path,
    *,
    name: str = "compat-proj",
    schedule: dict | None = None,
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
            capture_interval_seconds=60,
            lifecycle_state="active",
            operational_status="idle",
            storage_path=str(storage),
            schedule=schedule,
        )
        session.add(proj)
        session.flush()
        project_id = proj.id
    return {"camera_id": cam_id, "project_id": project_id, "storage_path": storage}


# ---------------------------------------------------------------------------
# parse_schedule: None and {} => always open
# ---------------------------------------------------------------------------


class TestAlwaysOpenSchedule:
    def test_none_schedule_is_always_open(self) -> None:
        s = parse_schedule(None)
        assert s.is_always_open
        assert is_within_window(s, datetime(2026, 1, 1, 3, 0, tzinfo=_UTC))

    def test_empty_dict_schedule_is_always_open(self) -> None:
        s = parse_schedule({})
        assert s.is_always_open
        assert is_within_window(s, datetime(2026, 6, 15, 15, 0, tzinfo=_UTC))

    def test_disabled_schedule_is_always_open(self) -> None:
        s = parse_schedule({"enabled": False})
        assert s.is_always_open
        assert is_within_window(s, datetime(2026, 3, 1, 0, 0, tzinfo=_UTC))


# ---------------------------------------------------------------------------
# Fixed-interval capture with no schedule
# ---------------------------------------------------------------------------


class TestFixedIntervalNoSchedule:
    async def test_no_schedule_project_captures_frames(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        ctx = _seed_project(migrated_factory, tmp_path, schedule=None)
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )

        target = CaptureTarget(
            project_id=ctx["project_id"],
            project_name="compat-proj",
            camera_id=ctx["camera_id"],
            interval_seconds=60,
            schedule=None,
        )
        state = CaptureState(project_id=ctx["project_id"], camera_id=ctx["camera_id"])

        fake_config = MagicMock()
        n_captures = 3
        for _ in range(n_captures):
            with (
                patch.object(supervisor, "_load_camera", return_value=fake_config),
                patch(
                    "timelapse_manager.capture.supervisor.build_adapter",
                    return_value=FakeAdapter(),
                ),
            ):
                await supervisor._capture_once(target, state)

        with session_scope(migrated_factory) as session:
            frames = (
                session.query(Frame)
                .filter(Frame.project_id == ctx["project_id"])
                .order_by(Frame.sequence_index)
                .all()
            )

        assert len(frames) == n_captures
        assert [f.sequence_index for f in frames] == list(range(1, n_captures + 1))
        # All files exist on disk
        for f in frames:
            assert f.file_path is not None
            assert Path(f.file_path).exists()
        await supervisor.stop()

    async def test_empty_schedule_project_captures_frames(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        ctx = _seed_project(
            migrated_factory, tmp_path, name="compat-proj-2", schedule={}
        )
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )

        target = CaptureTarget(
            project_id=ctx["project_id"],
            project_name="compat-proj-2",
            camera_id=ctx["camera_id"],
            interval_seconds=60,
            schedule={},
        )
        state = CaptureState(project_id=ctx["project_id"], camera_id=ctx["camera_id"])

        fake_config = MagicMock()
        with (
            patch.object(supervisor, "_load_camera", return_value=fake_config),
            patch(
                "timelapse_manager.capture.supervisor.build_adapter",
                return_value=FakeAdapter(),
            ),
        ):
            await supervisor._capture_once(target, state)

        assert state.frames_captured == 1
        assert state.state == "running"
        await supervisor.stop()

    async def test_no_schedule_start_loads_target(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """_load_targets includes a project with no schedule."""
        ctx = _seed_project(
            migrated_factory, tmp_path, name="compat-proj-3", schedule=None
        )
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )

        targets = supervisor._load_targets()
        project_ids = [t.project_id for t in targets]
        assert ctx["project_id"] in project_ids

    async def test_malformed_schedule_falls_back_to_always_open(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """A project with a malformed schedule JSON is treated as always-open."""
        bad_schedule = {
            "timezone": "Mars/Phobos",
            "windows": [{"start_time": "08:00", "end_time": "17:00"}],
        }
        ctx = _seed_project(
            migrated_factory,
            tmp_path,
            name="compat-proj-4",
            schedule=bad_schedule,
        )
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )

        target_raw = CaptureTarget(
            project_id=ctx["project_id"],
            project_name="compat-proj-4",
            camera_id=ctx["camera_id"],
            interval_seconds=60,
            schedule=bad_schedule,
        )
        # _parse_schedule_safely should log and return always-open
        parsed = supervisor._parse_schedule_safely(target_raw)
        assert parsed.is_always_open

        state = CaptureState(project_id=ctx["project_id"], camera_id=ctx["camera_id"])
        fake_config = MagicMock()
        with (
            patch.object(supervisor, "_load_camera", return_value=fake_config),
            patch(
                "timelapse_manager.capture.supervisor.build_adapter",
                return_value=FakeAdapter(),
            ),
        ):
            await supervisor._capture_once(target_raw, state)

        assert state.frames_captured == 1
        await supervisor.stop()
