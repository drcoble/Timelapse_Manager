"""Remove a render job's output file from disk, confined to its render root.

The single unlink used everywhere a render or export artifact is reclaimed: the
automatic prune (:mod:`.post_actions`) and the manual delete
(:mod:`timelapse_manager.services.renders`). Keeping one implementation means the
containment guard -- a stored path that does not resolve inside the project's
render root is never deleted -- cannot drift between the two callers.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..db.models import RenderJob

logger = logging.getLogger(__name__)


def unlink_confined(path: Path, render_root: Path, *, job_id: int) -> bool:
    """Unlink ``path`` when it resolves inside ``render_root``; return whether it did.

    ``render_root`` must already be resolved. A path outside the root is left on
    disk and logged, so a corrupted or tampered row can never trigger a delete
    elsewhere. A file that is already gone is not an error (returns ``False``).
    """
    resolved = path.resolve()
    if not resolved.is_relative_to(render_root):
        logger.warning(
            "not deleting output of render %s: %s is outside render root %s",
            job_id,
            resolved,
            render_root,
        )
        return False
    try:
        resolved.unlink()
    except FileNotFoundError:
        return False
    return True


def delete_render_output(job: RenderJob, render_root: Path) -> bool:
    """Unlink a job's recorded output file when confined to the render root.

    Never touches frames or anything outside the render root: a stored path that
    does not resolve inside it is left on disk (only the row is removed by the
    caller). Returns whether a file was removed; a job with no recorded output, or
    whose file is already missing, returns ``False``.
    """
    if not job.output_file_path:
        return False
    return unlink_confined(Path(job.output_file_path), render_root, job_id=job.id)
