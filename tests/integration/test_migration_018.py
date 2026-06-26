"""Round-trip migration test for revision 018_add_frame_capture_timestamp_inferred.

Verifies:
- upgrade adds the capture_timestamp_inferred column with a server default of 0
- downgrade removes the column cleanly
- existing rows (written before migration) read back capture_timestamp_inferred=False
  after upgrade (server_default backfill)
"""

from __future__ import annotations

import sqlite3

from alembic import command
from alembic.config import Config

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _column_names(db_url: str, table: str) -> set[str]:
    """Return the set of column names for a table in a SQLite DB."""
    # db_url is 'sqlite:///path/to/db'; strip the prefix.
    path = db_url.replace("sqlite:///", "")
    conn = sqlite3.connect(path)
    try:
        cursor = conn.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in cursor.fetchall()}
    finally:
        conn.close()


def _row_values(db_url: str, table: str, column: str) -> list:
    """Return all values of a column from a table."""
    path = db_url.replace("sqlite:///", "")
    conn = sqlite3.connect(path)
    try:
        cursor = conn.execute(f"SELECT {column} FROM {table}")
        return [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMigration018RoundTrip:
    """Migration 018 adds capture_timestamp_inferred; downgrade removes it."""

    def test_upgrade_adds_column(self, alembic_cfg: Config, tmp_db_url: str) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, "018_add_frame_capture_timestamp_inferred")

        cols = _column_names(tmp_db_url, "frame")
        assert "capture_timestamp_inferred" in cols

    def test_downgrade_removes_column(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, "018_add_frame_capture_timestamp_inferred")
        command.downgrade(alembic_cfg, "017_add_render_job_export_kind")

        cols = _column_names(tmp_db_url, "frame")
        assert "capture_timestamp_inferred" not in cols

    def test_upgrade_then_downgrade_then_upgrade_again(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        """Idempotent round-trip: upgrade → downgrade → upgrade must not error."""
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, "018_add_frame_capture_timestamp_inferred")
        command.downgrade(alembic_cfg, "017_add_render_job_export_kind")
        command.upgrade(alembic_cfg, "018_add_frame_capture_timestamp_inferred")

        cols = _column_names(tmp_db_url, "frame")
        assert "capture_timestamp_inferred" in cols

    def test_existing_rows_backfilled_with_false(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        """Rows written before the migration read back
        capture_timestamp_inferred=0 (False).

        The ORM models always reflect HEAD schema, so seeding must be done via
        raw SQL at revision 017 (before the column exists) to properly simulate
        pre-existing rows.
        """
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        # Migrate to one revision before 018 so there is a frame table but no column.
        command.upgrade(alembic_cfg, "017_add_render_job_export_kind")

        path = tmp_db_url.replace("sqlite:///", "")
        conn = sqlite3.connect(path)
        try:
            # Probe the actual frame columns at this revision to build a safe INSERT.
            cur = conn.execute("PRAGMA table_info(frame)")
            existing_cols = {row[1] for row in cur.fetchall()}
            # Confirm capture_timestamp_inferred is absent (that's the whole point).
            assert "capture_timestamp_inferred" not in existing_cols

            # Seed camera and project to satisfy the frame FK during the later
            # batch migration table copy (which runs with FK checks enabled).
            conn.execute(
                "INSERT INTO camera (id, name, address, protocol) VALUES (?, ?, ?, ?)",
                (1, "pre-018-cam", "127.0.0.1", "vapix"),
            )
            # Probe project columns too to avoid NOT NULL errors.
            cur2 = conn.execute("PRAGMA table_info(project)")
            proj_cols = {row[1] for row in cur2.fetchall()}
            proj_required = {
                "id": 1,
                "camera_id": 1,
                "name": "pre-018-proj",
                "lifecycle_state": "active",
                "operational_status": "idle",
                "frame_count": 0,
            }
            p_cols = [c for c in proj_required if c in proj_cols]
            p_ph = ", ".join("?" * len(p_cols))
            conn.execute(
                f"INSERT INTO project ({', '.join(p_cols)}) VALUES ({p_ph})",
                [proj_required[c] for c in p_cols],
            )

            # Insert frame with known-present columns only.
            insert_cols = [
                c
                for c in (
                    "project_id",
                    "sequence_index",
                    "origin",
                    "capture_status",
                    "lifecycle_state",
                )
                if c in existing_cols
            ]
            values_map = {
                "project_id": 1,
                "sequence_index": 1,
                "origin": "captured",
                "capture_status": "pending",
                "lifecycle_state": "active",
            }
            col_list = ", ".join(insert_cols)
            placeholders = ", ".join("?" * len(insert_cols))
            conn.execute(
                f"INSERT INTO frame ({col_list}) VALUES ({placeholders})",
                [values_map[c] for c in insert_cols],
            )
            conn.commit()
            row_count = conn.execute("SELECT COUNT(*) FROM frame").fetchone()[0]
        finally:
            conn.close()

        assert row_count == 1, "Frame row was not inserted"

        # Now upgrade to 018 — should add the column and backfill 0 (false).
        command.upgrade(alembic_cfg, "018_add_frame_capture_timestamp_inferred")

        values = _row_values(tmp_db_url, "frame", "capture_timestamp_inferred")
        # SQLite stores false as 0.
        assert len(values) == 1, f"Expected 1 row after upgrade, got {len(values)}"
        assert all(v == 0 for v in values), f"Expected all 0, got {values}"
