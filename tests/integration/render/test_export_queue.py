"""Integration tests for async frame export on the render queue.

An export is a ``kind="export"`` render job: the bounded worker drains it like a
render but routes it to the zip builder instead of the encoder. These tests cover
the worker fork end-to-end and -- load-bearing -- the two data-loss guards that
keep an export and a render from deleting each other's outputs.

Mirrors ``test_queue.py`` (seed a Camera + Project + real frame files in the
migrated DB, drive a real ``RenderQueue``).
"""

from __future__ import annotations

import asyncio
import struct
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from timelapse_manager.config.settings import Settings
from timelapse_manager.db.models import Camera, Event, Frame, Project, RenderJob
from timelapse_manager.db.session import session_scope
from timelapse_manager.render.export import build_export_zip
from timelapse_manager.render.post_actions import run_post_actions
from timelapse_manager.render.queue import RenderQueue
from timelapse_manager.render.spec import project_render_root

# A minimal valid JPEG body, reused for every seeded frame file.
_JPEG = (
    b"\xff\xd8"
    + b"\xff\xc0"
    + struct.pack(">H", 17)
    + b"\x08"
    + struct.pack(">HH", 48, 64)
    + b"\x01\x01\x11\x00"
    + b"\xff\xd9"
)


def _seed_project(
    factory: sessionmaker,  # type: ignore[type-arg]
    settings: Settings,
    *,
    frame_count: int = 3,
) -> tuple[int, list[int]]:
    """Insert a Camera + Project + Frames with real files; return ids."""
    frames_root = settings.paths.frames_root
    assert frames_root is not None
    frame_ids: list[int] = []
    with session_scope(factory) as session:
        cam = Camera(name="x-cam", address="127.0.0.1", protocol="vapix")
        session.add(cam)
        session.flush()
        proj = Project(
            camera_id=cam.id,
            name="x-project",
            lifecycle_state="active",
            operational_status="idle",
        )
        session.add(proj)
        session.flush()
        project_id = proj.id

        frame_dir = frames_root / str(project_id)
        frame_dir.mkdir(parents=True, exist_ok=True)
        base_ts = datetime(2024, 3, 1, tzinfo=UTC)
        for i in range(frame_count):
            filename = f"frame_{i:04d}.jpg"
            (frame_dir / filename).write_bytes(_JPEG)
            ts = base_ts + timedelta(hours=i)
            frame = Frame(
                project_id=project_id,
                sequence_index=i,
                capture_timestamp=ts.replace(tzinfo=None),
                file_path=filename,
                capture_status="captured",
                lifecycle_state="active",
                width=64,
                height=48,
            )
            session.add(frame)
            session.flush()
            frame_ids.append(frame.id)
        proj.frame_count = frame_count
    return project_id, frame_ids


def _enqueue_export(
    factory: sessionmaker,  # type: ignore[type-arg]
    project_id: int,
    frame_ids: list[int],
) -> int:
    with session_scope(factory) as session:
        job = RenderJob(
            project_id=project_id,
            kind="export",
            status="pending",
            output_settings={"frame_ids": frame_ids},
        )
        session.add(job)
        session.flush()
        return job.id


def _job(factory: sessionmaker, job_id: int) -> RenderJob:  # type: ignore[type-arg]
    with session_scope(factory) as session:
        job = session.get(RenderJob, job_id)
        assert job is not None
        session.expunge(job)
        return job


async def _drain_until_terminal(
    factory: sessionmaker,  # type: ignore[type-arg]
    queue: RenderQueue,
    job_id: int,
) -> str:
    await queue.start()
    try:
        deadline = asyncio.get_event_loop().time() + 10.0
        while asyncio.get_event_loop().time() < deadline:
            if _job(factory, job_id).status in ("done", "failed"):
                break
            await asyncio.sleep(0.05)
    finally:
        await queue.stop()
    return _job(factory, job_id).status


# ---------------------------------------------------------------------------
# Worker fork: an export job produces a downloadable zip of its frames.
# ---------------------------------------------------------------------------


