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
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import ErrorAdapter, FakeAdapter, SlowAdapter
from timelapse_manager.cameras.base import CapturedFrame, UnreachableCaptureError
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

    @pytest.mark.parametrize("outcome", ["frame", "error"])
    async def test_cancel_landing_as_capture_finishes_is_not_swallowed(
        self, tmp_path: Path, outcome: str
    ) -> None:
        # Regression for #33. The runner is cancelled in the same event-loop
        # iteration in which the adapter's capture finishes. On Python 3.11,
        # ``asyncio.wait_for`` then returned the frame (or raised the capture's
        # own error) instead of the CancelledError, so the runner carried on
        # with the cancel consumed and stop() waited on it forever. The
        # cancellation must win whatever the capture produced. Only 3.11 can
        # turn this red: 3.12's wait_for is already built on asyncio.timeout.
        supervisor = CaptureSupervisor(
            _make_settings(tmp_path, timeout_seconds=30.0),
            MagicMock(),
            disk_monitor=_permissive_disk_monitor(),
        )
        adapter = _GatedAdapter(fail=outcome == "error")
        task = asyncio.create_task(supervisor._capture_with_timeout(adapter))
        await adapter.entered.wait()

        # Same synchronous block: the capture becomes ready to finish and the
        # caller is cancelled before either gets to run.
        adapter.release.set()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        await supervisor.stop()


class _GatedAdapter(FakeAdapter):
    """A capture that parks until released, then returns a frame or fails."""

    def __init__(self, *, fail: bool) -> None:
        super().__init__()
        self._fail = fail
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def capture(self) -> CapturedFrame:
        self.entered.set()
        await self.release.wait()
        if self._fail:
            raise UnreachableCaptureError("camera went away")
        return await super().capture()


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
            # Poll rather than a fixed 0.1 s sleep: the first cycle makes
            # several thread hops, which can take longer under -n auto load.
            for _ in range(250):
                if state.last_error is not None:
                    break
                await asyncio.sleep(0.02)
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


def _assert_runner_launched(supervisor: CaptureSupervisor, project_id: int) -> None:
    """The runner exists and its loop is alive.

    Its ``state`` is not asserted: the loop starts at once, and by the time a
    loaded host lets the test look, it may already have made its first capture
    (which fails against the fake camera and reads ``error``). A launched,
    unfinished task is the contract under test.
    """
    runner = supervisor._runners[project_id]
    assert runner.task is not None and not runner.task.done()
    assert runner.state.state != "stopped"


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
        _assert_runner_launched(supervisor, project_id)
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
        _assert_runner_launched(supervisor, project_id)
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
# stop() racing a reconcile tick that is stopping a runner
# ---------------------------------------------------------------------------


