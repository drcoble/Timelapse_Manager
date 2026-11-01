"""Exports and housekeeping (#67 P7, R12; #101).

Stored (not deflated) images, disk-space pre-check and mid-write abort, no ``.part``
left behind, exports on their own slot, queued-export cancellation on the jobs
executor, the export TTL, and the purge of long-dead session rows.
"""

from __future__ import annotations

import asyncio
import errno
import secrets
import threading
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from timelapse_manager.config.settings import Settings
from timelapse_manager.db.models import Event, Project, RenderJob, User
from timelapse_manager.db.models import Session as SessionRow
from timelapse_manager.db.session import session_scope
from timelapse_manager.encode import (
    Encoder,
    OutputSettings,
    RenderResult,
    RenderSpec,
)
from timelapse_manager.executors import create_jobs_executor
from timelapse_manager.render import export as export_mod
from timelapse_manager.render import queue as queue_mod
from timelapse_manager.render.export import build_export_zip, remove_stale_parts
from timelapse_manager.render.queue import Clock, RenderQueue
from timelapse_manager.render.scheduler import RenderScheduler
from timelapse_manager.render.spec import project_render_root
from timelapse_manager.security.sessions import hash_token, purge_stale_sessions
from timelapse_manager.services.renders import purge_expired_exports
from timelapse_manager.usernames import username_key

from .test_export_queue import (
    _enqueue_export,
    _fake_disk_usage,
    _job,
    _seed_project,
)

NOW = datetime(2026, 10, 28, 12, 0, 0)


def _root(factory: sessionmaker, settings: Settings, project_id: int) -> Path:  # type: ignore[type-arg]
    with session_scope(factory) as session:
        project = session.get(Project, project_id)
        assert project is not None
        return project_render_root(settings, project)


async def _wait_until(predicate, timeout: float = 10.0) -> None:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("condition not reached in time")
        await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------
# P7: zip mode, disk space, .part cleanup
# ---------------------------------------------------------------------------


