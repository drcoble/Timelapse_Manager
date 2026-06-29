"""Integration tests for the render scheduler: scheduling logic and clock injection."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import sessionmaker

from timelapse_manager.config.settings import Settings
from timelapse_manager.db.models import Camera, Project, RenderJob
from timelapse_manager.db.session import session_scope
from timelapse_manager.encode.encoder import (
    Encoder,
    OutputSettings,
    RenderResult,
    RenderSpec,
)
from timelapse_manager.render.queue import Clock, RenderQueue
from timelapse_manager.render.scheduler import RenderScheduler

# ---------------------------------------------------------------------------
# Fake encoder: instant success, no filesystem side effects
# ---------------------------------------------------------------------------


class FakeEncoder(Encoder):
    async def validate(self, output: OutputSettings, *, has_chapters: bool) -> None:
        pass

    async def render(self, spec: RenderSpec) -> RenderResult:
        return RenderResult(
            success=True,
            output_path=spec.output_path,
            duration_seconds=0.001,
            browser_streamable=False,
            codec=spec.output_settings.codec,
            container=spec.output_settings.container,
        )


# ---------------------------------------------------------------------------
# Fixed clock for deterministic scheduling
# ---------------------------------------------------------------------------


class FixedClock(Clock):
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(0)  # yield without real wait


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _seed_active_project(
    factory: sessionmaker,  # type: ignore[type-arg]
    settings: Settings,
    *,
    render_schedule: dict | None = None,
    archive_schedule: dict | None = None,
    name: str = "sched-proj",
) -> int:
    frames_root = settings.paths.frames_root
    assert frames_root is not None
    with session_scope(factory) as session:
        cam = Camera(name=f"{name}-cam", address="127.0.0.1", protocol="vapix")
        session.add(cam)
        session.flush()

        proj = Project(
            camera_id=cam.id,
            name=name,
            lifecycle_state="active",
            operational_status="idle",
            render_schedule=render_schedule,
            archive_schedule=archive_schedule,
        )
        session.add(proj)
        session.flush()
        project_id = proj.id

    return project_id


def _pending_jobs(factory: sessionmaker, project_id: int, kind: str) -> list[int]:  # type: ignore[type-arg]
    with session_scope(factory) as session:
        rows = (
            session.query(RenderJob)
            .filter(
                RenderJob.project_id == project_id,
                RenderJob.kind == kind,
                RenderJob.status.in_(["pending", "encoding"]),
            )
            .all()
        )
        return [r.id for r in rows]


def _all_jobs(factory: sessionmaker, project_id: int, kind: str) -> list[RenderJob]:  # type: ignore[type-arg]
    with session_scope(factory) as session:
        rows = (
            session.query(RenderJob)
            .filter(RenderJob.project_id == project_id, RenderJob.kind == kind)
            .all()
        )
        for r in rows:
            session.expunge(r)
        return rows


# ---------------------------------------------------------------------------
# Test: fresh project (no prior jobs) → always enqueues on run_once
# ---------------------------------------------------------------------------


async def test_fresh_project_with_render_schedule_enqueues_scheduled_job(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """A fresh project (no prior jobs) with a render schedule is always due."""
    schedule = {"enabled": True, "interval_seconds": 3600}
    project_id = _seed_active_project(
        migrated_factory, settings_no_autostart, render_schedule=schedule
    )

    clock = FixedClock(datetime(2024, 3, 1, 12, 0, 0, tzinfo=UTC))
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=FakeEncoder())
    scheduler = RenderScheduler(
        settings_no_autostart, migrated_factory, queue, clock=clock
    )

    enqueued = await scheduler.run_once()

    assert len(enqueued) >= 1
    jobs = _pending_jobs(migrated_factory, project_id, "scheduled")
    assert len(jobs) == 1


async def test_fresh_project_with_archive_schedule_enqueues_archive_job(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    schedule = {"enabled": True, "interval_seconds": 86400}
    project_id = _seed_active_project(
        migrated_factory,
        settings_no_autostart,
        archive_schedule=schedule,
        name="arch-proj",
    )

    clock = FixedClock(datetime(2024, 3, 1, 12, 0, 0, tzinfo=UTC))
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=FakeEncoder())
    scheduler = RenderScheduler(
        settings_no_autostart, migrated_factory, queue, clock=clock
    )

    await scheduler.run_once()

    jobs = _pending_jobs(migrated_factory, project_id, "archive")
    assert len(jobs) == 1


async def test_no_schedule_does_not_enqueue_any_jobs(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    # Project with no schedules configured.
    project_id = _seed_active_project(
        migrated_factory, settings_no_autostart, name="no-sched-proj"
    )

    clock = FixedClock(datetime(2024, 3, 1, 12, 0, 0, tzinfo=UTC))
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=FakeEncoder())
    scheduler = RenderScheduler(
        settings_no_autostart, migrated_factory, queue, clock=clock
    )

    enqueued = await scheduler.run_once()

    assert enqueued == []
    jobs = _all_jobs(migrated_factory, project_id, "scheduled")
    assert jobs == []


async def test_disabled_schedule_does_not_enqueue(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    schedule = {"enabled": False, "interval_seconds": 3600}
    _seed_active_project(
        migrated_factory,
        settings_no_autostart,
        render_schedule=schedule,
        name="disabled-proj",
    )

    clock = FixedClock(datetime(2024, 3, 1, 12, 0, 0, tzinfo=UTC))
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=FakeEncoder())
    scheduler = RenderScheduler(
        settings_no_autostart, migrated_factory, queue, clock=clock
    )

    enqueued = await scheduler.run_once()

    assert enqueued == []


# ---------------------------------------------------------------------------
# Test: interval not yet elapsed → not re-enqueued
# ---------------------------------------------------------------------------


async def test_job_not_re_enqueued_before_interval_elapsed(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """A recently-enqueued job keeps the scheduler quiet until the interval passes."""
    interval_seconds = 3600
    schedule = {"enabled": True, "interval_seconds": interval_seconds}
    project_id = _seed_active_project(
        migrated_factory,
        settings_no_autostart,
        render_schedule=schedule,
        name="interval-proj",
    )

    # Insert a completed job 10 seconds ago (well within the 3600-second interval).
    recent_time = datetime(2024, 3, 1, 11, 59, 50, tzinfo=UTC)
    with session_scope(migrated_factory) as session:
        job = RenderJob(
            project_id=project_id,
            kind="scheduled",
            status="done",
            completed_at=recent_time.replace(tzinfo=None),
        )
        session.add(job)
        session.flush()
        # Override created_at to match recent_time.
        job.created_at = recent_time.replace(tzinfo=None)

    clock = FixedClock(datetime(2024, 3, 1, 12, 0, 0, tzinfo=UTC))  # 10s after the job
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=FakeEncoder())
    scheduler = RenderScheduler(
        settings_no_autostart, migrated_factory, queue, clock=clock
    )

    await scheduler.run_once()

    # No new job should be enqueued — the interval hasn't elapsed.
    new_pending = _pending_jobs(migrated_factory, project_id, "scheduled")
    assert len(new_pending) == 0


async def test_job_enqueued_after_interval_elapsed(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    interval_seconds = 3600
    schedule = {"enabled": True, "interval_seconds": interval_seconds}
    project_id = _seed_active_project(
        migrated_factory,
        settings_no_autostart,
        render_schedule=schedule,
        name="elapsed-proj",
    )

    # Insert a done job exactly ``interval_seconds + 1`` ago.
    old_time = datetime(2024, 3, 1, 11, 0, 0, tzinfo=UTC)  # 3601s before clock
    with session_scope(migrated_factory) as session:
        job = RenderJob(
            project_id=project_id,
            kind="scheduled",
            status="done",
            completed_at=old_time.replace(tzinfo=None),
        )
        session.add(job)
        session.flush()
        job.created_at = old_time.replace(tzinfo=None)

    # Clock is set 3601 seconds after the last job.
    clock = FixedClock(datetime(2024, 3, 1, 12, 0, 1, tzinfo=UTC))
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=FakeEncoder())
    scheduler = RenderScheduler(
        settings_no_autostart, migrated_factory, queue, clock=clock
    )

    await scheduler.run_once()

    new_pending = _pending_jobs(migrated_factory, project_id, "scheduled")
    assert len(new_pending) == 1


# ---------------------------------------------------------------------------
# Test: double-enqueue guard — non-terminal job prevents re-enqueue
# ---------------------------------------------------------------------------


async def test_non_terminal_job_prevents_re_enqueue(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """While a pending/encoding job exists, the scheduler skips re-enqueue."""
    schedule = {"enabled": True, "interval_seconds": 3600}
    project_id = _seed_active_project(
        migrated_factory,
        settings_no_autostart,
        render_schedule=schedule,
        name="guard-proj",
    )

    # Manually insert a pending scheduled job (simulates a prior run_once).
    with session_scope(migrated_factory) as session:
        existing = RenderJob(
            project_id=project_id,
            kind="scheduled",
            status="pending",
        )
        session.add(existing)
        session.flush()

    clock = FixedClock(datetime(2024, 3, 1, 12, 0, 0, tzinfo=UTC))
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=FakeEncoder())
    scheduler = RenderScheduler(
        settings_no_autostart, migrated_factory, queue, clock=clock
    )

    await scheduler.run_once()

    # No additional job should have been enqueued.
    all_pending = _pending_jobs(migrated_factory, project_id, "scheduled")
    assert len(all_pending) == 1  # only the pre-existing one


# ---------------------------------------------------------------------------
# Test: per-schedule output_settings/overlay_config carried onto the job
# ---------------------------------------------------------------------------


async def test_schedule_output_settings_copied_onto_job(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """A schedule's output_settings/overlay_config land on the enqueued job."""
    output_settings = {"width": 1280, "height": 720, "fps": 24, "codec": "h265"}
    overlay_config = {"timestamp": True}
    schedule = {
        "enabled": True,
        "interval_seconds": 3600,
        "output_settings": output_settings,
        "overlay_config": overlay_config,
    }
    project_id = _seed_active_project(
        migrated_factory,
        settings_no_autostart,
        render_schedule=schedule,
        name="output-settings-proj",
    )

    clock = FixedClock(datetime(2024, 3, 1, 12, 0, 0, tzinfo=UTC))
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=FakeEncoder())
    scheduler = RenderScheduler(
        settings_no_autostart, migrated_factory, queue, clock=clock
    )

    await scheduler.run_once()

    jobs = _all_jobs(migrated_factory, project_id, "scheduled")
    assert len(jobs) == 1
    assert jobs[0].output_settings == output_settings
    assert jobs[0].overlay_config == overlay_config


