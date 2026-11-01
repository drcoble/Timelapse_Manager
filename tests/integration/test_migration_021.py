"""Round-trip migration test for revision 021_add_exact_time_fire_table.

Verifies:
- upgrade creates the exact_time_fire table with the expected columns
- downgrade drops the table cleanly
- the round-trip is idempotent (upgrade -> downgrade -> upgrade)
- seeded fire rows (and their FK-linked project/frame parents) are handled
  correctly on downgrade: the fire table disappears, but the parent project
  and frame rows survive intact (they are not children of exact_time_fire)
"""

from __future__ import annotations

import sqlite3

from alembic import command
from alembic.config import Config

_REV = "021_add_exact_time_fire_table"
_PREV = "020_add_project_exact_time_anchors"
_TABLE = "exact_time_fire"

# Expected columns on the exact_time_fire table after upgrade.
_EXPECTED_COLUMNS = {
    "id",
    "project_id",
    "anchor_id",
    "local_date",
    "status",
    "fired_at",
    "frame_id",
    "detail",
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


def _count(db_url: str, table: str) -> int:
    path = db_url.replace("sqlite:///", "")
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _seed_parent_and_fire_row(db_url: str) -> None:
    """Seed camera + project + frame + one exact_time_fire row.

    The fire row references both project_id and frame_id so that both FK
    relationships (CASCADE and SET NULL) are exercised.
    """
    conn = sqlite3.connect(db_url.replace("sqlite:///", ""))
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("INSERT INTO camera (id, name) VALUES (1, 'cam')")
        conn.execute(
            "INSERT INTO project (id, camera_id, name, operational_status, "
            "lifecycle_state, frame_count) VALUES (1, 1, 'p', 'idle', 'active', 0)"
        )
        conn.execute(
            "INSERT INTO frame (id, project_id, sequence_index, capture_status, "
            "origin, lifecycle_state) "
            "VALUES (1, 1, 1, 'pending', 'captured', 'active')"
        )
        conn.execute(
            "INSERT INTO exact_time_fire "
            "(project_id, anchor_id, local_date, status, fired_at, frame_id) "
            "VALUES (1, 'solar_noon', '2026-01-01', 'captured',"
            " '2026-01-01 12:00:00', 1)"
        )
        conn.commit()
    finally:
        conn.close()


class TestMigration021RoundTrip:
    """Migration 021 creates exact_time_fire; downgrade drops it."""

    def test_upgrade_creates_table(self, alembic_cfg: Config, tmp_db_url: str) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)

        assert _table_exists(tmp_db_url, _TABLE), "exact_time_fire table not created"

    def test_upgrade_table_has_expected_columns(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)

        cols = _column_names(tmp_db_url, _TABLE)
        assert cols >= _EXPECTED_COLUMNS, f"missing columns: {_EXPECTED_COLUMNS - cols}"

    def test_downgrade_drops_table(self, alembic_cfg: Config, tmp_db_url: str) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)
        command.downgrade(alembic_cfg, _PREV)

        assert not _table_exists(tmp_db_url, _TABLE), (
            "exact_time_fire table survived downgrade"
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

    def test_fire_rows_removed_and_parents_intact_after_downgrade(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        """The fire table (and its rows) is dropped cleanly on downgrade.

        The parent project and frame rows are NOT children of exact_time_fire;
        they must survive the table drop intact.
        """
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)
        _seed_parent_and_fire_row(tmp_db_url)

        assert _count(tmp_db_url, _TABLE) == 1, "fire row not seeded"

        command.downgrade(alembic_cfg, _PREV)

        assert not _table_exists(tmp_db_url, _TABLE), (
            "exact_time_fire table survived downgrade"
        )
        assert _count(tmp_db_url, "project") == 1, "project row lost during downgrade"
        assert _count(tmp_db_url, "frame") == 1, "frame row lost during downgrade"
