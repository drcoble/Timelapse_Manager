"""Programmatic database migration to head.

A packaged deployment (systemd service, Docker container, or the frozen
``timelapse-manager run`` executable) must come up with a schema already in
place; there is no separate "run migrations first" step a service manager would
perform. This module applies the Alembic migrations to head against the
configured database, resolving the migration scripts relative to the
package/bundle rather than the working directory so it works from any CWD and
inside a frozen bundle.

The same routine backs the ``migrate`` CLI subcommand and the foreground serve
path, so the schema is brought to head in exactly one place.

Before upgrading an existing database that is behind head, a verified copy is
written beside it (issue #67, R6): ``<db>.pre-<revision>-<UTC stamp>``. That is
the rollback point for an upgrade whose migration fails or misbehaves; restore
it with ``timelapse-manager db restore``.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from ..config.settings import Settings
from .backup import BackupError, read_revision, take_snapshot, timed

if TYPE_CHECKING:
    from alembic.config import Config

logger = logging.getLogger(__name__)


class MigrationAbortedError(RuntimeError):
    """The pre-migration snapshot failed, so the database was not upgraded."""


def apply_migrations(settings: Settings) -> None:
    """Upgrade the configured database to the head revision.

    Builds the Alembic configuration with the migration directory and database
    URL resolved from settings/bundle, snapshots an existing database that is
    behind head, then runs ``upgrade head``. Idempotent: a database already at
    head is left unchanged and is not copied.

    :raises MigrationAbortedError: the snapshot could not be written or did not
        verify (for example, too little free space). Nothing was migrated.
    """
    from alembic import command

    alembic_cfg = _alembic_config(settings)
    snapshot_before_upgrade(settings, alembic_cfg)
    command.upgrade(alembic_cfg, "head")


def snapshot_before_upgrade(settings: Settings, alembic_cfg: Config) -> Path | None:
    """Snapshot the database if it exists and is behind head; return the copy.

    Skipped (``None``) for a non-file database, a missing or empty file, a
    database with no recorded revision (a new install), and one already at a
    head revision.
    """
    from alembic.script import ScriptDirectory

    database = sqlite_file(settings.database.url)
    if database is None or not database.is_file() or database.stat().st_size == 0:
        return None
    try:
        revision = read_revision(database)
    except (BackupError, OSError, sqlite3.Error) as exc:
        raise _abort(f"could not read the revision of {database}: {exc}") from exc
    if revision is None:
        return None
    heads = set(ScriptDirectory.from_config(alembic_cfg).get_heads())
    if set(revision.split("+")) <= heads:
        return None
    try:
        with timed(f"pre-migration snapshot of {database}"):
            snapshot = take_snapshot(database, revision)
    except (BackupError, OSError, sqlite3.Error) as exc:
        raise _abort(f"pre-migration snapshot of {database} failed: {exc}") from exc
    logger.info(
        "wrote pre-migration snapshot %s (database at revision %s)", snapshot, revision
    )
    return snapshot


def sqlite_file(url: str) -> Path | None:
    """The file behind a SQLite URL, or ``None`` for another backend/memory."""
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    if not parsed.drivername.startswith("sqlite"):
        return None
    if not parsed.database or parsed.database == ":memory:":
        return None
    if parsed.database.startswith("file:"):
        return None
    return Path(parsed.database)


def _alembic_config(settings: Settings) -> Config:
    from alembic.config import Config

    from ..paths import alembic_config_path, alembic_script_location

    alembic_cfg = Config(str(alembic_config_path()))
    alembic_cfg.set_main_option("script_location", str(alembic_script_location()))
    alembic_cfg.set_main_option("sqlalchemy.url", settings.database.url)
    return alembic_cfg


def _abort(reason: str) -> MigrationAbortedError:
    """Log and build the error that stops startup before any migration runs."""
    message = (
        f"database migration aborted before any change: {reason}. The database "
        "was not modified; fix the cause and start again."
    )
    logger.error(message)
    return MigrationAbortedError(message)