@pytest.mark.flaky(reruns=2, reruns_delay=1)
async def test_export_job_produces_downloadable_zip(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    project_id, frame_ids = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_export(migrated_factory, project_id, frame_ids)

    status = await _drain_until_terminal(
        migrated_factory,
        RenderQueue(settings_no_autostart, migrated_factory),
        job_id,
    )

    assert status == "done"
    job = _job(migrated_factory, job_id)
    assert job.output_file_path is not None
    zip_path = Path(job.output_file_path)
    assert zip_path.is_file()
    # The zip is confined to the project render root and holds every frame.
    with session_scope(migrated_factory) as session:
        proj = session.get(Project, project_id)
        assert proj is not None
        root = project_render_root(settings_no_autostart, proj).resolve()
    assert zip_path.resolve().is_relative_to(root)
    with zipfile.ZipFile(zip_path) as archive:
        assert len(archive.namelist()) == len(frame_ids)


async def test_export_writes_exactly_one_completion_event(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    project_id, frame_ids = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_export(migrated_factory, project_id, frame_ids)

    await _drain_until_terminal(
        migrated_factory,
        RenderQueue(settings_no_autostart, migrated_factory),
        job_id,
    )

    with session_scope(migrated_factory) as session:
        export_events = [
            e
            for e in session.query(Event)
            .filter(Event.scope == "project", Event.scope_id == project_id)
            .all()
            if (e.event_metadata or {}).get("action") == "export"
        ]
    assert len(export_events) == 1
    meta = export_events[0].event_metadata
    assert meta["render_id"] == job_id
    assert meta["frame_count"] == len(frame_ids)


async def test_export_skips_missing_file_without_failing(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """A frame whose image is gone is skipped; the export still completes."""
    project_id, frame_ids = _seed_project(migrated_factory, settings_no_autostart)
    # Delete one frame's file on disk before exporting.
    frames_root = settings_no_autostart.paths.frames_root
    assert frames_root is not None
    victim = next((frames_root / str(project_id)).glob("*.jpg"))
    victim.unlink()

    job_id = _enqueue_export(migrated_factory, project_id, frame_ids)
    status = await _drain_until_terminal(
        migrated_factory,
        RenderQueue(settings_no_autostart, migrated_factory),
        job_id,
    )

    assert status == "done"
    zip_path = Path(_job(migrated_factory, job_id).output_file_path or "")
    with zipfile.ZipFile(zip_path) as archive:
        # One file missing -> one fewer entry, no crash.
        assert len(archive.namelist()) == len(frame_ids) - 1


async def test_export_of_vanished_job_fails(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """A builder run against a missing job row yields a failed result."""
    project_id, frame_ids = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_export(migrated_factory, project_id, frame_ids)
    with session_scope(migrated_factory) as session:
        session.delete(session.get(RenderJob, job_id))

    result = await asyncio.to_thread(
        build_export_zip,
        settings_no_autostart,
        migrated_factory,
        job_id=job_id,
    )
    assert result.success is False
    assert result.output_path is None


# ---------------------------------------------------------------------------
# Data-loss guard 1: an export job never triggers prune of render outputs.
# ---------------------------------------------------------------------------


async def test_export_job_does_not_prune_render_outputs(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """``run_post_actions`` early-returns for an export, so a configured prune
    that would otherwise delete render outputs never runs."""
    project_id, frame_ids = _seed_project(migrated_factory, settings_no_autostart)

    with session_scope(migrated_factory) as session:
        proj = session.get(Project, project_id)
        assert proj is not None
        render_root = project_render_root(settings_no_autostart, proj)
    render_root.mkdir(parents=True, exist_ok=True)

    # A done render output that a prune (keep=1) would delete if it ran.
    render_file = render_root / "render-1.mp4"
    render_file.write_bytes(b"\x00" * 64)
    with session_scope(migrated_factory) as session:
        render_job = RenderJob(
            project_id=project_id,
            kind="manual",
            status="done",
            output_settings={},
            output_file_path=str(render_file),
            completed_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(render_job)
        session.flush()
        render_job_id = render_job.id

    # Run post-actions for an EXPORT job carrying a configured prune. The early
    # return must stop the prune from ever touching the render output.
    export_zip = render_root / "export-9.zip"
    export_zip.write_bytes(b"PK\x03\x04")
    await run_post_actions(
        settings_no_autostart,
        migrated_factory,
        job_id=99,
        output_path=export_zip,
        action_specs=[{"type": "prune", "keep": 1}],
        kind="export",
    )

    # The render output and its row both survive.
    assert render_file.is_file()
    assert _job(migrated_factory, render_job_id).status == "done"


# ---------------------------------------------------------------------------
# Data-loss guard 2 (the load-bearing one): a render's prune never deletes an
# export's zip or its job row.
# ---------------------------------------------------------------------------


async def test_render_prune_does_not_delete_export_zip_or_row(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """A scheduled render's configured prune must exclude export jobs.

    ``_prune``'s candidate query is "non-archive done renders"; without the
    explicit export exclusion an export job (also non-archive, also done) would be
    a prune candidate, so a render's prune would delete the user's export zip and
    its row -- making the download 404. This asserts the export survives.
    """
    project_id, _frame_ids = _seed_project(migrated_factory, settings_no_autostart)

    with session_scope(migrated_factory) as session:
        proj = session.get(Project, project_id)
        assert proj is not None
        render_root = project_render_root(settings_no_autostart, proj)
    render_root.mkdir(parents=True, exist_ok=True)

    # A done export job + its zip on disk.
    export_zip = render_root / "export-7.zip"
    export_zip.write_bytes(b"PK\x03\x04")
    with session_scope(migrated_factory) as session:
        export_job = RenderJob(
            project_id=project_id,
            kind="export",
            status="done",
            output_settings={"frame_ids": [1, 2]},
            output_file_path=str(export_zip),
            completed_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(export_job)
        session.flush()
        export_job_id = export_job.id

    # A pile of done manual renders the prune WILL act on, plus the triggering one.
    render_files: list[Path] = []
    trigger_file = render_root / "render-trigger.mp4"
    trigger_file.write_bytes(b"\x00" * 64)
    with session_scope(migrated_factory) as session:
        for i in range(3):
            outfile = render_root / f"render-{i}.mp4"
            outfile.write_bytes(b"\x00" * 64)
            render_files.append(outfile)
            session.add(
                RenderJob(
                    project_id=project_id,
                    kind="manual",
                    status="done",
                    output_settings={},
                    output_file_path=str(outfile),
                    completed_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
        trigger = RenderJob(
            project_id=project_id,
            kind="manual",
            status="done",
            output_settings={},
            output_file_path=str(trigger_file),
            completed_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(trigger)
        session.flush()
        trigger_id = trigger.id

    # Fire prune as a scheduled render (manual triggers are prune-exempt).
    await run_post_actions(
        settings_no_autostart,
        migrated_factory,
        job_id=trigger_id,
        output_path=trigger_file,
        action_specs=[{"type": "prune", "keep": 1}],
        kind="scheduled",
    )

    # The export zip and its row both survive the prune (the guard works).
    assert export_zip.is_file()
    assert _job(migrated_factory, export_job_id).status == "done"
    # Sanity: the prune actually ran (older manual renders were swept).
    assert not render_files[0].is_file()
