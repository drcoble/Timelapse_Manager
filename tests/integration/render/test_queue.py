"""Integration tests for the render queue: concurrency, status transitions."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from timelapse_manager.config.settings import RenderSettings, Settings
from timelapse_manager.db.models import Camera, Event, Frame, Project, RenderJob
from timelapse_manager.db.session import session_scope
from timelapse_manager.encode.encoder import (
    Encoder,
    OutputSettings,
    RenderResult,
    RenderSpec,
)
from timelapse_manager.encode.ffmpeg_impl import EncoderTimeoutError
from timelapse_manager.render import queue as queue_mod
from timelapse_manager.render.queue import Clock, RenderQueue
from timelapse_manager.render.spec import project_render_root

# ---------------------------------------------------------------------------
# Fake encoder for deterministic queue testing
# ---------------------------------------------------------------------------


class FakeEncoder(Encoder):
    """Configurable fake encoder.

    Tracks active renders, supports a configurable artificial delay, and can be
    made to fail on demand. Never touches the filesystem.
    """

    def __init__(
        self,
        *,
        delay: float = 0.0,
        fail: bool = False,
    ) -> None:
        self.delay = delay
        self.fail = fail
        self.active_count = 0
        self.peak_active = 0
        self.call_count = 0
        self._active_lock = asyncio.Lock()

    async def validate(self, output: OutputSettings, *, has_chapters: bool) -> None:
        pass

    async def render(self, spec: RenderSpec) -> RenderResult:
        async with self._active_lock:
            self.active_count += 1
            self.peak_active = max(self.peak_active, self.active_count)
        self.call_count += 1
        try:
            if self.delay > 0:
                await asyncio.sleep(self.delay)
            if self.fail:
                raise RuntimeError("simulated encoder failure")
            return RenderResult(
                success=True,
                output_path=spec.output_path,
                duration_seconds=0.001,
                browser_streamable=False,
                codec=spec.output_settings.codec,
                container=spec.output_settings.container,
            )
        finally:
            async with self._active_lock:
                self.active_count -= 1


# ---------------------------------------------------------------------------
# Fixtures: seed a Camera + Project + Frames in the migrated DB
# ---------------------------------------------------------------------------


def _seed_project(
    factory: sessionmaker,  # type: ignore[type-arg]
    settings: Settings,
    *,
    frame_count: int = 3,
) -> int:
    """Insert a Camera + Project + Frames; return project_id."""
    frames_root = settings.paths.frames_root
    assert frames_root is not None
    with session_scope(factory) as session:
        cam = Camera(name="q-cam", address="127.0.0.1", protocol="vapix")
        session.add(cam)
        session.flush()

        proj = Project(
            camera_id=cam.id,
            name="q-project",
            lifecycle_state="active",
            operational_status="idle",
        )
        session.add(proj)
        session.flush()
        project_id = proj.id

        # Create the project frame directory and write tiny JPEG stubs.
        frame_dir = frames_root / str(project_id)
        frame_dir.mkdir(parents=True, exist_ok=True)

        base_ts = datetime(2024, 3, 1, tzinfo=UTC)
        for i in range(frame_count):
            filename = f"frame_{i:04d}.jpg"
            filepath = frame_dir / filename
            # Write a minimal valid JPEG so resolve_absolute + gather_frames works.
            import struct

            sof = (
                b"\xff\xc0"
                + struct.pack(">H", 17)
                + b"\x08"
                + struct.pack(">HH", 48, 64)
                + b"\x01\x01\x11\x00"
            )
            filepath.write_bytes(b"\xff\xd8" + sof + b"\xff\xd9")

            ts = base_ts + timedelta(hours=i)
            session.add(
                Frame(
                    project_id=project_id,
                    sequence_index=i,
                    capture_timestamp=ts.replace(tzinfo=None),
                    file_path=filename,
                    capture_status="captured",
                    lifecycle_state="active",
                    width=64,
                    height=48,
                )
            )
        proj.frame_count = frame_count

    return project_id


def _enqueue_job(
    factory: sessionmaker,  # type: ignore[type-arg]
    project_id: int,
    *,
    kind: str = "manual",
    status: str = "pending",
    started_at: datetime | None = None,
) -> int:
    """Insert a RenderJob (pending unless ``status`` says otherwise); return its id.

    A row a running worker must never claim is inserted directly in its final
    status: inserting it ``pending`` and flipping it in a second commit leaves a
    window in which the drain loop's re-poll claims and renders it (#189).
    """
    with session_scope(factory) as session:
        job = RenderJob(
            project_id=project_id,
            kind=kind,
            status=status,
            started_at=started_at,
            output_settings={
                "fps": 1.0,
                "width": 64,
                "height": 48,
                "codec": "h264",
                "container": "mp4",
            },
        )
        session.add(job)
        session.flush()
        return job.id


def _job_status(factory: sessionmaker, job_id: int) -> str:  # type: ignore[type-arg]
    with session_scope(factory) as session:
        job = session.get(RenderJob, job_id)
        assert job is not None
        return job.status


def _fake_disk_usage(*, free: int, total: int = 100_000_000_000):  # type: ignore[no-untyped-def]
    """Return a ``shutil.disk_usage`` stand-in reporting fixed free/total bytes."""

    def _probe(_path: Path) -> SimpleNamespace:
        return SimpleNamespace(total=total, used=total - free, free=free)

    return _probe


# ---------------------------------------------------------------------------
# Test: status transitions pending → encoding → done
# ---------------------------------------------------------------------------


async def test_job_transitions_pending_to_done(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_job(migrated_factory, project_id)

    assert _job_status(migrated_factory, job_id) == "pending"

    encoder = FakeEncoder()
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=encoder)
    await queue.start()
    try:
        # Wait for the worker to drain the job to a terminal state and park.
        await asyncio.wait_for(queue.wait_idle(), 30.0)
    finally:
        await queue.stop()

    assert _job_status(migrated_factory, job_id) == "done"


async def test_failing_encoder_transitions_job_to_failed(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_job(migrated_factory, project_id)

    encoder = FakeEncoder(fail=True)
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=encoder)
    await queue.start()
    try:
        deadline = asyncio.get_event_loop().time() + 10.0
        while asyncio.get_event_loop().time() < deadline:
            if _job_status(migrated_factory, job_id) in ("done", "failed"):
                break
            await asyncio.sleep(0.05)
    finally:
        await queue.stop()

    assert _job_status(migrated_factory, job_id) == "failed"


# ---------------------------------------------------------------------------
# Test: bounded concurrency (max_concurrent=1)
# ---------------------------------------------------------------------------


async def test_max_concurrent_1_peak_active_never_exceeds_1(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """With max_concurrent=1 and a slow encoder, at most 1 job runs at a time."""
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_ids = [
        _enqueue_job(migrated_factory, project_id, kind="manual") for _ in range(3)
    ]

    settings = Settings(
        database=settings_no_autostart.database,
        logging=settings_no_autostart.logging,
        paths=settings_no_autostart.paths,
        capture=settings_no_autostart.capture,
        render=RenderSettings(autostart=False, max_concurrent=1),
    )
    encoder = FakeEncoder(delay=0.1)
    queue = RenderQueue(settings, migrated_factory, encoder=encoder)
    await queue.start()
    try:
        await asyncio.wait_for(queue.wait_idle(), 30.0)
    finally:
        await queue.stop()

    # All 3 jobs should complete and peak concurrency must never have exceeded 1.
    statuses = [_job_status(migrated_factory, jid) for jid in job_ids]
    assert all(s == "done" for s in statuses), f"Not all jobs done: {statuses}"
    assert encoder.peak_active <= 1, (
        f"Peak active was {encoder.peak_active}, expected ≤1"
    )


# ---------------------------------------------------------------------------
# Test: failing job does not prevent other jobs from succeeding
# ---------------------------------------------------------------------------


async def test_failing_job_does_not_block_other_jobs(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    project_id = _seed_project(migrated_factory, settings_no_autostart)

    # First job will fail; second and third should still succeed.
    job1 = _enqueue_job(migrated_factory, project_id, kind="manual")

    # After a failing encoder sees the first job, swap it for a succeeding one.
    fail_encoder = FakeEncoder(fail=True)
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=fail_encoder)
    await queue.start()
    try:
        await asyncio.wait_for(queue.wait_idle(), 30.0)
    finally:
        await queue.stop()

    assert _job_status(migrated_factory, job1) == "failed"

    # Now enqueue a second job with a succeeding encoder.
    job2 = _enqueue_job(migrated_factory, project_id, kind="manual")
    success_encoder = FakeEncoder()
    queue2 = RenderQueue(
        settings_no_autostart, migrated_factory, encoder=success_encoder
    )
    await queue2.start()
    try:
        await asyncio.wait_for(queue2.wait_idle(), 30.0)
    finally:
        await queue2.stop()

    assert _job_status(migrated_factory, job2) == "done"


# ---------------------------------------------------------------------------
# Test: cancel_job (pending job → failed synchronously)
# ---------------------------------------------------------------------------


async def test_cancel_pending_job_transitions_to_failed(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_job(migrated_factory, project_id)

    # Queue not started — job stays pending.
    encoder = FakeEncoder()
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=encoder)

    changed = await queue.cancel_job(job_id)

    assert changed is True
    assert _job_status(migrated_factory, job_id) == "failed"
    # Encoder should never have been called.
    assert encoder.call_count == 0


async def test_cancel_already_terminal_job_returns_false(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_job(migrated_factory, project_id)

    # Mark the job failed directly.
    with session_scope(migrated_factory) as session:
        job = session.get(RenderJob, job_id)
        assert job is not None
        job.status = "failed"

    encoder = FakeEncoder()
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=encoder)

    changed = await queue.cancel_job(job_id)

    assert changed is False


# ---------------------------------------------------------------------------
# Test: cancel_job (in-flight job)
# ---------------------------------------------------------------------------


async def test_cancel_inflight_job_transitions_to_failed(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_job(migrated_factory, project_id)

    encoder = FakeEncoder(delay=9999.0)
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=encoder)
    await queue.start()
    try:
        # Poll the fake encoder's active count (not DB status) so we catch the
        # moment the encoder is actually running before we cancel.
        deadline = asyncio.get_event_loop().time() + 10.0
        while asyncio.get_event_loop().time() < deadline:
            if encoder.active_count > 0:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("Encoder never became active")

        await queue.cancel_job(job_id)

    finally:
        await queue.stop()

    # After cancellation cleanup the job should be failed.
    assert _job_status(migrated_factory, job_id) == "failed"
    assert encoder.active_count == 0
    # Not a user's cancel: the interrupted render is still recorded as an error.
    assert _render_events(migrated_factory, job_id) == [
        ("error", "render.failed", f"render {job_id} failed: render cancelled")
    ]


def _render_events(
    factory: sessionmaker,  # type: ignore[type-arg]
    job_id: int,
) -> list[tuple[str, str | None, str]]:
    """``(level, type, message)`` of every event about ``job_id``, oldest first."""
    with session_scope(factory) as session:
        rows = session.query(Event).order_by(Event.id).all()
        return [
            (e.level, (e.event_metadata or {}).get("type"), e.message)
            for e in rows
            if (e.event_metadata or {}).get("render_id") == job_id
        ]


async def _wait_active(encoder: FakeEncoder) -> None:
    """Wait until the fake encoder is running a render."""
    deadline = asyncio.get_event_loop().time() + 10.0
    while asyncio.get_event_loop().time() < deadline:
        if encoder.active_count > 0:
            return
        await asyncio.sleep(0.02)
    pytest.fail("Encoder never became active")


async def test_user_cancel_of_inflight_job_writes_no_failure_event(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """A user's cancel ends the job failed but is not logged as an error.

    The render service records who cancelled it (info); the worker must not add
    "render N failed: render cancelled" at error level, which raised an active
    alert on the test VM after a cancel-on-delete.
    """
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_job(migrated_factory, project_id)

    encoder = FakeEncoder(delay=9999.0)
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=encoder)
    await queue.start()
    try:
        await _wait_active(encoder)
        assert await queue.cancel_job(job_id, user_requested=True) is True
    finally:
        await queue.stop()

    assert _job_status(migrated_factory, job_id) == "failed"
    assert encoder.active_count == 0
    assert _render_events(migrated_factory, job_id) == []


async def test_user_cancel_flag_does_not_outlive_its_cancel(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """After a user cancel, a shutdown-interrupted render is no longer silent.

    It is requeued with an info event (#178), where the first, user-cancelled
    render wrote nothing.
    """
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    first = _enqueue_job(migrated_factory, project_id)

    encoder = FakeEncoder(delay=9999.0)
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=encoder)
    await queue.start()
    try:
        await _wait_active(encoder)
        await queue.cancel_job(first, user_requested=True)
        second = _enqueue_job(migrated_factory, project_id)
        queue.notify()
        await _wait_active(encoder)
    finally:
        await queue.stop()  # cuts the second render off: not a user's cancel

    assert _render_events(migrated_factory, first) == []
    assert _render_events(migrated_factory, second) == [
        (
            "info",
            "render.requeued",
            f"render {second} was interrupted by a service shutdown and requeued",
        )
    ]


async def test_user_cancel_of_pending_job_writes_no_event(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_job(migrated_factory, project_id)
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=FakeEncoder())

    assert await queue.cancel_job(job_id, user_requested=True) is True

    assert _job_status(migrated_factory, job_id) == "failed"
    assert _render_events(migrated_factory, job_id) == []


# ---------------------------------------------------------------------------
# Test: orphaned `encoding` jobs are reclaimed on start()
# ---------------------------------------------------------------------------


async def test_start_reclaims_orphaned_encoding_job(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """A job stranded in ``encoding`` (no live task) is failed at startup.

    Simulates the aftermath of a crash -- or a shutdown that cancelled the drain
    loop while a non-cancellable claim transaction committed -- which leaves a job
    in ``encoding`` with no task. The claim query only takes ``pending`` rows, so
    such a job would never be picked up again; ``start`` must sweep it to
    ``failed``. A genuinely ``pending`` job must be left untouched and still run.
    """
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    orphan_id = _enqueue_job(migrated_factory, project_id)
    pending_id = _enqueue_job(migrated_factory, project_id)

    # Strand the first job in `encoding` with no live task, as a crash would.
    with session_scope(migrated_factory) as session:
        job = session.get(RenderJob, orphan_id)
        assert job is not None
        job.status = "encoding"

    encoder = FakeEncoder()
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=encoder)
    await queue.start()
    try:
        # The orphan must be failed immediately (synchronously, before the loop).
        assert _job_status(migrated_factory, orphan_id) == "failed"

        # The still-pending job must be untouched by the sweep and run to done.
        await asyncio.wait_for(queue.wait_idle(), 30.0)
    finally:
        await queue.stop()

    assert _job_status(migrated_factory, pending_id) == "done"
    # The orphan was never re-run by the encoder.
    assert encoder.call_count == 1


# ---------------------------------------------------------------------------
# Test: free-space gate refuses a render below the low watermark
# ---------------------------------------------------------------------------


async def test_render_refused_when_free_space_below_watermark(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A render is failed without ever encoding when the volume is below the floor.

    The output volume reports free space under the low watermark, so the worker
    must refuse the job (mark it ``failed``) before invoking the encoder rather
    than start an encode that could fill the disk.
    """
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_job(migrated_factory, project_id)

    # 500 MB free is below the 1 GB default low watermark.
    monkeypatch.setattr(
        "timelapse_manager.render.export.shutil.disk_usage",
        _fake_disk_usage(free=500_000_000),
    )

    encoder = FakeEncoder()
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=encoder)
    await queue.start()
    try:
        deadline = asyncio.get_event_loop().time() + 10.0
        while asyncio.get_event_loop().time() < deadline:
            if _job_status(migrated_factory, job_id) in ("done", "failed"):
                break
            await asyncio.sleep(0.05)
    finally:
        await queue.stop()

    assert _job_status(migrated_factory, job_id) == "failed"
    # The encoder must never have been invoked for a refused job.
    assert encoder.call_count == 0


