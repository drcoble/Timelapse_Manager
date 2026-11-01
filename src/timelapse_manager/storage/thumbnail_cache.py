"""Cleanup of the on-disk frame-thumbnail cache (#67 P3, R12).

Thumbnails live under ``<data_dir>/thumbnails/<project_id>/`` (see
:func:`~timelapse_manager.storage.paths.thumbnail_cache_dir`). They are a
regenerable derivative, so every function here is best-effort: a failure is
logged and never blocks the operation that triggered it.

* :func:`remove_frame_thumbnails` -- a frame was permanently deleted;
* :func:`remove_project_thumbnails` -- a project was deleted;
* :func:`prune_thumbnail_cache` -- the hourly housekeeping pass: directories of
  projects that no longer exist, and in-progress files ffmpeg left behind.

Nothing here follows a symlink or deletes outside the cache root: directories
are reached through :func:`~timelapse_manager.storage.paths.contained_thumbnail_dir`,
and the sweep skips any entry that is not a plain directory or file.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import paths

if TYPE_CHECKING:
    from ..config import Settings

logger = logging.getLogger(__name__)

# An in-progress thumbnail older than this is abandoned (a crash or kill mid
# write); a live ffmpeg run is bounded far below it by the generation timeout.
TEMP_FILE_MAX_AGE_SECONDS = 3600.0


@dataclass(frozen=True)
class ThumbnailPruneResult:
    """What one :func:`prune_thumbnail_cache` pass removed."""

    orphan_dirs: int
    temp_files: int


def remove_frame_thumbnails(settings: Settings, project_id: int, frame_id: int) -> None:
    """Unlink every cached size of one frame's thumbnail; tolerate missing files."""
    try:
        cache_dir = paths.contained_thumbnail_dir(settings, project_id)
    except paths.PathContainmentError:
        logger.warning(
            "not removing thumbnails of frame %s: the cache of project %s is "
            "outside the thumbnail root",
            frame_id,
            project_id,
        )
        return
    for width in sorted(paths.THUMBNAIL_WIDTHS):
        target = cache_dir / paths.thumbnail_file_name(frame_id, width)
        try:
            target.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            logger.warning("could not remove thumbnail %s", target, exc_info=True)


def remove_project_thumbnails(settings: Settings, project_id: int) -> None:
    """Remove a project's whole thumbnail cache directory; tolerate its absence."""
    try:
        cache_dir = paths.contained_thumbnail_dir(settings, project_id)
    except paths.PathContainmentError:
        logger.warning(
            "not removing the thumbnail cache of project %s: outside the "
            "thumbnail root",
            project_id,
        )
        return
    _remove_tree(cache_dir)


def prune_thumbnail_cache(
    settings: Settings,
    live_project_ids: Callable[[], set[int]],
    *,
    now: float,
    temp_max_age_seconds: float = TEMP_FILE_MAX_AGE_SECONDS,
) -> ThumbnailPruneResult:
    """Remove orphaned project caches and abandoned in-progress thumbnails.

    The cache root is listed *before* ``live_project_ids`` is called, so a
    project created during the pass is in the id set and its new directory is
    never taken for an orphan. Only entries named by a decimal project id are
    considered; anything else under the root is left alone, as are symlinks.

    :param live_project_ids: returns the ids of every existing project.
    :param now: the current wall-clock time (``time.time()``).
    :param temp_max_age_seconds: a ``*.tmp-*`` file older than this is removed.
    """
    root = paths.thumbnail_cache_root(settings)
    try:
        with os.scandir(root) as entries:
            listed_ids = [
                int(entry.name)
                for entry in entries
                if entry.name.isascii()
                and entry.name.isdigit()
                and entry.is_dir(follow_symlinks=False)
            ]
    except FileNotFoundError:
        return ThumbnailPruneResult(orphan_dirs=0, temp_files=0)

    live = live_project_ids()
    orphan_dirs = 0
    temp_files = 0
    for project_id in listed_ids:
        try:
            cache_dir = paths.contained_thumbnail_dir(settings, project_id)
        except paths.PathContainmentError:
            continue
        if project_id not in live:
            if _remove_tree(cache_dir):
                orphan_dirs += 1
            continue
        temp_files += _remove_stale_temp_files(cache_dir, now - temp_max_age_seconds)
    if orphan_dirs or temp_files:
        logger.info(
            "thumbnail cache: removed %d orphaned project dir(s), %d stale temp "
            "file(s)",
            orphan_dirs,
            temp_files,
        )
    return ThumbnailPruneResult(orphan_dirs=orphan_dirs, temp_files=temp_files)


def _remove_tree(cache_dir: Path) -> bool:
    """Remove ``cache_dir`` recursively; return whether it was removed."""
    if cache_dir.is_symlink():
        return False
    try:
        shutil.rmtree(cache_dir)
    except FileNotFoundError:
        return False
    except OSError:
        logger.warning("could not remove thumbnail cache %s", cache_dir, exc_info=True)
        return False
    return True


def _remove_stale_temp_files(cache_dir: Path, cutoff: float) -> int:
    """Unlink in-progress thumbnail files last modified before ``cutoff``."""
    removed = 0
    try:
        with os.scandir(cache_dir) as entries:
            stale = [
                Path(entry.path)
                for entry in entries
                if paths.THUMBNAIL_TEMP_MARKER in entry.name
                and entry.is_file(follow_symlinks=False)
                and entry.stat(follow_symlinks=False).st_mtime < cutoff
            ]
    except FileNotFoundError:
        return 0
    for path in stale:
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            logger.warning("could not remove %s", path, exc_info=True)
            continue
        removed += 1
    return removed
