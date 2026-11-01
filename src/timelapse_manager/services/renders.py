"""Render write use-cases -- the single implementation of the domain rules.

Each public function performs one write against a caller-supplied session and
returns a :class:`~timelapse_manager.services._base.Result` pairing the produced
value with the post-commit effects the caller must run once it has committed. The
web and API layers are thin adapters over these functions: they parse their own
transport input (and, for a manual render, validate the target through the
encoder), call the service, commit, then run the returned effects.

Validation lives here, once. A missing project or render raises a transport-
neutral :class:`~timelapse_manager.services.errors.NotFoundError` (mapped to
``404``). Services never commit and never signal the render worker directly; they
mutate, ``flush``, and return :data:`Effect.RENDER_NOTIFY` for the caller to run.

The two operations that reach the running render worker -- validating a manual
render's target through the encoder and tearing down an in-flight render's task
(killing the ffmpeg child) -- are deliberately **not** here: they are async
worker calls the adapter performs around the service, so the service touches only
the database. Enqueue returns :data:`Effect.RENDER_NOTIFY` so the adapter wakes
the worker after committing; cancel returns no effect because the worker has
already settled the job's terminal state in its own session by the time cancel's
audit is written.

Deleting a render or a frame export follows the same split. The adapter first
stops a still-``pending``/``encoding`` job through the worker (``cancel_job``,
which kills ffmpeg or waits out the zip thread) and only then calls
:func:`delete_render` / :func:`delete_export`, which refuse a job that is still
running so a row can never vanish under a live writer.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, text

from ..db.models import Event, Project, RenderJob, User
from ..db.models.render_job import active_job_filter
from ..db.session import session_scope
from ..render.outputs import delete_render_output, unlink_confined
from ..render.spec import project_render_root
from ..security.principal import ensure_sentinel_admin
from ._base import Actor, Effect, Result
from .errors import ConflictError, NotFoundError, PermissionError_

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from ..config.settings import Settings

logger = logging.getLogger(__name__)

#: Roles allowed to trigger, cancel, and delete renders and exports.
OPERATING_ROLES = frozenset({"admin", "operator"})
#: Job states in which the worker may still be writing the output.
RUNNING_STATUSES = frozenset({"pending", "encoding"})


@dataclass(frozen=True)
class DeletedJob:
    """What a delete removed, for the caller's response and toast.

    ``file_name``/``size_bytes`` describe the output file as it was found just
    before the delete; both are ``None`` when there was no file on disk (never
    produced, already missing, or outside the render root).
    """

    id: int
    kind: str
    project_id: int
    file_name: str | None
    size_bytes: int | None


def _get_project_or_404(session: Session, project_id: int) -> Project:
    """Return a project row or raise :class:`NotFoundError` (mapped to ``404``)."""
    project = session.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"project {project_id} not found")
    return project


def _get_render_or_404(session: Session, render_id: int) -> RenderJob:
    """Return a render-job row or raise :class:`NotFoundError` (mapped to ``404``)."""
    job = session.get(RenderJob, render_id)
    if job is None:
        raise NotFoundError(f"render {render_id} not found")
    return job


def _audit(
    session: Session,
    actor: Actor,
    project_id: int | None,
    message: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Append a project-scoped audit event attributed to ``actor``.

    The actor is a foreign key to a user row, so the sentinel administrator is
    ensured to exist before the insert (matching every other audited write) so the
    foreign key never fails for a bearer-token caller. ``metadata`` is the
    event's JSON details (its ``type`` key, if any, is the event type).
    """
    ensure_sentinel_admin(session)
    session.add(
        Event(
            scope="project",
            scope_id=project_id,
            level="info",
            message=message,
            event_metadata=metadata,
            actor_user_id=actor.user_id,
            timestamp=datetime.datetime.now(datetime.UTC).replace(tzinfo=None),
        )
    )


