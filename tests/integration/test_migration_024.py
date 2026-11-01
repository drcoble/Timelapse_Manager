"""Round-trip migration test for revision 024_add_login_attempt.

Verifies:
- upgrade creates the login_attempt table with the expected columns
- upgrade creates all three named indexes
- downgrade drops all three indexes and the table
- the round-trip is idempotent (upgrade -> downgrade -> upgrade)
- a pre-existing login_attempt row is removed cleanly with the table on
  downgrade (no orphan state left behind)
"""

from __future__ import annotations

import sqlite3

from alembic import command
from alembic.config import Config

_REV = "024_add_login_attempt"
_PREV = "023_add_notification_webhook_signing_secret"
_TABLE = "login_attempt"

_EXPECTED_COLUMNS = {"id", "ip", "username", "attempted_at", "succeeded"}

# All three index names created by the upgrade step.
_EXPECTED_INDEXES = {
    "ix_login_attempt_ip_attempted_at",
    "ix_login_attempt_username_attempted_at",
    "ix_login_attempt_attempted_at",
}


def _column_names(db_url: str, table: str) -> set[str]:
    """Return the set of column names for a table in a SQLite DB."""
    path = db_url.replace("sqlite:///", "")
    conn = sqlite3.connect(path)
    try:
        cursor = conn.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in cursor.fetchall()}
    finally:
        conn.close()


def _table_exists(db_url: str, table: str) -> bool:
    """Return True when the named table is present in sqlite_master."""
    path = db_url.replace("sqlite:///", "")
    conn = sqlite3.connect(path)
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        )
        return cursor.fetchone() is not None
    finally:
        conn.close()


def _index_names_for_table(db_url: str, table: str) -> set[str]:
    """Return the set of named index names for the given table in sqlite_master.

    Filters by tbl_name to avoid noise from indexes on other tables.
    """
    path = db_url.replace("sqlite:///", "")
    conn = sqlite3.connect(path)
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
            (table,),
        )
        return {row[0] for row in cursor.fetchall()}
    finally:
        conn.close()


def _count(db_url: str, table: str) -> int:
    path = db_url.replace("sqlite:///", "")
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


class TestMigration024RoundTrip:
    """Migration 024 creates login_attempt table + 3 indexes; downgrade drops all."""

    def test_upgrade_creates_table(self, alembic_cfg: Config, tmp_db_url: str) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)

        assert _table_exists(tmp_db_url, _TABLE), "login_attempt table not created"

    def test_upgrade_table_has_expected_columns(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)

        cols = _column_names(tmp_db_url, _TABLE)
        assert cols >= _EXPECTED_COLUMNS, f"missing columns: {_EXPECTED_COLUMNS - cols}"

    def test_upgrade_creates_all_indexes(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)

        idx = _index_names_for_table(tmp_db_url, _TABLE)
        assert idx >= _EXPECTED_INDEXES, f"missing indexes: {_EXPECTED_INDEXES - idx}"

    def test_downgrade_drops_table_and_indexes(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)
        command.downgrade(alembic_cfg, _PREV)

        assert not _table_exists(tmp_db_url, _TABLE), (
            "login_attempt table survived downgrade"
        )
        assert not _index_names_for_table(tmp_db_url, _TABLE), (
            "login_attempt indexes survived downgrade"
        )

    def test_upgrade_then_downgrade_then_upgrade_again(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        """Idempotent round-trip: upgrade -> downgrade -> upgrade must not error."""
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)
        command.downgrade(alembic_cfg, _PREV)
        command.upgrade(alembic_cfg, _REV)

        assert _table_exists(tmp_db_url, _TABLE)
        assert _index_names_for_table(tmp_db_url, _TABLE) >= _EXPECTED_INDEXES

    def test_seeded_rows_removed_cleanly_with_table_on_downgrade(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        """login_attempt rows seeded at 024 are removed with the table on downgrade.

        The table has no foreign keys, so downgrade is a plain drop_table.
        After downgrade the table (and its rows) must be gone with no errors.
        """
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)

        path = tmp_db_url.replace("sqlite:///", "")
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "INSERT INTO login_attempt (ip, username, attempted_at, succeeded) "
                "VALUES (?, ?, ?, ?)",
                ("192.0.2.1", "alice", "2026-06-30 10:00:00", 0),
            )
            conn.execute(
                "INSERT INTO login_attempt (ip, username, attempted_at, succeeded) "
                "VALUES (?, ?, ?, ?)",
                ("192.0.2.1", "alice", "2026-06-30 10:00:05", 1),
            )
            conn.commit()
            pre_count = conn.execute(f"SELECT COUNT(*) FROM {_TABLE}").fetchone()[0]
        finally:
            conn.close()

        assert pre_count == 2, "login_attempt rows not seeded"

        command.downgrade(alembic_cfg, _PREV)

        assert not _table_exists(tmp_db_url, _TABLE), (
            "login_attempt table survived downgrade"
        )
