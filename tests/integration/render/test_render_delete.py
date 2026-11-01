"""Deleting renders and frame exports (issue #9), and the frame window they record.

Covers:

* the services ``delete_render`` / ``delete_export``: row and file removed, a
  path outside the render root never deleted, a missing file tolerated, the
  audit event written, roles enforced, a running job refused, kinds kept apart;
* :func:`timelapse_manager.api.renders.delete_job`, the cancel-then-delete the
  API and web routes share, against a real :class:`RenderQueue`: a render that
  is encoding is stopped (no encoder running, no partial file) before its row
  is deleted, and an export whose zip thread is running is waited out;
* the frame window (count, first/last capture time) and chapters the worker
  records on the job, for renders and exports.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from timelapse_manager.api.renders import delete_job
from timelapse_manager.config.settings import Settings
from timelapse_manager.db.engine import create_db_engine
from timelapse_manager.db.models import Camera, Event, Frame, Project, RenderJob, User
from timelapse_manager.db.session import session_scope
from timelapse_manager.encode.encoder import (
    Encoder,
    OutputSettings,
    RenderResult,
    RenderSpec,
)
from timelapse_manager.render import export as export_mod
from timelapse_manager.render.export import build_export_zip
from timelapse_manager.render.queue import RenderQueue
from timelapse_manager.render.spec import project_render_root
from timelapse_manager.runtime import AppContext, set_context
from timelapse_manager.services import renders as render_service
from timelapse_manager.services._base import Actor
from timelapse_manager.services.errors import (
    ConflictError,
    NotFoundError,
    PermissionError_,
)

_BASE_TS = datetime(2026, 3, 1, 6, 0)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_project(
    factory: sessionmaker,  # type: ignore[type-arg]
    settings: Settings,
    *,
    frames: int = 3,
) -> int:
    """Insert a camera, a project and ``frames`` captured frames with files."""
    frames_root = settings.paths.frames_root
    assert frames_root is not None
    with session_scope(factory) as session:
        cam = Camera(name="del-cam", address="127.0.0.1", protocol="vapix")
        session.add(cam)
        session.flush()
        proj = Project(
            camera_id=cam.id,
            name="del-project",
            lifecycle_state="active",
            operational_status="idle",
        )
        session.add(proj)
        session.flush()
        frame_dir = frames_root / str(proj.id)
        frame_dir.mkdir(parents=True, exist_ok=True)
        for i in range(frames):
            name = f"frame_{i:04d}.jpg"
            (frame_dir / name).write_bytes(b"\xff\xd8stub\xff\xd9")
            session.add(
                Frame(
                    project_id=proj.id,
                    sequence_index=i,
                    capture_timestamp=_BASE_TS + timedelta(hours=i),
                    file_path=name,
                    capture_status="captured",
                    lifecycle_state="active",
                    width=64,
                    height=48,
                )
            )
        proj.frame_count = frames
        return proj.id


def _seed_user(
    factory: sessionmaker,  # type: ignore[type-arg]
    *,
    username: str = "op1",
    role: str = "operator",
) -> int:
    from timelapse_manager.usernames import username_key

    with session_scope(factory) as session:
        user = User(
            username=username,
            username_key=username_key(username),
            role=role,
            auth_source="local",
            enabled=True,
        )
        session.add(user)
        session.flush()
        return user.id


def _render_root(
    factory: sessionmaker,  # type: ignore[type-arg]
    settings: Settings,
    project_id: int,
) -> Path:
    with session_scope(factory) as session:
        project = session.get(Project, project_id)
        assert project is not None
        root = project_render_root(settings, project)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _seed_job(
    factory: sessionmaker,  # type: ignore[type-arg]
    project_id: int,
    *,
    kind: str = "manual",
    status: str = "done",
    output: Path | None = None,
) -> int:
    with session_scope(factory) as session:
        job = RenderJob(
            project_id=project_id,
            kind=kind,
            status=status,
            output_settings={"fps": 1.0, "codec": "h264", "container": "mp4"},
            output_file_path=str(output) if output is not None else None,
        )
        session.add(job)
        session.flush()
        return job.id


def _job_exists(factory: sessionmaker, job_id: int) -> bool:  # type: ignore[type-arg]
    with session_scope(factory) as session:
        return session.get(RenderJob, job_id) is not None


def _messages(factory: sessionmaker) -> list[str]:  # type: ignore[type-arg]
    with session_scope(factory) as session:
        return list(session.execute(select(Event.message)).scalars().all())


def _levels(factory: sessionmaker) -> set[str]:  # type: ignore[type-arg]
    with session_scope(factory) as session:
        return set(session.execute(select(Event.level)).scalars().all())


def _delete(
    factory: sessionmaker,  # type: ignore[type-arg]
    settings: Settings,
    actor: Actor,
    job_id: int,
    *,
    export: bool = False,
) -> render_service.DeletedJob:
    fn = render_service.delete_export if export else render_service.delete_render
    with session_scope(factory) as session:
        return fn(session, actor, job_id, settings=settings).value


# ---------------------------------------------------------------------------
# Service: delete_render / delete_export
# ---------------------------------------------------------------------------


class TestDeleteService:
    def test_deletes_row_and_file_and_audits(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
    ) -> None:
        pid = _seed_project(migrated_factory, settings_no_autostart)
        uid = _seed_user(migrated_factory)
        out = _render_root(migrated_factory, settings_no_autostart, pid) / "r.mp4"
        out.write_bytes(b"\x00" * 1234)
        job_id = _seed_job(migrated_factory, pid, output=out)

        deleted = _delete(
            migrated_factory,
            settings_no_autostart,
            Actor(user_id=uid, role="operator"),
            job_id,
        )

        assert not out.exists()
        assert not _job_exists(migrated_factory, job_id)
        assert (deleted.file_name, deleted.size_bytes) == ("r.mp4", 1234)
        assert f"render {job_id} deleted by op1 (file r.mp4, 1234 bytes)" in _messages(
            migrated_factory
        )

    def test_path_outside_render_root_is_never_deleted(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        tmp_path: Path,
    ) -> None:
        pid = _seed_project(migrated_factory, settings_no_autostart)
        outside = tmp_path / "elsewhere" / "precious.mp4"
        outside.parent.mkdir()
        outside.write_bytes(b"keep me")
        job_id = _seed_job(migrated_factory, pid, output=outside)

        deleted = _delete(
            migrated_factory,
            settings_no_autostart,
            Actor(user_id=_seed_user(migrated_factory), role="admin"),
            job_id,
        )

        assert outside.read_bytes() == b"keep me"
        assert not _job_exists(migrated_factory, job_id)
        assert deleted.file_name is None

    def test_missing_file_is_not_an_error(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
    ) -> None:
        pid = _seed_project(migrated_factory, settings_no_autostart)
        gone = _render_root(migrated_factory, settings_no_autostart, pid) / "gone.mp4"
        job_id = _seed_job(migrated_factory, pid, output=gone)

        _delete(
            migrated_factory,
            settings_no_autostart,
            Actor(user_id=_seed_user(migrated_factory), role="operator"),
            job_id,
        )

        assert not _job_exists(migrated_factory, job_id)
        assert any(
            m == f"render {job_id} deleted by op1 (no output file on disk)"
            for m in _messages(migrated_factory)
        )

    def test_viewer_is_refused(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
    ) -> None:
        pid = _seed_project(migrated_factory, settings_no_autostart)
        job_id = _seed_job(migrated_factory, pid)
        uid = _seed_user(migrated_factory, username="v", role="viewer")

        with pytest.raises(PermissionError_):
            _delete(
                migrated_factory,
                settings_no_autostart,
                Actor(user_id=uid, role="viewer"),
                job_id,
            )
        assert _job_exists(migrated_factory, job_id)

    @pytest.mark.parametrize("status", ["pending", "encoding"])
    def test_running_job_is_refused(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        status: str,
    ) -> None:
        """The service never removes a row a worker may still be writing for."""
        pid = _seed_project(migrated_factory, settings_no_autostart)
        job_id = _seed_job(migrated_factory, pid, status=status)

        with pytest.raises(ConflictError):
            _delete(
                migrated_factory,
                settings_no_autostart,
                Actor(user_id=_seed_user(migrated_factory), role="admin"),
                job_id,
            )
        assert _job_exists(migrated_factory, job_id)

    def test_deleted_highest_id_is_never_reused(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
    ) -> None:
        """Ids only grow (AUTOINCREMENT), so the audit log names one job per id."""
        pid = _seed_project(migrated_factory, settings_no_autostart)
        actor = Actor(user_id=_seed_user(migrated_factory), role="admin")
        _seed_job(migrated_factory, pid)
        highest = _seed_job(migrated_factory, pid)

        _delete(migrated_factory, settings_no_autostart, actor, highest)
        with session_scope(migrated_factory) as session:
            render = render_service.enqueue_render(
                session, actor, pid, output_settings={}, overlay_config={}
            ).value
            export = render_service.enqueue_export(
                session, actor, pid, frame_ids=[]
            ).value
            new_ids = (render.id, export.id)

        assert min(new_ids) > highest
        assert new_ids[1] > new_ids[0]

    def test_unknown_id_is_not_found(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
    ) -> None:
        with pytest.raises(NotFoundError):
            _delete(
                migrated_factory,
                settings_no_autostart,
                Actor(user_id=_seed_user(migrated_factory), role="admin"),
                999,
            )

    def test_kinds_are_kept_apart(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
    ) -> None:
        """delete_render refuses an export; delete_export refuses a render."""
        pid = _seed_project(migrated_factory, settings_no_autostart)
        export_id = _seed_job(migrated_factory, pid, kind="export")
        render_id = _seed_job(migrated_factory, pid)
        actor = Actor(user_id=_seed_user(migrated_factory), role="admin")

        with pytest.raises(NotFoundError):
            _delete(migrated_factory, settings_no_autostart, actor, export_id)
        with pytest.raises(NotFoundError):
            _delete(
                migrated_factory, settings_no_autostart, actor, render_id, export=True
            )
        assert _job_exists(migrated_factory, export_id)
        assert _job_exists(migrated_factory, render_id)


class TestDeleteExportService:
    def test_deletes_zip_and_row_and_audits(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
    ) -> None:
        pid = _seed_project(migrated_factory, settings_no_autostart)
        root = _render_root(migrated_factory, settings_no_autostart, pid)
        job_id = _seed_job(migrated_factory, pid, kind="export")
        zip_path = root / f"export-{job_id}.zip"
        zip_path.write_bytes(b"PK" + b"\x00" * 98)
        with session_scope(migrated_factory) as session:
            job = session.get(RenderJob, job_id)
            assert job is not None
            job.output_file_path = str(zip_path)

        deleted = _delete(
            migrated_factory,
            settings_no_autostart,
            Actor(user_id=_seed_user(migrated_factory), role="operator"),
            job_id,
            export=True,
        )

        assert not zip_path.exists()
        assert not _job_exists(migrated_factory, job_id)
        assert deleted.size_bytes == 100
        assert (
            f"export {job_id} deleted by op1 (file export-{job_id}.zip, 100 bytes)"
            in _messages(migrated_factory)
        )

    def test_removes_unrecorded_zip_and_part_of_a_cancelled_export(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
    ) -> None:
        """A cancelled export records no path; its conventional files still go."""
        pid = _seed_project(migrated_factory, settings_no_autostart)
        root = _render_root(migrated_factory, settings_no_autostart, pid)
        job_id = _seed_job(migrated_factory, pid, kind="export", status="failed")
        (root / f"export-{job_id}.zip").write_bytes(b"late")
        (root / f"export-{job_id}.zip.part").write_bytes(b"partial")

        _delete(
            migrated_factory,
            settings_no_autostart,
            Actor(user_id=_seed_user(migrated_factory), role="admin"),
            job_id,
            export=True,
        )

        assert not (root / f"export-{job_id}.zip").exists()
        assert not (root / f"export-{job_id}.zip.part").exists()

    def test_viewer_and_running_export_are_refused(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
    ) -> None:
        pid = _seed_project(migrated_factory, settings_no_autostart)
        done_id = _seed_job(migrated_factory, pid, kind="export")
        running_id = _seed_job(migrated_factory, pid, kind="export", status="encoding")
        uid = _seed_user(migrated_factory)

        with pytest.raises(PermissionError_):
            _delete(
                migrated_factory,
                settings_no_autostart,
                Actor(user_id=uid, role="viewer"),
                done_id,
                export=True,
            )
        with pytest.raises(ConflictError):
            _delete(
                migrated_factory,
                settings_no_autostart,
                Actor(user_id=uid, role="admin"),
                running_id,
                export=True,
            )


# ---------------------------------------------------------------------------
# Cancel-then-delete against a real render worker
# ---------------------------------------------------------------------------


class PartialWritingEncoder(Encoder):
    """Writes a partial output, then waits; on cancel removes it like ffmpeg's
    encoder does (kill the child, unlink the partial) before re-raising."""

    def __init__(self) -> None:
        self.active = 0
        self.started = asyncio.Event()
        self.cancelled_at: float | None = None
        self.partial: Path | None = None

    async def validate(self, output: OutputSettings, *, has_chapters: bool) -> None:
        pass

    async def render(self, spec: RenderSpec) -> RenderResult:
        self.active += 1
        spec.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.partial = spec.output_path
        spec.output_path.write_bytes(b"partial")
        self.started.set()
        try:
            await asyncio.sleep(9999)
        except asyncio.CancelledError:
            spec.output_path.unlink(missing_ok=True)
            self.cancelled_at = time.monotonic()
            raise
        finally:
            self.active -= 1
        raise AssertionError("unreachable")


def _install_context(
    settings: Settings,
    factory: sessionmaker,  # type: ignore[type-arg]
    queue: RenderQueue | None,
) -> None:
    """Install a minimal app context so delete_job finds settings + the queue."""
    set_context(
        AppContext(
            settings=settings,
            db_engine=create_db_engine(settings.database.url),
            session_factory=factory,
            logger=logging.getLogger("test"),
            app_version="test",
            ffmpeg_version="test",
            render_queue=queue,
        )
    )


async def _wait_for(predicate, timeout: float = 10.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail("condition not reached in time")
        await asyncio.sleep(0.02)


class TestCancelThenDelete:
    async def test_encoding_render_is_stopped_before_its_row_is_deleted(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
    ) -> None:
        settings = settings_no_autostart
        pid = _seed_project(migrated_factory, settings)
        uid = _seed_user(migrated_factory)
        job_id = _seed_job(migrated_factory, pid, status="pending")
        encoder = PartialWritingEncoder()
        queue = RenderQueue(settings, migrated_factory, encoder=encoder)
        _install_context(settings, migrated_factory, queue)
        await queue.start()
        try:
            await asyncio.wait_for(encoder.started.wait(), 10)
            assert encoder.partial is not None and encoder.partial.exists()

            with migrated_factory() as session:
                user = session.get(User, uid)
                deleted = await delete_job(session, user, job_id, kind="render")
        finally:
            await queue.stop()

        assert deleted.id == job_id
        assert encoder.cancelled_at is not None, "the encoder was never cancelled"
        assert encoder.active == 0, "an encoder is still running"
        assert not encoder.partial.exists(), "the partial output was left behind"
        assert not _job_exists(migrated_factory, job_id)
        messages = _messages(migrated_factory)
        # A user's cancel-on-delete is info, attributed, and not a failure: the
        # worker writes no "render N failed: render cancelled" error (seen on the
        # test VM as an active alert after a delete of a running render).
        assert f"render {job_id} cancelled by op1" in messages
        assert not any(m.startswith(f"render {job_id} failed") for m in messages)
        assert _levels(migrated_factory) <= {"info"}
        assert any(m.startswith(f"render {job_id} deleted by op1") for m in messages)

    async def test_pending_render_is_cancelled_then_deleted(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
    ) -> None:
        """A queued job (worker not draining) is flipped failed, then deleted."""
        pid = _seed_project(migrated_factory, settings_no_autostart)
        uid = _seed_user(migrated_factory)
        job_id = _seed_job(migrated_factory, pid, status="pending")
        queue = RenderQueue(settings_no_autostart, migrated_factory)
        _install_context(settings_no_autostart, migrated_factory, queue)

        with migrated_factory() as session:
            await delete_job(session, session.get(User, uid), job_id, kind="render")

        assert not _job_exists(migrated_factory, job_id)

    async def test_running_job_without_a_worker_is_refused(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
    ) -> None:
        from fastapi import HTTPException

        pid = _seed_project(migrated_factory, settings_no_autostart)
        uid = _seed_user(migrated_factory)
        job_id = _seed_job(migrated_factory, pid, status="encoding")
        _install_context(settings_no_autostart, migrated_factory, None)

        with migrated_factory() as session, pytest.raises(HTTPException) as info:
            await delete_job(session, session.get(User, uid), job_id, kind="render")

        assert info.value.status_code == 503
        assert _job_exists(migrated_factory, job_id)

    async def test_viewer_is_refused_before_anything_is_cancelled(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
    ) -> None:
        pid = _seed_project(migrated_factory, settings_no_autostart)
        uid = _seed_user(migrated_factory, username="v", role="viewer")
        job_id = _seed_job(migrated_factory, pid, status="pending")
        queue = RenderQueue(settings_no_autostart, migrated_factory)
        _install_context(settings_no_autostart, migrated_factory, queue)

        with migrated_factory() as session, pytest.raises(PermissionError_):
            await delete_job(session, session.get(User, uid), job_id, kind="render")

        with session_scope(migrated_factory) as session:
            job = session.get(RenderJob, job_id)
            assert job is not None and job.status == "pending"

    async def test_running_export_thread_is_waited_out_before_delete(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cancelling an export waits for its zip thread; no archive survives."""
        pid = _seed_project(migrated_factory, settings_no_autostart)
        uid = _seed_user(migrated_factory)
        with session_scope(migrated_factory) as session:
            frame_ids = list(session.execute(select(Frame.id)).scalars().all())
            job = RenderJob(
                project_id=pid,
                kind="export",
                status="pending",
                output_settings={"frame_ids": frame_ids},
            )
            session.add(job)
            session.flush()
            job_id = job.id

        writing = threading.Event()
        finished = threading.Event()
        real_write_zip = export_mod._write_zip  # noqa: SLF001

        def slow_write_zip(temp_path, sources, **kwargs):  # type: ignore[no-untyped-def]
            bundled = real_write_zip(temp_path, sources, **kwargs)
            writing.set()
            time.sleep(0.5)  # the zip is "still being written" during the cancel
            finished.set()
            return bundled

        monkeypatch.setattr(export_mod, "_write_zip", slow_write_zip)
        queue = RenderQueue(settings_no_autostart, migrated_factory)
        _install_context(settings_no_autostart, migrated_factory, queue)
        await queue.start()
        try:
            await _wait_for(writing.is_set)
            with migrated_factory() as session:
                await delete_job(session, session.get(User, uid), job_id, kind="export")
            assert finished.is_set(), "delete returned while the zip thread ran"
        finally:
            await queue.stop()

        root = _render_root(migrated_factory, settings_no_autostart, pid)
        assert not (root / f"export-{job_id}.zip").exists()
        assert not (root / f"export-{job_id}.zip.part").exists()
        assert not _job_exists(migrated_factory, job_id)


