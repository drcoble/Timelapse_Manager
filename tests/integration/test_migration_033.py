"""Round-trip migration test for revision 033_event_hot_path_indexes (#66, C4).

Verifies:
- upgrade creates ``ix_event_open_alerts`` with its full partial predicate and
  ``ix_event_scope_scope_id``;
- downgrade drops both and nothing else;
- on a copy of a populated database, every table's row count is unchanged after
  upgrade, after downgrade and after re-upgrade (index-only: no rebuild, so no
  cascade can fire);
- the ORM models and the migrated schema agree (the ``alembic check``
  comparison, run directly because ``alembic/env.py`` has no target metadata).

The previous revision is read from the script itself, so the test does not
change if ``down_revision`` is re-pointed.
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
from timelapse_manager.db.models.event import OPEN_ALERT_INDEX_WHERE

_REV = "033_event_hot_path_indexes"
_NEW_INDEXES = {"ix_event_open_alerts", "ix_event_scope_scope_id"}


def _prev(cfg: Config) -> str:
    down = ScriptDirectory.from_config(cfg).get_revision(_REV).down_revision
    assert isinstance(down, str)
    return down


def _path(db_url: str) -> str:
    return db_url.replace("sqlite:///", "")


def _index_sql(db_url: str) -> dict[str, str]:
    """Return ``{index name: CREATE INDEX sql}`` for the event table."""
    conn = sqlite3.connect(_path(db_url))
    try:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = 'event' AND sql IS NOT NULL"
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
    """Seed every table the cascade rules touch, plus a realistic event mix."""
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
                "INSERT INTO frame (project_id, sequence_index, capture_status, "
                "origin, lifecycle_state) VALUES (?, ?, 'captured', 'captured', "
                "'active')",
                [(camera, seq) for seq in range(1, 51)],
            )
        conn.executemany(
            "INSERT INTO event (scope, scope_id, level, message, timestamp, "
            "alert_cleared_at) VALUES (?, ?, ?, 'e', '2026-10-01 00:00:00', ?)",
            [
                (
                    ("project", "camera", "system")[i % 3],
                    None if i % 3 == 2 else i % 2 + 1,
                    ("info", "warning", "error")[i % 3],
                    "2026-10-02 00:00:00" if i % 5 == 0 else None,
                )
                for i in range(300)
            ],
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


class TestMigration033:
    def test_upgrade_creates_both_indexes(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)

        indexes = _index_sql(tmp_db_url)
        assert set(indexes) >= _NEW_INDEXES
        open_alerts = " ".join(indexes["ix_event_open_alerts"].split())
        assert open_alerts.endswith(
            "ON event (level, timestamp) WHERE alert_cleared_at IS NULL "
            "AND level IN ('warning', 'error', 'critical')"
        ), open_alerts
        # The frozen migration text and the model constant must not drift: the
        # alert reads repeat the model's predicate to make SQLite use the index.
        assert open_alerts.endswith(f"WHERE {OPEN_ALERT_INDEX_WHERE}")
        assert " ".join(indexes["ix_event_scope_scope_id"].split()).endswith(
            "ON event (scope, scope_id, id)"
        )

    def test_downgrade_drops_only_the_new_indexes(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        prev = _prev(alembic_cfg)
        command.upgrade(alembic_cfg, prev)
        before = _index_sql(tmp_db_url)
        command.upgrade(alembic_cfg, _REV)

        command.downgrade(alembic_cfg, prev)

        assert _index_sql(tmp_db_url) == before
        assert not (_NEW_INDEXES & set(before))

    def test_populated_copy_round_trip_keeps_every_row(
        self, alembic_cfg: Config, tmp_db_url: str, tmp_path: Path
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        prev = _prev(alembic_cfg)
        command.upgrade(alembic_cfg, prev)
        _populate(tmp_db_url)
        original = _counts(tmp_db_url)
        assert original["event"] == 300
        assert original["frame"] == 100
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
        assert set(_index_sql(copy_url)) >= _NEW_INDEXES
        # The original was never touched.
        assert _counts(tmp_db_url) == original

    def test_models_match_the_migrated_schema(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, "head")

        assert _schema_drift(tmp_db_url) == []
