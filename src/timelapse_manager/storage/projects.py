"""Project-level deletion with on-disk cleanup.

Deleting a project removes its database row; the ``ON DELETE CASCADE`` foreign
keys (frames, render jobs, milestones) drop the child rows in the same
transaction. The *files* those rows pointed at -- captured/uploaded frame images
and rendered video outputs -- are not reached by the database cascade, so this
module gathers their absolute paths first and unlinks them after the row delete.

Two rules keep the operation safe and predictable:

* **The row delete is authoritative.** Paths are resolved and materialised
  *before* ``session.delete(project)`` (after the cascade fires the child rows
  are gone and cannot be queried). File removal is then best-effort: an unlink or
  directory-removal failure is suppressed so a permission error or an
  already-missing file never blocks the delete.
* **Only the default per-project directory is removed.** A project that uses an
  explicit ``storage_path`` may point at an operator-meaningful or shared
  directory, so its files are unlinked individually but the directory itself is
  left in place. The default ``<frames_root>/<project_id>`` directory is removed
  only if it is empty.
"""

from __future__ import annotations

import contextlib
import datetime
import logging
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import Settings
from ..db.models import Event, Frame, Project, RenderJob
from ..db.models.event import OPEN_ALERT_INDEX_LEVELS
from ..render.outputs import unlink_confined
from . import paths
from .thumbnail_cache import remove_project_thumbnails

logger = logging.getLogger(__name__)


def _gather_frame_files(
    session: Session, settings: Settings, project: Project
) -> list[Path]:
    """Resolve the absolute paths of every frame file belonging to ``project``."""
    rows = session.execute(
        select(Frame.file_path).where(Frame.project_id == project.id)
    ).all()
    bounds = paths.frame_boundaries(settings, project)
    files: list[Path] = []
    escaped = 0
    for (stored,) in rows:
        if stored is None:
            continue
        # Only files inside the project's frame directories are deleted; a path
        # that escapes them is left on disk (#170).
        try:
            files.append(
                paths.resolve_contained_frame_path(
                    settings, project, stored, boundaries=bounds
                )
            )
        except paths.PathContainmentError:
            escaped += 1
    if escaped:
        logger.warning(
            "not deleting %d frame file(s) of project %s: outside its frame directory",
            escaped,
            project.id,
        )
    return files


def _gather_render_files(session: Session, project: Project) -> list[tuple[int, Path]]:
    """Resolve the absolute paths of every render output belonging to ``project``.

    Render rows store an already-absolute ``output_file_path``; a job that never
    produced a file leaves it null and contributes nothing.
    """
    rows = session.execute(
        select(RenderJob.id, RenderJob.output_file_path).where(
            RenderJob.project_id == project.id
        )
    ).all()
    return [(job_id, Path(stored)) for job_id, stored in rows if stored]


def clear_project_alerts(session: Session, project_id: int) -> int:
    """Auto-clear the project's open alerts, keeping the events; return the count.

    Run in the delete's own transaction (#282), like the camera delete's
    ``camera_offline`` clear (#132): the alerts point at a project that is about
    to stop existing, so they would otherwise stay in the "Active alerts" badge
    for good. The rows are kept (they are history) with
    ``alert_clear_reason="project_deleted"`` and no clearing user.
    """
    result = session.execute(
        update(Event)
        .where(
            Event.scope == "project",
            Event.scope_id == project_id,
            Event.alert_cleared_at.is_(None),
            Event.level.in_(OPEN_ALERT_INDEX_LEVELS),
        )
        .values(
            alert_cleared_at=datetime.datetime.now(datetime.UTC).replace(tzinfo=None),
            alert_cleared_by=None,
            alert_clear_reason="project_deleted",
        )
        .execution_options(synchronize_session=False)
    )
    return int(getattr(result, "rowcount", 0) or 0)


def delete_project_with_files(
    session: Session, settings: Settings, project: Project
) -> None:
    """Delete a project's row (cascading children) and clean up its on-disk files.

    Frame and render file paths are resolved *before* the row delete so the
    cascade does not erase the rows they come from. The row is then deleted and
    flushed (the authoritative effect), after which the files are unlinked and the
    default per-project frame directory is removed if empty. All filesystem work
    is best-effort: a failure is logged-by-suppression and never prevents the
    delete from committing.
    """
    from ..render.spec import project_render_root

    frame_files = _gather_frame_files(session, settings, project)
    render_files = _gather_render_files(session, project)
    render_root = project_render_root(settings, project).resolve()
    # Only the default-layout directory is safe to remove (a custom storage_path
    # may be shared or operator-meaningful); compute it before the row is gone.
    default_dir: Path | None = (
        paths.frame_dir(settings, project)
        if paths.uses_default_layout(project)
        else None
    )

    project_id = project.id
    clear_project_alerts(session, project_id)
    session.delete(project)
    session.flush()

    for path in frame_files:
        with contextlib.suppress(FileNotFoundError, OSError):
            path.unlink()
    # Render outputs are unlinked only inside the project's render root, as the
    # single-render delete does (#170).
    for job_id, path in render_files:
        with contextlib.suppress(OSError):
            unlink_confined(path, render_root, job_id=job_id)
    if default_dir is not None:
        # rmdir fails (suppressed) if the directory is missing or not empty; we
        # only ever want to reap an emptied default directory.
        with contextlib.suppress(OSError):
            default_dir.rmdir()
    # The thumbnail cache is a derivative under the data directory, never a
    # shared location, so it is removed whole (#67 R12).
    remove_project_thumbnails(settings, project_id)