# ---------------------------------------------------------------------------
# The frame window the worker records
# ---------------------------------------------------------------------------


class QuickEncoder(Encoder):
    """Succeeds immediately without touching the filesystem."""

    async def validate(self, output: OutputSettings, *, has_chapters: bool) -> None:
        pass

    async def render(self, spec: RenderSpec) -> RenderResult:
        return RenderResult(
            success=True,
            output_path=spec.output_path,
            duration_seconds=0.001,
            browser_streamable=True,
            codec="h264",
            container="mp4",
        )


class TestFrameWindowRecorded:
    async def test_render_records_frame_count_window_and_chapters(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
    ) -> None:
        pid = _seed_project(migrated_factory, settings_no_autostart, frames=4)
        job_id = _seed_job(migrated_factory, pid, status="pending")
        queue = RenderQueue(
            settings_no_autostart, migrated_factory, encoder=QuickEncoder()
        )
        await queue.start()
        try:
            await asyncio.wait_for(queue.wait_idle(), 10)
        finally:
            await queue.stop()

        with session_scope(migrated_factory) as session:
            job = session.get(RenderJob, job_id)
            assert job is not None
            assert job.status == "done"
            assert job.frame_count == 4
            assert job.first_frame_at == _BASE_TS
            assert job.last_frame_at == _BASE_TS + timedelta(hours=3)
            assert job.chapters == []

    def test_export_records_bundled_frame_window(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
    ) -> None:
        """Only frames actually bundled count; a missing file is left out."""
        pid = _seed_project(migrated_factory, settings_no_autostart, frames=3)
        frames_root = settings_no_autostart.paths.frames_root
        assert frames_root is not None
        (frames_root / str(pid) / "frame_0002.jpg").unlink()  # the newest frame
        with session_scope(migrated_factory) as session:
            frame_ids = list(
                session.execute(select(Frame.id).order_by(Frame.id)).scalars().all()
            )
            job = RenderJob(
                project_id=pid,
                kind="export",
                status="encoding",
                output_settings={"frame_ids": frame_ids},
            )
            session.add(job)
            session.flush()
            job_id = job.id

        result = build_export_zip(
            settings_no_autostart, migrated_factory, job_id=job_id
        )

        assert result.success
        with session_scope(migrated_factory) as session:
            job = session.get(RenderJob, job_id)
            assert job is not None
            assert job.frame_count == 2
            assert job.first_frame_at == _BASE_TS
            assert job.last_frame_at == _BASE_TS + timedelta(hours=1)

    def test_cancelled_export_leaves_no_archive(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
    ) -> None:
        """A job flipped to failed before the rename gets no final zip."""
        pid = _seed_project(migrated_factory, settings_no_autostart)
        job_id = _seed_job(migrated_factory, pid, kind="export", status="failed")

        result = build_export_zip(
            settings_no_autostart, migrated_factory, job_id=job_id
        )

        assert not result.success
        root = _render_root(migrated_factory, settings_no_autostart, pid)
        assert not (root / f"export-{job_id}.zip").exists()
        assert not (root / f"export-{job_id}.zip.part").exists()


