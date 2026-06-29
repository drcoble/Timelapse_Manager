"""Periodic render and archive scheduler.

For every active project the scheduler periodically asks two questions:

* is a *scheduled* render due (per the project's ``render_schedule`` cadence)?
* is an *archive* render due (per its ``archive_schedule`` cadence)?

and enqueues a ``RenderJob`` of the matching kind when so. It mirrors the capture
supervisor's reliability shape: one long-lived asyncio task, an injectable clock,
and a bulletproof :meth:`stop`. It enqueues only -- the bounded render worker
drains the queue -- so it never blocks capture or hogs the loop.

Cadence shape
-------------
Both schedules are a small JSON document::

    {"enabled": true, "interval_seconds": 86400}

An absent schedule, ``enabled: false``, or a missing/non-positive interval all
mean "off" (nothing is ever enqueued). Due-ness is derived from the **database**,
not in-memory state: a kind is due when no job of that kind exists for the project
yet, or the newest one is older than the interval. Reading due-ness from the
database makes the scheduler restart-safe (downtime does not reset the clock) and
makes an injected-clock test deterministic.

Double-enqueue guard
--------------------
Before enqueuing, the scheduler checks there is no non-terminal
(``pending``/``encoding``) job of that kind already queued for the project, so a
slow render does not pile up a fresh job every check interval.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..db.models import Project, RenderJob
from ..db.session import session_scope
from .queue import Clock, RenderQueue
from .settings import output_settings_from_schedule

logger = logging.getLogger(__name__)

# Job statuses that count as "still in flight" for the double-enqueue guard.
_NON_TERMINAL_STATUSES = ("pending", "encoding")


class RenderScheduler:
    """Enqueues recurring render and archive jobs on each project's cadence.

    The clock is injectable so a test can advance time deterministically; the
    worker queue is shared so an enqueued job is picked up immediately.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        queue: RenderQueue,
        *,
        clock: Clock | None = None,
    ) -> None:
        """Create the scheduler; performs no I/O and starts no task."""
        self._settings = settings
        self._session_factory = session_factory
        self._queue = queue
        self._clock = clock if clock is not None else Clock()
        self._task: asyncio.Task[None] | None = None
        self._started = False
        self._stopped = False

    async def start(self) -> None:
        """Launch the periodic scheduling loop. Idempotent."""
        if self._started:
            return
        self._started = True
        self._task = asyncio.create_task(self._loop(), name="render-scheduler")
        logger.info("render scheduler started")

    async def stop(self) -> None:
        """Cancel the scheduling loop and await it. Idempotent and bulletproof."""
        if self._stopped:
            return
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        logger.info("render scheduler stopped")

    async def run_once(self) -> list[int]:
        """Evaluate every project once and enqueue any due jobs; return their ids.

        Exposed (and used by the loop) so a test can drive a single deterministic
        scheduling pass with an injected clock rather than waiting on the timer.
        """
        now = self._clock.now()
        enqueued = await asyncio.to_thread(self._enqueue_due_jobs, now)
        if enqueued:
            self._queue.notify()
        return enqueued

    async def _loop(self) -> None:
        """Re-evaluate every project on the configured check interval."""
        interval = max(1.0, self._settings.render.scheduler_check_interval_seconds)
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad pass must not kill the loop
                logger.exception("render scheduling pass failed")
            await self._clock.sleep(interval)

    def _enqueue_due_jobs(self, now: datetime) -> list[int]:
        """Enqueue due scheduled/archive jobs for all active projects. Synchronous."""
        created: list[int] = []
        with session_scope(self._session_factory) as session:
            projects = (
                session.execute(
                    select(Project).where(Project.lifecycle_state == "active")
                )
                .scalars()
                .all()
            )
            for project in projects:
                created += self._enqueue_for_project(session, project, now)
        return created

    def _enqueue_for_project(
        self, session: Session, project: Project, now: datetime
    ) -> list[int]:
        """Enqueue a scheduled and/or archive job for one project if due."""
        created: list[int] = []
        if self._is_due(session, project, "scheduled", project.render_schedule, now):
            created.append(
                self._create_job(
                    session, project.id, "scheduled", project.render_schedule
                )
            )
        if self._is_due(session, project, "archive", project.archive_schedule, now):
            created.append(
                self._create_job(
                    session, project.id, "archive", project.archive_schedule
                )
            )
        return created

    def _is_due(
        self,
        session: Session,
        project: Project,
        kind: str,
        schedule: dict[str, Any] | None,
        now: datetime,
    ) -> bool:
        """Return whether a job of ``kind`` is due for the project.

        Off when the schedule is absent, disabled, or has no positive interval.
        Otherwise due when there is no non-terminal job of this kind already
        queued *and* the newest job of this kind is missing or older than the
        interval. Due-ness is read from the database so it survives restarts.
        """
        interval = _interval_seconds(schedule)
        if interval is None:
            return False
        if self._has_non_terminal(session, project.id, kind):
            return False
        last_at = self._last_job_time(session, project.id, kind)
        if last_at is None:
            return True
        return (now - last_at).total_seconds() >= interval

    def _has_non_terminal(self, session: Session, project_id: int, kind: str) -> bool:
        """Return whether a pending/encoding job of ``kind`` already exists."""
        existing = session.execute(
            select(RenderJob.id)
            .where(RenderJob.project_id == project_id)
            .where(RenderJob.kind == kind)
            .where(RenderJob.status.in_(_NON_TERMINAL_STATUSES))
            .limit(1)
        ).first()
        return existing is not None

    def _last_job_time(
        self, session: Session, project_id: int, kind: str
    ) -> datetime | None:
        """Return when the newest job of ``kind`` was created, as aware UTC.

        Uses ``created_at`` so a job's clock is the moment it was enqueued, giving
        a stable cadence anchor independent of how long encoding took.
        """
        row = session.execute(
            select(RenderJob.created_at)
            .where(RenderJob.project_id == project_id)
            .where(RenderJob.kind == kind)
            .order_by(RenderJob.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        return row if row.tzinfo is not None else row.replace(tzinfo=UTC)

    def _create_job(
        self,
        session: Session,
        project_id: int,
        kind: str,
        schedule: dict[str, Any] | None,
    ) -> int:
        """Insert a pending job of ``kind`` for the project and return its id.

        The schedule's enumerated encode choices (encoder/container/fps/
        resolution) are translated into the job's ``output_settings`` so the
        render honours them; a legacy schedule that nests an ``output_settings``
        dict instead is passed through unchanged. An ``overlay_config`` dict, if
        present, is copied as-is. When neither is derivable both are left unset
        and the spec builder applies the project/encoder defaults.
        """
        output_settings = output_settings_from_schedule(schedule)
        overlay_config = None
        if isinstance(schedule, dict):
            raw_overlay = schedule.get("overlay_config")
            if isinstance(raw_overlay, dict):
                overlay_config = raw_overlay
        job = RenderJob(
            project_id=project_id,
            kind=kind,
            status="pending",
            output_settings=output_settings,
            overlay_config=overlay_config,
        )
        session.add(job)
        session.flush()
        logger.info("enqueued %s render job=%s project=%s", kind, job.id, project_id)
        return job.id


def _interval_seconds(schedule: dict[str, Any] | None) -> float | None:
    """Return a schedule's positive interval in seconds, or ``None`` if off.

    ``None``/missing, ``enabled: false``, or a non-positive/invalid interval all
    read as "off".
    """
    if not schedule or not isinstance(schedule, dict):
        return None
    if not schedule.get("enabled", False):
        return None
    raw = schedule.get("interval_seconds")
    if raw is None:
        return None
    try:
        interval = float(raw)
    except (TypeError, ValueError):
        return None
    return interval if interval > 0 else None