def enqueue_render(
    session: Session,
    actor: Actor,
    project_id: int,
    *,
    output_settings: dict[str, Any],
    overlay_config: dict[str, Any],
    dedupe: bool = False,
) -> Result[RenderJob]:
    """Queue a pending manual render for a project and ask the caller to notify.

    The project must exist (a missing one is a ``404``). The output/overlay
    settings are already resolved by the caller (the API validates the target
    through the encoder first; the web adapter merges any inline overrides onto
    the stored render schedule), so this simply inserts the pending ``manual`` job
    and flushes -- it does not commit. The caller commits and runs
    :data:`Effect.RENDER_NOTIFY` so the worker picks the job up without waiting out
    its poll interval.

    With ``dedupe`` (a user's submit, #67 R14), a manual job of the project that
    is still ``pending`` or ``encoding`` with equal output settings and overlay
    is returned instead of inserting a second one, with no effect and no audit
    event: a double-click queues one render. :func:`is_deduplicated` tells the
    caller which happened. The check and the insert run under the database's
    write lock (taken first, held until the caller commits), so two concurrent
    submits cannot both miss each other.
    """
    _get_project_or_404(session, project_id)
    if dedupe:
        _reserve_write_lock(session)
        existing = _find_queued_duplicate(
            session, project_id, output_settings, overlay_config
        )
        if existing is not None:
            return Result(existing, ())
    job = RenderJob(
        project_id=project_id,
        kind="manual",
        status="pending",
        output_settings=output_settings,
        overlay_config=overlay_config,
    )
    session.add(job)
    session.flush()
    _audit(session, actor, project_id, f"manual render {job.id} triggered")
    return Result(job, (Effect.RENDER_NOTIFY,))


def is_deduplicated(result: Result[RenderJob]) -> bool:
    """Return whether :func:`enqueue_render` returned an existing job.

    A new job always carries :data:`Effect.RENDER_NOTIFY`; a deduplicated one
    has nothing new for the worker, so it carries no effect.
    """
    return Effect.RENDER_NOTIFY not in result.effects


def _reserve_write_lock(session: Session) -> None:
    """Begin the session's write transaction now, before a check-then-insert.

    The SQLite driver opens a transaction only at the first write, so a plain
    read before the insert runs outside it: two requests could both read "no
    duplicate" and both insert. A no-op ``UPDATE`` takes the write lock up
    front (waiting out any other writer, up to the busy timeout); it is held
    until the caller commits, so the second submit reads the first one's row.
    """
    session.execute(text("UPDATE render_job SET id = id WHERE 1 = 0"))


def _find_queued_duplicate(
    session: Session,
    project_id: int,
    output_settings: dict[str, Any],
    overlay_config: dict[str, Any],
) -> RenderJob | None:
    """Return a queued manual job with these exact settings, if there is one.

    Queued means ``pending`` or ``encoding``. The settings are JSON columns,
    compared as dicts (a project has a handful of queued jobs at most). The
    oldest match wins.
    """
    candidates = session.execute(
        select(RenderJob)
        .where(
            RenderJob.project_id == project_id,
            RenderJob.kind == "manual",
            active_job_filter(),
        )
        .order_by(RenderJob.id)
    ).scalars()
    for job in candidates:
        if (job.output_settings or {}) == output_settings and (
            job.overlay_config or {}
        ) == overlay_config:
            return job
    return None


def enqueue_export(
    session: Session, actor: Actor, project_id: int, *, frame_ids: list[int]
) -> Result[RenderJob]:
    """Queue a pending frame export (a zip of ``frame_ids``) and ask to notify.

    The selection is already resolved to one project's concrete id-set by the
    caller. Inserts the ``kind="export"`` job, flushes, and records who asked for
    it (``export N triggered``) so the export card can show it. Does not commit.
    """
    _get_project_or_404(session, project_id)
    job = RenderJob(
        project_id=project_id,
        kind="export",
        status="pending",
        output_settings={"frame_ids": frame_ids},
    )
    session.add(job)
    session.flush()
    _audit(session, actor, project_id, f"export {job.id} triggered")
    return Result(job, (Effect.RENDER_NOTIFY,))