def test_images_are_stored_not_deflated(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    project_id, frame_ids = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_export(migrated_factory, project_id, frame_ids)

    result = build_export_zip(settings_no_autostart, migrated_factory, job_id=job_id)

    assert result.success and result.output_path is not None
    with zipfile.ZipFile(result.output_path) as archive:
        infos = archive.infolist()
        assert len(infos) == len(frame_ids)
        assert {i.compress_type for i in infos} == {zipfile.ZIP_STORED}


def test_enospc_while_writing_leaves_no_part_file(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, frame_ids = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_export(migrated_factory, project_id, frame_ids)

    def full_disk(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError(errno.ENOSPC, "No space left on device")

    # Members are copied from an O_NOFOLLOW descriptor (#172), not ZipFile.write.
    monkeypatch.setattr(export_mod.shutil, "copyfileobj", full_disk)

    with pytest.raises(OSError, match="No space left"):
        build_export_zip(settings_no_autostart, migrated_factory, job_id=job_id)

    root = _root(migrated_factory, settings_no_autostart, project_id)
    assert not (root / f"export-{job_id}.zip").exists()
    assert not (root / f"export-{job_id}.zip.part").exists()


def test_precheck_fails_early_when_the_zip_would_not_fit(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, frame_ids = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_export(migrated_factory, project_id, frame_ids)
    # Above the 5 % floor (5 GB of 100 GB), but only by a single byte.
    monkeypatch.setattr(
        "timelapse_manager.render.export.shutil.disk_usage",
        _fake_disk_usage(free=5_000_000_001),
    )

    result = build_export_zip(settings_no_autostart, migrated_factory, job_id=job_id)

    assert result.success is False
    assert result.error == export_mod.INSUFFICIENT_DISK_REASON
    root = _root(migrated_factory, settings_no_autostart, project_id)
    assert not (root / f"export-{job_id}.zip.part").exists()


def test_free_space_is_rechecked_while_writing(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, frame_ids = _seed_project(
        migrated_factory, settings_no_autostart, frame_count=6
    )
    job_id = _enqueue_export(migrated_factory, project_id, frame_ids)
    monkeypatch.setattr(export_mod, "_SPACE_CHECK_EVERY", 2)
    reads = [0]

    def shrinking(path: Path) -> Any:
        reads[0] += 1
        # Ample for the gates and the first re-check, then the volume fills up.
        free = 50_000_000_000 if reads[0] <= 3 else 4_000_000_000
        return _fake_disk_usage(free=free)(path)

    monkeypatch.setattr("timelapse_manager.render.export.shutil.disk_usage", shrinking)

    result = build_export_zip(settings_no_autostart, migrated_factory, job_id=job_id)

    assert result.success is False
    assert result.error == export_mod.INSUFFICIENT_DISK_REASON
    root = _root(migrated_factory, settings_no_autostart, project_id)
    assert not (root / f"export-{job_id}.zip").exists()
    assert not (root / f"export-{job_id}.zip.part").exists()


async def test_start_removes_stale_part_files_only(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    project_id, _ = _seed_project(migrated_factory, settings_no_autostart)
    root = _root(migrated_factory, settings_no_autostart, project_id)
    root.mkdir(parents=True, exist_ok=True)
    stale = root / "export-77.zip.part"
    stale.write_bytes(b"half a zip")
    finished = root / "export-78.zip"
    finished.write_bytes(b"a zip")
    render_part = root / "render-1.mp4.part"
    render_part.write_bytes(b"x")

    assert remove_stale_parts(settings_no_autostart, migrated_factory) == 1

    assert not stale.exists()
    assert finished.exists()
    assert render_part.exists()

    # The queue does it on start as well.
    stale.write_bytes(b"again")
    queue = RenderQueue(settings_no_autostart, migrated_factory)
    await queue.start()
    await queue.stop()
    assert not stale.exists()


# ---------------------------------------------------------------------------
# P7: exports have their own slot
# ---------------------------------------------------------------------------


class _GatedEncoder(Encoder):
    """Holds every render open until released."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def validate(self, output: OutputSettings, *, has_chapters: bool) -> None:
        pass

    async def render(self, spec: RenderSpec) -> RenderResult:
        self.started.set()
        await self.release.wait()
        return RenderResult(
            success=True,
            output_path=spec.output_path,
            duration_seconds=0.001,
            browser_streamable=False,
            codec=spec.output_settings.codec,
            container=spec.output_settings.container,
        )


async def test_an_export_runs_while_a_render_holds_the_render_slot(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    project_id, frame_ids = _seed_project(migrated_factory, settings_no_autostart)
    assert settings_no_autostart.render.max_concurrent == 1
    with session_scope(migrated_factory) as session:
        render = RenderJob(
            project_id=project_id,
            kind="manual",
            status="pending",
            output_settings={
                "fps": 1.0,
                "width": 64,
                "height": 48,
                "codec": "h264",
                "container": "mp4",
            },
        )
        session.add(render)
        session.flush()
        render_id = render.id
    export_id = _enqueue_export(migrated_factory, project_id, frame_ids)
    encoder = _GatedEncoder()
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=encoder)
    await queue.start()
    try:
        await asyncio.wait_for(encoder.started.wait(), 10.0)
        await _wait_until(lambda: _job(migrated_factory, export_id).status == "done")
        assert _job(migrated_factory, render_id).status == "encoding"
    finally:
        encoder.release.set()
        await asyncio.wait_for(queue.wait_idle(), 10.0)
        await queue.stop()
    assert _job(migrated_factory, render_id).status == "done"


# ---------------------------------------------------------------------------
# #101: a queued export is cancellable before it starts
# ---------------------------------------------------------------------------


async def test_cancelling_a_queued_export_does_not_wait_for_the_executor(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one worker is busy: the claimed export waits in the executor's queue.

    The cancel must settle the job at once and the zip must never be built, not
    start once the executor frees up.
    """
    project_id, frame_ids = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_export(migrated_factory, project_id, frame_ids)
    executor = create_jobs_executor(1)
    blocker_running = threading.Event()
    release = threading.Event()
    built: list[int] = []

    def block() -> None:
        blocker_running.set()
        release.wait(30.0)

    real_prepare = RenderQueue._prepare

    def prepare_then_occupy(self, prepared_id):  # type: ignore[no-untyped-def]
        result = real_prepare(self, prepared_id)
        # Queued behind this very call, so ahead of the export's zip build.
        executor.submit(block)
        return result

    monkeypatch.setattr(RenderQueue, "_prepare", prepare_then_occupy)
    monkeypatch.setattr(
        export_mod,
        "build_export_zip",
        lambda *args, **kwargs: built.append(kwargs["job_id"]),
    )
    queue = RenderQueue(settings_no_autostart, migrated_factory, jobs_executor=executor)
    await queue.start()
    try:
        await _wait_until(lambda: job_id in queue._export_gates)  # noqa: SLF001
        await _wait_until(blocker_running.is_set)

        changed = await asyncio.wait_for(
            queue.cancel_job(job_id, user_requested=True), 5.0
        )

        assert changed is True
        assert _job(migrated_factory, job_id).status == "failed"
    finally:
        release.set()
        await queue.stop()
        executor.shutdown(wait=True)
    assert built == []
    root = _root(migrated_factory, settings_no_autostart, project_id)
    assert not (root / f"export-{job_id}.zip").exists()
    assert not (root / f"export-{job_id}.zip.part").exists()


def test_export_gate_start_and_cancel_are_mutually_exclusive() -> None:
    gate = queue_mod._ExportGate()  # noqa: SLF001
    assert gate.cancel_if_queued() is True
    assert gate.start() is False

    gate = queue_mod._ExportGate()  # noqa: SLF001
    assert gate.start() is True
    assert gate.cancel_if_queued() is False


# ---------------------------------------------------------------------------
# P7: export TTL
# ---------------------------------------------------------------------------


def _done_export(
    factory: sessionmaker,  # type: ignore[type-arg]
    settings: Settings,
    project_id: int,
    *,
    age_days: float,
    output: Path | None = None,
    status: str = "done",
) -> tuple[int, Path]:
    """Insert an export of the given age with a zip file on disk."""
    root = _root(factory, settings, project_id)
    root.mkdir(parents=True, exist_ok=True)
    with session_scope(factory) as session:
        job = RenderJob(
            project_id=project_id,
            kind="export",
            status=status,
            output_settings={"frame_ids": []},
            completed_at=NOW - timedelta(days=age_days),
        )
        session.add(job)
        session.flush()
        job_id = job.id
        zip_path = output if output is not None else root / f"export-{job_id}.zip"
        zip_path.write_bytes(b"PK")
        job.output_file_path = str(zip_path)
    return job_id, zip_path


def _exists(factory: sessionmaker, job_id: int) -> bool:  # type: ignore[type-arg]
    with session_scope(factory) as session:
        return session.get(RenderJob, job_id) is not None


def test_expired_exports_are_deleted_with_their_zip(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    project_id, _ = _seed_project(migrated_factory, settings_no_autostart)
    assert settings_no_autostart.render.export_ttl_days == 7
    old_id, old_zip = _done_export(
        migrated_factory, settings_no_autostart, project_id, age_days=8
    )
    fresh_id, fresh_zip = _done_export(
        migrated_factory, settings_no_autostart, project_id, age_days=6
    )
    running_id, running_zip = _done_export(
        migrated_factory,
        settings_no_autostart,
        project_id,
        age_days=30,
        status="encoding",
    )

    deleted = purge_expired_exports(migrated_factory, settings_no_autostart, NOW)

    assert deleted == 1
    assert not _exists(migrated_factory, old_id)
    assert not old_zip.exists()
    assert _exists(migrated_factory, fresh_id) and fresh_zip.exists()
    assert _exists(migrated_factory, running_id) and running_zip.exists()
    with session_scope(migrated_factory) as session:
        events = session.scalars(
            select(Event.message).where(Event.message.like("export % deleted after%"))
        ).all()
    assert events == [
        f"export {old_id} deleted after 7 day(s) (render.export_ttl_days)"
    ]


def test_ttl_zero_keeps_exports_forever(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    settings_no_autostart.render.export_ttl_days = 0
    project_id, _ = _seed_project(migrated_factory, settings_no_autostart)
    job_id, zip_path = _done_export(
        migrated_factory, settings_no_autostart, project_id, age_days=3650
    )

    assert purge_expired_exports(migrated_factory, settings_no_autostart, NOW) == 0

    assert _exists(migrated_factory, job_id) and zip_path.exists()


def test_ttl_never_deletes_a_file_outside_the_render_root(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
    tmp_path: Path,
) -> None:
    project_id, _ = _seed_project(migrated_factory, settings_no_autostart)
    outside = tmp_path / "precious.zip"
    job_id, _ = _done_export(
        migrated_factory,
        settings_no_autostart,
        project_id,
        age_days=9,
        output=outside,
    )

    assert purge_expired_exports(migrated_factory, settings_no_autostart, NOW) == 1

    assert not _exists(migrated_factory, job_id)
    assert outside.exists()


async def test_housekeeping_runs_the_export_ttl_and_session_purge(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    project_id, _ = _seed_project(migrated_factory, settings_no_autostart)
    job_id, zip_path = _done_export(
        migrated_factory, settings_no_autostart, project_id, age_days=8
    )
    user_id = _seed_user(migrated_factory)
    old = _add_session(migrated_factory, user_id, revoked_at=NOW - timedelta(days=9))
    live = _add_session(migrated_factory, user_id, expires_at=NOW + timedelta(days=1))

    class _Clock(Clock):
        def now(self) -> datetime:
            return NOW.replace(tzinfo=UTC)

    queue = RenderQueue(settings_no_autostart, migrated_factory)
    scheduler = RenderScheduler(
        settings_no_autostart, migrated_factory, queue, clock=_Clock()
    )

    await scheduler.run_housekeeping(force=True)

    assert not _exists(migrated_factory, job_id) and not zip_path.exists()
    assert _session_ids(migrated_factory) == {live}
    assert old not in _session_ids(migrated_factory)


# ---------------------------------------------------------------------------
# R12: stale session purge
# ---------------------------------------------------------------------------


def _seed_user(factory: sessionmaker) -> int:  # type: ignore[type-arg]
    with session_scope(factory) as session:
        user = User(
            username="purge-user",
            username_key=username_key("purge-user"),
            role="viewer",
            auth_source="local",
            enabled=True,
        )
        session.add(user)
        session.flush()
        return user.id


def _add_session(
    factory: sessionmaker,  # type: ignore[type-arg]
    user_id: int,
    *,
    revoked_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> int:
    with session_scope(factory) as session:
        row = SessionRow(
            user_id=user_id,
            token_hash=hash_token(secrets.token_urlsafe(16)),
            revoked=revoked_at is not None,
            revoked_at=revoked_at,
            expires_at=expires_at,
        )
        session.add(row)
        session.flush()
        return row.id


def _session_ids(factory: sessionmaker) -> set[int]:  # type: ignore[type-arg]
    with session_scope(factory) as session:
        return set(session.scalars(select(SessionRow.id)))


def test_purge_removes_only_sessions_dead_for_more_than_a_week(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
) -> None:
    user_id = _seed_user(migrated_factory)
    day = timedelta(days=1)
    revoked_old = _add_session(migrated_factory, user_id, revoked_at=NOW - 8 * day)
    expired_old = _add_session(migrated_factory, user_id, expires_at=NOW - 8 * day)
    revoked_recent = _add_session(migrated_factory, user_id, revoked_at=NOW - 2 * day)
    expired_recent = _add_session(migrated_factory, user_id, expires_at=NOW - day)
    live = _add_session(migrated_factory, user_id, expires_at=NOW + day)
    no_expiry = _add_session(migrated_factory, user_id)

    deleted = purge_stale_sessions(migrated_factory, now=NOW)

    assert deleted == 2
    assert _session_ids(migrated_factory) == {
        revoked_recent,
        expired_recent,
        live,
        no_expiry,
    }
    assert revoked_old not in _session_ids(migrated_factory)
    assert expired_old not in _session_ids(migrated_factory)


def test_purge_runs_in_batches(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = _seed_user(migrated_factory)
    for _ in range(7):
        _add_session(migrated_factory, user_id, expires_at=NOW - timedelta(days=30))
    survivor = _add_session(migrated_factory, user_id, expires_at=NOW + timedelta(1))
    batches: list[int] = []
    real = __import__(
        "timelapse_manager.security.sessions", fromlist=["prune_in_batches"]
    ).prune_in_batches

    def counting(factory, build, batch_size, max_batches):  # type: ignore[no-untyped-def]
        def counted(limit: int):  # type: ignore[no-untyped-def]
            batches.append(limit)
            return build(limit)

        return real(factory, counted, batch_size, max_batches)

    monkeypatch.setattr(
        "timelapse_manager.security.sessions.prune_in_batches", counting
    )

    deleted = purge_stale_sessions(migrated_factory, now=NOW, batch_size=3)

    assert deleted == 7
    assert batches == [3, 3, 3]  # 3 + 3 + 1: stops on the short batch
    assert _session_ids(migrated_factory) == {survivor}
