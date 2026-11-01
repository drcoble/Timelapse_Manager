"""The render worker's terminal writes run off the event loop (#62).

A job that is cancelled, refused for low disk, cannot be prepared, or whose
encoder fails is recorded ``failed`` in a worker thread. Under SQLite's write
lock, held here by another connection, that write waits up to the busy timeout;
on the event loop it used to freeze the whole server for that long. These tests
check that:

* while a cancel's write waits on the lock, the loop keeps running, and the
  cancel completes (job ``failed``, no failure event for a user's cancel) once
  the lock is released;
* a second cancellation during that write (``stop`` landing while a user's
  cancel settles) does not abandon it: the job still ends ``failed`` before the
  task finishes;
* each failure path records ``failed`` with no database work on the loop (the
  loop guard records background work too).
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from tests import loop_guard_support as guard
from tests.integration.render.test_queue import (
    FakeEncoder,
    _enqueue_job,
    _job_status,
    _render_events,
    _seed_project,
)
from timelapse_manager.config.settings import Settings
from timelapse_manager.render import export as export_mod
from timelapse_manager.render.queue import RenderQueue

# Longest the loop may stall while the cancel's write waits on the lock. The
# regression stalls for the 15 s busy timeout.
_MAX_LOOP_STALL = 1.0
# How long the test holds the write lock: well under the busy timeout, so the
# worker's write waits and then succeeds.
_LOCK_HELD_SECONDS = 1.5


def _db_path(factory: sessionmaker) -> Path:  # type: ignore[type-arg]
    database = factory.kw["bind"].url.database
    assert database is not None
    return Path(database)


def _hold_write_lock(db_path: Path) -> sqlite3.Connection:
    """Open a connection holding SQLite's write lock until it is rolled back."""
    conn = sqlite3.connect(str(db_path), timeout=10.0, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    return conn


async def _wait_encoding(encoder: FakeEncoder) -> None:
    deadline = time.monotonic() + 10.0
    while encoder.active_count == 0:
        if time.monotonic() > deadline:
            pytest.fail("the encoder never became active")
        await asyncio.sleep(0.02)


async def _longest_stall(duration: float, step: float = 0.05) -> float:
    """Tick the loop for ``duration`` seconds; return the worst oversleep."""
    worst = 0.0
    end = time.monotonic() + duration
    while time.monotonic() < end:
        before = time.monotonic()
        await asyncio.sleep(step)
        worst = max(worst, time.monotonic() - before - step)
    return worst


def _background_violations() -> list[guard.LoopDbViolation]:
    return [v for v in guard.collect() if v.background]


async def test_user_cancel_under_write_lock_keeps_the_loop_running(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_job(migrated_factory, project_id)
    encoder = FakeEncoder(delay=9999.0)
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=encoder)
    await queue.start()
    try:
        await _wait_encoding(encoder)
        lock = _hold_write_lock(_db_path(migrated_factory))
        try:
            cancel = asyncio.create_task(queue.cancel_job(job_id, user_requested=True))
            stall = await _longest_stall(_LOCK_HELD_SECONDS)
            settled_under_lock = cancel.done()
        finally:
            lock.rollback()
            lock.close()
        changed = await asyncio.wait_for(cancel, 15.0)
    finally:
        await queue.stop()

    assert stall < _MAX_LOOP_STALL, f"the event loop stalled {stall:.2f}s"
    assert not settled_under_lock, "the cancel finished while the lock was held"
    assert changed is True
    assert encoder.active_count == 0
    assert _job_status(migrated_factory, job_id) == "failed"
    # A user's cancel writes no failure event (#39); the service logs it.
    assert _render_events(migrated_factory, job_id) == []
    assert _background_violations() == []


async def test_stop_during_a_cancel_write_still_settles_the_job(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_job(migrated_factory, project_id)
    encoder = FakeEncoder(delay=9999.0)
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=encoder)
    await queue.start()
    await _wait_encoding(encoder)

    lock = _hold_write_lock(_db_path(migrated_factory))
    try:
        cancel = asyncio.create_task(queue.cancel_job(job_id, user_requested=True))
        await asyncio.sleep(0.2)
        # Shutdown cancels the job task again while its cancel write waits.
        stop = asyncio.create_task(queue.stop())
        await asyncio.sleep(0.3)
        pending_under_lock = not cancel.done() and not stop.done()
    finally:
        lock.rollback()
        lock.close()
    await asyncio.wait_for(asyncio.gather(cancel, stop), 15.0)

    assert pending_under_lock, "cancel or stop returned before the write landed"
    assert _job_status(migrated_factory, job_id) == "failed"
    assert _render_events(migrated_factory, job_id) == []
    assert _background_violations() == []


def _fail_encoder(monkeypatch: pytest.MonkeyPatch) -> FakeEncoder:
    return FakeEncoder(fail=True)


def _low_disk(monkeypatch: pytest.MonkeyPatch) -> FakeEncoder:
    monkeypatch.setattr(
        export_mod, "free_space_below_low_watermark", lambda *_args: True
    )
    return FakeEncoder()


@pytest.mark.parametrize(
    ("frames", "arrange", "reason"),
    [
        (3, _fail_encoder, "simulated encoder failure"),
        (3, _low_disk, "insufficient free disk space to render"),
        # No frames: the spec cannot be built (SpecBuildError).
        (0, lambda _monkeypatch: FakeEncoder(), None),
    ],
    ids=["encoder-failure", "low-disk", "spec-error"],
)
async def test_failure_paths_record_failed_off_the_loop(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
    monkeypatch: pytest.MonkeyPatch,
    frames: int,
    arrange: Callable[[pytest.MonkeyPatch], FakeEncoder],
    reason: str | None,
) -> None:
    project_id = _seed_project(
        migrated_factory, settings_no_autostart, frame_count=frames
    )
    job_id = _enqueue_job(migrated_factory, project_id)
    queue = RenderQueue(
        settings_no_autostart, migrated_factory, encoder=arrange(monkeypatch)
    )
    await queue.start()
    try:
        await asyncio.wait_for(queue.wait_idle(), 10.0)
    finally:
        await queue.stop()

    assert _job_status(migrated_factory, job_id) == "failed"
    events = _render_events(migrated_factory, job_id)
    assert len(events) == 1
    level, event_type, message = events[0]
    assert (level, event_type) == ("error", "render.failed")
    if reason is not None:
        assert message == f"render {job_id} failed: {reason}"
    assert _background_violations() == []