def cancel_render(session: Session, actor: Actor, render_id: int) -> Result[RenderJob]:
    """Record the cancellation of a render and return the (settled) job.

    The task teardown -- cancelling the in-flight job's task so the ffmpeg child
    is killed and the partial output removed, or flipping a still-pending job to
    ``failed`` -- is performed by the render worker in its own session *before*
    the adapter calls this, so here only the event is written: ``render N
    cancelled by <user>``, at ``info`` and typed ``render.cancelled`` with the
    render id, so it stays below the alert threshold, never matches a
    ``render.failed`` notification rule, and gives the render card its reason.
    (The adapter cancels through the worker with ``user_requested=True``, so the
    worker writes no ``render.failed`` error for it.) A missing render is a
    ``404``. Returns no effect: the worker has already done the work, so there
    is nothing to notify. Does not commit.
    """
    from ..monitoring.events import EventType

    job = _get_render_or_404(session, render_id)
    # Ensured before the name lookup so a bearer-token caller is named.
    ensure_sentinel_admin(session)
    _audit(
        session,
        actor,
        job.project_id,
        f"render {render_id} cancelled by {_username(session, actor)}",
        metadata={"type": EventType.RENDER_CANCELLED.value, "render_id": render_id},
    )
    session.flush()
    return Result(job, ())


def delete_render(
    session: Session, actor: Actor, render_id: int, *, settings: Settings
) -> Result[DeletedJob]:
    """Delete a finished render: its output file (inside the render root) and row.

    Operators and admins only (``403`` otherwise), the same rule as triggering a
    render. An unknown id, or an export job, is a ``404`` -- exports are deleted
    through :func:`delete_export`. A render still ``pending``/``encoding`` is a
    ``409``: the caller must stop it through the worker first. The output file is
    unlinked only when it resolves inside the project's render root; a file that
    is already gone is not an error. Writes the audit event ``render N deleted by
    <user>`` with the file name and size. Does not commit.
    """
    job = _get_render_or_404(session, render_id)
    if job.kind == "export":
        raise NotFoundError(f"render {render_id} not found")
    return Result(_delete_job(session, actor, job, settings=settings, noun="render"))


def delete_export(
    session: Session, actor: Actor, export_id: int, *, settings: Settings
) -> Result[DeletedJob]:
    """Delete a frame export: its zip (inside the render root) and its row.

    The same rules as :func:`delete_render`. A non-export id is a ``404``. Besides
    the recorded output path, the export's conventional ``export-<id>.zip`` and
    ``.zip.part`` names are removed too: a cancelled export never records its
    path, but its thread may have produced either before it stopped.
    """
    job = session.get(RenderJob, export_id)
    if job is None or job.kind != "export":
        raise NotFoundError(f"export {export_id} not found")
    return Result(_delete_job(session, actor, job, settings=settings, noun="export"))


def output_file_info(
    job: RenderJob, project: Project, settings: Settings
) -> tuple[Path | None, int | None]:
    """Return ``(path, size)`` of a job's output when it exists inside the root.

    ``(None, None)`` when no path is recorded, the path escapes the project's
    render root, or the file is missing. Read at request time by the delete and
    by the render/export cards.
    """
    if not job.output_file_path:
        return None, None
    root = project_render_root(settings, project).resolve()
    resolved = Path(job.output_file_path).resolve()
    if not resolved.is_relative_to(root):
        return None, None
    try:
        return resolved, resolved.stat().st_size
    except OSError:
        return None, None


