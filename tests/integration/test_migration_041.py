"""Round-trip migration test for 041_project_active_since (#280).

Verifies:
- upgrade adds ``project.active_since`` (DATETIME NULL) with
  a native ``ADD COLUMN`` (the table is not rebuilt: its ``CREATE TABLE`` text
  keeps every original line);
- existing projects read NULL and keep every other value;
- downgrade drops the column natively and restores the previous column set;
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

_REV = "041_project_active_since"


def _prev(cfg: Config) -> str:
    down = ScriptDirectory.from_config(cfg).get_revision(_REV).down_revision
    assert isinstance(down, str)
    return down


def _path(db_url: str) -> str:
    return db_url.replace("sqlite:///", "")


def _columns(db_url: str) -> dict[str, tuple[str, int, str | None]]:
    """Return ``{column: (type, notnull, default)}`` for ``project``."""
    conn = sqlite3.connect(_path(db_url))
    try:
        rows = conn.execute("PRAGMA table_info(project)").fetchall()
    finally:
        conn.close()
    return {str(r[1]): (str(r[2]), int(r[3]), r[4]) for r in rows}


def _table_sql(db_url: str) -> str:
    conn = sqlite3.connect(_path(db_url))
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'project'"
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
    """Seed cameras, projects, frames, events, fire rows."""
    conn = sqlite3.connect(_path(db_url))
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute(
            "INSERT INTO user (id, username, username_key, auth_source, role, "
            "enabled) VALUES (1, 'admin', 'admin', 'local', 'admin', 1)"
        )
        addresses = {1: "https://driveway.example.test", 2: "http://10.0.16.9"}
        for camera, address in addresses.items():
            conn.execute(
                "INSERT INTO camera (id, name, address, protocol, "
                "credentials_inherit_default) VALUES (?, ?, ?, 'vapix', 1)",
                (camera, f"cam{camera}", address),
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
            conn.execute(
                "INSERT INTO exact_time_fire (project_id, anchor_id, local_date, "
                "status, fired_at) VALUES (?, 'noon', '2026-10-01', 'captured', "
                "'2026-10-01 12:05:00')",
                (camera,),
            )
            conn.executemany(
                "INSERT INTO event (scope, scope_id, level, message, timestamp) "
                "VALUES ('camera', ?, 'info', 'e', '2026-10-01 00:00:00')",
                [(camera,) for _ in range(20)],
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


class TestMigration041:
    def test_upgrade_adds_the_column_natively(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _prev(alembic_cfg))
        before_sql = _table_sql(tmp_db_url)

        command.upgrade(alembic_cfg, _REV)

        assert _columns(tmp_db_url)["active_since"] == ("DATETIME", 0, None)
        # Native ADD COLUMN splices the new column into the stored CREATE TABLE
        # text and leaves the rest byte for byte; a batch rebuild regenerates it.
        after_sql = _table_sql(tmp_db_url)
        for line in before_sql.splitlines():
            assert line in after_sql
        script = ScriptDirectory.from_config(alembic_cfg).get_revision(_REV)
        source = Path(str(script.path)).read_text(encoding="utf-8")
        assert "batch_alter_table" not in source.split('"""', 2)[2]

    def test_existing_projects_read_null_and_keep_their_values(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _prev(alembic_cfg))
        _populate(tmp_db_url)

        command.upgrade(alembic_cfg, _REV)

        conn = sqlite3.connect(_path(tmp_db_url))
        try:
            rows = conn.execute(
                "SELECT id, name, lifecycle_state, frame_count, active_since "
                "FROM project ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        assert rows == [
            (1, "p1", "active", 0, None),
            (2, "p2", "active", 0, None),
        ]

    def test_downgrade_drops_the_column(
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
                "UPDATE project SET active_since = '2026-10-01 00:00:00' WHERE id = 2"
            )
            conn.commit()
        finally:
            conn.close()

        command.downgrade(alembic_cfg, prev)

        assert _columns(tmp_db_url) == before_columns

    def test_populated_copy_round_trip_keeps_every_row(
        self, alembic_cfg: Config, tmp_db_url: str, tmp_path: Path
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        prev = _prev(alembic_cfg)
        command.upgrade(alembic_cfg, prev)
        _populate(tmp_db_url)
        original = _counts(tmp_db_url)
        assert original["camera"] == 2
        assert original["project"] == 2
        assert original["frame"] == 60
        assert original["event"] == 40
        assert original["exact_time_fire"] == 2
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
        assert "active_since" in _columns(copy_url)
        assert _counts(tmp_db_url) == original

    def test_models_match_the_migrated_schema(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, "head")

        assert _schema_drift(tmp_db_url) == []