async def test_render_proceeds_when_free_space_ample(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ample free space the gate is transparent and the render completes."""
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_job(migrated_factory, project_id)

    # 50 GB free clears both the byte and percentage floors.
    monkeypatch.setattr(
        "timelapse_manager.render.export.shutil.disk_usage",
        _fake_disk_usage(free=50_000_000_000),
    )

    encoder = FakeEncoder()
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=encoder)
    await queue.start()
    try:
        await asyncio.wait_for(queue.wait_idle(), 30.0)
    finally:
        await queue.stop()

    assert _job_status(migrated_factory, job_id) == "done"
    assert encoder.call_count == 1


# ---------------------------------------------------------------------------
# #67 R1: durable terminal writes, the in-loop orphan sweep, the claim lock
# ---------------------------------------------------------------------------


class _FastClock(Clock):
    """A clock whose backoff sleeps are near-instant and can be held open.

    ``sleeps`` records each requested delay; ``on_sleep`` (if set) is called
    with that list after each. While ``gate`` is set to an unset event, a
    sleep waits on it, so a test can look at the worker in the middle of a
    backoff.
    """

    def __init__(self) -> None:
        self.sleeps: list[float] = []
        self.on_sleep: Callable[[list[float]], None] | None = None
        self.gate: asyncio.Event | None = None
        self.sleeping = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self.on_sleep is not None:
            self.on_sleep(self.sleeps)
        self.sleeping.set()
        if self.gate is not None:
            await self.gate.wait()
        await asyncio.sleep(0)


def _locked_commit_factory(
    factory: sessionmaker,  # type: ignore[type-arg]
    locked: list[bool],
) -> sessionmaker:  # type: ignore[type-arg]
    """A session factory whose writes of a ``failed`` render row hit a lock.

    While ``locked[0]`` is true, a flush that would write a render job's
    ``failed`` status raises SQLite's "database is locked" (as a lock held past
    the busy timeout does). Every other write -- the claim, the spec, the reads
    -- goes through, so the failure is aimed at the worker's failure recorder
    alone.
    """

    class _LockedCommitSession(Session):
        def flush(self, objects: object = None) -> None:
            writes_failure = any(
                isinstance(obj, RenderJob) and obj.status == "failed"
                for obj in self.dirty
            )
            if writes_failure and locked[0]:
                raise OperationalError(
                    "UPDATE", {}, sqlite3.OperationalError("database is locked")
                )
            super().flush(objects)  # type: ignore[arg-type]

    return sessionmaker(
        bind=factory.kw["bind"], expire_on_commit=False, class_=_LockedCommitSession
    )


def _events_of_type(
    factory: sessionmaker,  # type: ignore[type-arg]
    event_type: str,
) -> list[tuple[str, str]]:
    """``(level, message)`` of every event of ``event_type``, oldest first."""
    with session_scope(factory) as session:
        return [
            (e.level, e.message)
            for e in session.query(Event).order_by(Event.id).all()
            if (e.event_metadata or {}).get("type") == event_type
        ]


async def _wait_until(predicate, timeout: float = 10.0) -> None:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_event_loop().time() + timeout
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            pytest.fail("condition not reached in time")
        await asyncio.sleep(0.02)


@pytest.fixture
def fast_terminal_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """No real sleeps between a recorder's lock retries."""
    monkeypatch.setattr(queue_mod, "_TERMINAL_WRITE_BASE_DELAY", 0.0)


