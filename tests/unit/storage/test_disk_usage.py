"""Unit tests for per-project disk-usage accounting (project status field)."""

from __future__ import annotations

from datetime import UTC, datetime

from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.storage.frames import sum_project_disk_usage


def _seed_camera_and_project(session) -> int:
    cam = Camera(
        name="du-cam",
        address="127.0.0.1",
        protocol="vapix",
        snapshot_uri="http://127.0.0.1/snap",
    )
    session.add(cam)
    session.flush()
    proj = Project(
        camera_id=cam.id,
        name="du-proj",
        capture_interval_seconds=60,
        lifecycle_state="active",
        operational_status="idle",
        frame_count=0,
    )
    session.add(proj)
    session.flush()
    return proj.id


def _add_frame(
    session, project_id: int, seq: int, size: int | None, state: str
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


class TestSumProjectDiskUsage:
    def test_sums_active_frame_sizes(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as session:
            pid = _seed_camera_and_project(session)
            _add_frame(session, pid, 1, 100, "active")
            _add_frame(session, pid, 2, 250, "active")
        with session_scope(migrated_factory) as session:
            assert sum_project_disk_usage(session, pid) == 350

    def test_excludes_soft_deleted_and_null_sizes(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as session:
            pid = _seed_camera_and_project(session)
            _add_frame(session, pid, 1, 100, "active")
            _add_frame(session, pid, 2, 999, "soft_deleted")  # excluded
            _add_frame(session, pid, 3, None, "active")  # contributes 0
        with session_scope(migrated_factory) as session:
            assert sum_project_disk_usage(session, pid) == 100

    def test_zero_when_no_frames(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as session:
            pid = _seed_camera_and_project(session)
        with session_scope(migrated_factory) as session:
            assert sum_project_disk_usage(session, pid) == 0