async def test_schedule_without_output_settings_leaves_job_defaults(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """Absent output_settings keeps the job unset so spec-builder defaults apply."""
    schedule = {"enabled": True, "interval_seconds": 3600}
    project_id = _seed_active_project(
        migrated_factory,
        settings_no_autostart,
        render_schedule=schedule,
        name="no-output-settings-proj",
    )

    clock = FixedClock(datetime(2024, 3, 1, 12, 0, 0, tzinfo=UTC))
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=FakeEncoder())
    scheduler = RenderScheduler(
        settings_no_autostart, migrated_factory, queue, clock=clock
    )

    await scheduler.run_once()

    jobs = _all_jobs(migrated_factory, project_id, "scheduled")
    assert len(jobs) == 1
    assert jobs[0].output_settings is None
    assert jobs[0].overlay_config is None


async def test_flat_render_settings_translated_onto_job(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """A flat render schedule's encode choices become the job's output_settings."""
    schedule = {
        "enabled": True,
        "interval_seconds": 3600,
        "encoder": "libx265",
        "container": "mkv",
        "fps": 30,
        "resolution": "1280x720",
    }
    project_id = _seed_active_project(
        migrated_factory,
        settings_no_autostart,
        render_schedule=schedule,
        name="flat-settings-proj",
    )

    clock = FixedClock(datetime(2024, 3, 1, 12, 0, 0, tzinfo=UTC))
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=FakeEncoder())
    scheduler = RenderScheduler(
        settings_no_autostart, migrated_factory, queue, clock=clock
    )

    await scheduler.run_once()

    jobs = _all_jobs(migrated_factory, project_id, "scheduled")
    assert len(jobs) == 1
    assert jobs[0].output_settings == {
        "fps": 30,
        "codec": "libx265",
        "container": "mkv",
        "width": 1280,
        "height": 720,
    }


