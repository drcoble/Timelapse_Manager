"""Migration test for revision 030_add_report_anonymized.

Verifies:
- upgrade adds ``report.anonymized`` (NOT NULL, default false), existing rows
  read back false, and new rows without the column get false
- the rebuild keeps 029's AUTOINCREMENT, the indexes and the unique key
- downgrade drops the column and still keeps AUTOINCREMENT and the rows
- the round-trip is idempotent
"""

from __future__ import annotations

import sqlite3

import pytest
from alembic import command
from alembic.config import Config

_REV = "030_add_report_anonymized"
_PREV = "029_report_autoincrement"
_INDEXES = {
    "ix_report_user_id_created_at",
    "ix_report_created_at",
    "ix_report_delivery_state",
}


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


def _columns(db_url: str) -> dict[str, tuple[int, str | None]]:
    """Column name -> (notnull, default)."""
    conn = _connect(db_url)
    try:
        return {
            row[1]: (row[3], row[4])
            for row in conn.execute("PRAGMA table_info(report)")
        }
    finally:
        conn.close()


def _indexes(db_url: str) -> set[str]:
    conn = _connect(db_url)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='report'"
            )
        }
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


def _rows(db_url: str) -> list[tuple[int, str]]:
    conn = _connect(db_url)
    try:
        return list(conn.execute("SELECT id, report_key FROM report ORDER BY id"))
    finally:
        conn.close()


class TestMigration030:
    def test_upgrade_adds_anonymized_defaulting_false(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _PREV)
        _seed(tmp_db_url)

        command.upgrade(alembic_cfg, _REV)

        columns = _columns(tmp_db_url)
        assert "anonymized" in columns
        notnull, default = columns["anonymized"]
        assert notnull == 1
        assert default is not None
        conn = _connect(tmp_db_url)
        try:
            existing = [r[0] for r in conn.execute("SELECT anonymized FROM report")]
            new_id = _insert(conn, "TLM-20261023-0004")
            conn.execute("UPDATE report SET anonymized = 1 WHERE id = 1")
            conn.commit()
            new_value = conn.execute(
                "SELECT anonymized FROM report WHERE id = ?", (new_id,)
            ).fetchone()[0]
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE report SET anonymized = NULL WHERE id = 2")
        finally:
            conn.close()
        assert existing == [0, 0, 0]
        assert new_value == 0

    def test_upgrade_keeps_autoincrement_indexes_and_unique_key(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _PREV)
        _seed(tmp_db_url)

        command.upgrade(alembic_cfg, _REV)

        assert "AUTOINCREMENT" in _ddl(tmp_db_url)
        assert _indexes(tmp_db_url) >= _INDEXES
        conn = _connect(tmp_db_url)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert(conn, "TLM-20261023-0001")
            conn.execute("DELETE FROM report WHERE id = 3")
            new_id = _insert(conn, "TLM-20261023-0005")
            conn.commit()
        finally:
            conn.close()
        assert new_id == 4  # the deleted id 3 is not reused

    def test_downgrade_drops_column_and_keeps_autoincrement(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _PREV)
        _seed(tmp_db_url)
        command.upgrade(alembic_cfg, _REV)

        command.downgrade(alembic_cfg, _PREV)

        assert "anonymized" not in _columns(tmp_db_url)
        assert "AUTOINCREMENT" in _ddl(tmp_db_url)
        assert _indexes(tmp_db_url) >= _INDEXES
        assert [key for _, key in _rows(tmp_db_url)] == [
            "TLM-20261023-0001",
            "TLM-20261023-0002",
            "TLM-20261023-0003",
        ]

    def test_round_trip(self, alembic_cfg: Config, tmp_db_url: str) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)
        command.downgrade(alembic_cfg, _PREV)
        command.upgrade(alembic_cfg, _REV)

        assert "anonymized" in _columns(tmp_db_url)
        assert "AUTOINCREMENT" in _ddl(tmp_db_url)
