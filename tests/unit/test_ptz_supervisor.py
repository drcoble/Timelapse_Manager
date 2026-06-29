"""Unit tests for PTZ positioning in the CaptureSupervisor (F3).

Drives ``_capture_once`` directly to verify two properties of the fail-closed
PTZ design:

1. When the project has a PTZ target, ``adapter.move_to`` is awaited before
   ``capture`` and a Frame is written on success.

2. When ``move_to`` raises ``PTZError``, the exception propagates out of
   ``_capture_once`` and NO Frame row is written — the adapter never reaches
   ``capture``.

Uses the same fixture/patch pattern as ``TestCaptureRecordsStreamId`` in
``tests/unit/test_supervisor.py``: a real migrated session factory, a
``MagicMock`` adapter, and ``patch.object`` for the supervisor's camera-load
helpers.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import FakeAdapter
from timelapse_manager.cameras.base import CaptureError, PTZError
from timelapse_manager.capture.supervisor import (
    CaptureState,
    CaptureSupervisor,
    CaptureTarget,
)
from timelapse_manager.config.settings import CaptureSettings, Settings
from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.storage.monitor import DiskSpaceMonitor

# ---------------------------------------------------------------------------
# Helpers (mirrors test_supervisor.py style)
# ---------------------------------------------------------------------------


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
        capture=CaptureSettings(autostart=False, timeout_seconds=0.05),
    )


def _seed_camera_and_project(migrated_factory) -> tuple[int, int]:  # type: ignore[type-arg]
    """Seed one Camera and one active Project; return (camera_id, project_id)."""
    with session_scope(migrated_factory) as session:
        cam = Camera(
            name="ptz-sup-cam",
            address="192.0.2.30",
            protocol="vapix",
        )
        session.add(cam)
        session.flush()
        cam_id = cam.id
        proj = Project(
            camera_id=cam_id,
            name="ptz-sup-project",
            lifecycle_state="active",
            operational_status="idle",
        )
        session.add(proj)
        session.flush()
        project_id = proj.id
    return cam_id, project_id


def _make_ptz_adapter(move_to_side_effect=None) -> MagicMock:
    """Return a FakeAdapter subclass with a controllable move_to AsyncMock.

    If ``move_to_side_effect`` is given it is used as the side_effect for the
    AsyncMock; otherwise move_to resolves successfully (returns None).
    """
    adapter = FakeAdapter()
    # Attach a move_to AsyncMock to the otherwise-concrete FakeAdapter.
    adapter.move_to = AsyncMock(side_effect=move_to_side_effect)  # type: ignore[method-assign]
    return adapter


# ---------------------------------------------------------------------------
# F3 — PTZ success: move_to called + Frame persisted
# ---------------------------------------------------------------------------


class TestPtzSupervisorSuccess:
    async def test_capture_once_calls_move_to_and_writes_frame(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """_capture_once awaits move_to for a preset target and persists a Frame."""
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        cam_id, project_id = _seed_camera_and_project(migrated_factory)

        adapter = _make_ptz_adapter()  # move_to succeeds (returns None)
        target = CaptureTarget(
            project_id=project_id,
            project_name="ptz-sup-project",
            camera_id=cam_id,
            interval_seconds=60,
            ptz_preset="home",
        )
        state = CaptureState(project_id=project_id, camera_id=cam_id)

        fake_config = MagicMock()
        with (
            patch.object(supervisor, "_load_camera", return_value=fake_config),
            patch.object(supervisor, "_load_default_credentials", return_value=None),
            patch(
                "timelapse_manager.capture.supervisor.build_adapter",
                return_value=adapter,
            ),
        ):
            await supervisor._capture_once(target, state)

        # move_to was called with the preset id.
        adapter.move_to.assert_awaited_once_with(preset_id="home")

        # A Frame row was persisted.
        with session_scope(migrated_factory) as session:
            frames = session.execute(
                Frame.__table__.select().where(Frame.project_id == project_id)
            ).all()
        assert len(frames) == 1

        await supervisor.stop()

    async def test_capture_once_calls_move_to_with_raw_position(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """_capture_once awaits move_to with pan/tilt/zoom for a raw-position target."""
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        cam_id, project_id = _seed_camera_and_project(migrated_factory)

        adapter = _make_ptz_adapter()
        target = CaptureTarget(
            project_id=project_id,
            project_name="ptz-sup-project",
            camera_id=cam_id,
            interval_seconds=60,
            ptz_pan=45.0,
            ptz_tilt=-10.0,
            ptz_zoom=2.0,
        )
        state = CaptureState(project_id=project_id, camera_id=cam_id)

        fake_config = MagicMock()
        with (
            patch.object(supervisor, "_load_camera", return_value=fake_config),
            patch.object(supervisor, "_load_default_credentials", return_value=None),
            patch(
                "timelapse_manager.capture.supervisor.build_adapter",
                return_value=adapter,
            ),
        ):
            await supervisor._capture_once(target, state)

        # move_to called with the raw position (no preset_id).
        adapter.move_to.assert_awaited_once_with(pan=45.0, tilt=-10.0, zoom=2.0)

        with session_scope(migrated_factory) as session:
            frames = session.execute(
                Frame.__table__.select().where(Frame.project_id == project_id)
            ).all()
        assert len(frames) == 1

        await supervisor.stop()

    async def test_capture_once_skips_move_to_when_no_ptz(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """_capture_once does not call move_to when the target has no PTZ fields."""
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        cam_id, project_id = _seed_camera_and_project(migrated_factory)

        adapter = _make_ptz_adapter()
        target = CaptureTarget(
            project_id=project_id,
            project_name="ptz-sup-project",
            camera_id=cam_id,
            interval_seconds=60,
            # No PTZ fields.
        )
        state = CaptureState(project_id=project_id, camera_id=cam_id)

        fake_config = MagicMock()
        with (
            patch.object(supervisor, "_load_camera", return_value=fake_config),
            patch.object(supervisor, "_load_default_credentials", return_value=None),
            patch(
                "timelapse_manager.capture.supervisor.build_adapter",
                return_value=adapter,
            ),
        ):
            await supervisor._capture_once(target, state)

        # move_to should never have been called.
        adapter.move_to.assert_not_called()

        with session_scope(migrated_factory) as session:
            frames = session.execute(
                Frame.__table__.select().where(Frame.project_id == project_id)
            ).all()
        assert len(frames) == 1

        await supervisor.stop()


# ---------------------------------------------------------------------------
# F3 — PTZ fail-closed: PTZError from move_to → no frame written
# ---------------------------------------------------------------------------


class TestPtzSupervisorFailClosed:
    async def test_ptz_error_propagates_and_no_frame_written(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """When move_to raises PTZError, _capture_once re-raises and writes no frame.

        This is the fail-closed contract: a positioning failure must prevent
        the subsequent capture() call so the frame is never persisted from the
        wrong position.
        """
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        cam_id, project_id = _seed_camera_and_project(migrated_factory)

        # move_to raises PTZError — camera could not move to the preset.
        ptz_err = PTZError("cannot move to preset: camera busy")
        adapter = _make_ptz_adapter(move_to_side_effect=ptz_err)

        target = CaptureTarget(
            project_id=project_id,
            project_name="ptz-sup-project",
            camera_id=cam_id,
            interval_seconds=60,
            ptz_preset="home",
        )
        state = CaptureState(project_id=project_id, camera_id=cam_id)

        fake_config = MagicMock()
        with (
            patch.object(supervisor, "_load_camera", return_value=fake_config),
            patch.object(supervisor, "_load_default_credentials", return_value=None),
            patch(
                "timelapse_manager.capture.supervisor.build_adapter",
                return_value=adapter,
            ),
            pytest.raises(PTZError),
        ):
            await supervisor._capture_once(target, state)

        # move_to was called (the attempt was made).
        adapter.move_to.assert_awaited_once_with(preset_id="home")

        # No Frame row written — capture() was never reached.
        with session_scope(migrated_factory) as session:
            frame_count = session.execute(
                Frame.__table__.select().where(Frame.project_id == project_id)
            ).all()
        assert len(frame_count) == 0

        await supervisor.stop()

    async def test_capture_error_subclass_from_move_to_also_fails_closed(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """Any CaptureError subclass from move_to fails closed (no frame written).

        PTZError extends CaptureError; the supervisor re-raises the same type.
        Verify the pattern holds for a plain CaptureError from move_to as well.
        """
        from timelapse_manager.cameras.base import OtherCaptureError

        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        cam_id, project_id = _seed_camera_and_project(migrated_factory)

        capture_err = OtherCaptureError("network timeout during move")
        adapter = _make_ptz_adapter(move_to_side_effect=capture_err)

        target = CaptureTarget(
            project_id=project_id,
            project_name="ptz-sup-project",
            camera_id=cam_id,
            interval_seconds=60,
            ptz_pan=0.0,
            ptz_tilt=0.0,
            ptz_zoom=1.0,
        )
        state = CaptureState(project_id=project_id, camera_id=cam_id)

        fake_config = MagicMock()
        with (
            patch.object(supervisor, "_load_camera", return_value=fake_config),
            patch.object(supervisor, "_load_default_credentials", return_value=None),
            patch(
                "timelapse_manager.capture.supervisor.build_adapter",
                return_value=adapter,
            ),
            pytest.raises(CaptureError),
        ):
            await supervisor._capture_once(target, state)

        with session_scope(migrated_factory) as session:
            frames = session.execute(
                Frame.__table__.select().where(Frame.project_id == project_id)
            ).all()
        assert len(frames) == 0

        await supervisor.stop()
