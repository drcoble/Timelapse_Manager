"""Alembic migration environment.

Runs migrations against an engine built by the application's own engine helper
so that the SQLite WAL connection hook applies during migrations exactly as it
does at runtime. There is no ORM metadata yet, so ``target_metadata`` is None;
migrations are written explicitly.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context

from timelapse_manager.db.engine import DEFAULT_DATABASE_URL, create_db_engine

# Alembic Config object, providing access to values in alembic.ini.
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No declarative models defined yet; explicit migrations only.
target_metadata = None


def _database_url() -> str:
    """Resolve the database URL for migrations.

    Precedence: the ``TLM_DATABASE__URL`` environment variable (the same override
    the application honors, so ``alembic``/``make migrate`` target the database
    the app actually uses) > an explicit ``sqlalchemy.url`` set on the Alembic
    config (used by the test fixtures) > the built-in default.
    """
    return (
        os.environ.get("TLM_DATABASE__URL")
        or config.get_main_option("sqlalchemy.url")
        or DEFAULT_DATABASE_URL
    )


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode, emitting SQL without a live engine."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live connection.

    Uses the application engine helper so the WAL pragma hook is registered for
    the migration connection.
    """
    connectable = create_db_engine(_database_url())
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
