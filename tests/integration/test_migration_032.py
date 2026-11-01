"""Round-trip migration test for revision 032_render_job_codec (#25).

Verifies:
- upgrade adds the nullable ``codec_string`` and ``mime_type`` columns
- a render that existed before the upgrade survives it with NULLs (the
  startup backfill probes it later, decision D14; nothing is probed here)
- downgrade drops the columns, keeps the row, and keeps the table's
  AUTOINCREMENT (migration 028)
- the round-trip is repeatable (upgrade -> downgrade -> upgrade)

The previous revision is read from the script itself, so the test does not
change when ``down_revision`` is re-pointed as the rc3 migrations merge.
"""

from __future__ import annotations

import sqlite3

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

_REV = "032_render_job_codec"
_NEW_COLUMNS = {"codec_string", "mime_type"}


def _prev(cfg: Config) -> str:
    down = ScriptDirectory.from_config(cfg).get_revision(_REV).down_revision
    assert isinstance(down, str)
    return down


def _connect(db_url: str) -> sqlite3.Connection:
    return sqlite3.connect(db_url.replace("sqlite:///", ""))


def _columns(db_url: str) -> dict[str, int]:
    """Return ``{column name: notnull flag}`` for render_job."""
    conn = _connect(db_url)
    try:
        rows = conn.execute("PRAGMA table_info(render_job)").fetchall()
        return {row[1]: row[3] for row in rows}
    finally:
        conn.close()


def _ddl(db_url: str) -> str:
    conn = _connect(db_url)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='render_job'"
        ).fetchone()
        return str(row[0])
    finally:
        conn.close()


def _seed_render_job(db_url: str) -> int:
    """Insert a camera, a project and a done render job; return the job id."""
    conn = _connect(db_url)
    try:
        conn.execute(
            "INSERT INTO camera (name, address, protocol, created_at, updated_at) "
            "VALUES ('m32-cam', '127.0.0.1', 'vapix', '2026-10-01', '2026-10-01')"
        )
        conn.execute(
            "INSERT INTO project (camera_id, name, lifecycle_state, "
            "operational_status, frame_count, created_at, updated_at) "
            "VALUES (1, 'm32', 'active', 'idle', 0, '2026-10-01', '2026-10-01')"
        )
        cursor = conn.execute(
            "INSERT INTO render_job (project_id, encoder_engine, kind, status, "
            "output_file_path, browser_streamable, created_at, updated_at) "
            "VALUES (1, 'ffmpeg', 'manual', 'done', '/tmp/render-1.mp4', 0, "
            "'2026-10-01', '2026-10-01')"
        )
        conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid
    finally:
        conn.close()


def _row(db_url: str, job_id: int) -> tuple[object, ...] | None:
    conn = _connect(db_url)
    try:
        return conn.execute(
            "SELECT id, status, output_file_path FROM render_job WHERE id = ?",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()


class TestMigration032RoundTrip:
    def test_upgrade_adds_nullable_columns(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)

        cols = _columns(tmp_db_url)
        assert set(cols) >= _NEW_COLUMNS
        assert all(cols[name] == 0 for name in _NEW_COLUMNS), "must be nullable"

    def test_existing_render_survives_with_nulls(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _prev(alembic_cfg))
        job_id = _seed_render_job(tmp_db_url)

        command.upgrade(alembic_cfg, _REV)

        conn = _connect(tmp_db_url)
        try:
            row = conn.execute(
                "SELECT status, codec_string, mime_type FROM render_job WHERE id = ?",
                (job_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row == ("done", None, None)

    def test_downgrade_drops_columns_and_keeps_rows(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        prev = _prev(alembic_cfg)
        command.upgrade(alembic_cfg, prev)
        job_id = _seed_render_job(tmp_db_url)
        command.upgrade(alembic_cfg, _REV)

        command.downgrade(alembic_cfg, prev)

        assert not (set(_columns(tmp_db_url)) & _NEW_COLUMNS)
        # The table rebuild must not cascade-delete the job (FKs suspended),
        # and must keep ids never-reused (028's AUTOINCREMENT).
        assert _row(tmp_db_url, job_id) == (job_id, "done", "/tmp/render-1.mp4")
        assert "AUTOINCREMENT" in _ddl(tmp_db_url)

    def test_round_trip_is_repeatable(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)
        command.downgrade(alembic_cfg, _prev(alembic_cfg))
        command.upgrade(alembic_cfg, _REV)

        assert set(_columns(tmp_db_url)) >= _NEW_COLUMNS
        assert "AUTOINCREMENT" in _ddl(tmp_db_url)
