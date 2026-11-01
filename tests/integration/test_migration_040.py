"""Round-trip migration test for revision 040_secrets_meta (#67 S8).

Verifies:
- upgrade creates the empty singleton table ``secrets_meta`` with its
  columns and the ``id = 1`` check, chained after 039;
- downgrade drops it and nothing else;
- on a copy of a populated database, every table's row count is unchanged after
  upgrade, after downgrade and after re-upgrade (a new table only: no rebuild,
  so no cascade can fire);
- the ORM models and the migrated schema agree (the ``alembic check``
  comparison, run directly because ``alembic/env.py`` has no target metadata).
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from timelapse_manager.db.models import Base

_REV = "040_secrets_meta"
_TABLE = "secrets_meta"


def _prev(cfg: Config) -> str:
    down = ScriptDirectory.from_config(cfg).get_revision(_REV).down_revision
    assert isinstance(down, str)
    return down


def _path(db_url: str) -> str:
    return db_url.replace("sqlite:///", "")


def _tables(db_url: str) -> dict[str, str]:
    """Return ``{table name: CREATE TABLE sql}`` for every user table."""
    conn = sqlite3.connect(_path(db_url))
    try:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    finally:
        conn.close()
    return {str(name): str(sql) for name, sql in rows}


def _counts(db_url: str) -> dict[str, int]:
    """Return the row count of every user table."""
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
    """Seed every table the cascade rules touch, plus a realistic frame mix."""
    conn = sqlite3.connect(_path(db_url))
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute(
            "INSERT INTO user (id, username, username_key, auth_source, role, "
            "enabled) VALUES (1, 'admin', 'admin', 'local', 'admin', 1)"
        )
        conn.execute(
            "INSERT INTO session (user_id, persistent, revoked) VALUES (1, 0, 0)"
        )
        for camera in (1, 2):
            conn.execute(
                "INSERT INTO camera (id, name) VALUES (?, ?)", (camera, f"cam{camera}")
            )
            conn.execute(
                "INSERT INTO project (id, camera_id, name, operational_status, "
                "lifecycle_state, frame_count) VALUES (?, ?, ?, 'idle', 'active', 0)",
                (camera, camera, f"p{camera}"),
            )
            conn.execute(
                "INSERT INTO milestone (project_id, user_id) VALUES (?, 1)", (camera,)
            )
            conn.execute(
                "INSERT INTO render_job (project_id, encoder_engine, kind, status) "
                "VALUES (?, 'ffmpeg', 'manual', 'done')",
                (camera,),
            )
            conn.execute(
                "INSERT INTO exact_time_fire (project_id, anchor_id, local_date, "
                "status, fired_at) VALUES (?, 'noon', '2026-10-01', 'fired', "
                "'2026-10-01 12:00:00')",
                (camera,),
            )
            conn.executemany(
                "INSERT INTO frame (project_id, sequence_index, capture_timestamp, "
                "file_size_bytes, capture_status, origin, lifecycle_state) "
                "VALUES (?, ?, ?, ?, 'captured', ?, ?)",
                [
                    (
                        camera,
                        seq,
                        f"2026-10-{seq % 28 + 1:02d} 12:00:00",
                        None if seq % 11 == 0 else 1000 + seq,
                        "uploaded" if seq % 7 == 0 else "captured",
                        "soft_deleted" if seq % 13 == 0 else "active",
                    )
                    for seq in range(1, 151)
                ],
            )
        conn.executemany(
            "INSERT INTO event (scope, scope_id, level, message) "
            "VALUES ('project', ?, 'info', 'e')",
            [(i % 2 + 1,) for i in range(50)],
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


class TestMigration040:
    def test_upgrade_creates_the_empty_singleton_table(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)

        sql = " ".join(_tables(tmp_db_url)[_TABLE].split())
        for column in (
            "id INTEGER NOT NULL",
            "provider VARCHAR NOT NULL",
            "key_fingerprint VARCHAR NOT NULL",
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL",
            "rotated_at DATETIME",
        ):
            assert column in sql
        assert "CONSTRAINT ck_secrets_meta_singleton CHECK (id = 1)" in sql
        assert _counts(tmp_db_url)[_TABLE] == 0

    def test_singleton_check_rejects_a_second_row(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)
        conn = sqlite3.connect(_path(tmp_db_url))
        try:
            conn.execute(
                "INSERT INTO secrets_meta (id, provider, key_fingerprint) "
                "VALUES (1, 'file', 'abc')"
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO secrets_meta (id, provider, key_fingerprint) "
                    "VALUES (2, 'file', 'abc')"
                )
        finally:
            conn.close()

    def test_downgrade_drops_only_the_new_table(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        prev = _prev(alembic_cfg)
        command.upgrade(alembic_cfg, prev)
        before = _tables(tmp_db_url)
        command.upgrade(alembic_cfg, _REV)

        command.downgrade(alembic_cfg, prev)

        assert _tables(tmp_db_url) == before
        assert _TABLE not in before

    def test_populated_copy_round_trip_keeps_every_row(
        self, alembic_cfg: Config, tmp_db_url: str, tmp_path: Path
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        prev = _prev(alembic_cfg)
        command.upgrade(alembic_cfg, prev)
        _populate(tmp_db_url)
        original = _counts(tmp_db_url)
        assert original["frame"] == 300
        assert original["event"] == 50
        copy_path = tmp_path / "populated-copy.db"
        shutil.copyfile(_path(tmp_db_url), copy_path)
        copy_url = f"sqlite:///{copy_path}"
        alembic_cfg.set_main_option("sqlalchemy.url", copy_url)

        command.upgrade(alembic_cfg, _REV)
        assert _counts(copy_url) == {**original, _TABLE: 0}
        command.downgrade(alembic_cfg, prev)
        assert _counts(copy_url) == original
        command.upgrade(alembic_cfg, _REV)
        assert _counts(copy_url) == {**original, _TABLE: 0}
        # The original was never touched.
        assert _counts(tmp_db_url) == original

    def test_models_match_the_migrated_schema(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, "head")

        assert _schema_drift(tmp_db_url) == []


class TestSingleHead:
    def test_040_follows_039_and_is_the_only_head(self, alembic_cfg: Config) -> None:
        script = ScriptDirectory.from_config(alembic_cfg)
        assert script.get_revision(_REV).down_revision == "039_camera_tls_pinned_cert"
        assert len(script.get_heads()) == 1  # newer revisions chain after it