class TestStopDuringReconcileRunnerStop:
    """``stop()`` must finish even when the reconcile loop is mid ``_stop_runner``.

    Regression: ``_stop_runner`` awaited the cancelled runner under
    ``suppress(CancelledError)``. When ``stop()`` cancelled the reconcile task
    while it was parked on that await, asyncio delegated the cancel to the runner
    and the resulting ``CancelledError`` was swallowed as if it were the
    runner's. The loop kept ticking and ``stop()`` hung on it forever.
    """

    async def test_stop_completes_while_reconcile_awaits_runner_unwind(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _fake_settings(tmp_path)
        with session_scope(migrated_factory) as session:
            cam_id = _seed_camera(session, name="rc-stoprace-cam")
            project_id = _seed_project(
                session,
                camera_id=cam_id,
                name="rc-stoprace-proj",
                lifecycle="active",
            )

        unwinding = asyncio.Event()
        release_unwind = asyncio.Event()

        async def runner_with_slow_unwind(target, state) -> None:
            # Stands in for a runner whose cancellation needs loop turns to
            # unwind (an in-flight to_thread write, an HTTP close, ...). Holding
            # the unwind open parks the reconcile tick inside _stop_runner.
            try:
                await asyncio.Event().wait()
            finally:
                unwinding.set()
                await release_unwind.wait()

        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        with patch.object(supervisor, "_run_project", runner_with_slow_unwind):
            await supervisor.start()
            reconcile_task = supervisor._reconcile_task
            assert reconcile_task is not None
            assert project_id in supervisor._runners

            # A loop-affecting edit makes the background tick stop+relaunch.
            with session_scope(migrated_factory) as session:
                project = session.get(Project, project_id)
                project.capture_interval_seconds = 7200
            supervisor.notify_reconcile()
            await asyncio.wait_for(unwinding.wait(), timeout=5)

            # The tick is now awaiting the runner's unwind. Shut down, then let
            # the runner finish unwinding.
            stop_task = asyncio.create_task(supervisor.stop())
            for _ in range(5):
                await asyncio.sleep(0)
            release_unwind.set()

            done, _ = await asyncio.wait({stop_task}, timeout=2)
            if not done:
                # Unhang teardown before failing: the swallowed cancel left the
                # loop alive, so cancel it again (it is now in its idle wait).
                reconcile_task.cancel()
                await asyncio.wait({stop_task}, timeout=5)
            assert done, "stop() hung: reconcile loop swallowed its cancellation"
            assert reconcile_task.cancelled()

    async def test_reconcile_once_is_serialized(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        # Two overlapping passes (the background tick plus a direct call) must
        # not both launch the same newly-qualifying project: the second launch
        # would overwrite the first runner in _runners and orphan its task, which
        # stop() then never cancels.
        settings = _fake_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        launched: list[asyncio.Task[None]] = []
        original_launch = supervisor._launch

        def recording_launch(target: CaptureTarget) -> None:
            original_launch(target)
            task = supervisor._runners[target.project_id].task
            assert task is not None
            launched.append(task)

        with session_scope(migrated_factory) as session:
            cam_id = _seed_camera(session, name="rc-serial-cam")
            _seed_project(
                session, camera_id=cam_id, name="rc-serial-proj", lifecycle="active"
            )

        with patch.object(supervisor, "_launch", recording_launch):
            await asyncio.gather(
                supervisor._reconcile_once(), supervisor._reconcile_once()
            )
        try:
            assert len(launched) == 1
        finally:
            for task in launched:
                task.cancel()
            await asyncio.gather(*launched, return_exceptions=True)
            await supervisor.stop()

    async def test_stop_from_cancelled_caller_still_tears_everything_down(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        # The app lifespan calls stop() from a ``finally``, which can run while
        # the lifespan task is itself being cancelled (cancelling() > 0). The
        # teardown must still cancel the runners and close the HTTP client
        # rather than bail out after the reconcile loop.
        settings = _fake_settings(tmp_path)
        with session_scope(migrated_factory) as session:
            cam_id = _seed_camera(session, name="rc-cancelstop-cam")
            project_id = _seed_project(
                session,
                camera_id=cam_id,
                name="rc-cancelstop-proj",
                lifecycle="active",
            )
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        await supervisor.start()
        runner_task = supervisor._runners[project_id].task
        assert runner_task is not None
        parked = asyncio.Event()

        async def lifespan_like() -> None:
            try:
                parked.set()
                await asyncio.Event().wait()
            finally:
                await supervisor.stop()

        owner = asyncio.create_task(lifespan_like())
        await parked.wait()
        owner.cancel()
        done, _ = await asyncio.wait({owner}, timeout=5)

        assert done
        assert owner.cancelled()
        assert runner_task.done()
        assert supervisor.http_client.is_closed


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
                _event("Device/IO/VirtualInput", active=False),
                _event("Device/IO/VirtualInput", active=True),
            ]
        )

        from unittest.mock import AsyncMock

        with patch(
            "timelapse_manager.capture.supervisor.capture_one_now",
            new=AsyncMock(return_value=None),
        ) as fire:
            await supervisor._consume_event_source(target, source)

        # Two rising edges, no cooldown: both fire. (A repeat of an active state
        # is not an edge; see TestEdgeDetection.)
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
                _event("Device/IO/VirtualInput", active=False),
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


# ---------------------------------------------------------------------------
# DST spring-forward: a clock anchor in the gap is shifted forward and logged
# ---------------------------------------------------------------------------


class TestDstSpringForwardAnchorLog:
    async def test_clock_anchor_in_gap_logs_forward_shift(
        self, migrated_factory, tmp_path: Path, caplog
    ) -> None:
        import logging
        from zoneinfo import ZoneInfo

        from timelapse_manager.capture import anchors as anchors_mod
        from timelapse_manager.capture.anchors import Anchor
        from timelapse_manager.db.models import ExactTimeFire

        supervisor = CaptureSupervisor(
            _fake_settings(tmp_path),
            migrated_factory,
            disk_monitor=_permissive_disk_monitor(),
        )
        with session_scope(migrated_factory) as session:
            cam_id = _seed_camera(session, name="dst-cam")
            proj_id = _seed_project(
                session, camera_id=cam_id, name="dst-proj", lifecycle="active"
            )

        target = CaptureTarget(
            project_id=proj_id,
            project_name="dst-proj",
            camera_id=cam_id,
            interval_seconds=None,
        )
        # 02:30 America/New_York on 2026-03-08 falls in the spring-forward gap
        # (02:00 -> 03:00). The decision carries the already-shifted fire instant
        # (03:00 EDT == 07:00 UTC). within_grace=False keeps us on the skip path
        # (no capture), which still claims the fire-log row and logs the shift.
        anchor = Anchor(
            id="a1", kind="clock", time="02:30", offset_minutes=0, enabled=True
        )
        decision = anchors_mod.AnchorDecision(
            anchor=anchor,
            local_date="2026-03-08",
            instant=datetime(2026, 3, 8, 7, 0, tzinfo=UTC),
            within_grace=False,
            has_geo=True,
        )
        # The Alembic migration the migrated_factory fixture runs calls
        # logging.config.fileConfig with disable_existing_loggers=True, which
        # disables the already-imported supervisor logger; re-enable it so the
        # info log under test is captured (a test-harness artifact, not the SUT).
        logging.getLogger("timelapse_manager.capture.supervisor").disabled = False
        with caplog.at_level(
            logging.INFO, logger="timelapse_manager.capture.supervisor"
        ):
            await supervisor._fire_anchor_decision(
                target, decision, ZoneInfo("America/New_York")
            )

        assert "spring-forward gap" in caplog.text
        assert "shifted forward" in caplog.text
        # The once-per-day fire-log row was claimed exactly once.
        with session_scope(migrated_factory) as session:
            rows = (
                session.query(ExactTimeFire)
                .filter_by(project_id=proj_id, anchor_id="a1")
                .all()
            )
            assert len(rows) == 1
        await supervisor.stop()

    async def test_normal_clock_anchor_does_not_log_shift(
        self, migrated_factory, tmp_path: Path, caplog
    ) -> None:
        import logging
        from zoneinfo import ZoneInfo

        from timelapse_manager.capture import anchors as anchors_mod
        from timelapse_manager.capture.anchors import Anchor

        supervisor = CaptureSupervisor(
            _fake_settings(tmp_path),
            migrated_factory,
            disk_monitor=_permissive_disk_monitor(),
        )
        with session_scope(migrated_factory) as session:
            cam_id = _seed_camera(session, name="ok-cam")
            proj_id = _seed_project(
                session, camera_id=cam_id, name="ok-proj", lifecycle="active"
            )

        target = CaptureTarget(
            project_id=proj_id,
            project_name="ok-proj",
            camera_id=cam_id,
            interval_seconds=None,
        )
        # 06:30 exists on the transition day -> no shift, no log.
        anchor = Anchor(
            id="b1", kind="clock", time="06:30", offset_minutes=0, enabled=True
        )
        decision = anchors_mod.AnchorDecision(
            anchor=anchor,
            local_date="2026-03-08",
            instant=datetime(2026, 3, 8, 10, 30, tzinfo=UTC),
            within_grace=False,
            has_geo=True,
        )
        # The Alembic migration the migrated_factory fixture runs calls
        # logging.config.fileConfig with disable_existing_loggers=True, which
        # disables the already-imported supervisor logger; re-enable it so the
        # info log under test is captured (a test-harness artifact, not the SUT).
        logging.getLogger("timelapse_manager.capture.supervisor").disabled = False
        with caplog.at_level(
            logging.INFO, logger="timelapse_manager.capture.supervisor"
        ):
            await supervisor._fire_anchor_decision(
                target, decision, ZoneInfo("America/New_York")
            )

        assert "spring-forward gap" not in caplog.text
        await supervisor.stop()


# ---------------------------------------------------------------------------
# Event-dedup cooldown survives a re-subscribe (within the same process)
# ---------------------------------------------------------------------------


class TestEventDedupPersistsAcrossResubscribe:
    def _supervisor(self, tmp_path: Path) -> CaptureSupervisor:
        return CaptureSupervisor(
            _fake_settings(tmp_path),
            MagicMock(),
            disk_monitor=_permissive_disk_monitor(),
        )

    def _target(self) -> CaptureTarget:
        return CaptureTarget(
            project_id=1,
            project_name="p",
            camera_id=1,
            interval_seconds=None,
            event_triggers=[
                {
                    "id": "t1",
                    "topic_id": "Device/IO/VirtualInput",
                    "cooldown_seconds": 3600,
                }
            ],
        )

    async def test_shared_last_fired_dedupes_second_subscription(
        self, tmp_path: Path
    ) -> None:
        from unittest.mock import AsyncMock

        supervisor = self._supervisor(tmp_path)
        target = self._target()
        last_fired: dict[str, float] = {}

        with patch(
            "timelapse_manager.capture.supervisor.capture_one_now",
            new=AsyncMock(return_value=None),
        ) as fire:
            # First subscription fires once and stamps the cooldown.
            await supervisor._consume_event_source(
                target,
                _FakeEventSource([_event("Device/IO/VirtualInput", active=True)]),
                last_fired,
            )
            # Simulated re-subscribe carrying the SAME dedup state: an immediate
            # second event for the same topic is still within cooldown and is
            # suppressed (a flapping camera does not fire on reconnect).
            await supervisor._consume_event_source(
                target,
                _FakeEventSource([_event("Device/IO/VirtualInput", active=True)]),
                last_fired,
            )

        assert fire.call_count == 1
        await supervisor.stop()

    async def test_fresh_last_fired_each_subscription_fires_again(
        self, tmp_path: Path
    ) -> None:
        from unittest.mock import AsyncMock

        supervisor = self._supervisor(tmp_path)
        target = self._target()

        with patch(
            "timelapse_manager.capture.supervisor.capture_one_now",
            new=AsyncMock(return_value=None),
        ) as fire:
            # No shared state (default fresh map per call): the cooldown does not
            # carry over, so each subscription fires -- the control for the
            # persistence test above.
            await supervisor._consume_event_source(
                target,
                _FakeEventSource([_event("Device/IO/VirtualInput", active=True)]),
            )
            await supervisor._consume_event_source(
                target,
                _FakeEventSource([_event("Device/IO/VirtualInput", active=True)]),
            )

        assert fire.call_count == 2
        await supervisor.stop()


# ---------------------------------------------------------------------------
# Per-capture SHA-256 is hashed off the event loop
# ---------------------------------------------------------------------------


class TestFrozenFrameHashOffLoop:
    async def test_sha256_computed_in_thread(self, tmp_path: Path, monkeypatch) -> None:
        import hashlib

        supervisor = CaptureSupervisor(
            _fake_settings(tmp_path),
            MagicMock(),
            disk_monitor=_permissive_disk_monitor(),
        )
        target = CaptureTarget(
            project_id=1, project_name="p", camera_id=1, interval_seconds=60
        )
        state = CaptureState(project_id=1, camera_id=1)
        captured = CapturedFrame(
            image_bytes=_FAKE_JPEG,
            width=1,
            height=1,
            format="jpeg",
            captured_at=datetime.now(UTC),
        )

        recorded: list = []
        real_run_blocking = supervisor._run_blocking

        async def spy(func, *args, **kwargs):
            recorded.append(func)
            return await real_run_blocking(func, *args, **kwargs)

        # Every blocking hop goes through the executor seam (WP-63-A).
        monkeypatch.setattr(supervisor, "_run_blocking", spy)

        await supervisor._check_frozen_frame(target, state, captured)

        expected = hashlib.sha256(_FAKE_JPEG).hexdigest()
        # A single frame is below the frozen-frame threshold, so the only
        # off-loop work is the hash itself; the recorded callable yields the
        # SHA-256 hexdigest of the captured bytes.
        assert len(recorded) == 1
        assert recorded[0]() == expected
        assert state.last_frame_hash == expected
        await supervisor.stop()


# ---------------------------------------------------------------------------
# _end_campaign's state write rides the SQLite write-retry helper
# ---------------------------------------------------------------------------


class TestEndCampaignWriteRetry:
    def test_retries_transient_lock_then_archives(
        self, migrated_factory, tmp_path: Path, monkeypatch
    ) -> None:
        import sqlite3

        import timelapse_manager.capture.supervisor as sup_mod
        from timelapse_manager.db.engine import run_with_write_retry as real_retry

        supervisor = CaptureSupervisor(
            _fake_settings(tmp_path),
            migrated_factory,
            disk_monitor=_permissive_disk_monitor(),
        )
        with session_scope(migrated_factory) as session:
            cam_id = _seed_camera(session, name="rc-cam")
            proj_id = _seed_project(
                session, camera_id=cam_id, name="rc-proj", lifecycle="active"
            )

        calls = {"n": 0}

        def flaky_retry(operation, **kwargs):
            # Prove _end_campaign routes its write through run_with_write_retry,
            # and that a transient "database is locked" is retried then succeeds:
            # fail the first underlying attempt, then delegate to the real helper.
            calls["n"] += 1
            attempts = {"locked_once": False}

            def wrapped():
                if not attempts["locked_once"]:
                    attempts["locked_once"] = True
                    raise sqlite3.OperationalError("database is locked")
                return operation()

            return real_retry(wrapped, base_delay=0.0)

        monkeypatch.setattr(sup_mod, "run_with_write_retry", flaky_retry)

        supervisor._end_campaign(proj_id, "rc-proj", "frame_count")

        assert calls["n"] == 1
        with session_scope(migrated_factory) as session:
            project = session.get(Project, proj_id)
            assert project is not None
            assert project.lifecycle_state == "archived"


# ---------------------------------------------------------------------------
# #69 CAM-R2: only the snapshot sits inside the capture budget
# ---------------------------------------------------------------------------

_NY = "America/New_York"
_ANCHOR = {
    "id": "noon",
    "kind": "clock",
    "time": "12:05",
    "offset_minutes": 0,
    "enabled": True,
}
# A realistic Axis ``Image`` group: what the scene-metadata read returns.
_IMAGE_PARAMS = (
    "root.Image.I0.Appearance.Resolution=1920x1080\n"
    "root.Image.I0.Appearance.Brightness=55\n"
    "root.Image.I0.Exposure.ExposureValue=70\n"
)


def _seed_anchor_project(factory, tmp_path: Path) -> CaptureTarget:  # noqa: ANN001
    """An anchor-only VAPIX project, shaped like the deployed 12:05 projects."""
    storage = tmp_path / "frames" / "anchor"
    storage.mkdir(parents=True, exist_ok=True)
    schedule = {"enabled": True, "timezone": _NY}
    with session_scope(factory) as session:
        cam = Camera(name="anchor-cam", address="10.0.16.71", protocol="vapix")
        session.add(cam)
        session.flush()
        proj = Project(
            camera_id=cam.id,
            name="anchor",
            capture_interval_seconds=None,
            lifecycle_state="active",
            operational_status="idle",
            storage_path=str(storage),
            schedule=schedule,
            exact_time_anchors=[_ANCHOR],
        )
        session.add(proj)
        session.flush()
        return CaptureTarget(
            project_id=proj.id,
            project_name=proj.name,
            camera_id=cam.id,
            interval_seconds=None,
            schedule=schedule,
            storage_path=str(storage),
            exact_time_anchors=[_ANCHOR],
        )


def _camera_http(snapshot_seconds: float, metadata: str | None):  # noqa: ANN202
    """Stand-in for the VAPIX ``http_get_image``: a snapshot that takes
    ``snapshot_seconds`` and a parameter (scene) read that returns ``metadata``,
    or never returns when ``metadata`` is None."""
    from tests.conftest import make_jpeg
    from timelapse_manager.cameras.vapix import PARAM_PATH

    async def get(client, url, credentials, timeout, **_kw):  # noqa: ANN001, ANN202
        if PARAM_PATH in url:
            if metadata is None:
                await asyncio.sleep(3600)
            return metadata.encode()
        await asyncio.sleep(snapshot_seconds)
        return make_jpeg(64, 48)

    return get


def _anchor_outcome(factory, project_id: int) -> tuple[str | None, list]:  # noqa: ANN001
    from timelapse_manager.db.models import ExactTimeFire

    with session_scope(factory) as session:
        fire = session.query(ExactTimeFire).filter_by(project_id=project_id).one()
        frames = (
            session.query(Frame.scene_metadata, Frame.capture_reason)
            .filter_by(project_id=project_id)
            .all()
        )
        return fire.status, [tuple(row) for row in frames]


class TestAnchorCaptureSceneMetadata:
    """The 12:05 anchors (every deployed project) capture through VAPIX.

    These drive the real anchor fire path end to end -- claim, ``capture_one_now``,
    the real ``VapixAdapter`` and ``frame_from_bytes``, the frame writer and the
    fire-log outcome -- with only the camera's HTTP replaced.
    """

    @staticmethod
    def _now() -> datetime:
        from zoneinfo import ZoneInfo

        return datetime(2026, 10, 14, 12, 5, 3, tzinfo=ZoneInfo(_NY)).astimezone(UTC)

    async def test_slow_metadata_read_no_longer_discards_the_frame(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """CAM-R2 regression: the scene read never answers. Before the fix the
        metadata read ran inside the capture budget, the budget expired, and the
        fetched JPEG was thrown away (fire-log ``failed``, no frame). Now the
        frame is written without metadata, after at most the metadata total."""
        import timelapse_manager.capture.supervisor as sup_mod

        # The snapshot is quick and the scene read never answers. Before the fix
        # that read ran inside the budget and expired it, so the regression shows
        # whatever the snapshot's own time; a snapshot sized to 0.9 of a 1 s
        # budget left 0.1 s and failed on a loaded host before the scene read
        # even started.
        budget = 2.0
        supervisor = CaptureSupervisor(
            _make_settings(tmp_path, timeout_seconds=budget),
            migrated_factory,
            disk_monitor=_permissive_disk_monitor(),
        )
        target = _seed_anchor_project(migrated_factory, tmp_path)
        loop = asyncio.get_running_loop()
        with patch(
            "timelapse_manager.cameras.vapix.http_get_image",
            _camera_http(0.05, metadata=None),
        ):
            started = loop.time()
            await supervisor._evaluate_exact_time_anchors(target, self._now())
            elapsed = loop.time() - started
        await supervisor.stop()

        status, frames = _anchor_outcome(migrated_factory, target.project_id)
        assert status == "captured"
        assert frames == [(None, "anchor:clock")]
        assert elapsed < budget + sup_mod.SCENE_METADATA_TOTAL_SECONDS + 1.0

    async def test_fast_metadata_is_still_persisted(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """Otherwise unchanged: a camera that answers the scene read in time
        gets its metadata stored with the anchor frame, as before."""
        supervisor = CaptureSupervisor(
            _make_settings(tmp_path, timeout_seconds=1.0),
            migrated_factory,
            disk_monitor=_permissive_disk_monitor(),
        )
        target = _seed_anchor_project(migrated_factory, tmp_path)
        with patch(
            "timelapse_manager.cameras.vapix.http_get_image",
            _camera_http(0.01, metadata=_IMAGE_PARAMS),
        ):
            await supervisor._evaluate_exact_time_anchors(target, self._now())
        await supervisor.stop()

        status, frames = _anchor_outcome(migrated_factory, target.project_id)
        assert status == "captured"
        [(meta, reason)] = frames
        assert reason == "anchor:clock"
        assert meta["source"] == "vapix"
        assert meta["captured_resolution"] == "64x48"
        assert meta["brightness"] == "55"

    async def test_snapshot_over_budget_still_fails_the_anchor(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """The budget still bounds the snapshot itself. Since #69 a failed
        attempt inside grace is parked for a retry rather than final."""
        supervisor = CaptureSupervisor(
            _make_settings(tmp_path, timeout_seconds=0.2),
            migrated_factory,
            disk_monitor=_permissive_disk_monitor(),
        )
        target = _seed_anchor_project(migrated_factory, tmp_path)
        with patch(
            "timelapse_manager.cameras.vapix.http_get_image",
            _camera_http(5.0, metadata=_IMAGE_PARAMS),
        ):
            await supervisor._evaluate_exact_time_anchors(target, self._now())
        await supervisor.stop()

        status, frames = _anchor_outcome(migrated_factory, target.project_id)
        assert status == "retry_pending"
        assert frames == []


class _MetadataAdapter(FakeAdapter):
    """A fast capture whose scene-metadata step does what the test asks."""

    def __init__(self, behaviour: str, notes: list[str] | None = None) -> None:
        super().__init__()
        self._behaviour = behaviour
        self._notes = notes or []
        self.metadata_started = asyncio.Event()

    async def capture(self) -> CapturedFrame:
        frame = await super().capture()
        frame.capture_notes = list(self._notes)
        return frame

    async def collect_scene_metadata(self, frame: CapturedFrame) -> dict | None:
        self.metadata_started.set()
        if self._behaviour == "hang":
            await asyncio.sleep(3600)
        if self._behaviour == "error":
            raise RuntimeError("param CGI exploded")
        return {"schema_version": 1, "source": "fake"}


class TestCollectSceneMetadata:
    async def test_interval_capture_writes_frame_when_metadata_hangs(
        self, migrated_factory, tmp_path: Path, monkeypatch
    ) -> None:
        import timelapse_manager.capture.supervisor as sup_mod

        monkeypatch.setattr(sup_mod, "SCENE_METADATA_TOTAL_SECONDS", 0.05)
        supervisor = CaptureSupervisor(
            _make_settings(tmp_path, timeout_seconds=1.0),
            migrated_factory,
            disk_monitor=_permissive_disk_monitor(),
        )
        cam_id, project_id = _seed_capture_project(migrated_factory)
        target = CaptureTarget(
            project_id=project_id,
            project_name="p",
            camera_id=cam_id,
            interval_seconds=60,
        )
        state = CaptureState(project_id=project_id, camera_id=cam_id)
        with patch(
            "timelapse_manager.capture.supervisor.build_adapter",
            return_value=_MetadataAdapter("hang"),
        ):
            count = await supervisor._capture_once(target, state)
        await supervisor.stop()

        assert count == 1
        with session_scope(migrated_factory) as session:
            [meta] = session.query(Frame.scene_metadata).all()
        assert meta == (None,)

    @pytest.mark.parametrize("producer", ["interval", "one_shot"])
    async def test_fast_metadata_is_written_with_the_frame(
        self, migrated_factory, tmp_path: Path, producer: str
    ) -> None:
        from timelapse_manager.capture.one_shot import capture_one_now

        supervisor = CaptureSupervisor(
            _make_settings(tmp_path, timeout_seconds=1.0),
            migrated_factory,
            disk_monitor=_permissive_disk_monitor(),
        )
        cam_id, project_id = _seed_capture_project(migrated_factory)
        target = CaptureTarget(
            project_id=project_id,
            project_name="p",
            camera_id=cam_id,
            interval_seconds=60,
        )
        adapter = _MetadataAdapter("fast")
        with (
            patch(
                "timelapse_manager.capture.supervisor.build_adapter",
                return_value=adapter,
            ),
            patch(
                "timelapse_manager.capture.one_shot.build_adapter",
                return_value=adapter,
            ),
        ):
            if producer == "interval":
                state = CaptureState(project_id=project_id, camera_id=cam_id)
                await supervisor._capture_once(target, state)
            else:
                await capture_one_now(supervisor, target, reason="anchor:clock")
        await supervisor.stop()

        with session_scope(migrated_factory) as session:
            [meta] = session.query(Frame.scene_metadata).all()
        assert meta == ({"schema_version": 1, "source": "fake"},)

    async def test_metadata_error_leaves_none(self, tmp_path: Path) -> None:
        supervisor = CaptureSupervisor(
            _fake_settings(tmp_path),
            MagicMock(),
            disk_monitor=_permissive_disk_monitor(),
        )
        adapter = _MetadataAdapter("error")
        frame = await adapter.capture()
        await supervisor.collect_scene_metadata(adapter, frame)
        assert frame.scene_metadata is None
        await supervisor.stop()

    async def test_cancel_during_metadata_propagates(self, tmp_path: Path) -> None:
        """Shutdown is never swallowed by the metadata step."""
        supervisor = CaptureSupervisor(
            _fake_settings(tmp_path),
            MagicMock(),
            disk_monitor=_permissive_disk_monitor(),
        )
        adapter = _MetadataAdapter("hang")
        frame = await adapter.capture()
        task = asyncio.create_task(supervisor.collect_scene_metadata(adapter, frame))
        await adapter.metadata_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await supervisor.stop()


def _seed_capture_project(factory) -> tuple[int, int]:  # noqa: ANN001
    with session_scope(factory) as session:
        cam = Camera(name="meta-cam", address="10.0.0.9", protocol="vapix")
        session.add(cam)
        session.flush()
        proj = Project(
            camera_id=cam.id,
            name="p",
            capture_interval_seconds=60,
            lifecycle_state="active",
            operational_status="idle",
        )
        session.add(proj)
        session.flush()
        return cam.id, proj.id


class TestStreamProfileDegradedWarning:
    """OD-2: a missing stream profile captures at the default and warns once."""

    @staticmethod
    def _degraded_events(factory) -> list[dict]:  # noqa: ANN001
        from timelapse_manager.db.models import Event

        with session_scope(factory) as session:
            rows = session.query(Event.event_metadata).all()
        return [
            meta
            for (meta,) in rows
            if meta and meta.get("reason") == "stream_profile_degraded"
        ]

    async def test_one_event_per_reason_per_runner(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        supervisor = CaptureSupervisor(
            _make_settings(tmp_path, timeout_seconds=1.0),
            migrated_factory,
            disk_monitor=_permissive_disk_monitor(),
        )
        cam_id, project_id = _seed_capture_project(migrated_factory)
        target = CaptureTarget(
            project_id=project_id,
            project_name="p",
            camera_id=cam_id,
            interval_seconds=60,
            stream_id="Deleted",
        )
        state = CaptureState(project_id=project_id, camera_id=cam_id)

        def adapter(notes: list[str]) -> _MetadataAdapter:
            return _MetadataAdapter("fast", notes=notes)

        build = "timelapse_manager.capture.supervisor.build_adapter"
        for notes in (["stream_profile_missing"], ["stream_profile_missing"]):
            with patch(build, return_value=adapter(notes)):
                await supervisor._capture_once(target, state)
        events = self._degraded_events(migrated_factory)
        assert [e["detail"] for e in events] == ["stream_profile_missing"]
        assert events[0]["stream_id"] == "Deleted"

        # A different reason is reported once too.
        with patch(build, return_value=adapter(["stream_profile_timeout"])):
            await supervisor._capture_once(target, state)
        # A relaunched runner reports afresh: _launch clears the latch.
        supervisor._degradations_reported.pop(project_id, None)
        with patch(build, return_value=adapter(["stream_profile_missing"])):
            await supervisor._capture_once(target, state)
        await supervisor.stop()

        details = [e["detail"] for e in self._degraded_events(migrated_factory)]
        assert details == [
            "stream_profile_missing",
            "stream_profile_timeout",
            "stream_profile_missing",
        ]
        with session_scope(migrated_factory) as session:
            assert session.query(Frame).count() == 4

    async def test_launch_clears_the_latch(self, tmp_path: Path) -> None:
        supervisor = CaptureSupervisor(
            _fake_settings(tmp_path),
            MagicMock(),
            disk_monitor=_permissive_disk_monitor(),
        )
        supervisor._degradations_reported[5] = {"stream_profile_missing"}
        target = CaptureTarget(
            project_id=5, project_name="p", camera_id=1, interval_seconds=60
        )
        with patch.object(supervisor, "_run_project", new=_never_returns):
            supervisor._launch(target)
        assert 5 not in supervisor._degradations_reported
        await supervisor.stop()


async def _never_returns(*_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
    await asyncio.Event().wait()


# ---------------------------------------------------------------------------
# Per-camera health (#69 CAM-R6, CAM-R1 item 3, CAM-R5 auth alert)
# ---------------------------------------------------------------------------

_HEALTH_NY = "America/New_York"
_HEALTH_ANCHOR = {
    "id": "noon",
    "kind": "clock",
    "time": "12:05",
    "offset_minutes": 0,
    "enabled": True,
}


def _health_instant():  # noqa: ANN202
    from zoneinfo import ZoneInfo

    return datetime(2026, 10, 14, 12, 5, tzinfo=ZoneInfo(_HEALTH_NY)).astimezone(UTC)


class _HealthClock:
    """A settable wall clock for the anchor-driven health tests."""

    def __init__(self, start: datetime) -> None:
        self.current = start

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.current.timestamp()

    async def sleep(self, seconds: float) -> None:
        from datetime import timedelta

        self.current += timedelta(seconds=max(0.0, seconds))
        await asyncio.sleep(0)


class _ScriptedHealthAdapter(FakeAdapter):
    """Each capture pops the next outcome: ``"ok"`` or an exception to raise."""

    def __init__(self, script: list[object]) -> None:
        super().__init__()
        self._script = script

    async def capture(self) -> CapturedFrame:
        outcome = self._script.pop(0) if self._script else "ok"
        if isinstance(outcome, BaseException):
            raise outcome
        return await super().capture()


def _health_settings(tmp_path: Path, threshold: int = 3) -> Settings:
    from timelapse_manager.config.settings import (
        DatabaseSettings,
        LoggingSettings,
        PathsSettings,
    )

    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    return Settings(
        database=DatabaseSettings(url=f"sqlite:///{tmp_path / 'test.db'}"),
        logging=LoggingSettings(level="WARNING", format="text"),
        paths=PathsSettings(
            data_dir=data_dir,
            frames_root=data_dir / "frames",
            token_file=data_dir / ".local-token",
        ),
        capture=CaptureSettings(
            autostart=False,
            timeout_seconds=1.0,
            offline_failure_threshold=threshold,
        ),
    )


def _health_supervisor(
    factory,  # noqa: ANN001
    tmp_path: Path,
    *,
    threshold: int = 3,
    clock: _HealthClock | None = None,
) -> CaptureSupervisor:
    return CaptureSupervisor(
        _health_settings(tmp_path, threshold),
        factory,
        clock=clock,
        disk_monitor=_permissive_disk_monitor(),
    )


def _seed_shared_camera(
    factory,  # noqa: ANN001
    tmp_path: Path,
    names: list[str],
    *,
    anchors: bool = False,
    camera_name: str = "Driveway",
) -> list[CaptureTarget]:
    """One camera shared by several active projects, like prod camera 2."""
    schedule = {"enabled": True, "timezone": _HEALTH_NY}
    targets: list[CaptureTarget] = []
    with session_scope(factory) as session:
        cam = Camera(name=camera_name, address="10.0.16.71", protocol="vapix")
        session.add(cam)
        session.flush()
        for name in names:
            storage = tmp_path / "frames" / name
            storage.mkdir(parents=True, exist_ok=True)
            proj = Project(
                camera_id=cam.id,
                name=name,
                capture_interval_seconds=None if anchors else 60,
                lifecycle_state="active",
                operational_status="idle",
                storage_path=str(storage),
                schedule=schedule if anchors else None,
                exact_time_anchors=[_HEALTH_ANCHOR] if anchors else None,
            )
            session.add(proj)
            session.flush()
            targets.append(
                CaptureTarget(
                    project_id=proj.id,
                    project_name=name,
                    camera_id=cam.id,
                    interval_seconds=None if anchors else 60,
                    schedule=schedule if anchors else None,
                    storage_path=str(storage),
                    exact_time_anchors=[_HEALTH_ANCHOR] if anchors else None,
                )
            )
    return targets


def _health_events(factory, *, scope: str) -> list[tuple[str, str, dict]]:  # noqa: ANN001
    from timelapse_manager.db.models import Event

    with session_scope(factory) as session:
        rows = session.query(Event).filter_by(scope=scope).order_by(Event.id).all()
        return [
            (row.level, row.message, dict(row.event_metadata or {})) for row in rows
        ]


def _health_reasons(factory, *, scope: str) -> list[str]:  # noqa: ANN001
    return [str(m.get("reason")) for _, _, m in _health_events(factory, scope=scope)]


async def _interval_attempt(
    supervisor: CaptureSupervisor,
    target: CaptureTarget,
    state: CaptureState,
    outcome: object,
) -> None:
    import random

    adapter = _ScriptedHealthAdapter([outcome])
    with patch(
        "timelapse_manager.capture.supervisor.build_adapter", return_value=adapter
    ):
        await supervisor._attempt_capture(target, state, random.Random(1))


class TestCameraHealthPerCamera:
    async def test_three_projects_on_one_camera_raise_one_alert_and_one_recovery(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        supervisor = _health_supervisor(migrated_factory, tmp_path, threshold=3)
        targets = _seed_shared_camera(
            migrated_factory, tmp_path, ["alpha", "bravo", "charlie"]
        )
        from timelapse_manager.capture.supervisor import _ProjectRunner

        states = [
            CaptureState(project_id=t.project_id, camera_id=t.camera_id)
            for t in targets
        ]
        # Registered runners (no task) so the per-camera mirror reaches them.
        for target, state in zip(targets, states, strict=True):
            supervisor._runners[target.project_id] = _ProjectRunner(
                state=state, target=target
            )
        down = UnreachableCaptureError("connect timeout")
        # Three rounds of failures across the three projects: the alert trips
        # when one project has failed three times in a row (#130), not at the
        # camera's third failure.
        for _ in range(3):
            for target, state in zip(targets, states, strict=True):
                await _interval_attempt(supervisor, target, state, down)
        assert all(s.offline_alerted for s in states)
        await _interval_attempt(supervisor, targets[1], states[1], "ok")
        supervisor._runners.clear()
        await supervisor.stop()

        events = _health_events(migrated_factory, scope="camera")
        assert [m["reason"] for _, _, m in events] == [
            "camera_offline",
            "camera_recovered",
        ]
        level, message, meta = events[0]
        assert level == "warning"
        assert message.startswith(
            "camera 'Driveway' is offline (projects: alpha, bravo, charlie): "
            "3 consecutive capture attempts failed"
        )
        assert meta["camera_id"] == targets[0].camera_id
        assert meta["auth_failed"] is False
        assert meta["projects"] == ["alpha", "bravo", "charlie"]
        # Nothing camera-health related is project-scoped any more.
        assert "camera_offline" not in _health_reasons(
            migrated_factory, scope="project"
        )
        # The per-project backoff is untouched; the mirror follows the camera.
        assert [s.attempt_count for s in states] == [3, 0, 3]
        assert all(not s.offline_alerted for s in states)

    async def test_alert_survives_a_restart_and_resolves_on_recovery(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        from timelapse_manager.monitoring.alerts import (
            auto_clear_for_event,
            get_active_alerts,
        )

        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["alpha"])
        first = _health_supervisor(migrated_factory, tmp_path, threshold=1)
        state = CaptureState(project_id=target.project_id, camera_id=target.camera_id)
        await _interval_attempt(first, target, state, UnreachableCaptureError("x"))
        assert state.offline_alerted
        await first.stop()

        # A new process: the book is empty until it is seeded from the DB.
        second = _health_supervisor(migrated_factory, tmp_path, threshold=1)
        await second._ensure_camera_health_synced()
        fresh = CaptureState(project_id=target.project_id, camera_id=target.camera_id)
        await _interval_attempt(second, target, fresh, "ok")
        await second.stop()

        assert _health_reasons(migrated_factory, scope="camera") == [
            "camera_offline",
            "camera_recovered",
        ]
        # The dispatcher applies the recovery to the camera-scoped alert.
        with session_scope(migrated_factory) as session:
            assert (
                auto_clear_for_event(
                    session,
                    scope="camera",
                    scope_id=target.camera_id,
                    reason="camera_recovered",
                )
                == 1
            )
        with session_scope(migrated_factory) as session:
            alerts, total = get_active_alerts(session, scope="camera")
        assert (alerts, total) == ([], 0)

    async def test_start_seeds_before_the_first_runner_reports(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["alpha"])
        first = _health_supervisor(migrated_factory, tmp_path, threshold=1)
        state = CaptureState(project_id=target.project_id, camera_id=target.camera_id)
        await _interval_attempt(first, target, state, UnreachableCaptureError("x"))
        await first.stop()

        second = _health_supervisor(migrated_factory, tmp_path, threshold=1)
        with patch.object(second, "_run_project", new=_never_returns):
            await second.start()
            assert second._camera_health.is_offline(target.camera_id)
            # The launched runner's state mirrors the camera on its next report.
            runner_state = second.state_for_project(target.project_id)
            assert runner_state is not None
            await second._report_camera_outcome(target, UnreachableCaptureError("y"))
            assert runner_state.offline_alerted
        await second.stop()
        # Still down after the restart: no second alert.
        assert _health_reasons(migrated_factory, scope="camera") == ["camera_offline"]

    async def test_a_local_failure_raises_no_alert_and_one_system_info_event(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        from sqlalchemy.exc import OperationalError

        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["alpha"])
        supervisor = _health_supervisor(migrated_factory, tmp_path, threshold=1)
        state = CaptureState(project_id=target.project_id, camera_id=target.camera_id)
        gone = CaptureTarget(
            project_id=target.project_id,
            project_name="alpha",
            camera_id=target.camera_id + 1000,  # the camera row was deleted
            interval_seconds=60,
            storage_path=target.storage_path,
        )
        import random

        for _ in range(3):
            await supervisor._attempt_capture(gone, state, random.Random(1))
        locked = OperationalError("UPDATE", {}, Exception("database is locked"))
        await supervisor._report_camera_outcome(target, locked)
        await supervisor._report_camera_outcome(target, locked)
        await supervisor.stop()

        assert _health_reasons(migrated_factory, scope="camera") == []
        system = _health_events(migrated_factory, scope="system")
        assert [m["reason"] for _, _, m in system] == [
            "capture_local_error",
            "capture_local_error",
        ]
        level, message, meta = system[0]
        # Info, not warning (#133): nothing resolves it, so a warning would
        # stand as an active alert until someone cleared it by hand.
        assert level == "info"
        assert meta["camera_id"] == gone.camera_id
        assert "no longer exists" in message
        assert system[1][2]["camera_id"] == target.camera_id
        # The project's own error state still shows the failure.
        assert state.last_error == f"camera {gone.camera_id} no longer exists"
        assert state.offline_alerted is False

    async def test_a_401_raises_the_alert_at_once(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        from timelapse_manager.cameras.base import AuthCaptureError

        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["alpha"])
        supervisor = _health_supervisor(migrated_factory, tmp_path, threshold=3)
        state = CaptureState(project_id=target.project_id, camera_id=target.camera_id)
        await _interval_attempt(
            supervisor,
            target,
            state,
            AuthCaptureError("401 Unauthorized from https://root:hunter2@10.0.16.71/"),
        )
        await supervisor.stop()

        (event,) = _health_events(migrated_factory, scope="camera")
        level, message, meta = event
        assert (level, meta["reason"], meta["auth_failed"]) == (
            "warning",
            "camera_offline",
            True,
        )
        assert meta["consecutive_failures"] == 1
        assert "rejected the credentials" in message
        assert "hunter2" not in repr(event)

    async def test_a_lost_event_write_is_retried_on_the_next_outcome(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["alpha"])
        supervisor = _health_supervisor(migrated_factory, tmp_path, threshold=1)
        state = CaptureState(project_id=target.project_id, camera_id=target.camera_id)
        real = supervisor._write_camera_health_event
        calls: list[str] = []

        def flaky(transition) -> None:  # noqa: ANN001
            calls.append(transition.kind)
            if len(calls) == 1:
                raise RuntimeError("database is locked")
            real(transition)

        with patch.object(supervisor, "_write_camera_health_event", new=flaky):
            await _interval_attempt(
                supervisor, target, state, UnreachableCaptureError("x")
            )
            assert state.offline_alerted is False
            await _interval_attempt(
                supervisor, target, state, UnreachableCaptureError("x")
            )
            assert state.offline_alerted is True
        await supervisor.stop()
        assert calls == ["offline", "offline"]
        assert _health_reasons(migrated_factory, scope="camera") == ["camera_offline"]


class TestAnchorOnlyCameraHealth:
    """Anchor attempts feed health, so an anchor-only camera can go offline."""

    @staticmethod
    async def _settle(
        supervisor: CaptureSupervisor,
        clock: _HealthClock,
        targets: list[CaptureTarget],
        *,
        max_cycles: int = 20,
    ) -> None:
        """Evaluate each runner's anchors, jumping to the soonest retry."""
        from datetime import timedelta

        for _ in range(max_cycles):
            wakes = [
                await supervisor._evaluate_exact_time_anchors(t, clock.now())
                for t in targets
            ]
            soon = [w for w in wakes if w and w - clock.now() < timedelta(hours=1)]
            if not soon:
                return
            clock.current = min(soon)
        raise AssertionError("did not settle")

    async def test_third_failed_attempt_raises_camera_offline_once(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """The §10.2 #13 shape: offline after attempt 3, anchor_failed after 7."""
        from datetime import timedelta

        script: list[object] = [UnreachableCaptureError("down")] * 10
        clock = _HealthClock(_health_instant() + timedelta(seconds=3))
        supervisor = _health_supervisor(migrated_factory, tmp_path, clock=clock)
        (target,) = _seed_shared_camera(
            migrated_factory, tmp_path, ["noon"], anchors=True
        )
        with patch(
            "timelapse_manager.capture.one_shot.build_adapter",
            lambda *a, **k: _ScriptedHealthAdapter(script),
        ):
            for attempt in range(1, 4):
                await supervisor._evaluate_exact_time_anchors(target, clock.now())
                reasons = _health_reasons(migrated_factory, scope="camera")
                assert reasons == ([] if attempt < 3 else ["camera_offline"])
                clock.current += timedelta(minutes=5)
            await self._settle(supervisor, clock, [target])
        await supervisor.stop()

        assert _health_reasons(migrated_factory, scope="camera") == ["camera_offline"]
        project = _health_reasons(migrated_factory, scope="project")
        # WP-69-A's anchor events are unchanged and not doubled.
        assert project == ["anchor_retry"] * 6 + ["anchor_failed"]
        assert len(script) == 3  # seven attempts made, three outcomes unused

    async def test_one_blip_across_four_anchor_projects_raises_no_alert(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """Prod camera 2's shape (#130): one hiccup fails all four first attempts.

        That is one failed round, not four failures: no ``camera_offline``, so
        no offline-and-recovered pair a few seconds apart.
        """
        from datetime import timedelta

        script: list[object] = [UnreachableCaptureError("down")] * 4 + ["ok"] * 4
        clock = _HealthClock(_health_instant() + timedelta(seconds=3))
        supervisor = _health_supervisor(migrated_factory, tmp_path, clock=clock)
        targets = _seed_shared_camera(
            migrated_factory, tmp_path, ["a", "b", "c", "d"], anchors=True
        )
        with patch(
            "timelapse_manager.capture.one_shot.build_adapter",
            lambda *a, **k: _ScriptedHealthAdapter(script),
        ):
            await self._settle(supervisor, clock, targets)
        await supervisor.stop()

        assert _health_events(migrated_factory, scope="camera") == []
        project = _health_reasons(migrated_factory, scope="project")
        assert "anchor_failed" not in project
        assert project.count("anchor_retry") == 4
        assert len(script) == 0  # every project retried once and captured

    async def test_an_outage_across_four_anchor_projects_raises_one_alert(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """A real outage still alerts once, naming every project, then recovers.

        Each project fails its first three attempts; the alert trips when the
        first project's streak reaches three, and the first capture after the
        outage resolves it.
        """
        from datetime import timedelta

        script: list[object] = [UnreachableCaptureError("down")] * 12 + ["ok"] * 4
        clock = _HealthClock(_health_instant() + timedelta(seconds=3))
        supervisor = _health_supervisor(migrated_factory, tmp_path, clock=clock)
        targets = _seed_shared_camera(
            migrated_factory, tmp_path, ["a", "b", "c", "d"], anchors=True
        )
        with patch(
            "timelapse_manager.capture.one_shot.build_adapter",
            lambda *a, **k: _ScriptedHealthAdapter(script),
        ):
            await self._settle(supervisor, clock, targets)
        await supervisor.stop()

        events = _health_events(migrated_factory, scope="camera")
        assert [m["reason"] for _, _, m in events] == [
            "camera_offline",
            "camera_recovered",
        ]
        assert "(projects: a, b, c, d)" in events[0][1]
        assert events[0][2]["consecutive_failures"] == 3
        project = _health_reasons(migrated_factory, scope="project")
        assert "anchor_failed" not in project
        assert project.count("anchor_retry") == 12

    async def test_auth_failure_raises_at_once_then_anchor_failed_after_two(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        from datetime import timedelta

        from timelapse_manager.cameras.base import AuthCaptureError

        script: list[object] = [AuthCaptureError("401 Unauthorized")] * 5
        clock = _HealthClock(_health_instant() + timedelta(seconds=3))
        supervisor = _health_supervisor(migrated_factory, tmp_path, clock=clock)
        (target,) = _seed_shared_camera(
            migrated_factory, tmp_path, ["noon"], anchors=True
        )
        with patch(
            "timelapse_manager.capture.one_shot.build_adapter",
            lambda *a, **k: _ScriptedHealthAdapter(script),
        ):
            await supervisor._evaluate_exact_time_anchors(target, clock.now())
            (event,) = _health_events(migrated_factory, scope="camera")
            assert event[2]["auth_failed"] is True
            await self._settle(supervisor, clock, [target])
        await supervisor.stop()

        assert _health_reasons(migrated_factory, scope="camera") == ["camera_offline"]
        assert _health_reasons(migrated_factory, scope="project") == [
            "anchor_retry",
            "anchor_failed",
        ]

    async def test_a_low_disk_skip_is_not_a_camera_outcome(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        from datetime import timedelta

        clock = _HealthClock(_health_instant() + timedelta(seconds=3))
        supervisor = CaptureSupervisor(
            _health_settings(tmp_path, threshold=1),
            migrated_factory,
            clock=clock,
            disk_monitor=DiskSpaceMonitor(
                low_watermark_bytes=10**18,
                low_watermark_percent=99.9,
                resume_watermark_bytes=10**18,
                resume_watermark_percent=99.9,
                check_interval_seconds=0.0,
                get_free_bytes=lambda _p: 1,
                get_total_bytes=lambda _p: 10**15,
            ),
        )
        (target,) = _seed_shared_camera(
            migrated_factory, tmp_path, ["noon"], anchors=True
        )
        supervisor._camera_health.seed([target.camera_id])
        await supervisor._evaluate_exact_time_anchors(target, clock.now())
        await supervisor.stop()
        assert _health_reasons(migrated_factory, scope="camera") == []
        assert supervisor._camera_health.is_offline(target.camera_id)


class TestListenerCameraHealth:
    async def test_event_captures_feed_the_camera(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        from unittest.mock import AsyncMock

        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["alpha"])
        supervisor = _health_supervisor(migrated_factory, tmp_path, threshold=2)
        triggered = CaptureTarget(
            project_id=target.project_id,
            project_name="alpha",
            camera_id=target.camera_id,
            interval_seconds=None,
            event_triggers=[
                {
                    "id": "t1",
                    "topic_id": "Device/IO/VirtualInput",
                    "cooldown_seconds": 0,
                }
            ],
        )
        # Rising edges only count, so each capture is its own assert/clear pair.
        source = _FakeEventSource(
            [
                _event("Device/IO/VirtualInput", active=index % 2 == 0)
                for index in range(5)
            ]
        )
        fire = AsyncMock(
            side_effect=[
                UnreachableCaptureError("down"),
                UnreachableCaptureError("down"),
                MagicMock(),  # a written frame
            ]
        )
        with patch("timelapse_manager.capture.supervisor.capture_one_now", new=fire):
            await supervisor._consume_event_source(triggered, source)
        await supervisor.stop()

        assert fire.call_count == 3
        assert _health_reasons(migrated_factory, scope="camera") == [
            "camera_offline",
            "camera_recovered",
        ]
