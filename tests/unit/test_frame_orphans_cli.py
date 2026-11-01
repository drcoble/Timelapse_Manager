"""``frames find-orphans``: a read-only report of row-less frame files (#67 R11)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
import yaml

from timelapse_manager.cli import main
from timelapse_manager.db.engine import create_db_engine
from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import create_session_factory, session_scope

_OLD = time.time() - 3600


def _run(*args: str) -> int:
    with pytest.raises(SystemExit) as excinfo:
        main(list(args))
    code = excinfo.value.code
    return int(code) if code is not None else 0


@pytest.fixture()
def state(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """A migrated database and config; yields (config, factory, frames_root)."""
    root = tmp_path / "state"
    root.mkdir()
    config = tmp_path / "config.yml"
    config.write_text(
        yaml.dump(
            {
                "paths": {"data_dir": str(root), "frames_root": str(root / "frames")},
                "database": {"url": f"sqlite:///{root / 'timelapse.db'}"},
            }
        ),
        encoding="utf-8",
    )
    assert _run("--config", str(config), "migrate") == 0
    capsys.readouterr()
    engine = create_db_engine(f"sqlite:///{root / 'timelapse.db'}")
    yield config, create_session_factory(engine), root / "frames"
    engine.dispose()


def _project(factory, name: str, storage_path: Path | None = None) -> int:
    with session_scope(factory) as session:
        camera = Camera(name=f"{name}-cam", address="127.0.0.1", protocol="vapix")
        session.add(camera)
        session.flush()
        project = Project(
            camera_id=camera.id,
            name=name,
            lifecycle_state="active",
            operational_status="idle",
            storage_path=str(storage_path) if storage_path else None,
        )
        session.add(project)
        session.flush()
        return project.id


def _frame(
    factory,
    project_id: int,
    file_path: str,
    *,
    seq: int,
    state: str = "active",
) -> None:
    with session_scope(factory) as session:
        session.add(
            Frame(
                project_id=project_id,
                sequence_index=seq,
                file_path=file_path,
                width=1,
                height=1,
                file_size_bytes=4,
                capture_status="captured",
                origin="captured",
                lifecycle_state=state,
            )
        )


def _file(path: Path, *, age_seconds: float = 3600, data: bytes = b"1234") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def _report(capsys: pytest.CaptureFixture[str], config: Path, *extra: str) -> list[str]:
    assert _run("--config", str(config), "frames", "find-orphans", *extra) == 0
    return capsys.readouterr().out.splitlines()


class TestFindOrphans:
    def test_lists_only_files_without_a_row(
        self, state, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config, factory, frames = state
        pid = _project(factory, "p1")
        kept = _file(frames / str(pid) / "kept.jpg")
        deleted = _file(frames / str(pid) / "deleted.jpg")
        stray = _file(frames / str(pid) / "stray.jpg", data=b"123456")
        leftover = _file(frames / str(pid) / "tmpabc123.tmp")
        _frame(factory, pid, kept.name, seq=1)
        _frame(factory, pid, deleted.name, seq=2, state="deleted")

        lines = _report(capsys, config)

        assert f"frame project={pid} size=6 {stray}" in lines
        assert f"temp project={pid} size=4 {leftover}" in lines
        assert not any(str(kept) in line or str(deleted) in line for line in lines)
        assert lines[-1] == "2 orphan file(s), 10 bytes; nothing was deleted"

    def test_deletes_nothing(self, state, capsys: pytest.CaptureFixture[str]) -> None:
        config, factory, frames = state
        pid = _project(factory, "p1")
        stray = _file(frames / str(pid) / "stray.jpg")
        _report(capsys, config)
        assert stray.exists()

    def test_recent_files_are_skipped_until_min_age_is_lowered(
        self, state, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config, factory, frames = state
        pid = _project(factory, "p1")
        fresh = _file(frames / str(pid) / "fresh.jpg", age_seconds=0)
        assert _report(capsys, config)[-1].startswith("0 orphan")
        lines = _report(capsys, config, "--min-age", "0")
        assert any(str(fresh) in line for line in lines)

    def test_project_filter(self, state, capsys: pytest.CaptureFixture[str]) -> None:
        config, factory, frames = state
        first = _project(factory, "p1")
        second = _project(factory, "p2")
        _file(frames / str(first) / "a.jpg")
        other = _file(frames / str(second) / "b.jpg")
        lines = _report(capsys, config, "--project", str(second))
        assert [line for line in lines if "project=" in line] == [
            f"frame project={second} size=4 {other}"
        ]

    def test_explicit_storage_path_with_absolute_rows(
        self, state, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        config, factory, _ = state
        directory = tmp_path / "elsewhere"
        pid = _project(factory, "p1", storage_path=directory)
        kept = _file(directory / "kept.jpg")
        stray = _file(directory / "stray.jpg")
        _frame(factory, pid, str(kept), seq=1)
        lines = _report(capsys, config)
        assert f"frame project={pid} size=4 {stray}" in lines
        assert not any(str(kept) in line for line in lines)

    def test_projects_sharing_a_directory_do_not_orphan_each_other(
        self, state, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        config, factory, _ = state
        directory = tmp_path / "shared"
        first = _project(factory, "p1", storage_path=directory)
        second = _project(factory, "p2", storage_path=directory)
        mine = _file(directory / "a.jpg")
        theirs = _file(directory / "b.jpg")
        _frame(factory, first, str(mine), seq=1)
        _frame(factory, second, str(theirs), seq=1)
        lines = _report(capsys, config, "--project", str(first))
        assert lines == ["0 orphan file(s), 0 bytes; nothing was deleted"]

    def test_ignores_subdirectories_symlinks_and_missing_directories(
        self, state, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        config, factory, frames = state
        pid = _project(factory, "p1")
        _project(factory, "p2")  # no directory on disk at all
        _file(frames / str(pid) / "nested" / "x.jpg")
        target = _file(tmp_path / "outside.jpg")
        link = frames / str(pid) / "link.jpg"
        link.symlink_to(target)
        lines = _report(capsys, config)
        assert lines == ["0 orphan file(s), 0 bytes; nothing was deleted"]

    def test_missing_database_is_an_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config = tmp_path / "config.yml"
        config.write_text(
            yaml.dump(
                {
                    "paths": {"data_dir": str(tmp_path)},
                    "database": {"url": f"sqlite:///{tmp_path / 'none.db'}"},
                }
            ),
            encoding="utf-8",
        )
        assert _run("--config", str(config), "frames", "find-orphans") == 1
        assert "does not exist" in capsys.readouterr().err

    def test_negative_min_age_is_an_error(
        self, state, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config, _, _ = state
        code = _run(
            "--config", str(config), "frames", "find-orphans", "--min-age", "-1"
        )
        assert code == 1
