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
"""

from __future__ import annotations

from ..config.settings import Settings


def apply_migrations(settings: Settings) -> None:
    """Upgrade the configured database to the head revision.

    Builds the Alembic configuration with the migration directory and database
    URL resolved from settings/bundle, then runs ``upgrade head``. Idempotent:
    a database already at head is left unchanged.
    """
    from alembic import command
    from alembic.config import Config

    from ..paths import alembic_config_path, alembic_script_location

    alembic_cfg = Config(str(alembic_config_path()))
    alembic_cfg.set_main_option("script_location", str(alembic_script_location()))
    alembic_cfg.set_main_option("sqlalchemy.url", settings.database.url)
    command.upgrade(alembic_cfg, "head")
