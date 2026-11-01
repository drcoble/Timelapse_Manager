"""Migration test for revision 029_report_autoincrement.

Verifies:
- upgrade rebuilds report with AUTOINCREMENT, keeping rows, indexes and the
  unique report_key constraint
- after the upgrade, deleting the highest-id report and inserting a new one
  gives a larger id
- downgrade removes AUTOINCREMENT, keeps the rows and clears the sequence row
- the round-trip is idempotent
"""

from __future__ import annotations

import sqlite3

import pytest
from alembic import command
from alembic.config import Config

_REV = "029_report_autoincrement"
_PREV = "028_render_job_autoincrement"


def _connect(db_url: str) -> sqlite3.Connection:
    return sqlite3.connect(db_url.replace("sqlite:///", ""))


def _ddl(db_url: str) -> str:
    conn = _connect(db_url)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='report'"
        ).fetchone()
        return str(row[0])
    finally:
        conn.close()


def _insert(conn: sqlite3.Connection, key: str) -> int:
    cursor = conn.execute(
        "INSERT INTO report (report_key, area, title, description, reported_at, "
        "created_at, delivery_state) VALUES (?, 'other', 't', 'd', "
        "'2026-10-23', '2026-10-23', 'ok')",
        (key,),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _seed(db_url: str) -> None:
    conn = _connect(db_url)
    try:
        for n in (1, 2, 3):
            _insert(conn, f"TLM-20261023-{n:04d}")
        conn.commit()
    finally:
        conn.close()


def _ids(db_url: str) -> list[int]:
    conn = _connect(db_url)
    try:
        return [r[0] for r in conn.execute("SELECT id FROM report ORDER BY id")]
    finally:
        conn.close()


class TestMigration029:
    def test_upgrade_adds_autoincrement_and_keeps_constraints(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _PREV)
        _seed(tmp_db_url)

        command.upgrade(alembic_cfg, _REV)

        assert "AUTOINCREMENT" in _ddl(tmp_db_url)
        assert _ids(tmp_db_url) == [1, 2, 3]
        conn = _connect(tmp_db_url)
        try:
            indexes = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND tbl_name='report'"
                )
            }
            with pytest.raises(sqlite3.IntegrityError):
                _insert(conn, "TLM-20261023-0001")  # the key is still unique
        finally:
            conn.close()
        assert {
            "ix_report_user_id_created_at",
            "ix_report_created_at",
            "ix_report_delivery_state",
        } <= indexes

    def test_deleted_highest_id_is_not_reused(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _PREV)
        _seed(tmp_db_url)
        command.upgrade(alembic_cfg, _REV)

        conn = _connect(tmp_db_url)
        try:
            conn.execute("DELETE FROM report WHERE id = 3")
            new_id = _insert(conn, "TLM-20261023-0004")
            conn.commit()
        finally:
            conn.close()

        assert new_id == 4

    def test_downgrade_removes_autoincrement_and_keeps_rows(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _PREV)
        _seed(tmp_db_url)
        command.upgrade(alembic_cfg, _REV)

        command.downgrade(alembic_cfg, _PREV)

        assert "AUTOINCREMENT" not in _ddl(tmp_db_url)
        assert _ids(tmp_db_url) == [1, 2, 3]
        conn = _connect(tmp_db_url)
        try:
            rows = conn.execute(
                "SELECT * FROM sqlite_sequence WHERE name = 'report'"
            ).fetchall()
        finally:
            conn.close()
        assert rows == []

    def test_round_trip(self, alembic_cfg: Config, tmp_db_url: str) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)
        command.downgrade(alembic_cfg, _PREV)
        command.upgrade(alembic_cfg, _REV)

        assert "AUTOINCREMENT" in _ddl(tmp_db_url)
