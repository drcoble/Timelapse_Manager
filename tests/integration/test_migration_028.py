"""Migration test for revision 028_render_job_autoincrement.

Verifies:
- upgrade rebuilds render_job with AUTOINCREMENT and keeps every row and index
- after the upgrade, deleting the highest-id job and inserting a new one gives
  a larger id (the bug: the new job reused the deleted id)
- sqlite_sequence is seeded past ids the event log already mentions (jobs that
  were deleted before the upgrade)
- downgrade removes AUTOINCREMENT, keeps the rows and clears the sequence row
- the round-trip is idempotent
"""

from __future__ import annotations

import sqlite3

from alembic import command
from alembic.config import Config

_REV = "028_render_job_autoincrement"
_PREV = "027_add_report_table"


def _connect(db_url: str) -> sqlite3.Connection:
    return sqlite3.connect(db_url.replace("sqlite:///", ""))


def _ddl(db_url: str) -> str:
    conn = _connect(db_url)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='render_job'"
        ).fetchone()
        return str(row[0])
    finally:
        conn.close()


def _seed(db_url: str, *, jobs: int = 3) -> None:
    """Insert a camera, a project and ``jobs`` render jobs (ids 1..jobs)."""
    conn = _connect(db_url)
    try:
        conn.execute(
            "INSERT INTO camera (name, address, protocol, created_at, updated_at) "
            "VALUES ('m28-cam', '127.0.0.1', 'vapix', '2026-10-01', '2026-10-01')"
        )
        conn.execute(
            "INSERT INTO project (camera_id, name, lifecycle_state, "
            "operational_status, frame_count, created_at, updated_at) "
            "VALUES (1, 'm28', 'active', 'idle', 0, '2026-10-01', '2026-10-01')"
        )
        for _ in range(jobs):
            _insert_job(conn)
        conn.commit()
    finally:
        conn.close()


def _insert_job(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        "INSERT INTO render_job (project_id, encoder_engine, kind, status, "
        "created_at, updated_at) "
        "VALUES (1, 'ffmpeg', 'manual', 'done', '2026-10-01', '2026-10-01')"
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _delete_highest_then_insert(db_url: str) -> tuple[int, int]:
    """Delete the highest-id job, insert a new one; return (deleted, new) ids."""
    conn = _connect(db_url)
    try:
        highest = conn.execute("SELECT max(id) FROM render_job").fetchone()[0]
        conn.execute("DELETE FROM render_job WHERE id = ?", (highest,))
        new_id = _insert_job(conn)
        conn.commit()
        return highest, new_id
    finally:
        conn.close()


def _sequence(db_url: str) -> list[tuple[str, int]]:
    conn = _connect(db_url)
    try:
        return conn.execute(
            "SELECT name, seq FROM sqlite_sequence WHERE name = 'render_job'"
        ).fetchall()
    finally:
        conn.close()


def _ids(db_url: str) -> list[int]:
    conn = _connect(db_url)
    try:
        return [r[0] for r in conn.execute("SELECT id FROM render_job ORDER BY id")]
    finally:
        conn.close()


class TestMigration028:
    def test_reuse_happens_before_the_upgrade(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        """Pins the bug this migration fixes (a plain rowid reuses the id)."""
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _PREV)
        _seed(tmp_db_url)

        deleted, new_id = _delete_highest_then_insert(tmp_db_url)

        assert new_id == deleted

    def test_upgrade_adds_autoincrement_and_keeps_rows_and_index(
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
                    "WHERE type='index' AND tbl_name='render_job'"
                )
            }
        finally:
            conn.close()
        assert "ix_render_job_project_id" in indexes

    def test_deleted_highest_id_is_not_reused_after_upgrade(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _PREV)
        _seed(tmp_db_url)
        command.upgrade(alembic_cfg, _REV)

        deleted, new_id = _delete_highest_then_insert(tmp_db_url)

        assert new_id > deleted

    def test_sequence_is_seeded_past_ids_the_event_log_names(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        """Jobs 4 and 9 were deleted before the upgrade but the log names them."""
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _PREV)
        _seed(tmp_db_url)
        conn = _connect(tmp_db_url)
        try:
            conn.execute(
                "INSERT INTO event (scope, scope_id, level, message, timestamp) "
                "VALUES ('project', 1, 'info', 'manual render 4 triggered', "
                "'2026-10-01')"
            )
            conn.execute(
                "INSERT INTO event (scope, scope_id, level, message, metadata, "
                "timestamp) VALUES ('project', 1, 'error', 'something failed', "
                '\'{"render_id": 9, "type": "render.failed"}\', \'2026-10-01\')'
            )
            conn.commit()
        finally:
            conn.close()

        command.upgrade(alembic_cfg, _REV)

        assert _sequence(tmp_db_url) == [("render_job", 9)]
        conn = _connect(tmp_db_url)
        try:
            assert _insert_job(conn) == 10
            conn.commit()
        finally:
            conn.close()

    def test_hostile_or_unstructured_log_values_are_ignored(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        """Numbers a user could put in the log must not move the counter.

        Free text ("render 99999999999999999999", a project name with "render
        500" in it), a render_id on an event type the worker never writes, and
        an absurd render_id on a render.failed event are all ignored; a huge
        sequence would make later inserts overflow (a render denial of service).
        """
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _PREV)
        _seed(tmp_db_url)
        events = [
            ("render 99999999999999999999 deleted by x", None),
            ("render 99999999999999999999", None),
            ("project 'my render 500' updated", None),
            ("user note: export 700 triggered", None),
            ("something", '{"render_id": 4611686018427387904, "type": "other"}'),
            (
                "render 4611686018427387904 failed: x",
                '{"render_id": 4611686018427387904, "type": "render.failed"}',
            ),
        ]
        conn = _connect(tmp_db_url)
        try:
            for message, metadata in events:
                conn.execute(
                    "INSERT INTO event (scope, scope_id, level, message, metadata, "
                    "timestamp) VALUES ('project', 1, 'info', ?, ?, '2026-10-01')",
                    (message, metadata),
                )
            conn.commit()
        finally:
            conn.close()

        command.upgrade(alembic_cfg, _REV)

        assert _sequence(tmp_db_url) == [("render_job", 3)]
        conn = _connect(tmp_db_url)
        try:
            assert _insert_job(conn) == 4
            conn.commit()
        finally:
            conn.close()

    def test_fresh_database_starts_at_one(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)
        _seed(tmp_db_url, jobs=1)

        assert "AUTOINCREMENT" in _ddl(tmp_db_url)
        assert _ids(tmp_db_url) == [1]

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
        assert _sequence(tmp_db_url) == []

    def test_round_trip(self, alembic_cfg: Config, tmp_db_url: str) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
        command.upgrade(alembic_cfg, _REV)
        command.downgrade(alembic_cfg, _PREV)
        command.upgrade(alembic_cfg, _REV)

        assert "AUTOINCREMENT" in _ddl(tmp_db_url)
