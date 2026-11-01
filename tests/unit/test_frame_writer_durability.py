"""Frame writer durability (#67 R11): no row-less file after a failed commit.

The writer puts the file on disk first, then commits the row. A failure between
the two must not leave the file behind, and the file and its directory entry are
``fsync``-ed before the row can commit.
"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from tests.unit.test_frame_writer import _FAKE_JPEG, _seed_camera_and_project
from timelapse_manager.cameras.base import CapturedFrame
from timelapse_manager.capture import frame_writer as frame_writer_module
from timelapse_manager.capture.frame_writer import FrameWriter
from timelapse_manager.db.models import Frame, Project
from timelapse_manager.db.session import session_scope


def _captured() -> CapturedFrame:
    return CapturedFrame(
        image_bytes=_FAKE_JPEG,
        width=1,
        height=1,
        format="jpeg",
        captured_at=datetime.now(UTC),
    )


def _files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.iterdir() if p.is_file())


class TestFailedCommitRemovesTheFile:
    def test_commit_failure_leaves_no_file_and_no_row(
        self, migrated_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = tmp_path / "frames"
        _, project_id = _seed_camera_and_project(
            migrated_factory, tmp_path, storage_path=storage
        )
        writer = FrameWriter(migrated_factory, tmp_path)

        def broken_commit(self: Session) -> None:
            raise OSError("disk I/O error")

        monkeypatch.setattr(Session, "commit", broken_commit)
        with pytest.raises(OSError, match="disk I/O error"):
            writer.write(project_id, _captured())
        monkeypatch.undo()

        assert _files(storage) == []
        with session_scope(migrated_factory) as session:
            assert session.query(Frame).count() == 0
            project = session.get(Project, project_id)
            assert project is not None and project.frame_count == 0

    def test_flush_failure_leaves_no_file(
        self, migrated_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = tmp_path / "frames"
        _, project_id = _seed_camera_and_project(
            migrated_factory, tmp_path, storage_path=storage
        )
        writer = FrameWriter(migrated_factory, tmp_path)

        real_flush = Session.flush

        def broken_flush(self: Session, *args: object, **kwargs: object) -> None:
            # Autoflush runs on the project read, before any file exists; fail
            # only once the frame file is on disk and the row is being inserted.
            if storage.is_dir() and _files(storage):
                raise RuntimeError("flush failed")
            real_flush(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Session, "flush", broken_flush)
        with pytest.raises(RuntimeError, match="flush failed"):
            writer.write(project_id, _captured())
        monkeypatch.undo()

        assert _files(storage) == []

    def test_a_missing_project_writes_no_file(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        writer = FrameWriter(migrated_factory, tmp_path / "root")
        with pytest.raises(ValueError):
            writer.write(999, _captured())
        assert not (tmp_path / "root").exists() or not list(
            (tmp_path / "root").rglob("*.jpg")
        )

    def test_a_committed_frame_keeps_its_file(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        storage = tmp_path / "frames"
        _, project_id = _seed_camera_and_project(
            migrated_factory, tmp_path, storage_path=storage
        )
        written = FrameWriter(migrated_factory, tmp_path).write(project_id, _captured())
        assert Path(written.file_path).read_bytes() == _FAKE_JPEG
        with session_scope(migrated_factory) as session:
            assert session.query(Frame).count() == 1

    def test_unlink_failure_does_not_mask_the_original_error(
        self, migrated_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = tmp_path / "frames"
        _, project_id = _seed_camera_and_project(
            migrated_factory, tmp_path, storage_path=storage
        )

        def broken_commit(self: Session) -> None:
            raise OSError("disk I/O error")

        def broken_unlink(path: object) -> None:
            raise PermissionError("read-only")

        monkeypatch.setattr(Session, "commit", broken_commit)
        monkeypatch.setattr(frame_writer_module.os, "unlink", broken_unlink)
        with pytest.raises(OSError, match="disk I/O error"):
            FrameWriter(migrated_factory, tmp_path).write(project_id, _captured())


class TestDirectoryFsync:
    def test_file_then_directory_are_synced_before_the_row_commits(
        self, migrated_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = tmp_path / "frames"
        _, project_id = _seed_camera_and_project(
            migrated_factory, tmp_path, storage_path=storage
        )
        events: list[str] = []
        real_fsync = os.fsync
        real_replace = os.replace
        real_commit = Session.commit

        def spy_fsync(fd: int) -> None:
            kind = "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
            events.append(f"fsync-{kind}")
            real_fsync(fd)

        def spy_replace(src: object, dst: object) -> None:
            events.append("replace")
            real_replace(src, dst)  # type: ignore[arg-type]

        def spy_commit(self: Session) -> None:
            events.append("commit")
            real_commit(self)

        monkeypatch.setattr(os, "fsync", spy_fsync)
        monkeypatch.setattr(os, "replace", spy_replace)
        monkeypatch.setattr(Session, "commit", spy_commit)

        FrameWriter(migrated_factory, tmp_path).write(project_id, _captured())

        assert events.index("fsync-file") < events.index("replace")
        assert events.index("replace") < events.index("fsync-dir")
        assert events.index("fsync-dir") < events.index("commit")

    def test_a_directory_fsync_failure_does_not_fail_the_write(
        self, migrated_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = tmp_path / "frames"
        _, project_id = _seed_camera_and_project(
            migrated_factory, tmp_path, storage_path=storage
        )
        real_fsync = os.fsync

        def fsync_files_only(fd: int) -> None:
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("directory fsync unsupported")
            real_fsync(fd)

        monkeypatch.setattr(os, "fsync", fsync_files_only)
        written = FrameWriter(migrated_factory, tmp_path).write(project_id, _captured())
        assert Path(written.file_path).exists()
