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
  cancel path then records ``failed`` with a *synchronous* database write (an
  ``await`` in a cancelling coroutine re-raises immediately and would skip the
  write) before re-raising.
* **Bulletproof stop.** :meth:`stop` cancels the drain loop and every in-flight
  job task, then awaits them all with ``return_exceptions=True`` so nothing leaks
  onto a closing event loop. Idempotent and safe with zero tasks.

The status enum on a job is fixed (``pending``/``encoding``/``done``/``failed``);
this module is the single place those transitions are written.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..db.models import Project, RenderJob
from ..db.session import session_scope
from ..encode import Encoder, RenderResult, RenderSpec, build_encoder
from ..ffmpeg_pin import resolve_ffmpeg_binary
from ..monitoring.events import EventType, log_event
from . import export, post_actions
from .spec import SpecBuildError, build_render_spec

logger = logging.getLogger(__name__)


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
            )
        )
        self._clock = clock if clock is not None else Clock()
        self._semaphore = asyncio.Semaphore(max(1, settings.render.max_concurrent))
        self._wakeup = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._job_tasks: dict[int, asyncio.Task[None]] = {}
        self._started = False
        self._stopped = False

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
        await asyncio.to_thread(self._reclaim_orphaned_encoding)
        self._loop_task = asyncio.create_task(self._drain_loop(), name="render-worker")
        # Pending jobs may already exist (a crash mid-queue, or a job enqueued
        # before start): wake the loop so it claims them immediately.
        self._wakeup.set()
        logger.info("render worker started")

    def notify(self) -> None:
        """Signal the drain loop that a new job may be available.

        Called by the trigger endpoint and the scheduler after enqueuing a job so
        the worker picks it up without waiting for its idle re-poll.
        """
        self._wakeup.set()

    async def stop(self) -> None:
        """Cancel the drain loop and every in-flight render, awaiting cleanup.

        Cancelling an in-flight job task propagates into the encoder, which kills
        the ffmpeg child and removes the partial output; the job's own cancel
        handler records ``failed`` synchronously before re-raising. All tasks are
        awaited with ``return_exceptions=True`` so nothing leaks onto a closing
        loop. Idempotent.
        """
        if self._stopped:
            return
        self._stopped = True

        tasks: list[asyncio.Task[None]] = []
        if self._loop_task is not None:
            self._loop_task.cancel()
            tasks.append(self._loop_task)
        for task in self._job_tasks.values():
            task.cancel()
            tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("render worker stopped (%d job task(s))", len(self._job_tasks))

    async def cancel_job(self, job_id: int) -> bool:
        """Cancel a pending or in-flight render; return whether anything changed.

        A still-``pending`` job is flipped straight to ``failed`` (the worker will
        skip it when it tries to claim it). An in-flight job's task is cancelled
        and awaited so cleanup -- child kill, partial removal, ``failed`` write --
        completes before this returns, and a caller's follow-up read sees the
        settled state with no orphan process or partial file.
        """
        task = self._job_tasks.get(job_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return True
        # No task: either still pending (flip it) or already terminal (no-op).
        return await asyncio.to_thread(self._cancel_pending, job_id)

    def _cancel_pending(self, job_id: int) -> bool:
        """Flip a still-pending job to failed in one transaction. Synchronous."""
        with session_scope(self._session_factory) as session:
            job = session.get(RenderJob, job_id)
            if job is None or job.status != "pending":
                return False
            job.status = "failed"
            job.completed_at = datetime.now(UTC).replace(tzinfo=None)
            return True

    async def _drain_loop(self) -> None:
        """Claim and launch pending jobs forever, until cancelled.

        Each cycle claims as many pending jobs as concurrency allows and launches
        a task per claimed job, then waits to be woken (or re-polls on a ceiling)
        so a job enqueued while at capacity is still picked up when a slot frees.
        """
        ceiling = max(1.0, self._settings.render.scheduler_check_interval_seconds)
        while True:
            self._wakeup.clear()
            await self._launch_ready_jobs()
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
        """Claim pending jobs up to free capacity and start a task for each."""
        free = self._semaphore._value  # noqa: SLF001 - bounded peek to avoid overclaim
        for _ in range(max(0, free)):
            job_id = await asyncio.to_thread(self._claim_next_pending)
            if job_id is None:
                break
            await self._semaphore.acquire()
            task = asyncio.create_task(
                self._run_job(job_id), name=f"render-job-{job_id}"
            )
            self._job_tasks[job_id] = task

    def _reclaim_orphaned_encoding(self) -> None:
        """Fail every job stuck in ``encoding`` with no live task. Synchronous.

        Called once at startup before the drain loop runs, so it can never race a
        live claim: at this point no job task exists. Each orphan is stamped
        ``completed_at`` and flipped to ``failed`` in a single transaction.

        Best effort: this is a maintenance sweep, not a startup precondition. The
        daemon tolerates an unmigrated or empty database at startup (it runs in a
        degraded mode rather than failing to boot), so any error here -- a missing
        table on a fresh database, for instance -- is logged and swallowed so the
        worker still starts.
        """
        try:
            with session_scope(self._session_factory) as session:
                orphans = (
                    session.query(RenderJob)
                    .filter(RenderJob.status == "encoding")
                    .all()
                )
                if not orphans:
                    return
                now = datetime.now(UTC).replace(tzinfo=None)
                for job in orphans:
                    job.status = "failed"
                    job.completed_at = now
                logger.warning(
                    "reclaimed %d orphaned encoding job(s) at startup", len(orphans)
                )
        except Exception:  # noqa: BLE001 - never block startup on the sweep
            logger.exception("failed to reclaim orphaned encoding jobs at startup")

    def _claim_next_pending(self) -> int | None:
        """Atomically flip the oldest pending job to encoding; return its id.

        The ``pending -> encoding`` flip and ``started_at`` stamp commit in one
        transaction, so a concurrent cancel (which flips ``pending -> failed``)
        and this claim cannot both win the same row. Returns ``None`` when no
        pending job remains. Synchronous; call via a thread executor.
        """
        with session_scope(self._session_factory) as session:
            job = (
                session.query(RenderJob)
                .filter(RenderJob.status == "pending")
                .order_by(RenderJob.id)
                .with_for_update()
                .first()
                if _supports_for_update(session)
                else session.query(RenderJob)
                .filter(RenderJob.status == "pending")
                .order_by(RenderJob.id)
                .first()
            )
            if job is None:
                return None
            job.status = "encoding"
            job.started_at = datetime.now(UTC).replace(tzinfo=None)
            return job.id

    async def _run_job(self, job_id: int) -> None:
        """Run one claimed render to completion, releasing the slot at the end.

        Cancellation may arrive *anywhere* -- while preparing the spec or while
        the encoder runs -- so a single handler records the job ``failed`` with a
        *synchronous* write (an ``await`` in a cancelling coroutine re-raises the
        cancellation before the write could commit) and re-raises. Awaiting the
        encoder directly (never shielded, never in a thread) is what lets that
        cancellation reach the encoder so it kills the ffmpeg child and removes
        the partial output. Any non-cancellation failure is contained: the job is
        marked ``failed`` and the loop carries on. The concurrency slot and the
        task handle are always released in ``finally``.
        """
        try:
            spec, post_action_specs, job_kind = await asyncio.to_thread(
                self._prepare, job_id
            )
            if job_kind == "export":
                # An export job bundles frame files into a zip in a worker thread
                # instead of running the encoder. Unlike the encoder, the thread
                # has no child process to kill on cancel; a cancelled export is
                # still marked failed by the handler below while its thread may
                # finish writing -- the builder's temp+rename and the download
                # route's done-status gate make a half-written archive harmless.
                result = await asyncio.to_thread(
                    export.build_export_zip,
                    self._settings,
                    self._session_factory,
                    job_id=job_id,
                )
            else:
                assert spec is not None  # only an export job returns a None spec
                result = await self._encoder.render(spec)
            await self._record_result(job_id, result)
            if result.success:
                await post_actions.run_post_actions(
                    self._settings,
                    self._session_factory,
                    job_id=job_id,
                    output_path=result.output_path,
                    action_specs=post_action_specs,
                    kind=job_kind,
                )
        except asyncio.CancelledError:
            # Synchronous write: an await here would re-raise before it committed.
            self._mark_failed_sync(job_id, "render cancelled")
            raise
        except SpecBuildError as exc:
            logger.warning("render job=%s could not be prepared: %s", job_id, exc)
            self._mark_failed_sync(job_id, str(exc))
        except Exception as exc:  # noqa: BLE001 - contain any failure per job
            logger.exception("render job=%s failed", job_id)
            self._mark_failed_sync(job_id, str(exc))
        finally:
            self._finish(job_id)

    def _finish(self, job_id: int) -> None:
        """Release the concurrency slot and forget the job's task.

        Runs exactly once, in ``_run_job``'s ``finally``, so the slot acquired in
        :meth:`_launch_ready_jobs` is released exactly once -- even if the task
        reached here before it was registered in ``_job_tasks``.
        """
        self._job_tasks.pop(job_id, None)
        self._semaphore.release()
        self._wakeup.set()

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
        return spec, actions, kind

    async def _record_result(self, job_id: int, result: RenderResult) -> None:
        """Persist a finished render's outcome with the fixed status mapping."""
        await asyncio.to_thread(self._record_result_sync, job_id, result)

    def _record_result_sync(self, job_id: int, result: RenderResult) -> None:
        """Write the terminal status, output path, and streamability. Synchronous."""
        with session_scope(self._session_factory) as session:
            job = session.get(RenderJob, job_id)
            if job is None:
                return
            job.completed_at = datetime.now(UTC).replace(tzinfo=None)
            job.browser_streamable = result.browser_streamable
            if result.success and result.output_path is not None:
                job.status = "done"
                job.output_file_path = str(result.output_path)
            else:
                job.status = "failed"
                # Emit a typed failure event so the dispatcher delivers it to the
                # configured notification channels (operators are notified when a
                # render fails).
                log_event(
                    session,
                    scope="project",
                    scope_id=job.project_id,
                    level="error",
                    type=EventType.RENDER_FAILED.value,
                    message=(
                        f"render {job_id} failed: {result.error or 'unknown error'}"
                    ),
                    metadata={"render_id": job_id},
                )

    def _mark_failed(self, job_id: int, reason: str) -> None:
        """Mark a job failed, recording the reason on its settings. Synchronous."""
        self._mark_failed_sync(job_id, reason)

    def _mark_failed_sync(self, job_id: int, reason: str) -> None:
        """Mark a job failed in a fresh synchronous transaction.

        Used from the cancellation handler where any ``await`` would re-raise the
        cancellation before the write could commit. Best effort: a failure here
        is logged and swallowed so it never masks the original outcome.
        """
        try:
            with session_scope(self._session_factory) as session:
                job = session.get(RenderJob, job_id)
                if job is None:
                    return
                job.status = "failed"
                job.completed_at = datetime.now(UTC).replace(tzinfo=None)
                log_event(
                    session,
                    scope="project",
                    scope_id=job.project_id,
                    level="error",
                    type=EventType.RENDER_FAILED.value,
                    message=f"render {job_id} failed: {reason}",
                    metadata={"render_id": job_id},
                )
        except Exception:  # noqa: BLE001 - never raise from the failure recorder
            logger.exception("failed to record failure for render job=%s", job_id)


def _supports_for_update(session: Session) -> bool:
    """Return whether the bound dialect supports ``SELECT ... FOR UPDATE``.

    SQLite (the application's database) does not, and silently ignoring
    ``with_for_update`` would still be correct there because writes are
    serialised by the database-level lock. This keeps the claim query portable to
    a row-locking backend without changing behaviour on SQLite.
    """
    return session.bind is not None and session.bind.dialect.name != "sqlite"
