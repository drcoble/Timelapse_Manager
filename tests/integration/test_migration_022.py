"""Round-trip migration test for revision 022_add_project_event_triggers.

Verifies:
- upgrade adds the event_triggers column (nullable JSON) to project
- downgrade removes the column via a FK-suspend batch table rebuild
- the round-trip is idempotent (upgrade -> downgrade -> upgrade)
- child rows (frame, render_job, milestone) that exist when downgrade runs
  survive the batch rebuild intact -- the FK-suspend guard prevents the
  implicit DROP-CASCADE from deleting them
"""

from __future__ import annotations

import sqlite3

from alembic import command
from alembic.config import Config

_REV = "022_add_project_event_triggers"
_PREV = "021_add_exact_time_fire_table"
_TABLE = "project"
_COLUMN = "event_triggers"


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
    """Seed camera + user + project + one child row per child table.

    Uses only columns guaranteed present at revision 022 and mirrors the
    seeding pattern from test_migration_cascade_safety.
    """
    conn = sqlite3.connect(db_url.replace("sqlite:///", ""))
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("INSERT INTO camera (id, name) VALUES (1, 'cam')")
        conn.execute(
            "INSERT INTO user (id, username, auth_source, role, enabled) "
            "VALUES (1, 'u', 'local', 'admin', 1)"
        )
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
            "INSERT INTO render_job (id, project_id, encoder_engine, kind, status) "
            "VALUES (1, 1, 'ffmpeg', 'manual', 'pending')"
        )
        conn.execute("INSERT INTO milestone (id, project_id, user_id) VALUES (1, 1, 1)")
        conn.commit()
    finally:
        conn.close()


class TestMigration022RoundTrip:
    """Migration 022 adds event_triggers to project; downgrade removes it."""

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

    def test_downgrade_preserves_child_rows(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        """Child rows (frame, render_job, milestone) survive the FK-suspend
        batch rebuild that removes event_triggers from project on downgrade.

        Without the suspend_foreign_keys guard the implicit DROP of the old
        project table would cascade-delete all three child tables.
        """
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)
        _seed_project_tree(tmp_db_url)

        command.downgrade(alembic_cfg, _PREV)

        assert _COLUMN not in _column_names(tmp_db_url, _TABLE)
        assert _count(tmp_db_url, "frame") == 1, (
            "frame row lost during downgrade rebuild"
        )
        assert _count(tmp_db_url, "render_job") == 1, "render_job row lost"
        assert _count(tmp_db_url, "milestone") == 1, "milestone row lost"
