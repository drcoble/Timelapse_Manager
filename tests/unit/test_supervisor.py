"""Unit tests for the CaptureSupervisor.

Drives supervisor helpers (_capture_with_timeout, _capture_once) directly rather
than racing the live background loop (which sleeps up to the interval).

Covers:
- Timeout returns None (gap, no error raised to caller)
- Fault isolation: one project's exception does not propagate
- Empty DB -> 0 tasks launched after start()
- stop() is idempotent (safe to call twice)
- _load_targets: only active projects with interval + camera.protocol qualify
- states_for_camera / state_for_project return correct live state
- CancelledError propagates through _run_project (not caught by isolation)

Uses migrated_factory for tests that touch the DB.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import ErrorAdapter, FakeAdapter, SlowAdapter
from timelapse_manager.cameras.base import CapturedFrame
from timelapse_manager.capture.supervisor import (
    CaptureState,
    CaptureSupervisor,
    CaptureTarget,
)
from timelapse_manager.config.settings import CaptureSettings, Settings
from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.storage.monitor import DiskSpaceMonitor


def _permissive_disk_monitor() -> DiskSpaceMonitor:
    """Return a monitor that always reports ample disk space.

    Prevents real-disk probes during supervisor unit tests so they pass on
    any CI machine regardless of actual free space.
    """
    return DiskSpaceMonitor(
        low_watermark_bytes=1,
        low_watermark_percent=0.001,
        resume_watermark_bytes=1,
        resume_watermark_percent=0.001,
        check_interval_seconds=0.0,
        get_free_bytes=lambda _p: 10**15,
        get_total_bytes=lambda _p: 10**15,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
)


def _make_settings(tmp_path: Path, timeout_seconds: float = 0.05) -> Settings:
    from timelapse_manager.config.settings import (
        DatabaseSettings,
        LoggingSettings,
        PathsSettings,
    )

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
        capture=CaptureSettings(autostart=False, timeout_seconds=timeout_seconds),
    )


def _fake_settings(tmp_path: Path) -> Settings:
    return _make_settings(tmp_path, timeout_seconds=0.05)


# ---------------------------------------------------------------------------
# _capture_with_timeout
# ---------------------------------------------------------------------------


class TestCaptureWithTimeout:
    async def test_returns_frame_when_adapter_succeeds_in_time(
        self, tmp_path: Path
    ) -> None:
        settings = _fake_settings(tmp_path)
        factory = MagicMock()
        supervisor = CaptureSupervisor(
            settings, factory, disk_monitor=_permissive_disk_monitor()
        )
        adapter = FakeAdapter()

        result = await supervisor._capture_with_timeout(adapter)

        assert result is not None
        assert isinstance(result, CapturedFrame)
        await supervisor.stop()

    async def test_returns_none_when_adapter_exceeds_timeout(
        self, tmp_path: Path
    ) -> None:
        # timeout_seconds=0.05 is far shorter than SlowAdapter's 3600s sleep
        settings = _make_settings(tmp_path, timeout_seconds=0.05)
        factory = MagicMock()
        supervisor = CaptureSupervisor(
            settings, factory, disk_monitor=_permissive_disk_monitor()
        )
        adapter = SlowAdapter()

        result = await supervisor._capture_with_timeout(adapter)

        assert result is None
        await supervisor.stop()


# ---------------------------------------------------------------------------
# Fault isolation (_capture_once via patched _load_camera)
# ---------------------------------------------------------------------------


class TestFaultIsolation:
    async def test_error_adapter_does_not_propagate_exception_in_run_project(
        self, tmp_path: Path
    ) -> None:
        """_run_project catches exceptions; they are recorded on state."""
        settings = _fake_settings(tmp_path)
        factory = MagicMock()
        supervisor = CaptureSupervisor(
            settings, factory, disk_monitor=_permissive_disk_monitor()
        )

        state = CaptureState(project_id=1, camera_id=1)
        target = CaptureTarget(
            project_id=1, project_name="p", camera_id=1, interval_seconds=1
        )

        # Patch _load_camera to return a fake config, build_adapter to give ErrorAdapter
        fake_config = MagicMock()
        with (
            patch.object(supervisor, "_load_camera", return_value=fake_config),
            patch(
                "timelapse_manager.capture.supervisor.build_adapter",
                return_value=ErrorAdapter(),
            ),
        ):
            # Run one iteration only by cancelling after the first capture attempt
            import contextlib

            task = asyncio.create_task(supervisor._run_project(target, state))
            await asyncio.sleep(0.1)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        # State should record the error, not remain "idle"
        assert state.last_error is not None
        await supervisor.stop()

    async def test_cancelled_error_propagates_through_run_project(
        self, tmp_path: Path
    ) -> None:
        """CancelledError must not be swallowed by the per-task isolation."""
        settings = _fake_settings(tmp_path)
        factory = MagicMock()
        supervisor = CaptureSupervisor(
            settings, factory, disk_monitor=_permissive_disk_monitor()
        )

        state = CaptureState(project_id=1, camera_id=1)
        target = CaptureTarget(
            project_id=1, project_name="p", camera_id=1, interval_seconds=3600
        )

        fake_config = MagicMock()
        with (
            patch.object(supervisor, "_load_camera", return_value=fake_config),
            patch(
                "timelapse_manager.capture.supervisor.build_adapter",
                return_value=SlowAdapter(),
            ),
        ):
            task = asyncio.create_task(supervisor._run_project(target, state))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await supervisor.stop()


# ---------------------------------------------------------------------------
# Stream-id provenance: a capture records the project's stream_id on the frame
# ---------------------------------------------------------------------------


class TestCaptureRecordsStreamId:
    async def test_capture_once_persists_target_stream_id_on_frame(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """_capture_once forwards the target's stream_id to the writer, so the
        persisted Frame records which stream captured it."""
        settings = _fake_settings(tmp_path)
        # The writer uses the (real, migrated) factory, so the row truly persists.
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )

        with session_scope(migrated_factory) as session:
            cam = Camera(
                name="sup-stream-cam",
                address="192.0.2.20",
                protocol="vapix",
            )
            session.add(cam)
            session.flush()
            cam_id = cam.id
            proj = Project(
                camera_id=cam_id,
                name="sup-stream-project",
                lifecycle_state="active",
                operational_status="idle",
                stream_id="Quality",
            )
            session.add(proj)
            session.flush()
            project_id = proj.id

        state = CaptureState(project_id=project_id, camera_id=cam_id)
        target = CaptureTarget(
            project_id=project_id,
            project_name="sup-stream-project",
            camera_id=cam_id,
            interval_seconds=1,
            stream_id="Quality",
        )

        fake_config = MagicMock()
        with (
            patch.object(supervisor, "_load_camera", return_value=fake_config),
            patch.object(supervisor, "_load_default_credentials", return_value=None),
            patch(
                "timelapse_manager.capture.supervisor.build_adapter",
                return_value=FakeAdapter(),
            ),
        ):
            await supervisor._capture_once(target, state)

        with session_scope(migrated_factory) as session:
            frame = session.execute(
                Frame.__table__.select().where(Frame.project_id == project_id)
            ).one()
        assert frame.stream_id == "Quality"
        await supervisor.stop()


# ---------------------------------------------------------------------------
# start() with empty DB -> 0 tasks
# ---------------------------------------------------------------------------


class TestStartWithEmptyDb:
    async def test_start_with_empty_db_launches_zero_tasks(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _fake_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )

        await supervisor.start()

        assert len(supervisor._runners) == 0
        await supervisor.stop()

    async def test_start_is_idempotent(self, migrated_factory, tmp_path: Path) -> None:
        settings = _fake_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )

        await supervisor.start()
        await supervisor.start()  # second call must be a no-op

        assert len(supervisor._runners) == 0
        await supervisor.stop()


# ---------------------------------------------------------------------------
# stop() idempotency
# ---------------------------------------------------------------------------


class TestStopIdempotency:
    async def test_stop_is_safe_when_nothing_started(self, tmp_path: Path) -> None:
        settings = _fake_settings(tmp_path)
        factory = MagicMock()
        supervisor = CaptureSupervisor(
            settings, factory, disk_monitor=_permissive_disk_monitor()
        )

        await supervisor.stop()  # should not raise
        await supervisor.stop()  # second call also safe

    async def test_stop_cancels_tasks_and_marks_stopped(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _fake_settings(tmp_path)
        # Seed one qualifying project so a task is launched
        storage = tmp_path / "frames"
        storage.mkdir()
        with session_scope(migrated_factory) as session:
            cam = Camera(
                name="sup-cam",
                address="127.0.0.1",
                protocol="vapix",
                snapshot_uri="http://127.0.0.1/snap",
            )
            session.add(cam)
            session.flush()
            proj = Project(
                camera_id=cam.id,
                name="sup-proj",
                capture_interval_seconds=3600,
                lifecycle_state="active",
                operational_status="idle",
                storage_path=str(storage),
            )
            session.add(proj)
            session.flush()

        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        await supervisor.start()
        assert len(supervisor._runners) == 1

        await supervisor.stop()

        for runner in supervisor._runners.values():
            assert runner.state.state == "stopped"
            assert runner.task is None or runner.task.done()


# ---------------------------------------------------------------------------
# _load_targets qualification logic
# ---------------------------------------------------------------------------


class TestLoadTargets:
    def test_active_project_with_interval_and_protocol_qualifies(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _fake_settings(tmp_path)
        with session_scope(migrated_factory) as session:
            cam = Camera(
                name="lt-cam",
                address="10.0.0.1",
                protocol="vapix",
                snapshot_uri="http://10.0.0.1/snap",
            )
            session.add(cam)
            session.flush()
            proj = Project(
                camera_id=cam.id,
                name="lt-proj",
                capture_interval_seconds=30,
                lifecycle_state="active",
                operational_status="idle",
            )
            session.add(proj)

        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        targets = supervisor._load_targets()
        assert len(targets) == 1

    def test_archived_project_does_not_qualify(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _fake_settings(tmp_path)
        with session_scope(migrated_factory) as session:
            cam = Camera(
                name="lt-cam2",
                address="10.0.0.2",
                protocol="vapix",
                snapshot_uri="http://10.0.0.2/snap",
            )
            session.add(cam)
            session.flush()
            proj = Project(
                camera_id=cam.id,
                name="lt-proj2",
                capture_interval_seconds=30,
                lifecycle_state="archived",
                operational_status="idle",
            )
            session.add(proj)

        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        targets = supervisor._load_targets()
        assert len(targets) == 0

    def test_project_without_interval_does_not_qualify(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _fake_settings(tmp_path)
        with session_scope(migrated_factory) as session:
            cam = Camera(
                name="lt-cam3",
                address="10.0.0.3",
                protocol="vapix",
                snapshot_uri="http://10.0.0.3/snap",
            )
            session.add(cam)
            session.flush()
            proj = Project(
                camera_id=cam.id,
                name="lt-proj3",
                capture_interval_seconds=None,
                lifecycle_state="active",
                operational_status="idle",
            )
            session.add(proj)

        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        targets = supervisor._load_targets()
        assert len(targets) == 0

    def test_camera_without_protocol_does_not_qualify(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _fake_settings(tmp_path)
        with session_scope(migrated_factory) as session:
            cam = Camera(name="lt-cam4", address="10.0.0.4", protocol=None)
            session.add(cam)
            session.flush()
            proj = Project(
                camera_id=cam.id,
                name="lt-proj4",
                capture_interval_seconds=30,
                lifecycle_state="active",
                operational_status="idle",
            )
            session.add(proj)

        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        targets = supervisor._load_targets()
        assert len(targets) == 0


# ---------------------------------------------------------------------------
# State queries
# ---------------------------------------------------------------------------


class TestStateQueries:
    def test_state_for_project_returns_none_when_untracked(
        self, tmp_path: Path
    ) -> None:
        settings = _fake_settings(tmp_path)
        factory = MagicMock()
        supervisor = CaptureSupervisor(
            settings, factory, disk_monitor=_permissive_disk_monitor()
        )

        assert supervisor.state_for_project(999) is None

    def test_states_for_camera_returns_empty_when_untracked(
        self, tmp_path: Path
    ) -> None:
        settings = _fake_settings(tmp_path)
        factory = MagicMock()
        supervisor = CaptureSupervisor(
            settings, factory, disk_monitor=_permissive_disk_monitor()
        )

        assert supervisor.states_for_camera(999) == []


# ---------------------------------------------------------------------------
# Runtime reconciliation
# ---------------------------------------------------------------------------


def _seed_camera(session, *, name: str) -> int:
    cam = Camera(
        name=name,
        address="127.0.0.1",
        protocol="vapix",
        snapshot_uri="http://127.0.0.1/snap",
    )
    session.add(cam)
    session.flush()
    return cam.id


def _seed_project(session, *, camera_id: int, name: str, lifecycle: str) -> int:
    proj = Project(
        camera_id=camera_id,
        name=name,
        capture_interval_seconds=3600,
        lifecycle_state=lifecycle,
        operational_status="idle",
    )
    session.add(proj)
    session.flush()
    return proj.id


class TestReconcile:
    async def test_reconcile_launches_project_created_after_start(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        # Start with an empty DB: no runners. Then create a qualifying project and
        # drive one reconcile tick; the supervisor must launch a runner for it
        # without a restart.
        settings = _fake_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        await supervisor.start()
        assert len(supervisor._runners) == 0

        with session_scope(migrated_factory) as session:
            cam_id = _seed_camera(session, name="rc-cam")
            project_id = _seed_project(
                session, camera_id=cam_id, name="rc-proj", lifecycle="active"
            )

        await supervisor._reconcile_once()

        assert project_id in supervisor._runners
        assert supervisor._runners[project_id].state.state == "running"
        await supervisor.stop()

    async def test_reconcile_stops_project_that_stops_qualifying(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        # A running project that is archived must have its runner cancelled and
        # removed on the next reconcile tick.
        settings = _fake_settings(tmp_path)
        with session_scope(migrated_factory) as session:
            cam_id = _seed_camera(session, name="rc-cam2")
            project_id = _seed_project(
                session, camera_id=cam_id, name="rc-proj2", lifecycle="active"
            )

        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        await supervisor.start()
        assert project_id in supervisor._runners
        runner = supervisor._runners[project_id]

        with session_scope(migrated_factory) as session:
            project = session.get(Project, project_id)
            project.lifecycle_state = "archived"

        await supervisor._reconcile_once()

        assert project_id not in supervisor._runners
        assert runner.task is not None
        # The reconcile tick cancels the runner task; on some event-loop
        # schedulings (notably 3.11) the task is still in the `cancelling`
        # state at this point. Wait for the cancellation to settle before
        # asserting it is done, so the check is deterministic.
        await asyncio.wait({runner.task}, timeout=5)
        assert runner.task.done()
        await supervisor.stop()

    async def test_reconcile_does_not_relaunch_already_running_project(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        # A still-qualifying project keeps its existing task untouched across
        # reconcile ticks (no double-launch, no churn).
        settings = _fake_settings(tmp_path)
        with session_scope(migrated_factory) as session:
            cam_id = _seed_camera(session, name="rc-cam3")
            project_id = _seed_project(
                session, camera_id=cam_id, name="rc-proj3", lifecycle="active"
            )

        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        await supervisor.start()
        original_task = supervisor._runners[project_id].task

        await supervisor._reconcile_once()

        assert supervisor._runners[project_id].task is original_task
        await supervisor.stop()

    async def test_notify_reconcile_before_start_is_noop(self, tmp_path: Path) -> None:
        settings = _fake_settings(tmp_path)
        factory = MagicMock()
        supervisor = CaptureSupervisor(
            settings, factory, disk_monitor=_permissive_disk_monitor()
        )
        # Must not raise; simply arms the wakeup for when the loop starts.
        supervisor.notify_reconcile()
        await supervisor.stop()

    async def test_reconcile_restarts_on_interval_change(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        # A still-qualifying project whose capture interval changed must have its
        # loop stopped and relaunched (new task identity) so the new interval
        # takes effect at runtime.
        settings = _fake_settings(tmp_path)
        with session_scope(migrated_factory) as session:
            cam_id = _seed_camera(session, name="rc-int-cam")
            project_id = _seed_project(
                session, camera_id=cam_id, name="rc-int-proj", lifecycle="active"
            )

        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        await supervisor.start()
        original_task = supervisor._runners[project_id].task

        with session_scope(migrated_factory) as session:
            project = session.get(Project, project_id)
            project.capture_interval_seconds = 7200

        await supervisor._reconcile_once()

        runner = supervisor._runners[project_id]
        assert runner.task is not original_task
        assert runner.target.interval_seconds == 7200
        await supervisor.stop()

    async def test_reconcile_restarts_on_camera_change(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        # Re-binding a still-qualifying project to a different (protocol-bearing)
        # camera restarts the loop rather than stopping it.
        settings = _fake_settings(tmp_path)
        with session_scope(migrated_factory) as session:
            cam_a = _seed_camera(session, name="rc-cam-a")
            cam_b = _seed_camera(session, name="rc-cam-b")
            project_id = _seed_project(
                session, camera_id=cam_a, name="rc-cam-proj", lifecycle="active"
            )

        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        await supervisor.start()
        original_task = supervisor._runners[project_id].task

        with session_scope(migrated_factory) as session:
            project = session.get(Project, project_id)
            project.camera_id = cam_b

        await supervisor._reconcile_once()

        runner = supervisor._runners[project_id]
        assert runner.task is not original_task
        assert runner.target.camera_id == cam_b
        await supervisor.stop()

    async def test_reconcile_restarts_on_storage_path_change(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        # Changing the storage path restarts the loop so the writer and the disk
        # monitor follow the new destination.
        settings = _fake_settings(tmp_path)
        with session_scope(migrated_factory) as session:
            cam_id = _seed_camera(session, name="rc-store-cam")
            project_id = _seed_project(
                session, camera_id=cam_id, name="rc-store-proj", lifecycle="active"
            )

        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        await supervisor.start()
        original_task = supervisor._runners[project_id].task

        new_path = str(tmp_path / "custom-frames")
        with session_scope(migrated_factory) as session:
            project = session.get(Project, project_id)
            project.storage_path = new_path

        await supervisor._reconcile_once()

        runner = supervisor._runners[project_id]
        assert runner.task is not original_task
        assert runner.target.storage_path == new_path
        await supervisor.stop()

    async def test_reconcile_no_change_keeps_task(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        # An unchanged project keeps its running task across reconcile ticks (no
        # churn from the restart-on-change pass).
        settings = _fake_settings(tmp_path)
        with session_scope(migrated_factory) as session:
            cam_id = _seed_camera(session, name="rc-nochange-cam")
            project_id = _seed_project(
                session,
                camera_id=cam_id,
                name="rc-nochange-proj",
                lifecycle="active",
            )

        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        await supervisor.start()
        original_task = supervisor._runners[project_id].task

        await supervisor._reconcile_once()

        assert supervisor._runners[project_id].task is original_task
        await supervisor.stop()

    async def test_reconcile_stops_runner_when_project_paused(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        # Pausing a project (lifecycle_state="paused") makes it non-qualifying, so
        # its runner must be cancelled and removed on the next reconcile tick --
        # this is the runtime mechanism behind the pause control.
        settings = _fake_settings(tmp_path)
        with session_scope(migrated_factory) as session:
            cam_id = _seed_camera(session, name="rc-pause-cam")
            project_id = _seed_project(
                session, camera_id=cam_id, name="rc-pause-proj", lifecycle="active"
            )

        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        await supervisor.start()
        assert project_id in supervisor._runners
        runner = supervisor._runners[project_id]

        with session_scope(migrated_factory) as session:
            project = session.get(Project, project_id)
            project.lifecycle_state = "paused"

        await supervisor._reconcile_once()

        assert project_id not in supervisor._runners
        assert runner.task is not None
        # The reconcile tick cancels the runner task; on some event-loop
        # schedulings (notably 3.11) the task is still in the `cancelling`
        # state at this point. Wait for the cancellation to settle before
        # asserting it is done, so the check is deterministic.
        await asyncio.wait({runner.task}, timeout=5)
        assert runner.task.done()
        await supervisor.stop()

    async def test_reconcile_relaunches_runner_when_project_resumed(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        # Resuming a paused project (back to lifecycle_state="active") makes it
        # qualifying again, so the supervisor must relaunch its runner on the next
        # reconcile tick.
        settings = _fake_settings(tmp_path)
        with session_scope(migrated_factory) as session:
            cam_id = _seed_camera(session, name="rc-resume-cam")
            project_id = _seed_project(
                session, camera_id=cam_id, name="rc-resume-proj", lifecycle="paused"
            )

        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        await supervisor.start()
        # Paused at startup: not a capture target, so no runner.
        assert project_id not in supervisor._runners

        with session_scope(migrated_factory) as session:
            project = session.get(Project, project_id)
            project.lifecycle_state = "active"

        await supervisor._reconcile_once()

        assert project_id in supervisor._runners
        assert supervisor._runners[project_id].state.state == "running"
        await supervisor.stop()

    async def test_reconcile_rename_does_not_restart(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        # A rename is not loop-affecting: the live task must be preserved (a
        # restart here would needlessly disturb an in-flight capture).
        settings = _fake_settings(tmp_path)
        with session_scope(migrated_factory) as session:
            cam_id = _seed_camera(session, name="rc-rename-cam")
            project_id = _seed_project(
                session, camera_id=cam_id, name="rc-rename-proj", lifecycle="active"
            )

        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        await supervisor.start()
        original_task = supervisor._runners[project_id].task

        with session_scope(migrated_factory) as session:
            project = session.get(Project, project_id)
            project.name = "rc-rename-proj-renamed"

        await supervisor._reconcile_once()

        assert supervisor._runners[project_id].task is original_task
        await supervisor.stop()


# ---------------------------------------------------------------------------
# _consume_event_source: trigger matching, debounce, fault isolation
# ---------------------------------------------------------------------------


class _FakeEventSource:
    """An async iterator that yields a fixed list of events, then ends."""

    def __init__(self, events: list[object]) -> None:
        self._events = events

    def __aiter__(self) -> _FakeEventSource:
        self._it = iter(self._events)
        return self

    async def __anext__(self) -> object:
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


def _event(topic_id: str, *, active: bool | None):
    from datetime import UTC, datetime

    from timelapse_manager.cameras.base import CameraEvent

    return CameraEvent(
        topic_id=topic_id,
        category="io",
        source={"port": "1"},
        data={},
        active=active,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        raw={},
    )


class TestConsumeEventSource:
    def _supervisor(self, tmp_path: Path) -> CaptureSupervisor:
        return CaptureSupervisor(
            _fake_settings(tmp_path),
            MagicMock(),
            disk_monitor=_permissive_disk_monitor(),
        )

    def _target(self, triggers: list[dict]) -> CaptureTarget:
        return CaptureTarget(
            project_id=1,
            project_name="p",
            camera_id=1,
            interval_seconds=None,
            event_triggers=triggers,
        )

    async def test_rising_edge_match_fires_capture(self, tmp_path: Path) -> None:
        supervisor = self._supervisor(tmp_path)
        target = self._target(
            [{"id": "t1", "topic_id": "Device/IO/VirtualInput", "cooldown_seconds": 0}]
        )
        source = _FakeEventSource([_event("Device/IO/VirtualInput", active=True)])

        from unittest.mock import AsyncMock

        with patch(
            "timelapse_manager.capture.supervisor.capture_one_now",
            new=AsyncMock(return_value=None),
        ) as fire:
            await supervisor._consume_event_source(target, source)

        assert fire.call_count == 1
        _, kwargs = fire.call_args
        assert kwargs["reason"] == "event:Device/IO/VirtualInput"
        assert kwargs["trigger"]["trigger_id"] == "t1"
        await supervisor.stop()

    async def test_cooldown_suppresses_second_event(self, tmp_path: Path) -> None:
        supervisor = self._supervisor(tmp_path)
        # A long cooldown vs. microseconds of real elapsed time: 2nd is suppressed.
        target = self._target(
            [
                {
                    "id": "t1",
                    "topic_id": "Device/IO/VirtualInput",
                    "cooldown_seconds": 3600,
                }
            ]
        )
        source = _FakeEventSource(
            [
                _event("Device/IO/VirtualInput", active=True),
                _event("Device/IO/VirtualInput", active=True),
            ]
        )

        from unittest.mock import AsyncMock

        with patch(
            "timelapse_manager.capture.supervisor.capture_one_now",
            new=AsyncMock(return_value=None),
        ) as fire:
            await supervisor._consume_event_source(target, source)

        assert fire.call_count == 1
        await supervisor.stop()

    async def test_zero_cooldown_fires_every_event(self, tmp_path: Path) -> None:
        supervisor = self._supervisor(tmp_path)
        target = self._target(
            [{"id": "t1", "topic_id": "Device/IO/VirtualInput", "cooldown_seconds": 0}]
        )
        source = _FakeEventSource(
            [
                _event("Device/IO/VirtualInput", active=True),
                _event("Device/IO/VirtualInput", active=True),
            ]
        )

        from unittest.mock import AsyncMock

        with patch(
            "timelapse_manager.capture.supervisor.capture_one_now",
            new=AsyncMock(return_value=None),
        ) as fire:
            await supervisor._consume_event_source(target, source)

        assert fire.call_count == 2
        await supervisor.stop()

    async def test_falling_edge_does_not_fire(self, tmp_path: Path) -> None:
        supervisor = self._supervisor(tmp_path)
        target = self._target(
            [{"id": "t1", "topic_id": "Device/IO/VirtualInput", "cooldown_seconds": 0}]
        )
        source = _FakeEventSource([_event("Device/IO/VirtualInput", active=False)])

        from unittest.mock import AsyncMock

        with patch(
            "timelapse_manager.capture.supervisor.capture_one_now",
            new=AsyncMock(return_value=None),
        ) as fire:
            await supervisor._consume_event_source(target, source)

        fire.assert_not_called()
        await supervisor.stop()

    async def test_disabled_trigger_does_not_fire(self, tmp_path: Path) -> None:
        supervisor = self._supervisor(tmp_path)
        target = self._target(
            [
                {
                    "id": "t1",
                    "topic_id": "Device/IO/VirtualInput",
                    "enabled": False,
                    "cooldown_seconds": 0,
                }
            ]
        )
        source = _FakeEventSource([_event("Device/IO/VirtualInput", active=True)])

        from unittest.mock import AsyncMock

        with patch(
            "timelapse_manager.capture.supervisor.capture_one_now",
            new=AsyncMock(return_value=None),
        ) as fire:
            await supervisor._consume_event_source(target, source)

        fire.assert_not_called()
        await supervisor.stop()

    async def test_unmatched_topic_does_not_fire(self, tmp_path: Path) -> None:
        supervisor = self._supervisor(tmp_path)
        target = self._target(
            [{"id": "t1", "topic_id": "Device/IO/VirtualInput", "cooldown_seconds": 0}]
        )
        source = _FakeEventSource(
            [_event("RuleEngine/MotionRegionDetector/Motion", active=True)]
        )

        from unittest.mock import AsyncMock

        with patch(
            "timelapse_manager.capture.supervisor.capture_one_now",
            new=AsyncMock(return_value=None),
        ) as fire:
            await supervisor._consume_event_source(target, source)

        fire.assert_not_called()
        await supervisor.stop()

    async def test_capture_error_is_swallowed(self, tmp_path: Path) -> None:
        from timelapse_manager.cameras.base import CaptureError

        supervisor = self._supervisor(tmp_path)
        target = self._target(
            [{"id": "t1", "topic_id": "Device/IO/VirtualInput", "cooldown_seconds": 0}]
        )
        source = _FakeEventSource(
            [
                _event("Device/IO/VirtualInput", active=True),
                _event("Device/IO/VirtualInput", active=True),
            ]
        )

        from unittest.mock import AsyncMock

        with patch(
            "timelapse_manager.capture.supervisor.capture_one_now",
            new=AsyncMock(side_effect=CaptureError("boom")),
        ) as fire:
            # Must not raise: a bad capture does not tear down the listener.
            await supervisor._consume_event_source(target, source)

        assert fire.call_count == 2
        await supervisor.stop()

    async def test_invalid_triggers_drain_without_firing(self, tmp_path: Path) -> None:
        supervisor = self._supervisor(tmp_path)
        # cooldown_seconds is invalid -> parse raises -> drain without matching.
        target = self._target(
            [{"topic_id": "Device/IO/VirtualInput", "cooldown_seconds": -5}]
        )
        source = _FakeEventSource([_event("Device/IO/VirtualInput", active=True)])

        from unittest.mock import AsyncMock

        with patch(
            "timelapse_manager.capture.supervisor.capture_one_now",
            new=AsyncMock(return_value=None),
        ) as fire:
            # Must return cleanly (no raise), having drained the source.
            await supervisor._consume_event_source(target, source)

        fire.assert_not_called()
        await supervisor.stop()
