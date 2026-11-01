"""A render a clean service stop interrupted is requeued once (#178)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.render.test_queue import (
    FakeEncoder,
    _enqueue_job,
    _job_status,
    _render_events,
    _seed_project,
    _wait_until,
)
from timelapse_manager.config.settings import Settings
from timelapse_manager.db.models import Event, Project, RenderJob
from timelapse_manager.db.session import session_scope
from timelapse_manager.render.queue import RenderQueue
from timelapse_manager.render.spec import project_render_root


def _requeues(factory: sessionmaker[Session], job_id: int) -> int:
    with session_scope(factory) as session:
        job = session.get(RenderJob, job_id)
        assert job is not None
        return job.shutdown_requeues


async def _run_until_encoding_then_stop(
    settings: Settings,
    factory: sessionmaker[Session],
    encoder: FakeEncoder,
    *,
    monotonic: Iterator[float] | None = None,
) -> RenderQueue:
    """Start a queue, wait for the encoder to be running, then stop it cleanly."""
    queue = RenderQueue(settings, factory, encoder=encoder)
    if monotonic is not None:
        queue._monotonic = lambda: next(monotonic)  # type: ignore[method-assign]
    await queue.start()
    try:
        await _wait_until(lambda: encoder.active_count > 0)
    finally:
        await queue.stop()
    return queue


async def test_clean_stop_requeues_once_and_the_next_start_completes_it(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_job(migrated_factory, project_id)
    with session_scope(migrated_factory) as session:
        project = session.get(Project, project_id)
        assert project is not None
        render_root = project_render_root(settings_no_autostart, project)
    render_root.mkdir(parents=True, exist_ok=True)
    partial = render_root / f"render-{job_id}.mp4"
    partial.write_bytes(b"partial")
    other = render_root / f"render-{job_id}0.mp4"  # another job's file
    other.write_bytes(b"keep")

    slow = FakeEncoder(delay=9999.0)
    await _run_until_encoding_then_stop(settings_no_autostart, migrated_factory, slow)

    assert _job_status(migrated_factory, job_id) == "pending"
    assert _requeues(migrated_factory, job_id) == 1
    with session_scope(migrated_factory) as session:
        job = session.get(RenderJob, job_id)
        assert job is not None
        assert (job.started_at, job.completed_at, job.output_file_path) == (
            None,
            None,
            None,
        )
    assert _render_events(migrated_factory, job_id) == [
        (
            "info",
            "render.requeued",
            f"render {job_id} was interrupted by a service shutdown and requeued",
        )
    ]
    assert not partial.exists()
    assert other.exists()

    fast = FakeEncoder()
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=fast)
    await queue.start()
    try:
        await asyncio.wait_for(queue.wait_idle(), 30.0)
    finally:
        await queue.stop()

    assert _job_status(migrated_factory, job_id) == "done"
    assert fast.call_count == 1


async def test_second_clean_stop_fails_the_job(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_job(migrated_factory, project_id)

    await _run_until_encoding_then_stop(
        settings_no_autostart, migrated_factory, FakeEncoder(delay=9999.0)
    )
    assert _job_status(migrated_factory, job_id) == "pending"

    await _run_until_encoding_then_stop(
        settings_no_autostart, migrated_factory, FakeEncoder(delay=9999.0)
    )

    assert _job_status(migrated_factory, job_id) == "failed"
    assert _requeues(migrated_factory, job_id) == 1
    events = _render_events(migrated_factory, job_id)
    assert [e[:2] for e in events] == [
        ("info", "render.requeued"),
        ("error", "render.failed"),
    ]
    assert "again after a requeue" in events[1][2]


async def test_crash_leaves_no_clean_mark_and_the_orphan_sweep_fails_it(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """An ``encoding`` row nobody settled is a crash: failed, never requeued."""
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_job(migrated_factory, project_id, status="encoding")

    encoder = FakeEncoder()
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=encoder)
    await queue.start()
    try:
        assert _job_status(migrated_factory, job_id) == "failed"
        await asyncio.wait_for(queue.wait_idle(), 30.0)
    finally:
        await queue.stop()

    assert encoder.call_count == 0
    assert _requeues(migrated_factory, job_id) == 0
    assert _render_events(migrated_factory, job_id) == [
        ("error", "render.failed", f"render {job_id} failed: orphaned (no live task)")
    ]


async def test_crash_after_a_requeue_is_still_failed(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_job(migrated_factory, project_id)
    await _run_until_encoding_then_stop(
        settings_no_autostart, migrated_factory, FakeEncoder(delay=9999.0)
    )
    assert _job_status(migrated_factory, job_id) == "pending"
    # The rerun is claimed, then the process dies: the row is left ``encoding``.
    with session_scope(migrated_factory) as session:
        job = session.get(RenderJob, job_id)
        assert job is not None
        job.status = "encoding"

    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=FakeEncoder())
    await queue.start()
    try:
        assert _job_status(migrated_factory, job_id) == "failed"
    finally:
        await queue.stop()


async def test_stop_past_the_grace_period_fails_instead_of_requeuing(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """The grace was not honoured: the stop was not clean, so no requeue."""
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_job(migrated_factory, project_id)
    grace = settings_no_autostart.server.shutdown_grace_seconds

    # stop() reads the clock once; the cancel handler reads it after the grace.
    await _run_until_encoding_then_stop(
        settings_no_autostart,
        migrated_factory,
        FakeEncoder(delay=9999.0),
        monotonic=iter([100.0, 100.0 + grace + 1.0, 100.0 + grace + 2.0]),
    )

    assert _job_status(migrated_factory, job_id) == "failed"
    assert _requeues(migrated_factory, job_id) == 0
    assert _render_events(migrated_factory, job_id) == [
        ("error", "render.failed", f"render {job_id} failed: render cancelled")
    ]


async def test_an_export_is_never_requeued(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """An export's zip thread cannot be interrupted, so a stop fails it."""
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_job(
        migrated_factory, project_id, kind="export", status="encoding"
    )
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=FakeEncoder())

    assert queue._mark_interrupted_by_shutdown_sync(job_id, True)

    assert _job_status(migrated_factory, job_id) == "failed"
    assert _requeues(migrated_factory, job_id) == 0


async def test_a_job_already_done_stays_done(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_job(migrated_factory, project_id, status="done")
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=FakeEncoder())

    assert queue._mark_interrupted_by_shutdown_sync(job_id, True)

    assert _job_status(migrated_factory, job_id) == "done"
    with session_scope(migrated_factory) as session:
        assert session.query(Event).count() == 0


@pytest.mark.parametrize("within_grace", [True, False])
async def test_a_job_not_encoding_is_not_requeued(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
    within_grace: bool,
) -> None:
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_job(migrated_factory, project_id, status="failed")
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=FakeEncoder())

    assert queue._mark_interrupted_by_shutdown_sync(job_id, within_grace)

    assert _job_status(migrated_factory, job_id) == "failed"
    assert _requeues(migrated_factory, job_id) == 0
