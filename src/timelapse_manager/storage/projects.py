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
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db.models import Frame, Project, RenderJob
from . import paths

logger = logging.getLogger(__name__)


def _gather_frame_files(
    session: Session, settings: Settings, project: Project
) -> list[Path]:
    """Resolve the absolute paths of every frame file belonging to ``project``."""
    rows = session.execute(
        select(Frame.file_path).where(Frame.project_id == project.id)
    ).all()
    files: list[Path] = []
    for (stored,) in rows:
        if stored is None:
            continue
        files.append(paths.resolve_absolute(settings, project.id, stored))
    return files


def _gather_render_files(session: Session, project: Project) -> list[Path]:
    """Resolve the absolute paths of every render output belonging to ``project``.

    Render rows store an already-absolute ``output_file_path``; a job that never
    produced a file leaves it null and contributes nothing.
    """
    rows = session.execute(
        select(RenderJob.output_file_path).where(RenderJob.project_id == project.id)
    ).all()
    return [Path(stored) for (stored,) in rows if stored]


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
    frame_files = _gather_frame_files(session, settings, project)
    render_files = _gather_render_files(session, project)
    # Only the default-layout directory is safe to remove (a custom storage_path
    # may be shared or operator-meaningful); compute it before the row is gone.
    default_dir: Path | None = (
        paths.frame_dir(settings, project)
        if paths.uses_default_layout(project)
        else None
    )

    session.delete(project)
    session.flush()

    for path in (*frame_files, *render_files):
        with contextlib.suppress(FileNotFoundError, OSError):
            path.unlink()
    if default_dir is not None:
        # rmdir fails (suppressed) if the directory is missing or not empty; we
        # only ever want to reap an emptied default directory.
        with contextlib.suppress(OSError):
            default_dir.rmdir()