@pytest.mark.usefixtures("fast_terminal_retries")
async def test_failure_recorder_hitting_a_locked_database_never_strands_the_job(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """The test-VM failure: a render fails, its failure write meets the lock.

    Before #67 the failure recorder swallowed the error, the task ended and
    freed its slot, and the row stayed ``encoding`` until a restart --
    ``tlm-upgrade.sh`` then refused with "render jobs active: 1". Now the write
    is retried (inside the recorder, then by the settle loop with backoff)
    until it lands, while the service keeps running: the row ends ``failed``
    with its failure event, and the first miss is recorded as a warning.
    """
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_job(migrated_factory, project_id)
    # Locked through one recorder call's retries and the settle loop's first
    # backoff; the lock "clears" during the second backoff.
    locked = [True]
    factory = _locked_commit_factory(migrated_factory, locked)
    clock = _FastClock()

    def clear_on_second_backoff(sleeps: list[float]) -> None:
        if len(sleeps) == 2:
            locked[0] = False

    clock.on_sleep = clear_on_second_backoff
    queue = RenderQueue(
        settings_no_autostart, factory, encoder=FakeEncoder(fail=True), clock=clock
    )
    await queue.start()
    try:
        await _wait_until(lambda: _job_status(migrated_factory, job_id) == "failed")
        # Settled while the worker is still running, not by a restart's sweep.
        assert queue.is_running()
        await asyncio.wait_for(queue.wait_idle(), 10.0)
    finally:
        await queue.stop()

    # The settle loop came back for it twice, with its 1, 2, 4 ... s backoff.
    assert clock.sleeps == [1.0, 2.0]
    assert _render_events(migrated_factory, job_id)[-1] == (
        "error",
        "render.failed",
        f"render {job_id} failed: simulated encoder failure",
    )
    warnings = _events_of_type(migrated_factory, queue_mod.STATE_WRITE_FAILED_EVENT)
    assert [level for level, _ in warnings] == ["warning"]
    assert queue.orphans_reclaimed_total == 0


@pytest.mark.usefixtures("fast_terminal_retries")
async def test_settle_holds_the_slot_until_the_terminal_write_lands(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """While a row cannot be written, its job keeps the slot and its task entry.

    With ``max_concurrent=1`` a second pending job is not claimed during the
    backoff, and the stuck row is not an orphan (its id is still in
    ``_job_tasks``). Once the write lands the slot frees and the second runs.
    """
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    first = _enqueue_job(migrated_factory, project_id)
    second = _enqueue_job(migrated_factory, project_id)
    locked = [True]
    factory = _locked_commit_factory(migrated_factory, locked)
    clock = _FastClock()
    clock.gate = asyncio.Event()
    settings = settings_no_autostart.model_copy(
        update={"render": RenderSettings(autostart=False, max_concurrent=1)}
    )
    encoder = _FailFirstEncoder()
    queue = RenderQueue(settings, factory, encoder=encoder, clock=clock)
    await queue.start()
    try:
        await asyncio.wait_for(clock.sleeping.wait(), 10.0)
        # Mid-backoff: the first job is still encoding, still tracked.
        assert _job_status(migrated_factory, first) == "encoding"
        assert first in queue._job_tasks  # noqa: SLF001
        activity = await queue.activity()
        assert (activity.live_tasks, activity.orphans) == (1, 0)
        # Let the drain loop run a sweep and a claim attempt meanwhile.
        queue.notify()
        await asyncio.sleep(0.3)
        assert _job_status(migrated_factory, first) == "encoding"
        assert _job_status(migrated_factory, second) == "pending"
        assert encoder.call_count == 1
        locked[0] = False
        clock.gate.set()
        await _wait_until(lambda: _job_status(migrated_factory, second) == "done")
    finally:
        clock.gate.set()
        await queue.stop()

    assert _job_status(migrated_factory, first) == "failed"
    assert queue.orphans_reclaimed_total == 0


class _FailFirstEncoder(FakeEncoder):
    """Fails its first render and succeeds after that."""

    async def render(self, spec: RenderSpec) -> RenderResult:
        self.fail = self.call_count == 0
        return await super().render(spec)


async def test_drain_loop_sweeps_an_orphan_while_running(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """A task-less ``encoding`` row is failed by the running worker (no restart).

    Its partial output, at the confined render path, is removed, and the count
    is kept for the readiness probe.
    """
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=FakeEncoder())
    await queue.start()
    try:
        await asyncio.wait_for(queue.wait_idle(), 10.0)
        # Strand a row while the worker runs, with a partial file on disk.
        orphan = _enqueue_job(
            migrated_factory,
            project_id,
            status="encoding",
            started_at=datetime(2026, 10, 1, 12, 0),
        )
        with session_scope(migrated_factory) as session:
            render_root = project_render_root(
                settings_no_autostart, session.get(Project, project_id)
            )
        render_root.mkdir(parents=True, exist_ok=True)
        partial = render_root / f"render-{orphan}.mp4"
        partial.write_bytes(b"partial")
        keep = render_root / f"render-{orphan}0.mp4"  # another job's file
        keep.write_bytes(b"other")
        queue.notify()
        await _wait_until(lambda: _job_status(migrated_factory, orphan) == "failed")
        assert queue.is_running()
    finally:
        await queue.stop()

    assert not partial.exists()
    assert keep.exists()
    assert queue.orphans_reclaimed_total == 1
    assert _render_events(migrated_factory, orphan) == [
        ("error", "render.failed", f"render {orphan} failed: orphaned (no live task)")
    ]


async def test_sweep_records_the_oldest_encoding_start(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """The sweep remembers the oldest ``encoding`` start for the probe."""
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_job(migrated_factory, project_id)
    encoder = FakeEncoder(delay=9999.0)
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=encoder)
    assert queue.oldest_encoding_started_at is None
    await queue.start()
    try:
        await _wait_active(encoder)
        await queue._sweep_orphans()  # noqa: SLF001
        with session_scope(migrated_factory) as session:
            job = session.get(RenderJob, job_id)
            assert job is not None
            started_at = job.started_at
        assert started_at is not None
        assert queue.oldest_encoding_started_at == started_at
        assert queue.orphans_reclaimed_total == 0
    finally:
        await queue.stop()


async def test_a_job_in_its_claim_window_is_not_an_orphan(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """A row claimed but not yet registered is neither swept nor lost by a cancel.

    The claim is held open (gated after its ``pending -> encoding`` commit), so
    the row is ``encoding`` with no task yet. The sweep and a cancel arriving
    then wait on the claim lock; once the task is registered the sweep leaves
    it alone and the cancel stops it like any running job.
    """
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_job(migrated_factory, project_id)
    encoder = FakeEncoder(delay=9999.0)
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=encoder)
    claimed = threading.Event()
    release = threading.Event()
    real_claim = queue._claim_next_pending  # noqa: SLF001

    def gated_claim() -> int | None:
        result = real_claim()
        if result is not None:
            claimed.set()
            release.wait(10.0)
        return result

    queue._claim_next_pending = gated_claim  # type: ignore[method-assign]  # noqa: SLF001
    await queue.start()
    try:
        await _wait_until(claimed.is_set)
        assert _job_status(migrated_factory, job_id) == "encoding"
        assert job_id not in queue._job_tasks  # noqa: SLF001
        sweep = asyncio.create_task(queue._sweep_orphans())  # noqa: SLF001
        cancel = asyncio.create_task(queue.cancel_job(job_id, user_requested=True))
        await asyncio.sleep(0.2)
        assert not sweep.done() and not cancel.done()
        release.set()
        await asyncio.wait_for(sweep, 10.0)
        assert await asyncio.wait_for(cancel, 10.0) is True
    finally:
        release.set()
        await queue.stop()

    assert _job_status(migrated_factory, job_id) == "failed"
    assert queue.orphans_reclaimed_total == 0
    # A user's cancel of a live job: no failure event, orphan or otherwise.
    assert _render_events(migrated_factory, job_id) == []


async def test_cancel_settles_an_orphaned_encoding_job(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """A cancel of a task-less ``encoding`` row fails it (it used to no-op)."""
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_job(migrated_factory, project_id)
    with session_scope(migrated_factory) as session:
        job = session.get(RenderJob, job_id)
        assert job is not None
        job.status = "encoding"
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=FakeEncoder())

    assert await queue.cancel_job(job_id, user_requested=True) is True
    assert await queue.cancel_job(job_id, user_requested=True) is False

    assert _job_status(migrated_factory, job_id) == "failed"
    assert queue.orphans_reclaimed_total == 1
    # The caller records a user's cancel; the worker adds no failure event.
    assert _render_events(migrated_factory, job_id) == []


async def test_cancel_running_job_with_one_slot_returns_promptly(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """``max_concurrent=1``, one job running and one pending: cancel is quick.

    The pending job never sits claimed behind the running one (the slot is
    taken before the claim), so the second job is still ``pending`` while the
    first runs, and a cancel of either settles at once.
    """
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    running = _enqueue_job(migrated_factory, project_id)
    waiting = _enqueue_job(migrated_factory, project_id)
    settings = settings_no_autostart.model_copy(
        update={"render": RenderSettings(autostart=False, max_concurrent=1)}
    )
    encoder = FakeEncoder(delay=9999.0)
    queue = RenderQueue(settings, migrated_factory, encoder=encoder)
    await queue.start()
    try:
        await _wait_active(encoder)
        queue.notify()
        await asyncio.sleep(0.2)
        assert _job_status(migrated_factory, waiting) == "pending"
        assert await asyncio.wait_for(queue.cancel_job(waiting), 2.0) is True
        assert await asyncio.wait_for(queue.cancel_job(running), 2.0) is True
    finally:
        await queue.stop()

    assert _job_status(migrated_factory, running) == "failed"
    assert _job_status(migrated_factory, waiting) == "failed"
    assert encoder.call_count == 1


async def test_activity_reports_live_tasks_rows_and_orphans(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    running = _enqueue_job(migrated_factory, project_id)
    settings = settings_no_autostart.model_copy(
        update={"render": RenderSettings(autostart=False, max_concurrent=1)}
    )
    encoder = FakeEncoder(delay=9999.0)
    queue = RenderQueue(settings, migrated_factory, encoder=encoder)
    await queue.start()
    try:
        await _wait_active(encoder)
        _enqueue_job(migrated_factory, project_id)  # pending behind the slot
        orphan = _enqueue_job(migrated_factory, project_id)
        with session_scope(migrated_factory) as session:
            job = session.get(RenderJob, orphan)
            assert job is not None
            job.status = "encoding"
        activity = await queue.activity()
    finally:
        await queue.stop()

    assert activity.as_dict() == {
        "live_tasks": 1,
        "pending_rows": 1,
        "encoding_rows": 2,
        "orphans": 1,
    }
    assert running != orphan


async def test_a_failing_cycle_does_not_kill_the_worker(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """A claim that raises (a lock past the busy timeout) is retried next cycle."""
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_job(migrated_factory, project_id)
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=FakeEncoder())
    real_claim = queue._claim_next_pending  # noqa: SLF001
    calls = [0]

    def flaky_claim() -> int | None:
        calls[0] += 1
        if calls[0] == 1:
            raise OperationalError(
                "UPDATE", {}, sqlite3.OperationalError("database is locked")
            )
        return real_claim()

    queue._claim_next_pending = flaky_claim  # type: ignore[method-assign]  # noqa: SLF001
    await queue.start()
    try:
        await asyncio.sleep(0.1)
        assert queue.is_running()
        queue.notify()
        await _wait_until(lambda: _job_status(migrated_factory, job_id) == "done")
    finally:
        await queue.stop()


class _WatchdogKilledEncoder(FakeEncoder):
    """Fails the way the ffmpeg watchdog does (WP-67-B)."""

    async def render(self, spec: RenderSpec) -> RenderResult:
        self.call_count += 1
        raise EncoderTimeoutError("ffmpeg made no progress for 300 s; killed")


@pytest.mark.usefixtures("fast_terminal_retries")
async def test_watchdog_failure_under_a_locked_database_is_settled(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """A watchdog kill goes through the same retried failure write as any other."""
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_job(migrated_factory, project_id)
    locked = [True]
    factory = _locked_commit_factory(migrated_factory, locked)
    clock = _FastClock()

    def clear_on_first_backoff(sleeps: list[float]) -> None:
        locked[0] = False

    clock.on_sleep = clear_on_first_backoff
    queue = RenderQueue(
        settings_no_autostart, factory, encoder=_WatchdogKilledEncoder(), clock=clock
    )
    await queue.start()
    try:
        await _wait_until(lambda: _job_status(migrated_factory, job_id) == "failed")
        assert queue.is_running()
    finally:
        await queue.stop()

    assert clock.sleeps == [1.0]
    assert _render_events(migrated_factory, job_id)[-1] == (
        "error",
        "render.failed",
        f"render {job_id} failed: ffmpeg made no progress for 300 s; killed",
    )
