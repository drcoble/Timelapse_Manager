"""Round-trip migration test for revision 034_exact_time_fire_retry (#69, CAM-R1).

Verifies:
- upgrade adds ``exact_time_fire.attempts`` (INTEGER NOT NULL DEFAULT 1) and
  ``exact_time_fire.next_attempt_at`` (DATETIME NULL) with native
  ``ADD COLUMN`` (the table is not rebuilt: its ``CREATE TABLE`` text keeps its
  original prefix);
- existing rows keep their status and read ``attempts = 1``;
- downgrade rewrites ``retry_pending`` to ``failed`` and drops both columns;
- on a copy of a populated database, every table's row count is unchanged after
  upgrade, after downgrade and after re-upgrade;
- the ORM models and the migrated schema agree.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from timelapse_manager.db.models import Base

_REV = "034_exact_time_fire_retry"


def _prev(cfg: Config) -> str:
    down = ScriptDirectory.from_config(cfg).get_revision(_REV).down_revision
    assert isinstance(down, str)
    return down


def _path(db_url: str) -> str:
    return db_url.replace("sqlite:///", "")


def _columns(db_url: str) -> dict[str, tuple[str, int, str | None]]:
    """Return ``{column: (type, notnull, default)}`` for ``exact_time_fire``."""
    conn = sqlite3.connect(_path(db_url))
    try:
        rows = conn.execute("PRAGMA table_info(exact_time_fire)").fetchall()
    finally:
        conn.close()
    return {str(r[1]): (str(r[2]), int(r[3]), r[4]) for r in rows}


def _table_sql(db_url: str) -> str:
    conn = sqlite3.connect(_path(db_url))
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'exact_time_fire'"
        ).fetchone()
    finally:
        conn.close()
    return str(row[0])


def _counts(db_url: str) -> dict[str, int]:
    conn = sqlite3.connect(_path(db_url))
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
            )
        ]
        return {
            table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in tables
        }
    finally:
        conn.close()


def _populate(db_url: str) -> None:
    """Seed projects, cameras, frames, events and fire rows in every status."""
    conn = sqlite3.connect(_path(db_url))
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute(
            "INSERT INTO user (id, username, username_key, auth_source, role, "
            "enabled) VALUES (1, 'admin', 'admin', 'local', 'admin', 1)"
        )
        statuses = ("captured", "failed", "skipped_missed", "skipped_no_geo")
        for camera in (1, 2):
            conn.execute(
                "INSERT INTO camera (id, name) VALUES (?, ?)", (camera, f"cam{camera}")
            )
            conn.execute(
                "INSERT INTO project (id, camera_id, name, operational_status, "
                "lifecycle_state, frame_count) VALUES (?, ?, ?, 'idle', 'active', 0)",
                (camera, camera, f"p{camera}"),
            )
            conn.executemany(
                "INSERT INTO frame (project_id, sequence_index, capture_status, "
                "origin, lifecycle_state) VALUES (?, ?, 'captured', 'captured', "
                "'active')",
                [(camera, seq) for seq in range(1, 31)],
            )
            conn.executemany(
                "INSERT INTO exact_time_fire (project_id, anchor_id, local_date, "
                "status, fired_at) VALUES (?, 'noon', ?, ?, '2026-10-01 12:05:00')",
                [
                    (camera, f"2026-10-0{day + 1}", status)
                    for day, status in enumerate(statuses)
                ],
            )
            conn.executemany(
                "INSERT INTO event (scope, scope_id, level, message, timestamp) "
                "VALUES ('project', ?, ?, 'e', '2026-10-01 00:00:00')",
                [(camera, ("info", "warning")[i % 2]) for i in range(20)],
            )
        conn.commit()
    finally:
        conn.close()


def _schema_drift(db_url: str) -> list[object]:
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            return list(
                compare_metadata(MigrationContext.configure(conn), Base.metadata)
            )
    finally:
        engine.dispose()


class TestMigration034:
    def test_upgrade_adds_both_columns_natively(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _prev(alembic_cfg))
        before_sql = _table_sql(tmp_db_url)

        command.upgrade(alembic_cfg, _REV)

        columns = _columns(tmp_db_url)
        assert columns["attempts"] == ("INTEGER", 1, "'1'")
        assert columns["next_attempt_at"] == ("DATETIME", 0, None)
        # Native ADD COLUMN splices the new column text into the stored
        # CREATE TABLE statement and leaves the rest byte for byte; a batch
        # rebuild regenerates the statement (and goes through a temp table).
        after_sql = _table_sql(tmp_db_url)
        for line in before_sql.splitlines():
            assert line in after_sql
        script = ScriptDirectory.from_config(alembic_cfg).get_revision(_REV)
        source = Path(str(script.path)).read_text(encoding="utf-8")
        assert "batch_alter_table" not in source.split('"""', 2)[2]

    def test_existing_rows_keep_status_and_read_one_attempt(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _prev(alembic_cfg))
        _populate(tmp_db_url)

        command.upgrade(alembic_cfg, _REV)

        conn = sqlite3.connect(_path(tmp_db_url))
        try:
            rows = conn.execute(
                "SELECT status, attempts, next_attempt_at FROM exact_time_fire "
                "WHERE project_id = 1 ORDER BY local_date"
            ).fetchall()
        finally:
            conn.close()
        assert rows == [
            ("captured", 1, None),
            ("failed", 1, None),
            ("skipped_missed", 1, None),
            ("skipped_no_geo", 1, None),
        ]

    def test_downgrade_rewrites_retry_pending_and_drops_the_columns(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        prev = _prev(alembic_cfg)
        command.upgrade(alembic_cfg, prev)
        _populate(tmp_db_url)
        before_columns = _columns(tmp_db_url)
        command.upgrade(alembic_cfg, _REV)
        conn = sqlite3.connect(_path(tmp_db_url))
        try:
            conn.execute(
                "UPDATE exact_time_fire SET status = 'retry_pending', attempts = 3, "
                "next_attempt_at = '2026-10-02 12:06:00' "
                "WHERE project_id = 2 AND local_date = '2026-10-02'"
            )
            conn.commit()
        finally:
            conn.close()

        command.downgrade(alembic_cfg, prev)

        assert _columns(tmp_db_url) == before_columns
        conn = sqlite3.connect(_path(tmp_db_url))
        try:
            statuses = {
                row[0] for row in conn.execute("SELECT status FROM exact_time_fire")
            }
        finally:
            conn.close()
        assert "retry_pending" not in statuses
        assert "failed" in statuses

    def test_populated_copy_round_trip_keeps_every_row(
        self, alembic_cfg: Config, tmp_db_url: str, tmp_path: Path
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        prev = _prev(alembic_cfg)
        command.upgrade(alembic_cfg, prev)
        _populate(tmp_db_url)
        original = _counts(tmp_db_url)
        assert original["exact_time_fire"] == 8
        assert original["frame"] == 60
        assert original["event"] == 40
        copy_path = tmp_path / "populated-copy.db"
        shutil.copyfile(_path(tmp_db_url), copy_path)
        copy_url = f"sqlite:///{copy_path}"
        alembic_cfg.set_main_option("sqlalchemy.url", copy_url)

        command.upgrade(alembic_cfg, _REV)
        assert _counts(copy_url) == original
        command.downgrade(alembic_cfg, prev)
        assert _counts(copy_url) == original
        command.upgrade(alembic_cfg, _REV)
        assert _counts(copy_url) == original
        assert "attempts" in _columns(copy_url)
        assert _counts(tmp_db_url) == original

    def test_models_match_the_migrated_schema(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, "head")

        assert _schema_drift(tmp_db_url) == []
