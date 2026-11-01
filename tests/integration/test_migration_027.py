"""Round-trip migration test for revision 027_add_report_table.

Verifies:
- upgrade creates the report table with the expected columns and indexes
- report_key is unique
- the user/project/camera foreign keys are ON DELETE SET NULL (a report
  outlives the user, project and camera it names)
- downgrade drops the indexes and the table
- the round-trip is repeatable (upgrade -> downgrade -> upgrade)
"""

from __future__ import annotations

import sqlite3

import pytest
from alembic import command
from alembic.config import Config

_REV = "027_add_report_table"
_PREV = "026_add_render_job_frame_window"
_TABLE = "report"

_EXPECTED_COLUMNS = {
    "id",
    "report_key",
    "user_id",
    "username",
    "area",
    "project_id",
    "camera_id",
    "title",
    "description",
    "reported_at",
    "created_at",
    "zip_path",
    "zip_size_bytes",
    "delivery_state",
    "deliveries",
    "gitea_issue_number",
    "gitea_issue_url",
}

_EXPECTED_INDEXES = {
    "ix_report_user_id_created_at",
    "ix_report_created_at",
    "ix_report_delivery_state",
}


def _connect(db_url: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_url.replace("sqlite:///", ""))
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _columns(db_url: str) -> set[str]:
    conn = _connect(db_url)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({_TABLE})")}
    finally:
        conn.close()


def _indexes(db_url: str) -> set[str]:
    conn = _connect(db_url)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
            (_TABLE,),
        )
        return {row[0] for row in rows}
    finally:
        conn.close()


def _table_exists(db_url: str) -> bool:
    conn = _connect(db_url)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (_TABLE,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _insert_report(conn: sqlite3.Connection, key: str, **fks: int | None) -> None:
    conn.execute(
        "INSERT INTO report (report_key, user_id, project_id, camera_id, area, "
        "title, description, reported_at, delivery_state) "
        "VALUES (?, ?, ?, ?, 'other', 't', '', '2026-10-23 12:00:00', 'ok')",
        (key, fks.get("user_id"), fks.get("project_id"), fks.get("camera_id")),
    )


class TestMigration027:
    def test_upgrade_creates_table_columns_and_indexes(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        command.upgrade(alembic_cfg, _REV)
        assert _columns(tmp_db_url) == _EXPECTED_COLUMNS
        assert _indexes(tmp_db_url) >= _EXPECTED_INDEXES

    def test_report_key_is_unique(self, alembic_cfg: Config, tmp_db_url: str) -> None:
        command.upgrade(alembic_cfg, _REV)
        conn = _connect(tmp_db_url)
        try:
            _insert_report(conn, "TLM-20261023-0001")
            with pytest.raises(sqlite3.IntegrityError):
                _insert_report(conn, "TLM-20261023-0001")
        finally:
            conn.close()

    def test_foreign_keys_set_null_on_delete(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        command.upgrade(alembic_cfg, _REV)
        conn = _connect(tmp_db_url)
        try:
            conn.execute("INSERT INTO camera (id, name) VALUES (1, 'cam')")
            conn.execute(
                "INSERT INTO user (id, username, username_key, auth_source, role, "
                "enabled) VALUES (5, 'u', 'u', 'local', 'viewer', 1)"
            )
            conn.execute(
                "INSERT INTO project (id, camera_id, name, operational_status, "
                "lifecycle_state, frame_count) "
                "VALUES (1, 1, 'p', 'idle', 'active', 0)"
            )
            _insert_report(
                conn, "TLM-20261023-0001", user_id=5, project_id=1, camera_id=1
            )
            conn.execute("DELETE FROM user WHERE id = 5")
            conn.execute("DELETE FROM project WHERE id = 1")
            conn.execute("DELETE FROM camera WHERE id = 1")
            conn.commit()
            row = conn.execute(
                "SELECT user_id, project_id, camera_id FROM report"
            ).fetchone()
        finally:
            conn.close()
        assert row == (None, None, None)

    def test_downgrade_drops_table_and_indexes(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        command.upgrade(alembic_cfg, _REV)
        conn = _connect(tmp_db_url)
        try:
            _insert_report(conn, "TLM-20261023-0001")
            conn.commit()
        finally:
            conn.close()
        command.downgrade(alembic_cfg, _PREV)
        assert not _table_exists(tmp_db_url)
        assert not _indexes(tmp_db_url)

    def test_round_trip_is_repeatable(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        command.upgrade(alembic_cfg, _REV)
        command.downgrade(alembic_cfg, _PREV)
        command.upgrade(alembic_cfg, _REV)
        assert _columns(tmp_db_url) == _EXPECTED_COLUMNS
        assert _indexes(tmp_db_url) >= _EXPECTED_INDEXES