def purge_expired_exports(
    session_factory: sessionmaker[Session],
    settings: Settings,
    now: datetime.datetime,
    *,
    batch_size: int = 100,
) -> int:
    """Delete finished frame exports older than ``render.export_ttl_days``.

    Housekeeping's retention for the zips (#67 P7): a ``done`` export whose
    ``completed_at`` is older than the TTL has its zip (and any ``.part``) unlinked
    inside the project's render root and its row removed, through the same delete
    as the manual one. ``0`` days keeps exports forever. Each export is its own
    short transaction, at most ``batch_size`` per call (the next hourly pass takes
    the rest). One info event per export names the TTL. Synchronous: call it off
    the event loop. Returns the number of exports deleted.
    """
    ttl_days = settings.render.export_ttl_days
    if ttl_days <= 0:
        return 0
    cutoff = now - datetime.timedelta(days=ttl_days)
    with session_scope(session_factory) as session:
        expired = list(
            session.scalars(
                select(RenderJob.id)
                .where(
                    RenderJob.kind == "export",
                    RenderJob.status == "done",
                    RenderJob.completed_at < cutoff,
                )
                .order_by(RenderJob.id)
                .limit(batch_size)
            )
        )
    deleted = 0
    for export_id in expired:
        try:
            with session_scope(session_factory) as session:
                job = session.get(RenderJob, export_id)
                if job is None or job.kind != "export" or job.status != "done":
                    continue
                project = session.get(Project, job.project_id)
                if project is None:
                    continue
                _remove_job(session, job, project, settings=settings, noun="export")
                session.add(
                    Event(
                        scope="project",
                        scope_id=project.id,
                        level="info",
                        message=(
                            f"export {export_id} deleted after {ttl_days} day(s) "
                            "(render.export_ttl_days)"
                        ),
                        timestamp=now,
                    )
                )
            deleted += 1
        except Exception:  # noqa: BLE001 - one bad export must not stop the pass
            logger.exception("could not delete expired export %s", export_id)
    if deleted:
        logger.info("deleted %d expired export(s)", deleted)
    return deleted


def _remove_job(
    session: Session,
    job: RenderJob,
    project: Project,
    *,
    settings: Settings,
    noun: str,
) -> DeletedJob:
    """Delete a job's row and its files (confined to the render root).

    The part shared by the manual delete and the TTL purge. Reads the output file
    as it was found, deletes the row, then reclaims the disk: the recorded output,
    and for an export its conventional ``.zip`` and ``.zip.part`` names too. Does
    not check the caller's rights or the job's status.
    """
    path, size = output_file_info(job, project, settings)
    deleted = DeletedJob(
        id=job.id,
        kind=job.kind,
        project_id=job.project_id,
        file_name=path.name if path is not None else None,
        size_bytes=size,
    )
    render_root = project_render_root(settings, project).resolve()
    session.delete(job)
    session.flush()
    # The row is gone from this transaction; now reclaim the disk. Best effort:
    # the containment guard and a missing file both leave the delete standing.
    delete_render_output(job, render_root)
    if noun == "export":
        for name in (f"export-{job.id}.zip", f"export-{job.id}.zip.part"):
            unlink_confined(render_root / name, render_root, job_id=job.id)
    return deleted


def _delete_job(
    session: Session, actor: Actor, job: RenderJob, *, settings: Settings, noun: str
) -> DeletedJob:
    """Shared delete for renders and exports (see :func:`delete_render`)."""
    if actor.role not in OPERATING_ROLES:
        raise PermissionError_(f"only operators and admins may delete a {noun}")
    if job.status in RUNNING_STATUSES:
        raise ConflictError(f"{noun} {job.id} is still running; cancel it first")
    project = _get_project_or_404(session, job.project_id)
    deleted = _remove_job(session, job, project, settings=settings, noun=noun)
    # Ensured before the name lookup so a bearer-token caller (the sentinel
    # administrator) is named rather than shown as a bare id.
    ensure_sentinel_admin(session)
    _audit(
        session,
        actor,
        deleted.project_id,
        f"{noun} {deleted.id} deleted by {_username(session, actor)} "
        f"({_describe_file(deleted)})",
    )
    session.flush()
    return deleted


def _username(session: Session, actor: Actor) -> str:
    """Return the acting user's name for an audit message (id when unknown)."""
    user = session.get(User, actor.user_id)
    return user.username if user is not None else f"user {actor.user_id}"


def _describe_file(deleted: DeletedJob) -> str:
    """Describe the removed file for the audit message."""
    if deleted.file_name is None:
        return "no output file on disk"
    return f"file {deleted.file_name}, {deleted.size_bytes} bytes"
