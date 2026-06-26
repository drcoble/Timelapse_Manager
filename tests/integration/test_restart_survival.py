"""Integration tests for restart-survival and resume behavior.

Verifies:
- On resume, next frame's sequence_index == max(existing) + 1.
- No overwrite of existing frame files (bytes and mtime unchanged).
- No backfill of the gap (frame_count == pre + post, indices contiguous-forward).
- An informational gap Event is written when the gap exceeds 2x interval.
- Archived projects are excluded from _load_targets.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.conftest import FakeAdapter
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
from timelapse_manager.db.models import Camera, Event, Frame, Project
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


def _seed_project_with_frames(
    migrated_factory,
    tmp_path: Path,
    *,
    frame_count: int,
    interval_seconds: int = 60,
    name: str = "restart-proj",
    lifecycle: str = "active",
) -> dict:
    """Seed a project with `frame_count` pre-existing Frame rows and real files."""
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
            capture_interval_seconds=interval_seconds,
            lifecycle_state=lifecycle,
            operational_status="idle",
            storage_path=str(storage),
            frame_count=frame_count,
        )
        session.add(proj)
        session.flush()
        project_id = proj.id

        # Create real files and Frame rows for each pre-existing frame
        file_paths = []
        for i in range(1, frame_count + 1):
            fname = f"{i:08d}.jpg"
            fpath = storage / fname
            content = f"frame-{i}".encode()
            fpath.write_bytes(content)
            capture_ts = datetime(2026, 1, 1, 0, i, tzinfo=_UTC).replace(tzinfo=None)
            fr = Frame(
                project_id=project_id,
                sequence_index=i,
                capture_timestamp=capture_ts,
                file_path=str(fpath),
                width=1,
                height=1,
                file_size_bytes=len(content),
                capture_status="captured",
                origin="captured",
                lifecycle_state="active",
            )
            session.add(fr)
            file_paths.append(fpath)

    return {
        "camera_id": cam_id,
        "project_id": project_id,
        "storage_path": storage,
        "file_paths": file_paths,
    }


# ---------------------------------------------------------------------------
# No overwrite, no backfill
# ---------------------------------------------------------------------------


class TestRestartSurvival:
    async def test_resume_continues_from_max_plus_1(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        pre_count = 3
        ctx = _seed_project_with_frames(
            migrated_factory, tmp_path, frame_count=pre_count
        )
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )

        target = CaptureTarget(
            project_id=ctx["project_id"],
            project_name="restart-proj",
            camera_id=ctx["camera_id"],
            interval_seconds=60,
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

        # New frame must be at pre_count + 1
        with session_scope(migrated_factory) as session:
            new_frame = (
                session.query(Frame)
                .filter(Frame.project_id == ctx["project_id"])
                .order_by(Frame.sequence_index.desc())
                .first()
            )
        assert new_frame is not None
        assert new_frame.sequence_index == pre_count + 1
        await supervisor.stop()

    async def test_no_backfill_of_gap(self, migrated_factory, tmp_path: Path) -> None:
        """Frame count == pre + post with no gap-filling rows."""
        pre_count = 5
        ctx = _seed_project_with_frames(
            migrated_factory, tmp_path, frame_count=pre_count, name="restart-proj-b"
        )
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )

        target = CaptureTarget(
            project_id=ctx["project_id"],
            project_name="restart-proj-b",
            camera_id=ctx["camera_id"],
            interval_seconds=60,
        )
        state = CaptureState(project_id=ctx["project_id"], camera_id=ctx["camera_id"])

        post_captures = 2
        fake_config = MagicMock()
        for _ in range(post_captures):
            with (
                patch.object(supervisor, "_load_camera", return_value=fake_config),
                patch(
                    "timelapse_manager.capture.supervisor.build_adapter",
                    return_value=FakeAdapter(),
                ),
            ):
                await supervisor._capture_once(target, state)

        # Total rows must be exactly pre + post, no gap rows
        with session_scope(migrated_factory) as session:
            total = (
                session.query(Frame)
                .filter(Frame.project_id == ctx["project_id"])
                .count()
            )
            indices = [
                r[0]
                for r in session.query(Frame.sequence_index)
                .filter(Frame.project_id == ctx["project_id"])
                .order_by(Frame.sequence_index)
                .all()
            ]
        assert total == pre_count + post_captures
        # Indices 1..pre_count then (pre_count+1)..(pre_count+post_captures)
        expected = list(range(1, pre_count + post_captures + 1))
        assert indices == expected
        await supervisor.stop()

    async def test_existing_files_bytes_unchanged_after_resume(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        pre_count = 3
        ctx = _seed_project_with_frames(
            migrated_factory, tmp_path, frame_count=pre_count, name="restart-proj-c"
        )
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )

        # Snapshot existing file content and mtime
        snapshots = {}
        for fpath in ctx["file_paths"]:
            snapshots[fpath] = {
                "content": fpath.read_bytes(),
                "mtime": os.path.getmtime(fpath),
            }

        target = CaptureTarget(
            project_id=ctx["project_id"],
            project_name="restart-proj-c",
            camera_id=ctx["camera_id"],
            interval_seconds=60,
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

        # Verify every pre-existing file is unchanged
        for fpath, snap in snapshots.items():
            assert fpath.exists(), f"{fpath} should still exist"
            assert fpath.read_bytes() == snap["content"], f"{fpath} bytes changed"
            assert os.path.getmtime(fpath) == snap["mtime"], f"{fpath} mtime changed"
        await supervisor.stop()

    async def test_sequence_indices_contiguous_forward(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        pre_count = 4
        ctx = _seed_project_with_frames(
            migrated_factory, tmp_path, frame_count=pre_count, name="restart-proj-d"
        )
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )

        target = CaptureTarget(
            project_id=ctx["project_id"],
            project_name="restart-proj-d",
            camera_id=ctx["camera_id"],
            interval_seconds=60,
        )
        state = CaptureState(project_id=ctx["project_id"], camera_id=ctx["camera_id"])

        post = 3
        fake_config = MagicMock()
        for _ in range(post):
            with (
                patch.object(supervisor, "_load_camera", return_value=fake_config),
                patch(
                    "timelapse_manager.capture.supervisor.build_adapter",
                    return_value=FakeAdapter(),
                ),
            ):
                await supervisor._capture_once(target, state)

        with session_scope(migrated_factory) as session:
            indices = sorted(
                r[0]
                for r in session.query(Frame.sequence_index)
                .filter(Frame.project_id == ctx["project_id"])
                .all()
            )
        assert indices == list(range(1, pre_count + post + 1))
        await supervisor.stop()


# ---------------------------------------------------------------------------
# Gap event logging
# ---------------------------------------------------------------------------


class TestGapEventLogging:
    async def test_large_gap_writes_info_event(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """A gap > 2x interval triggers an info event on resume."""
        interval = 60
        ctx = _seed_project_with_frames(
            migrated_factory,
            tmp_path,
            frame_count=2,
            interval_seconds=interval,
            name="gap-proj",
        )
        settings = _make_settings(tmp_path)

        # Make the clock return "now" much later than the last frame timestamp
        # The last frame is at 2026-01-01T00:02 UTC; gap threshold = 2 * 60 = 120s
        # Make now = last_frame_time + 600s (well over threshold)
        class _FakeClock:
            def now(self) -> datetime:
                return datetime(2026, 1, 1, 0, 12, tzinfo=_UTC)  # 10 min later

            async def sleep(self, seconds: float) -> None:
                pass

        supervisor = CaptureSupervisor(
            settings,
            migrated_factory,
            clock=_FakeClock(),
            disk_monitor=_permissive_disk_monitor(),
        )
        target = CaptureTarget(
            project_id=ctx["project_id"],
            project_name="gap-proj",
            camera_id=ctx["camera_id"],
            interval_seconds=interval,
        )

        # Call the synchronous method directly (it's called via to_thread in production)
        supervisor._log_resume_gap(target)

        with session_scope(migrated_factory) as session:
            events = (
                session.query(Event)
                .filter(Event.scope == "project")
                .filter(Event.scope_id == ctx["project_id"])
                .filter(Event.level == "info")
                .all()
            )
        assert len(events) == 1
        msg = events[0].message.lower()
        assert "downtime" in msg or "gap" in msg
        await supervisor.stop()

    async def test_small_gap_no_event(self, migrated_factory, tmp_path: Path) -> None:
        interval = 60
        ctx = _seed_project_with_frames(
            migrated_factory,
            tmp_path,
            frame_count=2,
            interval_seconds=interval,
            name="small-gap-proj",
        )
        settings = _make_settings(tmp_path)

        class _FakeClock:
            def now(self) -> datetime:
                # Only 1 second after last frame = within 2x interval threshold
                return datetime(2026, 1, 1, 0, 2, 1, tzinfo=_UTC)

            async def sleep(self, seconds: float) -> None:
                pass

        supervisor = CaptureSupervisor(
            settings,
            migrated_factory,
            clock=_FakeClock(),
            disk_monitor=_permissive_disk_monitor(),
        )
        target = CaptureTarget(
            project_id=ctx["project_id"],
            project_name="small-gap-proj",
            camera_id=ctx["camera_id"],
            interval_seconds=interval,
        )

        supervisor._log_resume_gap(target)

        with session_scope(migrated_factory) as session:
            event_count = (
                session.query(Event)
                .filter(Event.scope == "project")
                .filter(Event.scope_id == ctx["project_id"])
                .count()
            )
        assert event_count == 0
        await supervisor.stop()


# ---------------------------------------------------------------------------
# Archived project excluded
# ---------------------------------------------------------------------------


class TestArchivedProjectExcluded:
    def test_archived_project_gets_no_task(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        _seed_project_with_frames(
            migrated_factory,
            tmp_path,
            frame_count=1,
            name="archived-proj",
            lifecycle="archived",
        )
        settings = _make_settings(tmp_path)
        supervisor = CaptureSupervisor(
            settings, migrated_factory, disk_monitor=_permissive_disk_monitor()
        )
        targets = supervisor._load_targets()
        archived_ids = [t.project_id for t in targets]
        # No archived project should appear
        with session_scope(migrated_factory) as session:
            archived = (
                session.query(Project)
                .filter(Project.lifecycle_state == "archived")
                .all()
            )
        for a in archived:
            assert a.id not in archived_ids
