"""Round-trip migration test for revision 026_add_render_job_frame_window.

Verifies:
- upgrade adds the three nullable frame-window columns to render_job
- a render_job row that existed before the upgrade survives it with NULLs
  (the render card shows "not recorded" for it)
- downgrade drops the columns and keeps the row (and its project) intact
- the round-trip is idempotent (upgrade -> downgrade -> upgrade)
"""

from __future__ import annotations

import sqlite3

from alembic import command
from alembic.config import Config

_REV = "026_add_render_job_frame_window"
_PREV = "025_add_user_username_key"
_NEW_COLUMNS = {"frame_count", "first_frame_at", "last_frame_at"}


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


def _seed_render_job(db_url: str) -> int:
    """Insert a camera, a project and a done render job; return the job id."""
    conn = _connect(db_url)
    try:
        conn.execute(
            "INSERT INTO camera (name, address, protocol, created_at, updated_at) "
            "VALUES ('m26-cam', '127.0.0.1', 'vapix', '2026-10-01', '2026-10-01')"
        )
        conn.execute(
            "INSERT INTO project (camera_id, name, lifecycle_state, "
            "operational_status, frame_count, created_at, updated_at) "
            "VALUES (1, 'm26', 'active', 'idle', 0, '2026-10-01', '2026-10-01')"
        )
        cursor = conn.execute(
            "INSERT INTO render_job (project_id, encoder_engine, kind, status, "
            "output_file_path, created_at, updated_at) "
            "VALUES (1, 'ffmpeg', 'manual', 'done', '/tmp/render-1.mp4', "
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


class TestMigration026RoundTrip:
    """Migration 026 adds nullable frame-window columns; downgrade drops them."""

    def test_upgrade_adds_nullable_columns(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)

        cols = _columns(tmp_db_url)
        assert set(cols) >= _NEW_COLUMNS
        assert all(cols[name] == 0 for name in _NEW_COLUMNS), "must be nullable"

    def test_existing_row_survives_with_nulls(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _PREV)
        job_id = _seed_render_job(tmp_db_url)

        command.upgrade(alembic_cfg, _REV)

        conn = _connect(tmp_db_url)
        try:
            row = conn.execute(
                "SELECT status, frame_count, first_frame_at, last_frame_at "
                "FROM render_job WHERE id = ?",
                (job_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row == ("done", None, None, None)

    def test_downgrade_drops_columns_and_keeps_rows(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _PREV)
        job_id = _seed_render_job(tmp_db_url)
        command.upgrade(alembic_cfg, _REV)

        command.downgrade(alembic_cfg, _PREV)

        assert not (set(_columns(tmp_db_url)) & _NEW_COLUMNS)
        # The table rebuild must not cascade-delete the job (FKs suspended).
        assert _row(tmp_db_url, job_id) == (job_id, "done", "/tmp/render-1.mp4")

    def test_upgrade_then_downgrade_then_upgrade_again(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)
        command.downgrade(alembic_cfg, _PREV)
        command.upgrade(alembic_cfg, _REV)

        assert set(_columns(tmp_db_url)) >= _NEW_COLUMNS