def test_naive_utc_conversion_matches_storage() -> None:
    """Aware capture times are stored as naive UTC (what localdt expects)."""
    from timelapse_manager.render.queue import _naive_utc

    aware = datetime(2026, 3, 1, 1, 0, tzinfo=UTC) + timedelta(hours=5)
    assert _naive_utc(aware) == datetime(2026, 3, 1, 6, 0)


# ---------------------------------------------------------------------------
# #67 R1: an orphaned encoding row deletes without a restart
# ---------------------------------------------------------------------------


class TestOrphanDelete:
    async def test_orphaned_encoding_render_is_deleted(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
    ) -> None:
        """An ``encoding`` row with no task is settled, then deleted (no 409).

        It used to be refused ("could not be stopped") until a restart: the
        worker's cancel touched only ``pending`` rows. The cancel is recorded
        once, as for a running render, and its partial file is removed.
        """
        pid = _seed_project(migrated_factory, settings_no_autostart)
        uid = _seed_user(migrated_factory)
        job_id = _seed_job(migrated_factory, pid, status="encoding")
        partial = _render_root(migrated_factory, settings_no_autostart, pid) / (
            f"render-{job_id}.mp4"
        )
        partial.write_bytes(b"partial")
        queue = RenderQueue(settings_no_autostart, migrated_factory)
        _install_context(settings_no_autostart, migrated_factory, queue)

        with migrated_factory() as session:
            deleted = await delete_job(
                session, session.get(User, uid), job_id, kind="render"
            )

        assert deleted.id == job_id
        assert not _job_exists(migrated_factory, job_id)
        assert not partial.exists()
        messages = _messages(migrated_factory)
        assert messages.count(f"render {job_id} cancelled by op1") == 1
        assert not any(m.startswith(f"render {job_id} failed") for m in messages)

    async def test_pending_job_behind_a_running_one_deletes_with_one_slot(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
    ) -> None:
        """``max_concurrent=1``: the queued job is still ``pending``, not claimed
        behind the running render, so its delete is immediate (no 409)."""
        from timelapse_manager.config.settings import RenderSettings

        settings = settings_no_autostart.model_copy(
            update={"render": RenderSettings(autostart=False, max_concurrent=1)}
        )
        pid = _seed_project(migrated_factory, settings)
        uid = _seed_user(migrated_factory)
        running = _seed_job(migrated_factory, pid, status="pending")
        encoder = PartialWritingEncoder()
        queue = RenderQueue(settings, migrated_factory, encoder=encoder)
        _install_context(settings, migrated_factory, queue)
        await queue.start()
        try:
            await asyncio.wait_for(encoder.started.wait(), 10)
            waiting = _seed_job(migrated_factory, pid, status="pending")
            queue.notify()
            await asyncio.sleep(0.2)
            with migrated_factory() as session:
                await asyncio.wait_for(
                    delete_job(session, session.get(User, uid), waiting, kind="render"),
                    5.0,
                )
            assert not _job_exists(migrated_factory, waiting)
            assert encoder.active == 1, "the running render was disturbed"
        finally:
            await queue.stop()

        assert _job_exists(migrated_factory, running)
