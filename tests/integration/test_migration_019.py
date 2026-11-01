"""Round-trip migration test for revision 019_add_frame_capture_reason.

Verifies:
- upgrade adds the capture_reason column (nullable String) to frame
- downgrade removes the column cleanly via batch rebuild
- the round-trip is idempotent (upgrade -> downgrade -> upgrade)
- a pre-existing frame row (written at revision 018) reads back capture_reason=NULL
  after upgrade (additive nullable column)
- an existing frame row written at revision 019 survives the downgrade batch rebuild
"""

from __future__ import annotations

import sqlite3

from alembic import command
from alembic.config import Config

_REV = "019_add_frame_capture_reason"
_PREV = "018_add_frame_capture_timestamp_inferred"
_TABLE = "frame"
_COLUMN = "capture_reason"


def _column_names(db_url: str, table: str) -> set[str]:
    """Return the set of column names for a table in a SQLite DB."""
    path = db_url.replace("sqlite:///", "")
    conn = sqlite3.connect(path)
    try:
        cursor = conn.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in cursor.fetchall()}
    finally:
        conn.close()


def _count(db_url: str, table: str) -> int:
    path = db_url.replace("sqlite:///", "")
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _seed_project_tree(db_url: str) -> None:
    """Seed camera + project + frame at the current revision.

    Columns are held to the intersection present at all revisions under test
    so the same helper works at 018 and 019.
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
        conn.commit()
    finally:
        conn.close()


class TestMigration019RoundTrip:
    """Migration 019 adds capture_reason to frame; downgrade removes it."""

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

    def test_pre_existing_rows_read_back_null(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        """A frame row written at revision 018 reads back capture_reason=NULL
        after upgrading to 019 (additive nullable column; no backfill).
        """
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _PREV)
        _seed_project_tree(tmp_db_url)

        command.upgrade(alembic_cfg, _REV)

        path = tmp_db_url.replace("sqlite:///", "")
        conn = sqlite3.connect(path)
        try:
            rows = conn.execute(f"SELECT {_COLUMN} FROM {_TABLE} WHERE id=1").fetchall()
        finally:
            conn.close()
        assert len(rows) == 1, "frame row missing after upgrade"
        assert rows[0][0] is None, f"expected NULL, got {rows[0][0]!r}"

    def test_existing_row_survives_downgrade(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        """A frame row written at revision 019 survives the batch rebuild on
        downgrade; the row persists (column is gone, row is not).
        """
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)
        _seed_project_tree(tmp_db_url)

        # Set a non-NULL capture_reason to confirm the rebuild copies data, not just
        # nulls.
        path = tmp_db_url.replace("sqlite:///", "")
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                f"UPDATE {_TABLE} SET {_COLUMN}='anchor:solar_noon' WHERE id=1"
            )
            conn.commit()
        finally:
            conn.close()

        command.downgrade(alembic_cfg, _PREV)

        assert _COLUMN not in _column_names(tmp_db_url, _TABLE)
        assert _count(tmp_db_url, _TABLE) == 1, (
            "frame row lost during downgrade rebuild"
        )