async def test_flat_source_resolution_omits_dimensions_on_job(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """A "source" resolution leaves width/height off the job's output_settings."""
    schedule = {
        "enabled": True,
        "interval_seconds": 3600,
        "encoder": "libx264",
        "container": "mp4",
        "fps": 24,
        "resolution": "source",
    }
    project_id = _seed_active_project(
        migrated_factory,
        settings_no_autostart,
        render_schedule=schedule,
        name="flat-source-proj",
    )

    clock = FixedClock(datetime(2024, 3, 1, 12, 0, 0, tzinfo=UTC))
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=FakeEncoder())
    scheduler = RenderScheduler(
        settings_no_autostart, migrated_factory, queue, clock=clock
    )

    await scheduler.run_once()

    jobs = _all_jobs(migrated_factory, project_id, "scheduled")
    assert len(jobs) == 1
    out = jobs[0].output_settings
    assert out is not None
    assert "width" not in out
    assert "height" not in out


# ---------------------------------------------------------------------------
# Test: manual render still works while scheduler is quiet
# ---------------------------------------------------------------------------


async def test_manual_render_enqueued_independently_of_scheduler(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    project_id = _seed_active_project(
        migrated_factory, settings_no_autostart, name="manual-sched-proj"
    )

    clock = FixedClock(datetime(2024, 3, 1, 12, 0, 0, tzinfo=UTC))
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=FakeEncoder())
    scheduler = RenderScheduler(
        settings_no_autostart, migrated_factory, queue, clock=clock
    )

    # Scheduler runs once and enqueues nothing (no schedules configured).
    enqueued = await scheduler.run_once()
    assert enqueued == []

    # Manually insert a job.
    with session_scope(migrated_factory) as session:
        manual_job = RenderJob(
            project_id=project_id,
            kind="manual",
            status="pending",
        )
        session.add(manual_job)
        session.flush()
        manual_job_id = manual_job.id

    # The manual job should be visible and pending.
    with session_scope(migrated_factory) as session:
        j = session.get(RenderJob, manual_job_id)
        assert j is not None
        assert j.status == "pending"
