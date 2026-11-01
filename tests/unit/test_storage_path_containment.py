"""Frame-path containment in the render gather and the startup storage_path scan.

#67 S16: ``gather_frames`` drops a frame whose path escapes the project tree.
#67 S5: the startup scan warns about stored paths the policy refuses, and never
changes a row or stops startup.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from timelapse_manager.config.settings import (
    CaptureSettings,
    DatabaseSettings,
    LoggingSettings,
    PathsSettings,
    Settings,
)
from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.encode.frame_source import gather_frames
from timelapse_manager.services.projects import warn_on_storage_path_policy

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture()
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.delenv("TLM_CONFIG", raising=False)
    data_dir = tmp_path / "data"
    (data_dir / "frames").mkdir(parents=True)
    return Settings(
        database=DatabaseSettings(url=f"sqlite:///{data_dir / 'tl.db'}"),
        logging=LoggingSettings(level="WARNING", format="text"),
        paths=PathsSettings(data_dir=data_dir),
        capture=CaptureSettings(autostart=False),
    )


def _seed(
    factory: sessionmaker,  # type: ignore[type-arg]
    *,
    name: str,
    storage_path: str | None = None,
    file_paths: tuple[str, ...] = (),
) -> int:
    with session_scope(factory) as session:
        cam = Camera(name=f"{name}-cam", address="127.0.0.1", protocol="vapix")
        session.add(cam)
        session.flush()
        proj = Project(
            camera_id=cam.id,
            name=name,
            lifecycle_state="active",
            storage_path=storage_path,
        )
        session.add(proj)
        session.flush()
        for i, file_path in enumerate(file_paths):
            session.add(
                Frame(
                    project_id=proj.id,
                    sequence_index=i,
                    capture_timestamp=(_T0 + timedelta(minutes=i)).replace(tzinfo=None),
                    file_path=file_path,
                    capture_status="captured",
                    origin="captured",
                    lifecycle_state="active",
                )
            )
        return proj.id


class TestGatherFramesContainment:
    def test_symlink_and_traversal_frames_are_skipped(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings: Settings,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = tmp_path / "secret.jpg"
        secret.write_bytes(b"s")
        pid = _seed(
            migrated_factory,
            name="g1",
            file_paths=("ok.jpg", "link.jpg", "../../secret.jpg", str(secret)),
        )
        frame_dir = Path(settings.paths.frames_root or "") / str(pid)
        frame_dir.mkdir(parents=True)
        (frame_dir / "ok.jpg").write_bytes(b"x")
        (frame_dir / "link.jpg").symlink_to(secret)

        with (
            caplog.at_level(logging.WARNING, "timelapse_manager.encode.frame_source"),
            session_scope(migrated_factory) as session,
        ):
            sequence = gather_frames(session, settings, pid)

        assert [ref.sequence_index for ref in sequence.frames] == [0]
        assert sequence.frames[0].absolute_path == Path(
            os.path.realpath(frame_dir / "ok.jpg")
        )
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1  # one aggregated line, not one per frame
        assert "skipped 3 frame(s)" in warnings[0].getMessage()

    def test_custom_storage_and_pre_change_relative_frames_render(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings: Settings,
        tmp_path: Path,
    ) -> None:
        custom = tmp_path / "custom"
        custom.mkdir()
        (custom / "new.jpg").write_bytes(b"x")
        pid = _seed(
            migrated_factory,
            name="g2",
            storage_path=str(custom),
            file_paths=("old.jpg", str(custom / "new.jpg")),
        )
        default_dir = Path(settings.paths.frames_root or "") / str(pid)
        default_dir.mkdir(parents=True)
        (default_dir / "old.jpg").write_bytes(b"x")

        with session_scope(migrated_factory) as session:
            sequence = gather_frames(session, settings, pid)

        assert [ref.sequence_index for ref in sequence.frames] == [0, 1]


class TestStartupStoragePathScan:
    def test_warns_per_offending_project_and_never_edits_rows(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings: Settings,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        data_dir = Path(settings.paths.data_dir)
        bad = f"{data_dir}/legacy\nforged log line"
        inside = str(data_dir / "elsewhere")
        fine = str(Path(settings.paths.frames_root or "") / "custom")
        admin_only = str(tmp_path / "srv" / "frames")
        ids = [
            _seed(migrated_factory, name="s-bad", storage_path=bad),
            _seed(migrated_factory, name="s-inside", storage_path=inside),
            _seed(migrated_factory, name="s-fine", storage_path=fine),
            _seed(migrated_factory, name="s-admin", storage_path=admin_only),
            _seed(migrated_factory, name="s-default"),
        ]

        with (
            caplog.at_level(logging.WARNING, "timelapse_manager.services.projects"),
            session_scope(migrated_factory) as session,
        ):
            warned = warn_on_storage_path_policy(session, settings)

        # Admin rules: a path outside allowed_roots is not flagged.
        assert warned == 2
        messages = [r.getMessage() for r in caplog.records]
        assert len(messages) == 2
        assert all("storage_path policy" in m for m in messages)
        # The stored value is logged with repr, so a newline cannot forge a line.
        assert all("\n" not in m for m in messages)
        assert any("legacy\\nforged" in m for m in messages)
        with session_scope(migrated_factory) as session:
            stored = [session.get(Project, pid).storage_path for pid in ids]  # type: ignore[union-attr]
        assert stored == [bad, inside, fine, admin_only, None]

    def test_check_never_blocks_startup(
        self, settings: Settings, caplog: pytest.LogCaptureFixture
    ) -> None:
        from timelapse_manager.service import check_storage_paths

        broken = settings.model_copy(
            update={"database": DatabaseSettings(url="sqlite:////nonexistent/dir/x.db")}
        )
        with caplog.at_level(logging.WARNING, "timelapse_manager.service"):
            check_storage_paths(broken)  # must not raise
        assert "startup check skipped" in caplog.text

    def test_serve_runs_the_check_after_migrating(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import timelapse_manager.service as service
        from timelapse_manager.db import migrate

        calls: list[str] = []
        monkeypatch.setattr(
            migrate, "apply_migrations", lambda s: calls.append("migrate")
        )
        monkeypatch.setattr(
            service, "check_storage_paths", lambda s: calls.append("check")
        )

        def fake_run(coro: object) -> None:
            calls.append("serve")
            coro.close()  # type: ignore[attr-defined]

        monkeypatch.setattr(service.asyncio, "run", fake_run)
        service.serve(settings)
        assert calls == ["migrate", "check", "serve"]
