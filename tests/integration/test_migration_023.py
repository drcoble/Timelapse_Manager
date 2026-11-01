"""Round-trip migration test for revision
023_add_notification_webhook_signing_secret.

Verifies:
- upgrade adds the webhook_signing_secret column to notification_settings
- downgrade removes the column cleanly
- the round-trip is idempotent (upgrade -> downgrade -> upgrade)
- a pre-existing notification_settings row (and its other columns) survives the
  downgrade batch table rebuild without data loss
"""

from __future__ import annotations

import sqlite3

from alembic import command
from alembic.config import Config

_REV = "023_add_notification_webhook_signing_secret"
_PREV = "022_add_project_event_triggers"
_TABLE = "notification_settings"
_COLUMN = "webhook_signing_secret"


def _column_names(db_url: str, table: str) -> set[str]:
    """Return the set of column names for a table in a SQLite DB."""
    path = db_url.replace("sqlite:///", "")
    conn = sqlite3.connect(path)
    try:
        cursor = conn.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in cursor.fetchall()}
    finally:
        conn.close()


class TestMigration023RoundTrip:
    """Migration 023 adds webhook_signing_secret; downgrade removes it."""

    def test_upgrade_adds_column(self, alembic_cfg: Config, tmp_db_url: str) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)

        assert _COLUMN in _column_names(tmp_db_url, _TABLE)

    def test_downgrade_removes_column(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)
        command.downgrade(alembic_cfg, _PREV)

        assert _COLUMN not in _column_names(tmp_db_url, _TABLE)

    def test_upgrade_then_downgrade_then_upgrade_again(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        """Idempotent round-trip: upgrade -> downgrade -> upgrade must not error."""
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)
        command.downgrade(alembic_cfg, _PREV)
        command.upgrade(alembic_cfg, _REV)

        assert _COLUMN in _column_names(tmp_db_url, _TABLE)

    def test_existing_row_survives_downgrade(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        """A settings row written at HEAD survives the downgrade batch rebuild.

        Seeds the singleton row (with a signing secret and a sibling field) at
        revision 023, then downgrades. The batch table rebuild drops only the
        signing-secret column; the row and its other columns must persist.
        """
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)

        path = tmp_db_url.replace("sqlite:///", "")
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                f"INSERT INTO {_TABLE} (id, smtp_server, smtp_security, {_COLUMN}) "
                "VALUES (?, ?, ?, ?)",
                (1, "mail.example.com", "none", "enc:v1:placeholder-ciphertext"),
            )
            conn.commit()
        finally:
            conn.close()

        command.downgrade(alembic_cfg, _PREV)

        # The column is gone but the row and its sibling column survive.
        assert _COLUMN not in _column_names(tmp_db_url, _TABLE)
        conn = sqlite3.connect(path)
        try:
            rows = conn.execute(f"SELECT id, smtp_server FROM {_TABLE}").fetchall()
        finally:
            conn.close()
        assert rows == [(1, "mail.example.com")]
