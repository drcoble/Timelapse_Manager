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

Query cost
----------
A pass reads the active projects' id and two schedule columns, then answers
both questions for every project with a live schedule in **one** grouped query
(#66 C10): the newest ``created_at`` per ``(project, kind)`` comes from the
covering ``ix_render_job_project_kind_created`` and the in-flight flag from the
partial ``ix_render_job_active``, so neither grows with the job history. A
pass where no project has a live schedule issues no ``render_job`` read at
all.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any, NamedTuple

from sqlalchemy import (
    Engine,
    Select,
    func,
    literal_column,
    null,
    select,
    union_all,
)
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..db.engine import checkpoint_wal_passive
from ..db.models import Project, RenderJob
from ..db.models.render_job import active_job_filter
from ..db.session import session_scope
from .fps_suggest import resolve_auto_fps
from .queue import Clock, RenderQueue
from .settings import fps_is_auto, output_settings_from_schedule

logger = logging.getLogger(__name__)

# The job kinds the scheduler enqueues, in evaluation order per project.
_SCHEDULED_KINDS = ("scheduled", "archive")

# How often the housekeeping pass (report, event and thumbnail retention, WAL
# checkpoint) runs.
_HOUSEKEEPING_INTERVAL_SECONDS = 3600.0


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
        self._last_housekeeping: float | None = None

    async def start(self) -> None:
        """Launch the periodic scheduling loop. Idempotent."""
        if self._started:
            return
        self._started = True
        self._task = asyncio.create_task(self._loop(), name="render-scheduler")
        logger.info("render scheduler started")

    def is_running(self) -> bool:
        """Return True when the scheduling loop is started, not stopped, and alive.

        A pure, non-raising predicate for the liveness probe: True only when
        ``start`` ran, ``stop`` has not, and the loop task exists and has not
        finished (a crashed loop leaves a done task).
        """
        return (
            self._started
            and not self._stopped
            and self._task is not None
            and not self._task.done()
        )

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
            await self.run_housekeeping()
            await self._clock.sleep(interval)

    async def run_housekeeping(self, *, force: bool = False) -> None:
        """Run the periodic maintenance steps, at most once an hour.

        The steps are problem-report retention (#8), opt-in event retention
        (#66, off by default), thumbnail-cache cleanup (#67 R12), the purge of
        long-dead session rows (#67 R12), the deletion of exports past their TTL
        (#67 P7) and a passive WAL checkpoint (#66). The scheduler
        is the process's one periodic maintenance loop, so retention for stored
        artefacts hangs off it rather than adding a loop of its own. The first
        pass after start runs immediately. Each step runs in its own ``try``, so
        one failing step is logged and neither stops the others nor disturbs
        scheduling.
        """
        now = time.monotonic()
        if (
            not force
            and self._last_housekeeping is not None
            and now - self._last_housekeeping < _HOUSEKEEPING_INTERVAL_SECONDS
        ):
            return
        self._last_housekeeping = now
        try:
            await asyncio.to_thread(self._prune_reports)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - housekeeping must not kill the loop
            logger.exception("problem-report retention pass failed")
        try:
            await asyncio.to_thread(self._prune_events)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - housekeeping must not kill the loop
            logger.exception("event retention pass failed")
        try:
            await asyncio.to_thread(self._prune_thumbnails)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - housekeeping must not kill the loop
            logger.exception("thumbnail cache pass failed")
        try:
            await asyncio.to_thread(self._purge_sessions)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - housekeeping must not kill the loop
            logger.exception("stale session purge failed")
        try:
            await asyncio.to_thread(self._purge_exports)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - housekeeping must not kill the loop
            logger.exception("export retention pass failed")
        # Last, so the checkpoint can fold in the pass's own deletes.
        try:
            await asyncio.to_thread(self._checkpoint_wal)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - housekeeping must not kill the loop
            logger.exception("WAL checkpoint failed")

    def _prune_reports(self) -> None:
        from ..reports import prune_reports
        from ..reports.store import utcnow

        with session_scope(self._session_factory) as session:
            prune_reports(session, self._settings, utcnow())

    def _prune_thumbnails(self) -> None:
        """Remove orphaned thumbnail caches and stale temp files. Synchronous."""
        from ..storage.thumbnail_cache import prune_thumbnail_cache

        def live_project_ids() -> set[int]:
            with session_scope(self._session_factory) as session:
                return set(session.scalars(select(Project.id)))

        prune_thumbnail_cache(self._settings, live_project_ids, now=time.time())

    def _prune_events(self) -> int:
        """Apply opt-in event retention; a no-op while it is off. Synchronous."""
        from ..monitoring.event_retention import prune_events

        now = self._clock.now().astimezone(UTC).replace(tzinfo=None)
        return prune_events(
            self._session_factory, self._settings.monitoring.event_retention, now
        )

    def _purge_sessions(self) -> int:
        """Delete long-revoked and long-expired session rows. Synchronous."""
        from ..security.sessions import purge_stale_sessions

        now = self._clock.now().astimezone(UTC).replace(tzinfo=None)
        return purge_stale_sessions(self._session_factory, now=now)

    def _purge_exports(self) -> int:
        """Delete finished exports past ``render.export_ttl_days``. Synchronous."""
        from ..services.renders import purge_expired_exports

        now = self._clock.now().astimezone(UTC).replace(tzinfo=None)
        return purge_expired_exports(self._session_factory, self._settings, now)

    def _checkpoint_wal(self) -> None:
        """Run a passive WAL checkpoint, so the ``-wal`` file can shrink back.

        Passive: it never waits on a reader or writer. Synchronous.
        """
        bind = self._session_factory.kw.get("bind")
        if isinstance(bind, Engine):
            checkpoint_wal_passive(bind)

    def _enqueue_due_jobs(self, now: datetime) -> list[int]:
        """Enqueue due scheduled/archive jobs for all active projects. Synchronous.

        Reads only the project columns the decision needs, then the job history
        of every ``(project, kind)`` with a live schedule in one grouped query
        (:func:`_job_history`). Projects are evaluated in id order, the
        scheduled kind before the archive kind. A job inserted during the pass
        cannot change another pair's decision (each pair is evaluated once), so
        the history read before the inserts is exact.
        """
        created: list[int] = []
        with session_scope(self._session_factory) as session:
            rows = session.execute(
                select(Project.id, Project.render_schedule, Project.archive_schedule)
                .where(Project.lifecycle_state == "active")
                .order_by(Project.id)
            ).all()
            live: list[tuple[int, str, float, dict[str, Any] | None]] = []
            for project_id, render_schedule, archive_schedule in rows:
                for kind, schedule in zip(
                    _SCHEDULED_KINDS, (render_schedule, archive_schedule), strict=True
                ):
                    interval = _interval_seconds(schedule)
                    if interval is not None:
                        live.append((project_id, kind, interval, schedule))
            if not live:
                return created
            history = _job_history(session, {project_id for project_id, *_ in live})
            for project_id, kind, interval, schedule in live:
                if _is_due(history.get((project_id, kind)), interval, now):
                    created.append(
                        self._create_job(session, project_id, kind, schedule)
                    )
        return created

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
        render honours them (an ``"auto"`` fps is resolved from the frames). A
        legacy schedule that nests an ``output_settings`` dict instead is passed
        through unchanged. An ``overlay_config`` dict, if present, is copied as-is.
        When neither is derivable both are left unset and the spec builder
        applies the project/encoder defaults.
        """
        output_settings = output_settings_from_schedule(schedule)
        if output_settings is not None and fps_is_auto(schedule):
            # "Auto (suggested)" (#301): resolved now, from the frames the
            # render will use, and stored as a plain number on the job.
            output_settings["fps"] = resolve_auto_fps(
                session, project_id, self._settings.render.default_fps
            )
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


class _JobHistory(NamedTuple):
    """What the due check needs to know about one ``(project, kind)``."""

    # When the newest job of the kind was created (naive UTC as stored),
    # whatever its status. ``None`` only on a pair outside the requested
    # projects, which the in-flight arm can return and the caller never reads.
    last_created_at: datetime | None
    # Whether a ``pending``/``encoding`` job of the kind exists.
    in_flight: bool


def _job_history(
    session: Session, project_ids: set[int]
) -> dict[tuple[int, str], _JobHistory]:
    """Return the scheduler's job history per ``(project, kind)`` in one query.

    Pairs with no job at all are absent. The statement is a ``GROUP BY
    project_id, kind`` over two arms:

    * the newest ``created_at`` of each of the given projects' scheduled and
      archive jobs, read from the covering ``ix_render_job_project_kind_created``
      (migration 037), with an in-flight flag of 0;
    * one row with a flag of 1 per in-flight job of those kinds, read from the
      partial ``ix_render_job_active`` through :func:`active_job_filter`.

    A single grouped aggregate with the flag inline (``max(status IN ...)``)
    would read every historical row's status from the table instead.
    """
    newest: Select[Any] = (
        select(
            RenderJob.project_id.label("project_id"),
            RenderJob.kind.label("kind"),
            func.max(RenderJob.created_at).label("last_created_at"),
            literal_column("0").label("in_flight"),
        )
        .where(
            RenderJob.project_id.in_(sorted(project_ids)),
            RenderJob.kind.in_(_SCHEDULED_KINDS),
        )
        .group_by(RenderJob.project_id, RenderJob.kind)
    )
    live: Select[Any] = select(
        RenderJob.project_id,
        RenderJob.kind,
        null(),
        literal_column("1"),
    ).where(active_job_filter(), RenderJob.kind.in_(_SCHEDULED_KINDS))
    both = union_all(newest, live).subquery("job_history")
    rows = session.execute(
        select(
            both.c.project_id,
            both.c.kind,
            func.max(both.c.last_created_at),
            func.max(both.c.in_flight),
        ).group_by(both.c.project_id, both.c.kind)
    ).all()
    return {
        (int(project_id), str(kind)): _JobHistory(
            last_created_at=last_created_at, in_flight=bool(in_flight)
        )
        for project_id, kind, last_created_at, in_flight in rows
    }


def _is_due(history: _JobHistory | None, interval: float, now: datetime) -> bool:
    """Return whether a job is due, given its pair's history and live interval.

    Not due while a job of the kind is in flight. Otherwise due when there is
    no job of the kind yet, or the newest is at least ``interval`` seconds old.
    ``created_at`` is the cadence anchor, so a job's clock is the moment it was
    enqueued, independent of how long encoding took. A stored naive time is
    UTC.
    """
    if history is None:
        return True
    if history.in_flight:
        return False
    last_at = history.last_created_at
    if last_at is None:
        return True
    if last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=UTC)
    return (now - last_at).total_seconds() >= interval


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
