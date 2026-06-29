"""Bundle a selection of frame image files into a downloadable ``.zip``.

An *export* job is a render-job whose ``kind`` is ``export``: it produces a zip
archive of a chosen set of frame images instead of encoding a video. The bounded
render worker drains it through the same queue as a render, but routes it here
rather than to the encoder (see :mod:`timelapse_manager.render.queue`).

This module is a leaf of the render layer: it reads frame rows and resolves their
on-disk paths through :mod:`timelapse_manager.storage`, and it never imports
anything from the web layer.

Design points:

* **The output is a sibling of the project's renders.** The zip lands in the same
  per-project *render root* a rendered video uses, named ``export-<job_id>.zip``,
  so the existing render-root containment guard applies to it unchanged.
* **A missing source file is skipped, not fatal.** A frame whose image is gone
  from disk (a manual delete, a half-migrated store) is omitted from the archive
  and noted; the export still completes with the frames that are present, rather
  than failing the whole bundle for one absent file.
* **The archive is written atomically.** It is built at a temporary ``.part``
  path and renamed onto the final path only after it closes cleanly, so the final
  path only ever holds a complete zip. The render worker runs this in a worker
  thread; unlike the encoder there is no child process to kill on cancel, so a
  cancelled export is marked failed while its thread may still finish writing the
  temp file -- the temp+rename and the download route's ``done``-status gate make
  that harmless (a never-renamed ``.part`` is never served and is overwritten on
  the next attempt).
* **One audit event on completion.** Unlike a per-frame lifecycle mutation, an
  export is a single batch artifact, so it records exactly one project-scope
  :class:`Event` when the archive is produced, carrying the frame count and the
  job id.
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..db.models import Frame, Project, RenderJob
from ..db.session import session_scope
from ..encode import RenderResult
from ..monitoring.events import EventType, log_event
from ..storage import resolve_absolute
from .spec import project_render_root

logger = logging.getLogger(__name__)


def export_frame_ids(job: RenderJob) -> list[int]:
    """Return the frame-id set an export job carries in ``output_settings``.

    An export job stows its selection under the ``frame_ids`` key of the
    render-job's ``output_settings`` JSON (the column an encode job uses for its
    encode settings, which an export job does not need). A malformed or absent
    value yields an empty list, which the builder treats as "nothing to bundle".
    """
    raw = (job.output_settings or {}).get("frame_ids")
    if not isinstance(raw, list):
        return []
    ids: list[int] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, int):
            continue
        ids.append(item)
    return ids


def build_export_zip(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    job_id: int,
) -> RenderResult:
    """Build the export zip for ``job_id`` and return a render-shaped result.

    Reads the job's frame-id set (see :func:`export_frame_ids`), resolves each
    frame's on-disk image path, and streams the files that exist into a zip under
    the project's render root. A frame that is unknown, off the named project, or
    missing its file on disk is skipped and counted as such; the archive is still
    produced from the frames that are present.

    Returns a :class:`RenderResult` shaped exactly like the encoder's so the
    queue's result recorder persists it unchanged: ``output_path`` is the zip on
    success, and ``container`` is ``zip`` (there is no video codec). Writes one
    project-scope completion :class:`Event` carrying the bundled frame count.

    Synchronous; the worker calls it via a thread executor.
    """
    resolved = _resolve_job_sources(settings, session_factory, job_id)
    if resolved is None:
        return RenderResult(
            success=False,
            output_path=None,
            duration_seconds=None,
            browser_streamable=False,
            codec="",
            container="zip",
            error=f"export job {job_id} no longer exists",
        )
    project_id, render_root, sources = resolved
    render_root.mkdir(parents=True, exist_ok=True)
    final_path = render_root / f"export-{job_id}.zip"
    temp_path = render_root / f"export-{job_id}.zip.part"

    bundled = _write_zip(temp_path, sources)
    temp_path.replace(final_path)

    _record_export_event(
        session_factory,
        project_id=project_id,
        job_id=job_id,
        frame_count=bundled,
    )
    return RenderResult(
        success=True,
        output_path=final_path,
        duration_seconds=None,
        browser_streamable=False,
        codec="",
        container="zip",
    )


def _resolve_job_sources(
    settings: Settings,
    session_factory: sessionmaker[Session],
    job_id: int,
) -> tuple[int, Path, list[tuple[int, Path, str]]] | None:
    """Resolve an export job into its project id, render root, and source files.

    Returns ``(project_id, render_root, sources)`` where ``sources`` is the list
    of ``(frame_id, absolute_path, arcname)`` tuples for the frames that belong to
    the job's project and carry a stored file path -- ordered by the job's
    frame-id list, each path resolved to an absolute on-disk location through the
    storage resolver. Returns ``None`` when the job row has vanished. A frame id
    that is unknown or off the job's project is dropped here; a frame whose
    resolved file is missing on disk is kept in the list and dropped later by the
    file-existence check in :func:`_write_zip` (so it is counted as absent, not
    silently lost). Synchronous: it reads everything it needs while the session is
    open so no detached row is touched after.
    """
    with session_scope(session_factory) as session:
        job = session.get(RenderJob, job_id)
        if job is None:
            return None
        project_id = job.project_id
        project = session.get(Project, project_id)
        if project is None:
            return None
        render_root = project_render_root(settings, project)
        sources: list[tuple[int, Path, str]] = []
        for frame_id in export_frame_ids(job):
            frame = session.get(Frame, frame_id)
            if (
                frame is None
                or frame.project_id != project_id
                or frame.file_path is None
            ):
                continue
            absolute_path = resolve_absolute(settings, project_id, frame.file_path)
            sources.append((frame_id, absolute_path, _arcname(frame)))
        return project_id, render_root, sources


def _arcname(frame: Frame) -> str:
    """Build the name a frame's image takes inside the archive.

    Uses the stored file's basename, prefixed with the frame's sequence index so
    the archive is ordered and two frames that share a basename cannot collide.
    """
    stem = Path(frame.file_path or "").name or f"frame-{frame.id}"
    return f"{frame.sequence_index:08d}_{stem}"


def _write_zip(temp_path: Path, sources: list[tuple[int, Path, str]]) -> int:
    """Stream the present source files into the zip at ``temp_path``; return count.

    Each source is resolved to its absolute on-disk path through the storage
    resolver by the caller. A source whose file is missing on disk is skipped and
    logged rather than aborting the archive, so one absent frame never voids the
    rest of the bundle. Returns the number of files actually written.
    """
    bundled = 0
    with zipfile.ZipFile(
        temp_path, mode="w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        for frame_id, absolute_path, arcname in sources:
            if not absolute_path.is_file():
                logger.info(
                    "skipping frame %s in export: file %s is missing",
                    frame_id,
                    absolute_path,
                )
                continue
            archive.write(absolute_path, arcname=arcname)
            bundled += 1
    return bundled


def _record_export_event(
    session_factory: sessionmaker[Session],
    *,
    project_id: int,
    job_id: int,
    frame_count: int,
) -> None:
    """Write the single project-scope completion event for a finished export.

    An export is a batch artifact, not a per-frame mutation, so exactly one event
    is recorded. It carries no human actor (the worker is the writer), so
    ``actor_user_id`` is left ``None`` -- the foreign key forbids a fabricated id.
    """
    with session_scope(session_factory) as session:
        log_event(
            session,
            scope="project",
            scope_id=project_id,
            level="info",
            type=EventType.EXPORT_COMPLETE.value,
            message=f"export {job_id} produced a zip of {frame_count} frame(s)",
            metadata={
                "action": "export",
                "render_id": job_id,
                "frame_count": frame_count,
            },
        )
