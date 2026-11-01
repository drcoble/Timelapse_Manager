"""Read-only report of frame files that no ``Frame`` row refers to (#67 R11).

The frame writer puts the file on disk before it commits the row, so a crash (or
power loss) between the two steps leaves a file with no row: harmless, but it
holds disk space nobody sees. A failed commit removes its own file; this report
finds what a crash left. It lists and deletes nothing, so an operator decides.

Only the files directly inside each project's frame directory are examined (the
writer never nests), and a file younger than ``min_age_seconds`` is skipped: it
may belong to a capture or import that has written the file and not yet
committed the row.
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from ..db.models import Frame, Project
from .paths import ProjectRef, frame_dir, resolve_absolute

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ..config import Settings

logger = logging.getLogger(__name__)

# Default grace period before a row-less file counts as an orphan.
DEFAULT_MIN_AGE_SECONDS = 60.0

# Suffix of the writer's temporary file (``tempfile.mkstemp(suffix=".tmp")``): a
# crash before the rename leaves one of these rather than a finished frame file.
_TEMP_SUFFIX = ".tmp"

# Rows are streamed in pages of this size, so a large project is never held whole.
_ROW_PAGE = 5000


@dataclass(frozen=True)
class OrphanFile:
    """One file in a frame directory that no frame row refers to.

    :param project_id: the project whose frame directory holds the file.
    :param path: absolute path of the file.
    :param size_bytes: its size.
    :param kind: ``"temp"`` for a leftover temporary file, else ``"frame"`` (a
        finished image whose row was never committed).
    """

    project_id: int
    path: str
    size_bytes: int
    kind: str


def find_orphan_files(
    session: Session,
    settings: Settings,
    *,
    project_id: int | None = None,
    min_age_seconds: float = DEFAULT_MIN_AGE_SECONDS,
    now: float | None = None,
) -> list[OrphanFile]:
    """Return the files under project frame directories that have no frame row.

    Every frame row counts, whatever its lifecycle state, so a soft-deleted
    frame's file is not an orphan. Projects that share one directory (an explicit
    ``storage_path`` may be reused) are examined together, so one project's files
    are never reported against another. A directory that does not exist or cannot
    be read is skipped with a warning.

    :param project_id: restrict the report to this project's directory.
    :param min_age_seconds: ignore files modified more recently than this.
    :param now: wall-clock seconds to measure age against (for tests).
    """
    current = time.time() if now is None else now
    projects = session.execute(select(Project.id, Project.storage_path)).all()
    by_directory: dict[str, list[int]] = defaultdict(list)
    for pid, storage_path in projects:
        directory = os.path.normpath(frame_dir(settings, ProjectRef(pid, storage_path)))
        by_directory[directory].append(pid)

    wanted = [
        directory
        for directory, members in by_directory.items()
        if project_id is None or project_id in members
    ]
    orphans: list[OrphanFile] = []
    for directory in sorted(wanted):
        members = by_directory[directory]
        known = _known_paths(session, settings, members)
        owner = project_id if project_id is not None else members[0]
        orphans.extend(
            _scan_directory(directory, owner, known, current - min_age_seconds)
        )
    return orphans


def _known_paths(
    session: Session, settings: Settings, project_ids: list[int]
) -> set[str]:
    """Return the normalised absolute path of every frame row of these projects."""
    known: set[str] = set()
    for pid in project_ids:
        stmt = (
            select(Frame.file_path)
            .where(Frame.project_id == pid)
            .execution_options(yield_per=_ROW_PAGE)
        )
        for (stored,) in session.execute(stmt):
            if stored:
                known.add(os.path.normpath(resolve_absolute(settings, pid, stored)))
    return known


def _scan_directory(
    directory: str, project_id: int, known: set[str], newest_mtime: float
) -> list[OrphanFile]:
    """Return the unreferenced regular files in ``directory`` older than the cut."""
    found: list[OrphanFile] = []
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return found
    except OSError as exc:
        logger.warning("cannot read frame directory %s: %s", directory, exc)
        return found
    for entry in sorted(entries, key=lambda e: e.name):
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if os.path.normpath(entry.path) in known or info.st_mtime > newest_mtime:
            continue
        found.append(
            OrphanFile(
                project_id=project_id,
                path=entry.path,
                size_bytes=info.st_size,
                kind="temp" if entry.name.endswith(_TEMP_SUFFIX) else "frame",
            )
        )
    return found
