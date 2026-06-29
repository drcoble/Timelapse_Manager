"""SQLite engine construction with write-ahead logging enabled per connection.

This module owns how the application opens its SQLite database. WAL is enabled
via a ``connect`` event so it applies to every pooled connection, whether the
engine is created by the running application or reused by database migrations.

No ORM models are defined here yet; this is the connection substrate only.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from sqlalchemy import Engine, create_engine, event

from ..paths import default_database_url

# Default database location. Resolved once, from the single authoritative helper
# in :mod:`timelapse_manager.paths`, so this module and the settings model share
# one value and cannot drift apart. It anchors under an OS-standard, writable
# state directory (never the working directory), so migrations run from a frozen
# or service-managed process with no useful CWD still target the right database.
# Resolving the value is side-effect free; no directory is created here.
DEFAULT_DATABASE_URL = default_database_url()


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
    """Enable WAL and foreign-key enforcement on every new SQLite connection.

    Registered against the generic SQLAlchemy ``Engine`` class so the pragmas
    are applied uniformly to any SQLite connection opened in this process,
    including connections opened by Alembic migrations. SQLite disables foreign
    keys by default and the setting is per-connection, so it must be enabled on
    every connection for the schema's ``ON DELETE`` rules to take effect.
    Non-SQLite connections are ignored.
    """
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def create_db_engine(database_url: str = DEFAULT_DATABASE_URL) -> Engine:
    """Create a SQLAlchemy engine for the given SQLite database URL.

    The WAL pragma is applied automatically to each connection via the
    module-level ``connect`` event listener.
    """
    return create_engine(database_url, future=True)
