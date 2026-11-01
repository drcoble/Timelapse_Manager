"""Bounded background render worker.

The worker drains a queue of pending render jobs, runs each through the
:class:`~timelapse_manager.encode.Encoder`, and records the outcome. It mirrors
the capture supervisor's reliability shape: a single long-lived asyncio task,
bounded concurrency so renders never starve capture, an injectable clock, and a
bulletproof :meth:`stop`.

Design points the rest of the system relies on:

* **Bounded concurrency.** At most ``max_concurrent`` renders run at once; a
  :class:`asyncio.Semaphore` gates job tasks so extra pending jobs simply wait.
  Renders are subprocess-bound, so they do not hog the event loop, but the cap
  still bounds resource use and protects capture.
* **Atomic claim.** A job is claimed by a single ``pending -> encoding`` flip in
  one transaction *before* its task starts. This closes the race with a
  concurrent cancel: a cancel of a still-``pending`` job flips it straight to
  ``failed``; the worker's claim then finds the row no longer ``pending`` and
  skips it, so a job is never both cancelled and run.
* **Cancellation kills the child.** Each in-flight render runs in its own task
  that awaits :meth:`Encoder.render` directly -- never shielded, never wrapped in
  a thread. Cancelling that task propagates :class:`asyncio.CancelledError` into
  the encoder, which kills the ffmpeg child and removes the partial output. The
  cancel path then records ``failed`` before re-raising.
* **No database work on the event loop (#62).** Every terminal write -- the
  cancel, low-disk, spec and failure paths as well as the result -- runs in a
  worker thread, so a write waiting on SQLite's lock (up to the busy timeout)
  never freezes the server. The terminal writes are shielded and awaited to
  completion even if a further cancel arrives meanwhile (:meth:`_settle`), so a
  cancelled job is ``failed`` in the database by the time its task ends.
* **A user's cancel is not a failure.** A cancel a user asked for
  (:meth:`cancel_job` with ``user_requested=True``) still ends the job
  ``failed`` -- the status enum has no ``cancelled`` -- but writes no
  ``render.failed`` error event: the service records who cancelled it, at
  ``info``, below the alert threshold. A render cut off any other way (the
  worker stopping under it) is still an error.
* **Codec probe.** A finished render is probed with ffprobe (10 s ceiling)
  before its ``done`` row is written, so the row carries the RFC 6381 codec
  string and MIME type the page asks the viewer's browser about. A failed probe
  stores ``NULL`` and logs a warning; it never fails the render. Renders that
  predate the probe are backfilled once, one at a time, shortly after
  :meth:`start` (decision D14).
* **Durable terminal state (#67 R1).** Every terminal write is retried on
  SQLite's "database is locked" (:func:`run_with_write_retry`) and reports
  whether it landed. :meth:`_settle` keeps retrying a write that did not land,
  with backoff, until it does or :meth:`stop` begins, and the job keeps its
  concurrency slot and its ``_job_tasks`` entry meanwhile: a job row is never
  left ``encoding`` while the service runs, and no new job is claimed while a
  row cannot be written.
* **Claim and registration are one step.** A job is claimed and its task
  registered under :attr:`_claim_lock` (the slot is taken first, outside the
  lock), so an ``encoding`` row whose id is not in ``_job_tasks`` -- read under
  the same lock -- really has no task. The drain loop sweeps such orphans to
  ``failed`` on every cycle, and :meth:`cancel_job` settles one directly, so a
  stranded row never needs a restart.
* **Long jobs on their own executor (#66 C3).** Render preparation, the export
  zip, the free-space and output-file probes, the codec backfill's candidate
  scan and the post-render copies and prunes run on the injected jobs executor
  (:func:`~timelapse_manager.executors.run_in`), so a long export neither
  starves capture on the loop's default executor nor holds a request thread.
  The short claim, sweep and terminal writes stay on the default executor: a
  terminal write must never queue behind an export. With no executor injected
  (tests), the long jobs fall back to the default executor too.
* **Bulletproof stop.** :meth:`stop` cancels the drain loop and every in-flight
  job task, then awaits them all with ``return_exceptions=True`` so nothing leaks
  onto a closing event loop. Idempotent and safe with zero tasks.

The status enum on a job is fixed (``pending``/``encoding``/``done``/``failed``);
this module is the single place those transitions are written.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import Executor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..db.engine import run_with_write_retry
from ..db.models import Project, RenderJob
from ..db.models.render_job import active_job_filter
from ..db.session import session_scope
from ..encode import Encoder, RenderResult, RenderSpec, build_encoder
from ..encode.ffmpeg_impl import render_time_ceiling
from ..encode.media_probe import MediaInfo, probe_media
from ..executors import run_in
from ..ffmpeg_pin import FfmpegResolutionError, resolve_ffmpeg_binary
from ..monitoring.events import EventType, log_event
from . import export, post_actions
from .outputs import unlink_confined
from .spec import SpecBuildError, build_render_spec, project_render_root

logger = logging.getLogger(__name__)

# Probes a finished render file for its codec string and MIME type; ``None``
# when the probe failed. Injectable so tests never shell out to ffprobe.
MediaProber = Callable[[Path], Awaitable[MediaInfo | None]]

# How long after start the codec backfill waits before its first probe, so it
# does not compete with startup work. Real seconds (not the injectable clock):
# tests drive :meth:`RenderQueue.backfill_codecs` directly instead.
_BACKFILL_START_DELAY_SECONDS = 30.0

# Retry budget for one attempt of a terminal write (#67 R1): about 6 s of
# backoff on "database is locked", on top of each try's own busy timeout. A
# write still failing after that is retried again by :meth:`RenderQueue._settle`.
_TERMINAL_WRITE_ATTEMPTS = 8
_TERMINAL_WRITE_BASE_DELAY = 0.05
_TERMINAL_WRITE_MAX_DELAY = 2.0

# Backoff between the settle loop's attempts at a terminal write that did not
# land: 1, 2, 4 ... seconds, capped here. On the injectable clock.
_SETTLE_BACKOFF_MAX_SECONDS = 30.0

# The reason recorded for an ``encoding`` row with no live task.
# A shutdown requeues an interrupted render this many times (#178): once.
_MAX_SHUTDOWN_REQUEUES = 1

ORPHAN_REASON = "orphaned (no live task)"

# Event type of the warning written when a terminal write first fails. Not in
# :class:`EventType` (no routing rule needs it); dotted like every other type.
STATE_WRITE_FAILED_EVENT = "render.state_write_failed"


@dataclass(frozen=True)
class RenderActivity:
    """What the worker is doing right now, for the upgrade gate (#67 O1).

    ``live_tasks`` counts jobs with a running task in this process;
    ``pending_rows``/``encoding_rows`` count rows in those states; ``orphans``
    is the ``encoding`` rows with no live task. An upgrade is safe when
    ``live_tasks + pending_rows`` is zero: an orphan is already dead.
    """

    live_tasks: int
    pending_rows: int
    encoding_rows: int
    orphans: int

    def as_dict(self) -> dict[str, int]:
        """Return the activity as the endpoint's JSON body."""
        return {
            "live_tasks": self.live_tasks,
            "pending_rows": self.pending_rows,
            "encoding_rows": self.encoding_rows,
            "orphans": self.orphans,
        }


class Clock:
    """The minimal time surface the worker depends on.

    Abstracted so a test can drive the worker without real waits: ``now`` is the
    current aware-UTC instant and ``sleep`` is a cancellable async sleep.
    """

    def now(self) -> datetime:
        """Return the current aware-UTC instant."""
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        """Sleep for ``seconds`` on the event loop."""
        await asyncio.sleep(seconds)


class _ExportGate:
    """Decides, once, whether a queued export thread runs or was cancelled (#101).

    An export is handed to the jobs executor, whose few workers may be busy: the
    thread can wait in the executor's queue for a long time, and a thread cannot
    be cancelled once handed over. The thread calls :meth:`start` as its first
    act; :meth:`cancel_if_queued` is the cancel's side. Under one lock exactly one
    wins: a cancel that gets there first makes :meth:`start` return ``False`` (the
    thread then exits at once), and a thread that got there first makes the
    cancel report ``False`` (the cancel must wait for the running thread).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = False
        self._cancelled = False

    def start(self) -> bool:
        """Mark the thread started; return ``False`` when it was cancelled first."""
        with self._lock:
            if self._cancelled:
                return False
            self._started = True
            return True

    def cancel_if_queued(self) -> bool:
        """Cancel the thread when it has not started; return whether it did."""
        with self._lock:
            if self._started:
                return False
            self._cancelled = True
            return True


class RenderQueue:
    """Drains pending render jobs through an encoder with bounded concurrency.

    The encoder and clock are injectable so tests can substitute a fake/slow
    encoder (to exercise concurrency and cancellation) and drive timing.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        *,
        encoder: Encoder | None = None,
        clock: Clock | None = None,
        prober: MediaProber | None = None,
        jobs_executor: Executor | None = None,
    ) -> None:
        """Create the worker; performs no I/O and starts no tasks.

        :param settings: resolved application settings (the ``render`` section
            and paths).
        :param session_factory: factory for synchronous ORM sessions.
        :param encoder: the encoder used for every render; defaults to a
            :class:`FfmpegEncoder` built with the configured font path and the
            ffmpeg binary resolved for this environment (bundled when frozen, an
            explicit knob when set, else ``ffmpeg`` on ``PATH``).
        :param clock: time source; defaults to real UTC time + ``asyncio.sleep``.
        :param prober: probes a finished render for its codec string and MIME
            type; defaults to ffprobe resolved beside the configured ffmpeg.
        :param jobs_executor: the executor long jobs run on (preparation,
            exports, file probes, the backfill scan, post-render actions; #66
            C3). ``None`` runs them on the loop's default executor.
        """
        self._settings = settings
        self._session_factory = session_factory
        self._encoder: Encoder = (
            encoder
            if encoder is not None
            else build_encoder(
                settings.render.encoder_engine,
                ffmpeg_binary=resolve_ffmpeg_binary(settings),
                font_path=settings.render.font_path,
                hwaccel_enabled=settings.render.hwaccel_enabled,
                hwaccel_api=settings.render.hwaccel_api,
                hwaccel_device=settings.render.hwaccel_device,
                stall_timeout_seconds=settings.render.stall_timeout_seconds,
                max_job_seconds=settings.render.max_job_seconds,
            )
        )
        self._clock = clock if clock is not None else Clock()
        self._jobs_executor = jobs_executor
        self._prober: MediaProber = (
            prober if prober is not None else self._probe_with_ffprobe
        )
        # The one-shot codec backfill (D14). Kept apart from ``_job_tasks`` so it
        # never counts as in-flight work for ``wait_idle``.
        self._backfill_task: asyncio.Task[None] | None = None
        self._semaphore = asyncio.Semaphore(max(1, settings.render.max_concurrent))
        # Exports have a slot of their own (#67 P7), so a long export never holds
        # the render slot and a render never waits behind a zip.
        self._export_semaphore = asyncio.Semaphore(
            max(1, settings.render.max_concurrent_exports)
        )
        # The semaphore each in-flight job holds, released by ``_finish``.
        self._job_semaphores: dict[int, asyncio.Semaphore] = {}
        self._wakeup = asyncio.Event()
        # Set by the drain loop whenever it parks with no pending and no in-flight
        # work, cleared as soon as work arrives. Lets a caller await a settled
        # queue (every enqueued job reached a terminal state) without polling the
        # database -- see :meth:`wait_idle`.
        self._idle = asyncio.Event()
        # The loop :meth:`start` ran on. ``asyncio.Event`` is not thread-safe, so
        # :meth:`notify` marshals onto this loop when called from any other
        # thread (#66 L5). ``None`` until started.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self._job_tasks: dict[int, asyncio.Task[None]] = {}
        # The worker-thread future of each in-flight export. A thread cannot be
        # cancelled, so :meth:`cancel_job` awaits it after cancelling the job's
        # task: the caller then knows nothing is still writing the zip.
        self._export_threads: dict[int, asyncio.Future[RenderResult]] = {}
        # The start gate of each export handed to the jobs executor (#101). Its
        # thread may sit in the executor's queue behind other long jobs; the gate
        # lets a cancel stop it before it starts instead of waiting for it.
        self._export_gates: dict[int, _ExportGate] = {}
        # Jobs whose task is being cancelled because a user asked: their cancel
        # handler settles them without a failure event (see cancel_job).
        self._user_cancels: set[int] = set()
        # Held while a job is claimed and its task registered, and while anyone
        # decides that an ``encoding`` row has no task (the orphan sweep, a cancel,
        # the activity read). Never held across a render or a slot wait.
        self._claim_lock = asyncio.Lock()
        # In-memory counters for the readiness probe (#67 R5): orphans this
        # process has swept, and the oldest ``started_at`` among the ``encoding``
        # rows at the last sweep (naive UTC, ``None`` when there were none),
        # and the orphans the last sweep found but could not fail.
        self._orphans_reclaimed_total = 0
        self._unreclaimed_orphans = 0
        # ``(job id, started_at)`` of every ``encoding`` row at the last sweep,
        # replaced whole each sweep so a reader on another thread never sees it
        # half-built; and each prepared job's watchdog budget in seconds, kept
        # until the job finishes (#67 R5).
        self._encoding_rows: tuple[tuple[int, datetime | None], ...] = ()
        self._job_budget_seconds: dict[int, float] = {}
        self._oldest_encoding_started_at: datetime | None = None
        self._started = False
        self._stopped = False
        # ``time.monotonic()`` when :meth:`stop` began, ``None`` before. A job the
        # stop interrupts is requeued (#178) only if its cancel handler runs
        # within ``server.shutdown_grace_seconds`` of this.
        self._stop_began_at: float | None = None

    @property
    def encoder(self) -> Encoder:
        """The encoder this queue drives (the trigger endpoint validates through it)."""
        return self._encoder

    async def start(self) -> None:
        """Launch the drain loop. Idempotent.

        Before the loop starts, any job left in ``encoding`` from a previous run
        is swept to ``failed``. Such a job has no live task -- it was interrupted
        by a crash or by a shutdown that cancelled the drain loop while its claim
        transaction was still committing in a worker thread (``to_thread`` is not
        cancellable, so a ``pending -> encoding`` flip can land just after
        ``stop`` returns). The claim query only selects ``pending`` rows, so
        without this sweep an orphaned ``encoding`` job would never be picked up
        again. Flipping it to ``failed`` (rather than back to ``pending``) matches
        the cancel path's semantics -- an interrupted render is a failed render,
        not one silently retried -- and frees the project for a fresh job.
        """
        if self._started:
            return
        self._started = True
        # Captured before the first await, so a notify that lands while the
        # orphan sweep runs already marshals onto this loop.
        self._loop = asyncio.get_running_loop()
        await asyncio.to_thread(self._reclaim_orphaned_encoding)
        self._loop_task = asyncio.create_task(self._drain_loop(), name="render-worker")
        self._backfill_task = asyncio.create_task(
            self._backfill_after_delay(), name="render-codec-backfill"
        )
        # Pending jobs may already exist (a crash mid-queue, or a job enqueued
        # before start): wake the loop so it claims them immediately.
        self._wakeup.set()
        logger.info("render worker started")

    def notify(self) -> None:
        """Signal the drain loop that a new job may be available.

        Safe to call from any thread. Called by the trigger endpoint, the
        scheduler and post-commit effects (via
        :func:`~timelapse_manager.services._base.run_effects`) after enqueuing a
        job, so the worker picks it up without waiting for its idle re-poll. Sync
        request handlers run in FastAPI's threadpool, and :class:`asyncio.Event`
        is not thread-safe, so the signal is marshalled onto the queue's loop
        (see :meth:`_on_loop`); on that loop it runs at once. Before :meth:`start`
        it only updates the events; once the loop has closed it is dropped.
        """

        def signal() -> None:
            # New work may have arrived: the queue is no longer idle until the
            # drain loop has drained it back down to nothing. Both steps run in
            # one callback, so the loop never sees one without the other.
            self._idle.clear()
            self._wakeup.set()

        self._on_loop(signal)

    def _on_loop(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` on the queue's event loop, from whichever thread calls.

        * Not started: call ``fn`` directly; no loop is waiting on it yet.
        * Already on the queue's loop: call ``fn`` directly (fast path).
        * Another thread: hand it to ``loop.call_soon_threadsafe``, which also
          wakes the loop's selector, so it runs on the next iteration.
        * The loop has closed (a threadpool request finishing during shutdown):
          drop it.
        """
        loop = self._loop
        if loop is None:
            fn()
            return
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            fn()
            return
        if loop.is_closed():
            return
        # The loop can close between the check above and this call.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(fn)

    async def wait_idle(self) -> None:
        """Block until the drain loop has parked with no pending or in-flight jobs.

        Resolves the moment the worker has drained every queued job to a terminal
        state and gone back to sleep with an empty in-flight set. The backing
        event starts unset, so a caller that enqueues jobs and then awaits this
        returns only after those jobs have settled -- a deterministic alternative
        to polling job rows for a terminal status. On a queue with no work it
        resolves as soon as the loop completes its first (no-op) drain cycle.
        """
        await self._idle.wait()

    def is_running(self) -> bool:
        """Return True when the drain loop is started, not stopped, and alive.

        A pure, non-raising predicate for the liveness probe: True only when
        ``start`` ran, ``stop`` has not, and the drain-loop task exists and has
        not finished (a crashed loop leaves a done task).
        """
        return (
            self._started
            and not self._stopped
            and self._loop_task is not None
            and not self._loop_task.done()
        )

    @property
    def orphans_reclaimed_total(self) -> int:
        """How many task-less ``encoding`` rows this process has swept to ``failed``.

        Counts the startup sweep, the drain loop's sweep and orphan cancels.
        """
        return self._orphans_reclaimed_total

    @property
    def unreclaimed_orphans(self) -> int:
        """Orphans the last sweep found and could not fail (#67 R5).

        A sweep that fails every orphan it finds leaves this at zero, so a
        one-off orphan (a render interrupted by a restart) does not keep the
        readiness probe degraded. It stays non-zero only while the failing
        write keeps failing. Read from memory, so a probe never queries for it.
        """
        return self._unreclaimed_orphans

    @property
    def oldest_encoding_started_at(self) -> datetime | None:
        """The oldest ``started_at`` among ``encoding`` rows at the last sweep.

        Naive UTC like the column; ``None`` when no row was ``encoding``. Read
        from memory, so a probe never queries for it.
        """
        return self._oldest_encoding_started_at

    def encoding_overruns(self, now: datetime) -> list[float]:
        """Seconds by which each ``encoding`` job has outlived its budget (#67 R5).

        One entry per ``encoding`` row at the last sweep: its age minus its
        budget, positive once overdue. The budget is the render watchdog's own
        wall-clock ceiling for that job, :func:`render_time_ceiling` over its
        frame count (``max(3600, 2 * frames)`` s by default), so a long render
        that the watchdog still allows is never overdue. Until a job is
        prepared (and for an export, which has no watchdog) its frame count is
        unknown and the ceiling's floor is used. A job with no ``started_at``
        is skipped. Read from memory, so a probe never queries for it.

        :param now: an aware or naive-UTC datetime.
        """
        floor = render_time_ceiling(self._settings.render.max_job_seconds, 0)
        moment = _naive_utc(now)
        budgets = self._job_budget_seconds
        return [
            (moment - started_at).total_seconds() - budgets.get(job_id, floor)
            for job_id, started_at in self._encoding_rows
            if started_at is not None
        ]

    async def activity(self) -> RenderActivity:
        """Count live tasks and ``pending``/``encoding`` rows, consistently.

        The live task ids are read under :attr:`_claim_lock`, and the rows are
        counted in a worker thread while it is still held, so no claim can land
        between the two reads: a row just claimed is never mistaken for an
        orphan. The reads are two small queries, so the lock is held briefly.
        """
        async with self._claim_lock:
            live_ids = frozenset(self._job_tasks)
            pending, encoding_ids = await asyncio.to_thread(self._row_activity_sync)
        orphans = sum(1 for job_id in encoding_ids if job_id not in live_ids)
        return RenderActivity(
            live_tasks=len(live_ids),
            pending_rows=pending,
            encoding_rows=len(encoding_ids),
            orphans=orphans,
        )

    def _row_activity_sync(self) -> tuple[int, list[int]]:
        """``(pending row count, encoding row ids)``. Synchronous."""
        with session_scope(self._session_factory) as session:
            pending = session.execute(
                select(func.count())
                .select_from(RenderJob)
                .where(active_job_filter(), RenderJob.status == "pending")
            ).scalar_one()
            encoding_ids = list(
                session.execute(
                    select(RenderJob.id).where(
                        active_job_filter(), RenderJob.status == "encoding"
                    )
                ).scalars()
            )
        return int(pending or 0), encoding_ids

    async def stop(self) -> None:
        """Cancel the drain loop and every in-flight render, awaiting cleanup.

        Cancelling an in-flight job task propagates into the encoder, which kills
        the ffmpeg child and removes the partial output; the job's own cancel
        handler records the outcome (in a worker thread, awaited to completion)
        before re-raising: an encode job interrupted within
        ``server.shutdown_grace_seconds`` of this call goes back to ``pending``
        once and is run again by the next start (#178), anything else is
        ``failed`` (:meth:`_mark_interrupted_by_shutdown_sync`). All tasks are
        awaited with ``return_exceptions=True`` so nothing leaks onto a closing
        loop. Idempotent.
        """
        if self._stopped:
            return
        self._stopped = True
        self._stop_began_at = self._monotonic()

        tasks: list[asyncio.Task[None]] = []
        if self._loop_task is not None:
            self._loop_task.cancel()
            tasks.append(self._loop_task)
        if self._backfill_task is not None:
            self._backfill_task.cancel()
            tasks.append(self._backfill_task)
        for task in self._job_tasks.values():
            task.cancel()
            tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("render worker stopped (%d job task(s))", len(self._job_tasks))

    def _monotonic(self) -> float:
        """Monotonic seconds; a seam so tests can move the shutdown clock."""
        return time.monotonic()

    def _stop_within_grace(self) -> bool:
        """Whether this is a clean stop that is still inside its grace period.

        True only while :meth:`stop` is running and
        ``server.shutdown_grace_seconds`` has not run out since it began. A job
        interrupted later than that is failed, not requeued: the stop was not
        clean (#178).
        """
        if self._stop_began_at is None:
            return False
        elapsed = self._monotonic() - self._stop_began_at
        return elapsed < self._settings.server.shutdown_grace_seconds

    async def cancel_job(self, job_id: int, *, user_requested: bool = False) -> bool:
        """Cancel a pending or in-flight render; return whether anything changed.

        A still-``pending`` job is flipped straight to ``failed`` (the worker will
        skip it when it tries to claim it). An in-flight job's task is cancelled
        and awaited so cleanup -- child kill, partial removal, ``failed`` write --
        completes before this returns, and a caller's follow-up read sees the
        settled state with no orphan process or partial file. For an export, whose
        zip is built in a worker thread that cannot be interrupted, the thread is
        awaited as well; the builder sees the job ``failed`` before its final
        rename and discards the partial archive.

        ``user_requested`` marks a cancel a user asked for (the cancel button, a
        delete of a running render). The job still ends ``failed``, but no
        ``render.failed`` error event is written: it is not a failure, and the
        caller records who cancelled it through the render service (an ``info``
        ``render.cancelled`` event). Without it, an interrupted in-flight render
        is logged as a failure, as before.

        A job with no task is decided under :attr:`_claim_lock`, so a claim in
        flight finishes registering its task first and is cancelled like any
        running job. With no task even then, a ``pending`` job is flipped to
        ``failed``, and so is an ``encoding`` one: that row is an orphan (#67 R1)
        and settling it is what lets a delete go ahead without a restart. Returns
        False for an unknown or already terminal job.
        """
        task = self._job_tasks.get(job_id)
        if task is None:
            async with self._claim_lock:
                task = self._job_tasks.get(job_id)
                if task is None:
                    settled_from = await asyncio.to_thread(
                        self._cancel_untracked_sync, job_id, user_requested
                    )
                    if settled_from == "encoding":
                        self._orphans_reclaimed_total += 1
                    return settled_from is not None
        export_thread = self._export_threads.get(job_id)
        gate = self._export_gates.get(job_id)
        if user_requested:
            self._user_cancels.add(job_id)
        try:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            self._user_cancels.discard(job_id)
        # An export still queued on the jobs executor has no thread to wait for
        # (#101): closing its gate makes the thread return the moment the
        # executor reaches it, so the cancel does not wait behind other jobs.
        # One that already started is awaited so nothing is writing the zip.
        queued = gate is not None and gate.cancel_if_queued()
        if export_thread is not None and not queued:
            await asyncio.gather(export_thread, return_exceptions=True)
        return True

    def _cancel_untracked_sync(self, job_id: int, user_requested: bool) -> str | None:
        """Settle a job that has no task: ``pending`` or orphaned ``encoding``.

        Synchronous; runs under :attr:`_claim_lock` (held by the caller on the
        loop), so no claim can turn a ``pending`` row into a live job meanwhile.
        A ``pending`` job is flipped with no event, as before. An orphan is
        recorded like the sweep records one -- with its partial output removed --
        except that a user's cancel writes no failure event (the caller records
        the cancel). Returns the status the row was settled from, or ``None``
        when nothing changed. Raises when the write cannot land even after its
        retries; the caller's request then fails.
        """
        status = self._status_sync(job_id)
        if status == "pending":
            flipped = run_with_write_retry(
                lambda: self._flip_if_sync(job_id, "pending", event_reason=None),
                max_attempts=_TERMINAL_WRITE_ATTEMPTS,
                base_delay=_TERMINAL_WRITE_BASE_DELAY,
                max_delay=_TERMINAL_WRITE_MAX_DELAY,
            )
            return status if flipped else None
        if status == "encoding":
            reason = None if user_requested else ORPHAN_REASON
            if not self._fail_orphans_sync([job_id], reason=reason):
                return None
            if user_requested:
                logger.info("render job=%s (orphaned) cancelled on request", job_id)
            return status
        return None

    def _status_sync(self, job_id: int) -> str | None:
        """Return a job's status, or ``None`` when it does not exist. Synchronous."""
        with session_scope(self._session_factory) as session:
            return session.execute(
                select(RenderJob.status).where(RenderJob.id == job_id)
            ).scalar_one_or_none()

    def _flip_if_sync(
        self, job_id: int, expected: str, *, event_reason: str | None
    ) -> bool:
        """Flip a job from ``expected`` to ``failed``; return whether it did.

        Synchronous, one transaction, a single conditional ``UPDATE`` so a row
        that moved on meanwhile is left alone. ``event_reason`` adds the
        ``render.failed`` error event; ``None`` writes no event. Retry-safe.
        """
        with session_scope(self._session_factory) as session:
            result = cast(
                "CursorResult[Any]",
                session.execute(
                    update(RenderJob)
                    .where(RenderJob.id == job_id, RenderJob.status == expected)
                    .values(
                        status="failed",
                        completed_at=datetime.now(UTC).replace(tzinfo=None),
                    )
                    .execution_options(synchronize_session=False)
                ),
            )
            if not result.rowcount:
                return False
            if event_reason is not None:
                project_id = session.execute(
                    select(RenderJob.project_id).where(RenderJob.id == job_id)
                ).scalar_one()
                _log_render_failed(session, job_id, project_id, event_reason)
            return True

    async def _drain_loop(self) -> None:
        """Sweep orphans, claim and launch pending jobs forever, until cancelled.

        Each cycle first fails any ``encoding`` row with no live task (see
        :meth:`_sweep_orphans`), then claims as many pending jobs as concurrency
        allows and launches a task per claimed job, then waits to be woken (or
        re-polls on a ceiling) so a job enqueued while at capacity is still
        picked up when a slot frees. A database error in a cycle (a lock that
        outlasts the busy timeout, say) is logged and the cycle is retried at
        the next wake-up or ceiling: it never kills the worker.
        """
        ceiling = max(1.0, self._settings.render.scheduler_check_interval_seconds)
        while True:
            self._wakeup.clear()
            try:
                await self._sweep_orphans()
                await self._launch_ready_jobs()
            except Exception:  # noqa: BLE001 - one bad cycle never kills the worker
                logger.exception("render worker cycle failed; retrying")
            # Signal idleness for waiters. ``_launch_ready_jobs`` claims every
            # pending job it has capacity for and breaks once none remain, so an
            # empty in-flight set here means nothing is pending and nothing is
            # running -- the queue has settled. The check is synchronous with the
            # ``_job_tasks`` read above (no ``await`` between) so it cannot latch a
            # transient-empty state mid-launch.
            if not self._job_tasks:
                self._idle.set()
            else:
                self._idle.clear()
            # Re-poll on the ceiling: covers a slot freeing without a notify.
            #
            # ``asyncio.timeout`` -- not ``asyncio.wait_for`` -- is deliberate.
            # ``wait_for`` has a cancellation race: if the wrapped future
            # completes (here, the wakeup event is set by a finishing job's
            # ``_finish``) in the same loop iteration that the drain task is
            # cancelled (by ``stop``), it returns the result and *swallows* the
            # ``CancelledError``, leaving the task wedged forever and ``stop``'s
            # gather waiting on a task that will never finish. ``asyncio.timeout``
            # propagates the cancellation correctly. It raises ``TimeoutError`` on
            # expiry, so the surrounding suppression still covers the re-poll.
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(ceiling):
                    await self._wakeup.wait()

    async def _launch_ready_jobs(self) -> None:
        """Claim pending jobs up to free capacity and start a task for each.

        Renders and exports are claimed against their own slots (#67 P7): the
        render slots (``render.max_concurrent``) take the oldest pending
        non-export job, the export slots (``render.max_concurrent_exports``) the
        oldest pending export.

        The slot is taken first, outside any lock (it is free: ``locked()`` was
        checked with no ``await`` between, so the acquire does not wait). The
        claim and the task's registration then happen together under
        :attr:`_claim_lock`, so a claimed row is never ``encoding`` without a
        task in ``_job_tasks`` as seen by anyone holding the lock. The lock
        covers only the claim query, never a render or a slot wait.
        """
        await self._launch_for(self._semaphore, self._claim_next_pending)
        await self._launch_for(self._export_semaphore, self._claim_next_pending_export)

    async def _launch_for(
        self, semaphore: asyncio.Semaphore, claim: Callable[[], int | None]
    ) -> None:
        """Claim and launch jobs through ``claim`` while ``semaphore`` has a slot."""
        while not semaphore.locked():
            await semaphore.acquire()
            launched = False
            job_id: int | None = None
            try:
                async with self._claim_lock:
                    job_id = await asyncio.to_thread(claim)
                    if job_id is None:
                        return
                    self._job_semaphores[job_id] = semaphore
                    task = asyncio.create_task(
                        self._run_job(job_id), name=f"render-job-{job_id}"
                    )
                    self._job_tasks[job_id] = task
                    launched = True
            finally:
                # The job task releases its own slot in ``_finish``; anything
                # else -- nothing to claim, a failed or cancelled claim --
                # returns it here.
                if not launched:
                    if job_id is not None:
                        self._job_semaphores.pop(job_id, None)
                    semaphore.release()

    async def _sweep_orphans(self) -> None:
        """Fail every ``encoding`` row with no live task (#67 R1).

        The ids are read and compared with ``_job_tasks`` under
        :attr:`_claim_lock`, so a claim in flight (row ``encoding``, task not yet
        registered) is never taken for an orphan. The rows are then failed
        outside the lock, each by a conditional write: an orphan can never gain a
        task (only ``pending`` rows are claimed), and one that a cancel settled
        meanwhile is simply skipped. Also refreshes
        :attr:`oldest_encoding_started_at`, :attr:`unreclaimed_orphans` and the
        rows :meth:`encoding_overruns` reads.
        """
        async with self._claim_lock:
            rows = await asyncio.to_thread(self._encoding_rows_sync)
            orphan_ids = [job_id for job_id, _ in rows if job_id not in self._job_tasks]
        started = [started_at for _, started_at in rows if started_at is not None]
        self._oldest_encoding_started_at = min(started) if started else None
        self._encoding_rows = tuple(rows)
        # Counted as unreclaimed until the writes land; a write that still
        # fails after its retries raises out of here and leaves the count set.
        self._unreclaimed_orphans = len(orphan_ids)
        if orphan_ids:
            reclaimed = await asyncio.to_thread(
                self._fail_orphans_sync, orphan_ids, reason=ORPHAN_REASON
            )
            self._orphans_reclaimed_total += len(reclaimed)
        # Every id is now failed, or was settled by a cancel meanwhile.
        self._unreclaimed_orphans = 0

    def _encoding_rows_sync(self) -> list[tuple[int, datetime | None]]:
        """``(id, started_at)`` of every ``encoding`` row. Synchronous."""
        with session_scope(self._session_factory) as session:
            return [
                (int(job_id), started_at)
                for job_id, started_at in session.execute(
                    select(RenderJob.id, RenderJob.started_at).where(
                        active_job_filter(), RenderJob.status == "encoding"
                    )
                ).all()
            ]

    def _fail_orphans_sync(
        self, job_ids: list[int], *, reason: str | None
    ) -> list[int]:
        """Fail each orphaned ``encoding`` job and remove its partial output.

        Synchronous. Each row is flipped by its own retried, conditional write
        (still ``encoding`` -> ``failed``), with the ``render.failed`` event when
        ``reason`` is given. Then the job's conventional output names --
        ``render-<id>.*`` and ``export-<id>.zip*`` -- are unlinked, confined to
        the project's render root: no live task is writing them, and a row that
        is not ``done`` must not leave a file behind. Returns the ids actually
        failed. Raises only when a row's write does not land after its retries.
        """
        reclaimed: list[int] = []
        for job_id in job_ids:
            changed = run_with_write_retry(
                functools.partial(
                    self._flip_if_sync, job_id, "encoding", event_reason=reason
                ),
                max_attempts=_TERMINAL_WRITE_ATTEMPTS,
                base_delay=_TERMINAL_WRITE_BASE_DELAY,
                max_delay=_TERMINAL_WRITE_MAX_DELAY,
            )
            if not changed:
                continue
            reclaimed.append(job_id)
            logger.warning("render job=%s failed: %s", job_id, ORPHAN_REASON)
            self._remove_orphan_outputs(job_id)
        return reclaimed

    def _remove_orphan_outputs(self, job_id: int) -> None:
        """Unlink a failed orphan's partial outputs. Synchronous; never raises."""
        try:
            with session_scope(self._session_factory) as session:
                project_id = session.execute(
                    select(RenderJob.project_id).where(RenderJob.id == job_id)
                ).scalar_one_or_none()
                project = (
                    session.get(Project, project_id) if project_id is not None else None
                )
                if project is None:
                    return
                render_root = project_render_root(self._settings, project).resolve()
            if not render_root.is_dir():
                return
            for pattern in (f"render-{job_id}.*", f"export-{job_id}.zip*"):
                for path in render_root.glob(pattern):
                    unlink_confined(path, render_root, job_id=job_id)
        except Exception:  # noqa: BLE001 - the row is settled; the file is a nicety
            logger.exception("could not remove the output of orphaned job=%s", job_id)

    def _reclaim_orphaned_encoding(self) -> None:
        """Fail every job stuck in ``encoding`` with no live task. Synchronous.

        Also removes the ``export-*.zip.part`` files an interrupted export left
        (#67 P7).

        Called once at startup before the drain loop runs, so it can never race a
        live claim: at this point no job task exists. Uses the same writer as the
        drain loop's sweep (:meth:`_fail_orphans_sync`): each orphan is failed
        with the ``orphaned`` reason and its partial output removed.

        Best effort: this is a maintenance sweep, not a startup precondition. The
        daemon tolerates an unmigrated or empty database at startup (it runs in a
        degraded mode rather than failing to boot), so any error here -- a missing
        table on a fresh database, for instance -- is logged and swallowed so the
        worker still starts. The drain loop sweeps again on its first cycle.
        """
        try:
            orphan_ids = [job_id for job_id, _ in self._encoding_rows_sync()]
            if orphan_ids:
                reclaimed = self._fail_orphans_sync(orphan_ids, reason=ORPHAN_REASON)
                # Runs before the drain loop exists, so nothing reads it meanwhile.
                self._orphans_reclaimed_total += len(reclaimed)
                logger.warning(
                    "reclaimed %d orphaned encoding job(s) at startup", len(reclaimed)
                )
        except Exception:  # noqa: BLE001 - never block startup on the sweep
            logger.exception("failed to reclaim orphaned encoding jobs at startup")
        try:
            export.remove_stale_parts(self._settings, self._session_factory)
        except Exception:  # noqa: BLE001 - never block startup on the sweep
            logger.exception("failed to remove stale export part files at startup")

    def _claim_next_pending(self) -> int | None:
        """Atomically flip the oldest pending render to encoding; return its id.

        Exports are not claimed here: they have their own slots and claim
        (:meth:`_claim_next_pending_export`, #67 P7).
        """
        return self._claim_oldest_pending(exports=False)

    def _claim_next_pending_export(self) -> int | None:
        """Atomically flip the oldest pending export to encoding; return its id."""
        return self._claim_oldest_pending(exports=True)

    def _claim_oldest_pending(self, *, exports: bool) -> int | None:
        """Atomically flip the oldest pending job of one family to encoding.

        The ``pending -> encoding`` flip and ``started_at`` stamp commit in one
        transaction, and the flip is a conditional ``UPDATE`` (still
        ``pending``), so a concurrent cancel (which flips ``pending -> failed``)
        and this claim cannot both win the same row. Returns ``None`` when no
        pending job remains, or in the rare case the oldest one was settled
        between the read and the flip; the drain loop's re-poll picks up any
        job left behind. Synchronous; call via a thread executor.

        The read selects only ``id`` and repeats the partial predicate of
        ``ix_render_job_active`` (#66 C10), so it walks the in-flight jobs in id
        order instead of the whole job history, and never loads a job's
        ``output_settings``.
        """
        candidate = (
            select(RenderJob.id)
            .where(
                active_job_filter(),
                RenderJob.status == "pending",
                RenderJob.kind == "export" if exports else RenderJob.kind != "export",
            )
            .order_by(RenderJob.id)
            .limit(1)
        )
        with session_scope(self._session_factory) as session:
            if _supports_for_update(session):
                candidate = candidate.with_for_update()
            job_id = session.execute(candidate).scalar_one_or_none()
            if job_id is None:
                return None
            result = cast(
                "CursorResult[Any]",
                session.execute(
                    update(RenderJob)
                    .where(RenderJob.id == job_id, RenderJob.status == "pending")
                    .values(
                        status="encoding",
                        started_at=datetime.now(UTC).replace(tzinfo=None),
                    )
                    .execution_options(synchronize_session=False)
                ),
            )
            if not result.rowcount:
                logger.info("render job=%s settled before it was claimed", job_id)
                return None
            return int(job_id)

    async def _run_job(self, job_id: int) -> None:
        """Run one claimed render to completion, releasing the slot at the end.

        Cancellation may arrive *anywhere* -- while preparing the spec or while
        the encoder runs -- so a single handler records the job ``failed`` and
        re-raises. Awaiting the encoder directly (never shielded, never in a
        thread) is what lets that cancellation reach the encoder so it kills the
        ffmpeg child and removes the partial output. Any non-cancellation failure
        is contained: the job is marked ``failed`` and the loop carries on. Every
        one of those writes goes through :meth:`_settle` -- a worker thread,
        awaited to completion even through a further cancel -- so none of them
        blocks the event loop (#62). The concurrency slot and the task handle are
        always released in ``finally``.
        """
        try:
            spec, post_action_specs, job_kind = await run_in(
                self._jobs_executor, self._prepare, job_id
            )
            if job_kind == "export":
                # An export job bundles frame files into a zip in a worker thread
                # instead of running the encoder. Unlike the encoder, the thread
                # has no child process to kill on cancel; a cancelled export is
                # still marked failed by the handler below while its thread may
                # finish writing -- the builder's temp+rename, its re-check of the
                # job before the rename, and the download route's done-status
                # gate make a half-written archive harmless. The thread runs as
                # its own (shielded) future so cancel_job can await it.
                gate = _ExportGate()
                self._export_gates[job_id] = gate
                export_thread = asyncio.create_task(
                    run_in(
                        self._jobs_executor,
                        self._build_export,
                        gate,
                        job_id,
                    ),
                    # Named for the SIGUSR2 dump / debug endpoint (#7).
                    name=f"render-export-{job_id}",
                )
                self._export_threads[job_id] = export_thread
                result = await asyncio.shield(export_thread)
            else:
                assert spec is not None  # only an export job returns a None spec
                # The watchdog's ceiling for this job, for the readiness probe.
                self._job_budget_seconds[job_id] = render_time_ceiling(
                    self._settings.render.max_job_seconds, len(spec.frames)
                )
                # Refuse to start an encode that could fill the output volume.
                # Probe free space against the same low watermark capture pauses
                # on; below it, fail the job cleanly rather than wedge the disk.
                if await run_in(
                    self._jobs_executor,
                    export.free_space_below_low_watermark,
                    spec.project_render_root,
                    self._settings,
                ):
                    logger.warning(
                        "render job=%s refused: free space on %s is below the "
                        "low watermark",
                        job_id,
                        spec.project_render_root,
                    )
                    await self._settle(
                        job_id,
                        self._mark_failed_sync,
                        job_id,
                        "insufficient free disk space to render",
                    )
                    return
                result = await self._encoder.render(spec)
            media: MediaInfo | None = None
            if (
                job_kind != "export"
                and result.success
                and result.output_path is not None
            ):
                media = await self._probe(job_id, result.output_path)
            await self._settle(job_id, self._record_result_sync, job_id, result, media)
            if result.success:
                await post_actions.run_post_actions(
                    self._settings,
                    self._session_factory,
                    job_id=job_id,
                    output_path=result.output_path,
                    action_specs=post_action_specs,
                    kind=job_kind,
                    executor=self._jobs_executor,
                )
        except asyncio.CancelledError:
            # The write runs in a thread and is shielded: it completes (and the
            # job is settled) before the cancellation is re-raised.
            if job_id in self._user_cancels:
                await self._settle(job_id, self._mark_cancelled_sync, job_id)
            elif self._stopped:
                await self._settle(
                    job_id,
                    self._mark_interrupted_by_shutdown_sync,
                    job_id,
                    self._stop_within_grace(),
                )
            else:
                await self._settle(
                    job_id, self._mark_failed_sync, job_id, "render cancelled"
                )
            raise
        except SpecBuildError as exc:
            logger.warning("render job=%s could not be prepared: %s", job_id, exc)
            await self._settle(job_id, self._mark_failed_sync, job_id, str(exc))
        except Exception as exc:  # noqa: BLE001 - contain any failure per job
            logger.exception("render job=%s failed", job_id)
            await self._settle(job_id, self._mark_failed_sync, job_id, str(exc))
        finally:
            self._finish(job_id)

    async def _settle(
        self, job_id: int, write: Callable[..., bool], *args: object
    ) -> None:
        """Run a terminal-state write in a worker thread until it lands.

        The write never runs on the event loop: under a held SQLite write lock it
        can wait up to the busy timeout, and on the loop that froze every request
        (#62). It is shielded, and a cancellation that arrives while it runs (a
        ``stop`` during a user's cancel, or a cancel during a failure write) is
        held back until the write has finished, then re-raised. So whoever awaits
        the job's task -- :meth:`cancel_job`, :meth:`stop` -- sees the job settled
        in the database, as when the write was synchronous.

        ``write`` is one of the recorders (``_record_result_sync``,
        ``_mark_*_sync``): it retries on "database is locked" itself, never
        raises, and returns whether the row was written. When it returns False
        the write is tried again after 1, 2, 4 ... seconds (capped at
        :data:`_SETTLE_BACKOFF_MAX_SECONDS`), until it lands or :meth:`stop`
        begins (#67 R1). Meanwhile the job keeps its slot and its ``_job_tasks``
        entry, so its row is never an orphan and no new job is claimed while a
        row cannot be written. The first failure is recorded as a
        ``render.state_write_failed`` warning event, best effort.
        """
        cancelled = False
        attempt = 0
        while True:
            landed, interrupted = await self._run_shielded(job_id, write, *args)
            cancelled = cancelled or interrupted
            if landed:
                break
            attempt += 1
            if attempt == 1:
                _, interrupted = await self._run_shielded(
                    job_id, self._record_write_failed_sync, job_id
                )
                cancelled = cancelled or interrupted
            if self._stopped:
                logger.error(
                    "render job=%s: worker stopping with its final state unwritten;"
                    " the next start fails it as orphaned",
                    job_id,
                )
                break
            delay = min(2.0 ** (attempt - 1), _SETTLE_BACKOFF_MAX_SECONDS)
            logger.warning(
                "render job=%s: final state not written (attempt %d); retrying in"
                " %.0f s",
                job_id,
                attempt,
                delay,
            )
            try:
                await self._clock.sleep(delay)
            except asyncio.CancelledError:
                # Held back like a cancel during the write: the row must land
                # first. ``stop`` sets ``_stopped`` before it cancels, so the
                # next failed attempt ends the loop.
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    async def _run_shielded(
        self, job_id: int, write: Callable[..., bool], *args: object
    ) -> tuple[bool, bool]:
        """Run ``write`` in a thread to completion; return ``(result, cancelled)``.

        A cancellation while it runs is absorbed and reported in ``cancelled`` so
        the caller can re-raise it once the job is settled.
        """
        landed = asyncio.create_task(
            asyncio.to_thread(write, *args),
            # Named for the SIGUSR2 dump / debug endpoint (#7).
            name=f"render-settle-{job_id}",
        )
        cancelled = False
        while not landed.done():
            try:
                await asyncio.shield(landed)
            except asyncio.CancelledError:
                cancelled = True
        return bool(landed.result()), cancelled

    def _finish(self, job_id: int) -> None:
        """Release the concurrency slot and forget the job's task.

        Runs exactly once, in ``_run_job``'s ``finally``, so the slot acquired in
        :meth:`_launch_ready_jobs` is released exactly once. Every path to it has
        settled the job's row through :meth:`_settle` first, so a job leaves
        ``_job_tasks`` only once its row is terminal (or the worker is stopping).
        """
        self._job_tasks.pop(job_id, None)
        self._export_threads.pop(job_id, None)
        self._export_gates.pop(job_id, None)
        self._job_budget_seconds.pop(job_id, None)
        self._job_semaphores.pop(job_id, self._semaphore).release()
        self._wakeup.set()

    def _build_export(self, gate: _ExportGate, job_id: int) -> RenderResult:
        """Build an export's zip on the jobs executor, unless it was cancelled first.

        The gate is closed by :meth:`cancel_job` when the export was still
        queued behind other long jobs, so a cancelled export returns here without
        reading or writing anything (#101).
        """
        if not gate.start():
            return RenderResult(
                success=False,
                output_path=None,
                duration_seconds=None,
                browser_streamable=False,
                codec="",
                container="zip",
                error=f"export job {job_id} was cancelled",
            )
        return export.build_export_zip(
            self._settings, self._session_factory, job_id=job_id
        )

    def _prepare(
        self, job_id: int
    ) -> tuple[RenderSpec | None, list[dict[str, object]], str]:
        """Build the render spec, read post-action specs, and the job kind.

        Synchronous. The job kind (``manual``/``scheduled``/``archive``/
        ``export``) is read while the session is open and returned alongside the
        spec and actions so the run phase can branch on it and the post-action
        phase can apply kind-aware pruning without reopening the row.

        For an ``export`` job the spec is ``None``: an export bundles existing
        frame files into a zip rather than encoding a video, so it has no render
        spec and -- unlike a render -- must not require the project to have any
        *active* frames (an export of an explicit selection is valid even when
        every frame is excluded or soft-deleted). The return therefore short-
        circuits **before** :func:`build_render_spec`, which gathers active frames
        and raises when none remain.

        Raises :class:`SpecBuildError` when a render job, its project, or its
        frames cannot produce a renderable spec.
        """
        with session_scope(self._session_factory) as session:
            job = session.get(RenderJob, job_id)
            if job is None:
                raise SpecBuildError(f"render job {job_id} no longer exists")
            project = session.get(Project, job.project_id)
            if project is None:
                raise SpecBuildError(f"project {job.project_id} no longer exists")
            kind = job.kind
            actions = list(project.post_render_actions or [])
            if kind == "export":
                return None, actions, kind
            spec = build_render_spec(session, self._settings, job, project)
            _record_frame_window(job, spec)
        return spec, actions, kind

    def _record_result_sync(
        self, job_id: int, result: RenderResult, media: MediaInfo | None = None
    ) -> bool:
        """Write the terminal status, output path, streamability and probed codec.

        Synchronous; :meth:`_settle` runs it in a worker thread. ``media`` is the
        encode-time probe of the output (``None`` for an export, a failed render,
        or a failed probe: the codec columns then stay ``NULL``). Retried on
        "database is locked"; returns whether the row was written (a vanished
        row counts as written) and never raises.
        """

        def write() -> None:
            with session_scope(self._session_factory) as session:
                job = session.get(RenderJob, job_id)
                if job is None:
                    return
                job.completed_at = datetime.now(UTC).replace(tzinfo=None)
                job.browser_streamable = result.browser_streamable
                if media is not None:
                    job.codec_string = media.codec_string
                    job.mime_type = media.mime_type
                if result.success and result.output_path is not None:
                    job.status = "done"
                    job.output_file_path = str(result.output_path)
                else:
                    job.status = "failed"
                    # Emit a typed failure event so the dispatcher delivers it to
                    # the configured notification channels (operators are
                    # notified when a render fails).
                    _log_render_failed(
                        session, job_id, job.project_id, result.error or "unknown error"
                    )

        return self._durable_write(write, "record the result of", job_id)

    def _durable_write(self, write: Callable[[], None], what: str, job_id: int) -> bool:
        """Run one terminal write with lock retries; return whether it landed.

        Synchronous. ``write`` is a self-contained transaction, safe to re-run.
        Any error left after the retries is logged (with ``what`` naming the
        write) and reported as ``False``, never raised: the caller,
        :meth:`_settle`, decides whether to try again.
        """
        try:
            run_with_write_retry(
                write,
                max_attempts=_TERMINAL_WRITE_ATTEMPTS,
                base_delay=_TERMINAL_WRITE_BASE_DELAY,
                max_delay=_TERMINAL_WRITE_MAX_DELAY,
            )
        except Exception:  # noqa: BLE001 - reported to the settle loop, not raised
            logger.exception("failed to %s render job=%s", what, job_id)
            return False
        return True

    def _record_write_failed_sync(self, job_id: int) -> bool:
        """Record that a job's terminal write is failing, as a warning event.

        Synchronous and best effort: under the same lock it may well fail too,
        and then only the log line remains. Always returns True (the settle loop
        does not retry it).
        """
        try:
            with session_scope(self._session_factory) as session:
                project_id = session.execute(
                    select(RenderJob.project_id).where(RenderJob.id == job_id)
                ).scalar_one_or_none()
                log_event(
                    session,
                    scope="project",
                    scope_id=project_id,
                    level="warning",
                    type=STATE_WRITE_FAILED_EVENT,
                    message=(
                        f"render {job_id}: its final state could not be written;"
                        " retrying, no other render starts meanwhile"
                    ),
                    metadata={"render_id": job_id},
                )
        except Exception:  # noqa: BLE001 - best effort; the log line stays
            logger.warning(
                "could not record the state-write failure of render job=%s", job_id
            )
        return True

    async def _probe(self, job_id: int, path: Path) -> MediaInfo | None:
        """Probe one render's output; never raise (a failure is ``None``)."""
        if not await run_in(self._jobs_executor, path.is_file):
            logger.warning(
                "codec probe of render job=%s skipped: %s is not a file", job_id, path
            )
            return None
        try:
            media = await self._prober(path)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a probe never fails a render
            logger.warning("codec probe of render job=%s failed", job_id, exc_info=True)
            return None
        if media is None:
            logger.warning(
                "codec probe of render job=%s gave nothing; codec left unset", job_id
            )
        return media

    async def _probe_with_ffprobe(self, path: Path) -> MediaInfo | None:
        """The default prober: ffprobe beside the configured ffmpeg."""
        try:
            ffprobe = resolve_ffmpeg_binary(self._settings, "ffprobe")
        except FfmpegResolutionError as exc:
            logger.warning("ffprobe unavailable, codec not probed: %s", exc)
            return None
        return await probe_media(ffprobe, path)

    async def _backfill_after_delay(self) -> None:
        """Wait out startup, then run the one-shot codec backfill."""
        await asyncio.sleep(_BACKFILL_START_DELAY_SECONDS)
        await self.backfill_codecs()

    async def backfill_codecs(self) -> int:
        """Probe finished renders that have no codec recorded; return how many.

        One-shot and idempotent (decision D14): it picks ``done`` renders
        (never exports) whose ``codec_string`` and ``mime_type`` are both
        ``NULL`` -- a render whose probe ran but found no known codec keeps its
        MIME type and is not re-probed -- and probes them one at a time, each
        with ffprobe's 10 s ceiling. A render whose file is missing or outside
        its project's render root is skipped. Never fatal: any failure is
        logged and the backfill moves on (or stops), and the worker carries on.
        """
        try:
            candidates = await run_in(self._jobs_executor, self._backfill_candidates)
        except Exception:  # noqa: BLE001 - the backfill must never be fatal
            logger.exception("codec backfill could not list renders")
            return 0
        if not candidates:
            return 0
        logger.info("codec backfill: probing %d render(s)", len(candidates))
        probed = 0
        for job_id, path in candidates:
            media = await self._probe(job_id, path)
            if media is None:
                continue
            try:
                stored = await asyncio.to_thread(self._store_backfill, job_id, media)
            except Exception:  # noqa: BLE001 - one bad row never stops the rest
                logger.exception("codec backfill could not store render job=%s", job_id)
                continue
            if stored:
                probed += 1
        logger.info("codec backfill: recorded %d of %d", probed, len(candidates))
        return probed

    def _backfill_candidates(self) -> list[tuple[int, Path]]:
        """``(job id, output path)`` of every render the backfill should probe."""
        out: list[tuple[int, Path]] = []
        with session_scope(self._session_factory) as session:
            rows = session.execute(
                select(RenderJob, Project)
                .join(Project, Project.id == RenderJob.project_id)
                .where(
                    RenderJob.status == "done",
                    RenderJob.kind != "export",
                    RenderJob.codec_string.is_(None),
                    RenderJob.mime_type.is_(None),
                    RenderJob.output_file_path.is_not(None),
                )
                .order_by(RenderJob.id)
            ).all()
            for job, project in rows:
                assert job.output_file_path is not None  # filtered above
                root = project_render_root(self._settings, project).resolve()
                path = Path(job.output_file_path).resolve()
                if not path.is_relative_to(root) or not path.is_file():
                    logger.info(
                        "codec backfill: skipping render job=%s (file missing)", job.id
                    )
                    continue
                out.append((job.id, path))
        return out

    def _store_backfill(self, job_id: int, media: MediaInfo) -> bool:
        """Write a backfilled probe unless the row changed meanwhile. Synchronous."""
        with session_scope(self._session_factory) as session:
            job = session.get(RenderJob, job_id)
            if job is None or job.codec_string is not None or job.mime_type is not None:
                return False
            job.codec_string = media.codec_string
            job.mime_type = media.mime_type
            return True

    def _mark_failed(self, job_id: int, reason: str) -> bool:
        """Mark a job failed, recording the reason on its settings. Synchronous."""
        return self._mark_failed_sync(job_id, reason)

    def _mark_cancelled_sync(self, job_id: int) -> bool:
        """Settle a user-cancelled job as ``failed`` without a failure event.

        Synchronous; the cancellation handler runs it in a worker thread through
        :meth:`_settle`. The status enum has no ``cancelled``, so the job ends
        ``failed`` like any stopped render; the cancel itself is recorded by the
        render service, at ``info``, with the user's name. A job already
        ``done`` (the cancel landed after its result was written) stays
        ``done``. Retried on "database is locked"; returns whether the row is
        settled and never raises, like the failure recorder.
        """

        def write() -> None:
            with session_scope(self._session_factory) as session:
                job = session.get(RenderJob, job_id)
                if job is None or job.status == "done":
                    return
                job.status = "failed"
                job.completed_at = datetime.now(UTC).replace(tzinfo=None)

        if not self._durable_write(write, "record the cancel of", job_id):
            return False
        logger.info("render job=%s cancelled on request", job_id)
        return True

    def _mark_interrupted_by_shutdown_sync(
        self, job_id: int, within_grace: bool
    ) -> bool:
        """Settle a job the service stop interrupted: requeue it once, or fail it.

        Synchronous, run through :meth:`_settle` like the other recorders; same
        contract (retried on "database is locked", returns whether the row is
        settled, never raises). One transaction decides from the row itself:

        * an encode job (not an export) never requeued before, and the stop is
          still inside its grace (``within_grace``): back to ``pending`` with
          ``shutdown_requeues`` raised to 1, a clean slate (no start or end time,
          no output path) and a ``render.requeued`` info event. The next start
          runs it again.
        * requeued once already: ``failed``, so a render that itself brings the
          service down cannot loop.
        * the grace ran out, or it is an export (its zip thread cannot be
          interrupted, so it is not safe to hand back to the queue): ``failed``
          with the ``render cancelled`` reason, as before.

        A crash never reaches here: its row stays ``encoding`` and the startup
        orphan sweep fails it. A job already ``done`` stays ``done``.
        """
        requeued = False

        def write() -> None:
            nonlocal requeued
            requeued = False
            with session_scope(self._session_factory) as session:
                job = session.get(RenderJob, job_id)
                if job is None or job.status == "done":
                    return
                if (
                    within_grace
                    and job.status == "encoding"
                    and job.kind != "export"
                    and job.shutdown_requeues < _MAX_SHUTDOWN_REQUEUES
                ):
                    job.status = "pending"
                    job.shutdown_requeues = job.shutdown_requeues + 1
                    job.started_at = None
                    job.completed_at = None
                    job.output_file_path = None
                    log_event(
                        session,
                        scope="project",
                        scope_id=job.project_id,
                        level="info",
                        type=EventType.RENDER_REQUEUED.value,
                        message=(
                            f"render {job_id} was interrupted by a service "
                            "shutdown and requeued"
                        ),
                        metadata={"render_id": job_id},
                    )
                    requeued = True
                    return
                job.status = "failed"
                job.completed_at = datetime.now(UTC).replace(tzinfo=None)
                if job.shutdown_requeues >= _MAX_SHUTDOWN_REQUEUES:
                    reason = (
                        "interrupted by a service shutdown again after a requeue;"
                        " not requeued twice"
                    )
                else:
                    reason = "render cancelled"
                _log_render_failed(session, job_id, job.project_id, reason)

        if not self._durable_write(write, "record the shutdown of", job_id):
            return False
        if requeued:
            logger.info("render job=%s interrupted by shutdown; requeued", job_id)
            # The encoder removed its partial output on cancel; sweep the
            # conventional names too so the rerun starts from nothing.
            self._remove_orphan_outputs(job_id)
        return True

    def _mark_failed_sync(self, job_id: int, reason: str) -> bool:
        """Mark a job failed in a fresh synchronous transaction.

        Synchronous; ``_run_job``'s failure and cancellation handlers run it in a
        worker thread through :meth:`_settle`, never on the event loop. A job
        already ``done`` stays ``done``. Retried on "database is locked";
        returns whether the row is settled. It never raises: a failure is logged
        and reported, so it never masks the original outcome, and the settle
        loop tries again (#67 R1).
        """

        def write() -> None:
            with session_scope(self._session_factory) as session:
                job = session.get(RenderJob, job_id)
                if job is None or job.status == "done":
                    return
                job.status = "failed"
                job.completed_at = datetime.now(UTC).replace(tzinfo=None)
                _log_render_failed(session, job_id, job.project_id, reason)

        return self._durable_write(write, "record the failure of", job_id)


def _log_render_failed(
    session: Session, job_id: int, project_id: int | None, reason: str
) -> None:
    """Add the typed ``render.failed`` error event to the caller's transaction.

    The dispatcher delivers it to the configured notification channels, so
    operators hear about a failed render.
    """
    log_event(
        session,
        scope="project",
        scope_id=project_id,
        level="error",
        type=EventType.RENDER_FAILED.value,
        message=f"render {job_id} failed: {reason}",
        metadata={"render_id": job_id},
    )


def _record_frame_window(job: RenderJob, spec: RenderSpec) -> None:
    """Store what the render is about to encode on its (attached) job row.

    The frame count, the first and last capture times (stored naive UTC like
    every other timestamp column), and the computed chapters, so the render card
    can show exactly what went into the video. Written in the same transaction as
    the spec is built, before the encoder starts.
    """
    frames = spec.frames.frames
    job.frame_count = len(frames)
    if frames:
        job.first_frame_at = _naive_utc(frames[0].capture_timestamp)
        job.last_frame_at = _naive_utc(frames[-1].capture_timestamp)
    job.chapters = [
        {"timecode_seconds": chapter.timecode_seconds, "label": chapter.label}
        for chapter in spec.chapters
    ]


def _naive_utc(value: datetime) -> datetime:
    """Return an aware datetime as naive UTC (the database's storage form)."""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _supports_for_update(session: Session) -> bool:
    """Return whether the bound dialect supports ``SELECT ... FOR UPDATE``.

    SQLite (the application's database) does not, and silently ignoring
    ``with_for_update`` would still be correct there because writes are
    serialised by the database-level lock. This keeps the claim query portable to
    a row-locking backend without changing behaviour on SQLite.
    """
    return session.bind is not None and session.bind.dialect.name != "sqlite"
