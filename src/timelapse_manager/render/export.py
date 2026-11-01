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
* **Only contained files are bundled (#67 S16).** Each frame path goes through
  the same containment check as the frame-image route: a stored path that
  resolves outside the project's frame directories (a traversal, an absolute
  escape, a symlink out of the tree) is skipped with a WARNING and counted as
  absent, and the zip reads the checked real path, not the symlinked one.
* **The archive is written atomically.** It is built at a temporary ``.part``
  path and renamed onto the final path only after it closes cleanly, so the final
  path only ever holds a complete zip. The render worker runs this in a worker
  thread; unlike the encoder there is no child process to kill on cancel, so a
  cancelled export is marked failed while its thread may still finish writing the
  temp file -- the temp+rename and the download route's ``done``-status gate make
  that harmless (a never-renamed ``.part`` is never served and is overwritten on
  the next attempt). Just before the rename the builder re-reads the job: when it
  was cancelled (``failed``) or deleted meanwhile, the ``.part`` is removed and no
  final zip appears, so a delete that cancels a running export first never leaves
  an orphan archive behind.
* **Images are stored, not deflated (#67 P7).** JPEG, PNG and WebP are already
  compressed, so deflating them burns CPU for about nothing; they are written
  ``ZIP_STORED``. Only other members (a JSON manifest, say) are deflated.
* **Disk space is checked before and while writing (#67 P7).** The archive is
  about the sum of its files, so the build fails early with ``insufficient disk
  space`` when that (plus 1%) would take the volume under the low watermark, and
  re-checks every 200 files so a volume filling for other reasons aborts cleanly.
  A ``.part`` file is removed on *any* failure, not only on a cancel, and the
  queue deletes stale ``.part`` files when it starts (:func:`remove_stale_parts`).
* **The frame window is recorded.** On success the job row gets the number of
  frames actually bundled and the capture times of the first and last of them,
  so the export card can show what the zip holds.
* **One audit event on completion.** Unlike a per-frame lifecycle mutation, an
  export is a single batch artifact, so it records exactly one project-scope
  :class:`Event` when the archive is produced, carrying the frame count and the
  job id.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import stat
import time
import zipfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..db.models import Frame, Project, RenderJob
from ..db.session import session_scope
from ..encode import RenderResult
from ..monitoring.events import EventType, log_event
from ..storage.frames import load_frames_by_id
from ..storage.paths import (
    PathContainmentError,
    frame_boundaries,
    resolve_contained_frame_path,
)
from .outputs import unlink_confined
from .spec import project_render_root

logger = logging.getLogger(__name__)

#: Reason recorded on an export refused or aborted for lack of disk space.
INSUFFICIENT_DISK_REASON = "insufficient disk space"
# Past this many bytes a zip member needs the ZIP64 extension (zipfile's own limit).
_ZIP64_LIMIT = zipfile.ZIP64_LIMIT
#: Frame image suffixes already compressed: stored in the zip, not deflated.
_STORED_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
#: Slack added to the size estimate for the zip's own headers (1%).
_ESTIMATE_SLACK = 1.01
#: Files written between two mid-write free-space checks.
_SPACE_CHECK_EVERY = 200


class _Source(NamedTuple):
    """One frame file to bundle: its id, on-disk path, archive name, capture time."""

    frame_id: int
    absolute_path: Path
    arcname: str
    capture_timestamp: datetime | None


def _nearest_existing(path: Path) -> Path:
    """Return ``path`` or its closest existing ancestor.

    A render root may not exist yet when its volume's free space is probed, and
    :func:`shutil.disk_usage` raises on a missing path. Walking up to the nearest
    existing parent yields the same volume's free space without requiring the leaf
    to exist; the filesystem root is the guaranteed terminator.
    """
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            return current
        current = parent
    return current


def free_space_below_low_watermark(output_dir: Path, settings: Settings) -> bool:
    """Return whether free space on ``output_dir``'s volume is below the floor.

    Probes the volume holding ``output_dir`` (or its nearest existing ancestor,
    since the render root may not exist yet) and compares the reading against the
    same low watermark capture uses to pause: free space is "below" when it sits
    under ``low_watermark_bytes`` *or* under ``low_watermark_percent`` of the
    volume -- the conservative reading, so either floor refuses the write. Renders
    and exports gate on this before writing so a job can never start an output
    that would fill the disk and wedge the system.
    """
    usage = shutil.disk_usage(_nearest_existing(output_dir))
    low_bytes = settings.storage.low_watermark_bytes
    low_percent = settings.storage.low_watermark_percent
    free_percent = (usage.free / usage.total * 100.0) if usage.total > 0 else 0.0
    return usage.free < low_bytes or free_percent < low_percent


def free_bytes_above_floor(output_dir: Path, settings: Settings) -> int:
    """Return the bytes writable on ``output_dir``'s volume before the floor.

    Free space minus the larger of the two low-watermark floors (bytes and
    percent), the same reading as :func:`free_space_below_low_watermark`. Negative
    when the volume is already under the floor.
    """
    usage = shutil.disk_usage(_nearest_existing(output_dir))
    floor = max(
        settings.storage.low_watermark_bytes,
        int(usage.total * settings.storage.low_watermark_percent / 100.0),
    )
    return usage.free - floor


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
        return _failed(f"export job {job_id} no longer exists")
    project_id, render_root, sources = resolved
    if free_space_below_low_watermark(render_root, settings):
        logger.warning(
            "export job %s refused: free space on %s is below the low watermark",
            job_id,
            render_root,
        )
        return _failed(INSUFFICIENT_DISK_REASON)
    estimate = _estimate_bytes(sources)
    if free_bytes_above_floor(render_root, settings) < estimate:
        logger.warning(
            "export job %s refused: about %d byte(s) needed but free space on %s "
            "would fall under the low watermark",
            job_id,
            estimate,
            render_root,
        )
        return _failed(INSUFFICIENT_DISK_REASON)
    render_root.mkdir(parents=True, exist_ok=True)
    final_path = render_root / f"export-{job_id}.zip"
    temp_path = render_root / f"export-{job_id}.zip.part"

    def has_room(remaining: int) -> bool:
        return free_bytes_above_floor(render_root, settings) >= remaining

    try:
        bundled = _write_zip(temp_path, sources, has_room=has_room)
        if bundled is None:
            logger.warning(
                "export job %s aborted: free space on %s ran out while writing",
                job_id,
                render_root,
            )
            _discard(temp_path)
            return _failed(INSUFFICIENT_DISK_REASON)
        if not _still_wanted(session_factory, job_id):
            # Cancelled (or deleted) while the zip was being written: never let
            # the archive appear under a row that is failed or gone.
            _discard(temp_path)
            return _failed(f"export job {job_id} was cancelled")
        temp_path.replace(final_path)
    except BaseException:
        # Any failure (ENOSPC, a read error, a DB error) leaves no half archive.
        _discard(temp_path)
        raise

    _record_export_complete(
        session_factory,
        project_id=project_id,
        job_id=job_id,
        bundled=bundled,
    )
    return RenderResult(
        success=True,
        output_path=final_path,
        duration_seconds=None,
        browser_streamable=False,
        codec="",
        container="zip",
    )


def _failed(error: str) -> RenderResult:
    """Return the failed, render-shaped result for an export that made no zip."""
    return RenderResult(
        success=False,
        output_path=None,
        duration_seconds=None,
        browser_streamable=False,
        codec="",
        container="zip",
        error=error,
    )


def _discard(path: Path) -> None:
    """Remove a temporary archive; a file that is already gone is fine."""
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def _file_size(path: Path) -> int:
    """Return a file's size, or 0 when it is missing or unreadable."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _estimate_bytes(sources: list[_Source]) -> int:
    """Estimate the archive size: the sum of the source files, plus 1%."""
    return int(sum(_file_size(s.absolute_path) for s in sources) * _ESTIMATE_SLACK)


def _still_wanted(session_factory: sessionmaker[Session], job_id: int) -> bool:
    """Return whether the export row still exists and was not cancelled."""
    with session_scope(session_factory) as session:
        job = session.get(RenderJob, job_id)
        return job is not None and job.status != "failed"


def _resolve_job_sources(
    settings: Settings,
    session_factory: sessionmaker[Session],
    job_id: int,
) -> tuple[int, Path, list[_Source]] | None:
    """Resolve an export job into its project id, render root, and source files.

    Returns ``(project_id, render_root, sources)`` where ``sources`` is the list
    of :class:`_Source` entries for the frames that belong to
    the job's project and carry a stored file path -- ordered by the job's
    frame-id list, each path resolved to an absolute on-disk location through the
    storage resolver. Returns ``None`` when the job row has vanished. A frame id
    that is unknown or off the job's project is dropped here, and so is a frame
    whose path resolves outside the project's frame directories (one WARNING for
    the job, #67 S16): neither is bundled, so both count as absent. A frame whose
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
        frame_ids = export_frame_ids(job)
        # Chunked IN loads instead of a get per frame (#66 C7); the selection
        # order, duplicates and the project check are kept by walking the
        # original id list.
        frames = load_frames_by_id(session, frame_ids)
        boundaries = frame_boundaries(settings, project)
        sources: list[_Source] = []
        escaped: list[int] = []
        for frame_id in frame_ids:
            frame = frames.get(frame_id)
            if (
                frame is None
                or frame.project_id != project_id
                or frame.file_path is None
            ):
                continue
            try:
                absolute_path = resolve_contained_frame_path(
                    settings, project, frame.file_path, boundaries=boundaries
                )
            except PathContainmentError:
                escaped.append(frame_id)
                continue
            sources.append(
                _Source(
                    frame_id,
                    absolute_path,
                    _arcname(frame),
                    frame.capture_timestamp,
                )
            )
        if escaped:
            logger.warning(
                "export job %s: skipped %d frame(s) whose path is outside project "
                "%s's frame directory (first frame id %s)",
                job_id,
                len(escaped),
                project_id,
                escaped[0],
            )
        return project_id, render_root, sources


def _arcname(frame: Frame) -> str:
    """Build the name a frame's image takes inside the archive.

    Uses the stored file's basename, prefixed with the frame's sequence index so
    the archive is ordered and two frames that share a basename cannot collide.
    """
    stem = Path(frame.file_path or "").name or f"frame-{frame.id}"
    return f"{frame.sequence_index:08d}_{stem}"


def _write_zip(
    temp_path: Path,
    sources: list[_Source],
    *,
    has_room: Callable[[int], bool] | None = None,
) -> list[_Source] | None:
    """Stream the present source files into the zip at ``temp_path``.

    Each source carries the real on-disk path the caller already checked for
    containment (#67 S16). A source whose file is missing on disk is skipped and
    logged rather than aborting the archive, so one absent frame never voids the
    rest of the bundle. Already-compressed images are stored, not deflated.

    ``has_room(remaining_bytes)`` is asked every :data:`_SPACE_CHECK_EVERY` files
    whether the volume still has room for the files not yet written; when it says
    no, the write stops and ``None`` is returned (the caller removes the part
    file). Returns the sources actually written, in archive order.
    """
    present = [s for s in sources if s.absolute_path.is_file()]
    for source in sources:
        if source not in present:
            logger.info(
                "skipping frame %s in export: file %s is missing",
                source.frame_id,
                source.absolute_path,
            )
    remaining = sum(_file_size(s.absolute_path) for s in present)
    bundled: list[_Source] = []
    with zipfile.ZipFile(
        temp_path, mode="w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        for index, source in enumerate(present):
            if (
                has_room is not None
                and index
                and index % _SPACE_CHECK_EVERY == 0
                and not has_room(int(remaining * _ESTIMATE_SLACK))
            ):
                return None
            stored = source.absolute_path.suffix.lower() in _STORED_SUFFIXES
            size = _write_member(
                archive,
                source,
                compress_type=zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED,
            )
            if size is None:
                continue
            remaining -= size
            bundled.append(source)
    return bundled


def _write_member(
    archive: zipfile.ZipFile, source: _Source, *, compress_type: int
) -> int | None:
    """Copy one source file into ``archive``; return its size, or ``None`` if skipped.

    The file is opened with ``O_NOFOLLOW``: the path was checked for containment
    earlier (:func:`_resolve_job_sources`) and is a real path, so a symlink at the
    leaf means it was swapped in after the check and the file is skipped, not
    read (#172). The archive is fed from the opened descriptor, so the bytes
    bundled are the ones of the file that was opened. A parent directory swapped
    after the check is not caught; that needs write access to the frame tree.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(source.absolute_path, flags)
    except OSError as exc:
        logger.warning(
            "skipping frame %s in export: cannot open %s safely (%s)",
            source.frame_id,
            source.absolute_path,
            exc.strerror or type(exc).__name__,
        )
        return None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            logger.warning(
                "skipping frame %s in export: %s is not a regular file",
                source.frame_id,
                source.absolute_path,
            )
            return None
        member = zipfile.ZipInfo(
            source.arcname, date_time=time.localtime(max(info.st_mtime, 315532800))[:6]
        )
        member.compress_type = compress_type
        member.external_attr = (info.st_mode & 0xFFFF) << 16
        with archive.open(member, "w", force_zip64=info.st_size > _ZIP64_LIMIT) as out:
            shutil.copyfileobj(handle, out)
    return info.st_size


def remove_stale_parts(
    settings: Settings, session_factory: sessionmaker[Session]
) -> int:
    """Delete ``export-*.zip.part`` files left by a build that never finished.

    Run once when the render queue starts, after orphaned ``encoding`` jobs were
    failed: no export thread exists then, so a part file whose job is not
    ``encoding`` is a leftover from a crash or kill. Each file is removed only
    when it resolves inside its project's render root. Synchronous; returns the
    number of files removed.
    """
    removed = 0
    with session_scope(session_factory) as session:
        projects = list(session.scalars(select(Project)))
        encoding = set(
            session.scalars(
                select(RenderJob.id).where(
                    RenderJob.kind == "export", RenderJob.status == "encoding"
                )
            )
        )
        roots = [project_render_root(settings, p) for p in projects]
    for root in roots:
        if not root.is_dir():
            continue
        resolved_root = root.resolve()
        for part in root.glob("export-*.zip.part"):
            stem = part.name.removeprefix("export-").removesuffix(".zip.part")
            if stem.isdigit() and int(stem) in encoding:
                continue
            if unlink_confined(
                part, resolved_root, job_id=int(stem) if stem.isdigit() else 0
            ):
                removed += 1
    if removed:
        logger.info("removed %d stale export part file(s)", removed)
    return removed


def _record_export_complete(
    session_factory: sessionmaker[Session],
    *,
    project_id: int,
    job_id: int,
    bundled: list[_Source],
) -> None:
    """Record the bundled frame window and the single completion event.

    The job row gets the bundled frame count and the capture times of the first
    and last bundled frame (the selection is ordered by sequence, so the min/max
    are taken rather than assuming order). An export is a batch artifact, not a
    per-frame mutation, so exactly one event is recorded. It carries no human
    actor (the worker is the writer), so ``actor_user_id`` is left ``None`` -- the
    foreign key forbids a fabricated id.
    """
    frame_count = len(bundled)
    times = [s.capture_timestamp for s in bundled if s.capture_timestamp is not None]
    with session_scope(session_factory) as session:
        job = session.get(RenderJob, job_id)
        if job is not None:
            job.frame_count = frame_count
            job.first_frame_at = min(times) if times else None
            job.last_frame_at = max(times) if times else None
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
