"""Round-trip migration test for revision 037_render_job_hot_path_indexes (#66, C10).

Verifies:
- upgrade creates ``ix_render_job_active`` (partial on the in-flight statuses,
  with the model's predicate text) and ``ix_render_job_project_kind_created``
  with their exact column lists;
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
from timelapse_manager.db.models.render_job import ACTIVE_JOB_INDEX_WHERE

_REV = "037_render_job_hot_path_indexes"
_NEW_INDEXES = {"ix_render_job_active", "ix_render_job_project_kind_created"}


def _prev(cfg: Config) -> str:
    down = ScriptDirectory.from_config(cfg).get_revision(_REV).down_revision
    assert isinstance(down, str)
    return down


def _path(db_url: str) -> str:
    return db_url.replace("sqlite:///", "")


def _index_sql(db_url: str) -> dict[str, str]:
    """Return ``{index name: CREATE INDEX sql}`` for the render_job table."""
    conn = sqlite3.connect(_path(db_url))
    try:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = 'render_job' AND sql IS NOT NULL"
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
    """Seed every table the cascade rules touch, plus a render_job history."""
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
            conn.executemany(
                "INSERT INTO render_job (project_id, encoder_engine, kind, status) "
                "VALUES (?, 'ffmpeg', ?, ?)",
                [
                    (
                        camera,
                        ("manual", "scheduled", "archive", "export")[n % 4],
                        "pending" if n == 39 else ("failed" if n % 9 == 0 else "done"),
                    )
                    for n in range(40)
                ],
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


class TestMigration037:
    def test_upgrade_creates_both_indexes(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)

        indexes = {
            name: " ".join(sql.split()) for name, sql in _index_sql(tmp_db_url).items()
        }
        assert set(indexes) >= _NEW_INDEXES
        assert indexes["ix_render_job_active"].endswith(
            "ON render_job (id) WHERE status IN ('pending', 'encoding')"
        )
        assert indexes["ix_render_job_project_kind_created"].endswith(
            "ON render_job (project_id, kind, created_at)"
        )
        for name in _NEW_INDEXES:
            assert "UNIQUE" not in indexes[name].upper()
        assert "WHERE" not in indexes["ix_render_job_project_kind_created"].upper()

    def test_frozen_predicate_matches_the_model(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        """The migration's frozen WHERE is the text the queries repeat.

        SQLite uses the partial index only when a query repeats its predicate,
        and the queries build theirs from ``ACTIVE_JOB_INDEX_WHERE``.
        """
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)

        sql = " ".join(_index_sql(tmp_db_url)["ix_render_job_active"].split())
        assert sql.endswith(f"WHERE {ACTIVE_JOB_INDEX_WHERE}")

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
        # The pre-existing render_job index survives the round trip.
        assert "ix_render_job_project_id" in before

    def test_populated_copy_round_trip_keeps_every_row(
        self, alembic_cfg: Config, tmp_db_url: str, tmp_path: Path
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        prev = _prev(alembic_cfg)
        command.upgrade(alembic_cfg, prev)
        _populate(tmp_db_url)
        original = _counts(tmp_db_url)
        assert original["render_job"] == 80
        assert original["frame"] == 300
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

    def test_metadata_create_all_emits_the_same_indexes(self, tmp_path: Path) -> None:
        """A database built from the models (tests, fresh installs) matches 037."""
        url = f"sqlite:///{tmp_path / 'models.db'}"
        engine = create_engine(url)
        try:
            Base.metadata.create_all(engine)
        finally:
            engine.dispose()

        indexes = {name: " ".join(sql.split()) for name, sql in _index_sql(url).items()}
        assert indexes["ix_render_job_active"].endswith(
            "ON render_job (id) WHERE status IN ('pending', 'encoding')"
        )
        assert indexes["ix_render_job_project_kind_created"].endswith(
            "ON render_job (project_id, kind, created_at)"
        )
