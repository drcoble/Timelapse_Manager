"""WAL-safe SQLite copies: pre-migration snapshots, ``db backup`` and ``db restore``.

Every copy goes through SQLite's online backup API (``sqlite3.Connection.backup``),
never a file copy. The API reads the source inside one read transaction, so the
copy is a consistent point-in-time image that includes pages still in the
``-wal`` file, and it works while the service is writing (issue #67, R6).

A finished copy is switched to ``journal_mode=DELETE`` so it is one
self-contained file with no ``-wal``/``-shm`` sidecars, and is checked with
``PRAGMA integrity_check`` before it is trusted. Files are created ``O_EXCL``
with mode ``0600``: the database holds password hashes and encrypted secrets,
and an exclusive create never follows a planted symlink.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Pre-migration snapshots kept beside the database (newest first). One more may
# be kept: see :func:`prune_snapshots`.
SNAPSHOT_KEEP = 3

# A snapshot needs room for the copy plus headroom for the migration itself
# (which can grow the database and its WAL), so require twice the live size.
SNAPSHOT_SPACE_FACTOR = 2

# Matches the timestamp written into snapshot and set-aside names.
_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S"

# Sidecar files SQLite keeps next to a database. A stale ``-journal`` next to a
# restored file would be rolled back into it, and a stale ``-wal`` replayed, so
# both travel with the file they belong to.
_MOVED_SIDECARS = ("-wal", "-journal")
_DROPPED_SIDECARS = ("-shm",)

# How long a copy waits for a competing writer's lock.
_BUSY_TIMEOUT_SECONDS = 15.0

_NEW_FILE_MODE = 0o600


class BackupError(RuntimeError):
    """A database copy could not be made, verified or put in place."""


@dataclass(frozen=True)
class Snapshot:
    """A pre-migration snapshot file and the parts of its name."""

    path: Path
    revision: str
    taken_at: str
    sequence: int


def utc_stamp(now: datetime | None = None) -> str:
    """The UTC ``yyyymmddThhmmss`` stamp used in snapshot and set-aside names."""
    return (now or datetime.now(UTC)).astimezone(UTC).strftime(_TIMESTAMP_FORMAT)


def database_size(path: Path) -> int:
    """Bytes used by the database file plus its ``-wal`` file (0 if absent)."""
    total = 0
    for candidate in (path, _sidecar(path, "-wal")):
        with contextlib.suppress(FileNotFoundError):
            total += candidate.stat().st_size
    return total


def read_revision(path: Path) -> str | None:
    """The Alembic revision recorded in the database file at ``path``."""
    conn = _connect(path)
    try:
        return revision_of(conn)
    finally:
        conn.close()


def revision_of(conn: sqlite3.Connection) -> str | None:
    """The Alembic revision recorded in an open database.

    ``None`` when there is no ``alembic_version`` table or no row (a new,
    never-migrated database). Several rows (branched heads) are joined with
    ``+`` so the value is still usable in a file name.
    """
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'alembic_version'"
    ).fetchone()
    if has_table is None:
        return None
    rows = conn.execute(
        "SELECT version_num FROM alembic_version ORDER BY version_num"
    ).fetchall()
    if not rows:
        return None
    return "+".join(str(row[0]) for row in rows)


def integrity_problems(conn: sqlite3.Connection) -> list[str]:
    """Run ``PRAGMA integrity_check``; an empty list means the copy is sound."""
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    messages = [str(row[0]) for row in rows]
    return [] if messages == ["ok"] else messages


def backup_file_to_file(source: Path, destination: Path) -> None:
    """Copy ``source`` to a new file ``destination`` and verify the copy.

    ``destination`` must not exist. On any failure it is removed and
    :class:`BackupError` is raised, so a half-written copy is never left behind.
    """
    _create_exclusive(destination)
    try:
        src = _connect(source)
        try:
            dst = sqlite3.connect(str(destination))
            try:
                src.backup(dst)
                _finish_copy(dst, destination)
            finally:
                dst.close()
        finally:
            src.close()
    except BaseException:
        _unlink_quietly(destination)
        raise


def backup_file_to_memory(source: Path) -> sqlite3.Connection:
    """A consistent in-memory copy of ``source`` (the caller closes it)."""
    src = _connect(source)
    try:
        memory = sqlite3.connect(":memory:")
        try:
            src.backup(memory)
        except BaseException:
            memory.close()
            raise
    finally:
        src.close()
    return memory


def backup_memory_to_file(memory: sqlite3.Connection, destination: Path) -> None:
    """Write an in-memory database to a new file ``destination`` and verify it."""
    _create_exclusive(destination)
    try:
        dst = sqlite3.connect(str(destination))
        try:
            memory.backup(dst)
            _finish_copy(dst, destination)
        finally:
            dst.close()
    except BaseException:
        _unlink_quietly(destination)
        raise


def snapshot_name(database: Path, revision: str, stamp: str) -> str:
    """``<db name>.pre-<revision>-<stamp>``.

    For example ``timelapse.db.pre-032_render_job_codec-20261027T120500``.
    """
    return f"{database.name}.pre-{revision}-{stamp}"


def list_snapshots(database: Path) -> list[Snapshot]:
    """Pre-migration snapshots of ``database``, oldest first."""
    pattern = re.compile(
        rf"^{re.escape(database.name)}\.pre-(?P<rev>.+)-(?P<ts>\d{{8}}T\d{{6}})"
        r"(?:-(?P<seq>\d+))?$"
    )
    found: list[Snapshot] = []
    for entry in database.parent.iterdir():
        match = pattern.match(entry.name)
        if match is None or not entry.is_file() or entry.is_symlink():
            continue
        found.append(
            Snapshot(
                path=entry,
                revision=match.group("rev"),
                taken_at=match.group("ts"),
                sequence=int(match.group("seq") or 0),
            )
        )
    found.sort(key=lambda snap: (snap.taken_at, snap.sequence, snap.path.name))
    return found


def prune_snapshots(
    database: Path, current_revision: str, keep: int = SNAPSHOT_KEEP
) -> list[Path]:
    """Delete old snapshots, keeping the newest ``keep``; returns what was removed.

    The oldest snapshot taken at ``current_revision`` is also kept. If a
    migration fails part-way and systemd restarts the service, every retry
    snapshots the half-migrated database at the same revision; without this
    rule a few retries would prune away the only copy taken before the first
    attempt.
    """
    snapshots = list_snapshots(database)
    protected = {snap.path for snap in snapshots[-keep:]} if keep > 0 else set()
    first_at_current = next(
        (snap for snap in snapshots if snap.revision == current_revision), None
    )
    if first_at_current is not None:
        protected.add(first_at_current.path)
    removed: list[Path] = []
    for snap in snapshots:
        if snap.path in protected:
            continue
        try:
            snap.path.unlink()
        except FileNotFoundError:
            continue
        removed.append(snap.path)
    return removed


def take_snapshot(database: Path, revision: str, now: datetime | None = None) -> Path:
    """Write a verified pre-migration snapshot of ``database`` beside it.

    Raises :class:`BackupError` when free space is short (less than
    :data:`SNAPSHOT_SPACE_FACTOR` times the database plus WAL) or the copy
    fails its integrity check. Then prunes older snapshots.
    """
    needed = SNAPSHOT_SPACE_FACTOR * database_size(database)
    free = shutil.disk_usage(database.parent).free
    if free < needed:
        raise BackupError(
            f"not enough free space in {database.parent} for a pre-migration "
            f"snapshot: {free} bytes free, {needed} needed"
        )
    stamp = utc_stamp(now)
    base = snapshot_name(database, revision, stamp)
    target = database.with_name(base)
    sequence = 0
    while target.exists() or target.is_symlink():
        sequence += 1
        target = database.with_name(f"{base}-{sequence}")
    backup_file_to_file(database, target)
    for removed in prune_snapshots(database, revision):
        logger.info("removed old pre-migration snapshot %s", removed)
    return target


def restore_into_place(memory: sqlite3.Connection, database: Path) -> Path | None:
    """Replace ``database`` with the in-memory copy; returns the set-aside path.

    The new file is written next to the live one first, so a failure leaves the
    live database untouched. Then the live file is renamed to
    ``<db>.replaced-<stamp>`` (its ``-wal``/``-journal`` renamed with it, so the
    set-aside copy stays complete), its ``-shm`` deleted, and the new file
    renamed into place. Returns ``None`` when there was no live database.
    """
    stamp = utc_stamp()
    staged = database.with_name(f"{database.name}.restore-{stamp}.tmp")
    backup_memory_to_file(memory, staged)
    with contextlib.suppress(FileNotFoundError):
        os.chmod(staged, database.stat().st_mode & 0o777)

    replaced: Path | None = None
    if database.exists():
        replaced = database.with_name(f"{database.name}.replaced-{stamp}")
        if replaced.exists() or replaced.is_symlink():
            _unlink_quietly(staged)
            raise BackupError(f"{replaced} already exists; try again in a second")
        os.replace(database, replaced)
        for suffix in _MOVED_SIDECARS:
            with contextlib.suppress(FileNotFoundError):
                os.replace(_sidecar(database, suffix), _sidecar(replaced, suffix))
    for suffix in _DROPPED_SIDECARS:
        _unlink_quietly(_sidecar(database, suffix))
    # A stray sidecar with no live database would be applied to the restored
    # file; move it aside under the restore stamp instead of deleting it.
    for suffix in _MOVED_SIDECARS:
        stray = _sidecar(database, suffix)
        if stray.exists():
            os.replace(stray, stray.with_name(f"{stray.name}.orphan-{stamp}"))
    os.replace(staged, database)
    return replaced


def _connect(path: Path) -> sqlite3.Connection:
    """Open an existing database file (never create one)."""
    if not path.is_file():
        raise BackupError(f"database {path} does not exist")
    conn = sqlite3.connect(str(path), timeout=_BUSY_TIMEOUT_SECONDS)
    return conn


def _finish_copy(dst: sqlite3.Connection, destination: Path) -> None:
    """Make the copy self-contained and check it."""
    dst.execute("PRAGMA journal_mode=DELETE")
    problems = integrity_problems(dst)
    if problems:
        raise BackupError(
            f"copy at {destination} failed its integrity check: {problems[:5]}"
        )


def _create_exclusive(path: Path) -> None:
    """Create an empty ``0600`` file at ``path``; fail if anything is there."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _NEW_FILE_MODE)
    except FileExistsError as exc:
        raise BackupError(f"{path} already exists; not overwriting it") from exc
    os.close(fd)


def _sidecar(path: Path, suffix: str) -> Path:
    return path.with_name(path.name + suffix)


def _unlink_quietly(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


@contextlib.contextmanager
def timed(label: str) -> Iterator[None]:
    """Log how long a copy took (snapshots run on the startup path)."""
    started = time.monotonic()
    yield
    logger.info("%s took %.2f s", label, time.monotonic() - started)
