"""Migration test for revision 031_entity_autoincrement (#17).

Verifies:
- before the upgrade, deleting the highest-id project and inserting another
  reuses the id (the bug)
- upgrade rebuilds user, camera, project, milestone and frame with
  AUTOINCREMENT and changes nothing else: every row of every table (so every
  child-table count and every FK column, including SET NULL links) is
  unchanged, ``foreign_key_check`` is empty, ``integrity_check`` is ``ok``,
  and the schema of every table and index matches apart from AUTOINCREMENT
- each sequence is seeded to the highest believable id recorded for it, from
  referencing columns, frame-event metadata and anchored audit messages, while
  user-influenced text and too-far candidates are ignored (with a warning)
- after the upgrade a deleted highest id is never handed out again, for all
  five tables
- a database that already has dangling references is refused untouched
- downgrade restores the old schema and rows and clears the sequences, and a
  re-upgrade seeds them again
- the ORM models declare the same AUTOINCREMENT (no models/migrations drift)
"""

from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from timelapse_manager.db.models import Base

_REV = "031_entity_autoincrement"
_TABLES = ("user", "camera", "project", "milestone", "frame")


def _prev(cfg: Config) -> str:
    """The revision 031 builds on, read from the script so a re-point needs no edit."""
    revision = ScriptDirectory.from_config(cfg).get_revision(_REV)
    assert revision is not None
    down = revision.down_revision
    assert isinstance(down, str)
    return down


def _connect(db_url: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_url.replace("sqlite:///", ""))
    # The application always runs with enforcement on (db/engine.py).
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

# Events recording ids of rows deleted before the upgrade, plus hostile and
# unstructured values that must not move a counter. (scope, scope_id, message,
# metadata).
_EVENTS: tuple[tuple[str, int | None, str, str | None], ...] = (
    # project: scope_id 5 (structured) and an anchored clone message naming 8.
    ("project", 5, "project 5 deleted", None),
    ("project", None, "project 8 cloned from 1", None),
    # Hostile: a project *named* like an audit message, inside real templates.
    (
        "project",
        2,
        "project 'project 50 created' reached its end date; capture stopped "
        "and the project was archived",
        None,
    ),
    (
        "project",
        2,
        "camera may be frozen: 3 consecutive identical frames captured for "
        "project 'project 999999999999 created'",
        None,
    ),
    ("project", 2, "project 60 created by someone", None),
    ("camera", 1, "project 70 created", None),
    # Too far: anchored, or structured, but more than _MAX_JUMP above max(id).
    ("project", None, "project 5000000 deleted", None),
    ("project", None, "project 999999999999 created", None),
    ("project", 3000000, "something about a project", None),
    # camera: scope_id 4 (structured) and an anchored message naming 6.
    ("camera", 4, "camera 4 updated", None),
    ("camera", None, "camera 6 deleted", None),
    ("camera", 1, "camera 9 updated by bob", None),
    ("project", 1, "camera 10 deleted", None),
    # milestone: anchored project-scoped message naming 5.
    ("project", 1, "milestone 5 deleted", None),
    ("camera", 1, "milestone 9 deleted", None),
    ("project", 1, "milestone 8 deleted (by request)", None),
    # frame: metadata names 9, a message-only import names 11.
    (
        "project",
        1,
        "frame 9 permanently deleted",
        '{"frame_id": 9, "action": "permanent_delete"}',
    ),
    ("project", 1, "frame 11 imported", None),
    ("project", 1, "something", '{"frame_id": 12, "action": "other"}'),
    ("project", 1, "something", '{"frame_id": "13", "action": "restore"}'),
    ("project", 1, "something", '{"frame_id": 4000000, "action": "restore"}'),
    (
        "project",
        1,
        "camera may be frozen: 3 consecutive identical frames captured for "
        "project 'frame 15 uploaded'",
        None,
    ),
)

# The sequence each table must hold after the upgrade (see _EVENTS).
_EXPECTED_SEQUENCE = {"user": 3, "camera": 6, "project": 8, "milestone": 5, "frame": 11}


def _seed(db_url: str) -> None:
    """Seed a small but complete graph with every kind of child link."""
    conn = _connect(db_url)
    try:
        for user_id, name in ((1, "admin"), (2, "bob"), (3, "carol")):
            conn.execute(
                "INSERT INTO user (id, username, username_key, auth_source, role, "
                "enabled) VALUES (?, ?, ?, 'local', 'admin', 1)",
                (user_id, name, name),
            )
            conn.execute(
                "INSERT INTO session (user_id, persistent, revoked) VALUES (?, 0, 0)",
                (user_id,),
            )
        for camera_id in (1, 2):
            conn.execute(
                "INSERT INTO camera (id, name, address, protocol) "
                "VALUES (?, ?, '127.0.0.1', 'vapix')",
                (camera_id, f"cam-{camera_id}"),
            )
        # Project 2's camera link is the RESTRICT child of camera 2; its name is
        # hostile (it reads like an audit message).
        for project_id, camera_id, name in (
            (1, 1, "alpha"),
            (2, 2, "project 50 created"),
        ):
            conn.execute(
                "INSERT INTO project (id, camera_id, name, lifecycle_state, "
                "operational_status, frame_count) "
                "VALUES (?, ?, ?, 'active', 'idle', 0)",
                (project_id, camera_id, name),
            )
        for frame_id in (1, 2, 3, 4):
            conn.execute(
                "INSERT INTO frame (id, project_id, sequence_index, "
                "capture_status, origin, lifecycle_state) "
                "VALUES (?, 1, ?, 'captured', 'captured', 'active')",
                (frame_id, frame_id),
            )
        for milestone_id in (1, 2):
            conn.execute(
                "INSERT INTO milestone (id, project_id, user_id, label) "
                "VALUES (?, 1, 2, 'm')",
                (milestone_id,),
            )
        conn.execute(
            "INSERT INTO render_job (project_id, encoder_engine, kind, status) "
            "VALUES (1, 'ffmpeg', 'manual', 'done')"
        )
        conn.execute(
            "INSERT INTO exact_time_fire (project_id, anchor_id, local_date, "
            "status, fired_at, frame_id) "
            "VALUES (1, 'noon', '2026-10-23', 'captured', '2026-10-23', 4)"
        )
        conn.execute(
            "INSERT INTO report (report_key, user_id, username, area, project_id, "
            "camera_id, title, description, reported_at, delivery_state) "
            "VALUES ('TLM-20261023-0001', 3, 'carol', 'project', 1, 1, 't', 'd', "
            "'2026-10-23', 'ok')"
        )
        conn.execute(
            "INSERT INTO event (scope, scope_id, level, message, actor_user_id, "
            "alert_cleared_by) VALUES ('project', 1, 'warning', 'an alert', 2, 3)"
        )
        for scope, scope_id, message, metadata in _EVENTS:
            conn.execute(
                "INSERT INTO event (scope, scope_id, level, message, metadata) "
                "VALUES (?, ?, 'info', ?, ?)",
                (scope, scope_id, message, metadata),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


def _user_tables(conn: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version' ORDER BY name"
        )
    ]


def _all_rows(db_url: str) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of every application table, in rowid order."""
    conn = _connect(db_url)
    try:
        return {
            table: conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            for table in _user_tables(conn)
        }
    finally:
        conn.close()


_TABLE_CONSTRAINT = re.compile(r"\A(?:PRIMARY KEY|UNIQUE|FOREIGN KEY|CONSTRAINT|CHECK)")


def _split_top_level(body: str) -> list[str]:
    """Split a CREATE TABLE body on the commas that are not inside parentheses."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return [" ".join(part.split()) for part in parts if part.strip()]


def _canonical_table_sql(sql: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """The table DDL with only batch recreate's cosmetic changes normalised.

    Batch recreate re-renders the DDL. The normalisations, and nothing else:
    - whitespace and line breaks (columns added by ADD COLUMN share a line);
    - the quoting of the table name;
    - ``DEFAULT (X)`` vs ``DEFAULT X`` around a single bare word, such as
      ``CURRENT_TIMESTAMP``: the same default in SQLite;
    - the order of the table constraints (compared as a sorted list);
    - ``id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT`` is folded back to the
      ``PRIMARY KEY (id)`` table constraint, because AUTOINCREMENT is only legal
      on the column form. That fold is the AUTOINCREMENT difference itself.
    Column names, types, NOT NULL, defaults, column order, CHECKs, UNIQUEs and
    foreign keys with their ON DELETE actions must all match verbatim.
    """
    match = re.match(r'\s*CREATE TABLE "?(\w+)"?\s*\((.*)\)\s*\Z', sql, re.DOTALL)
    assert match, sql
    name, body = match.groups()
    columns: list[str] = []
    constraints: list[str] = []
    for clause in _split_top_level(body):
        clause = re.sub(r"DEFAULT \((\w+)\)", r"DEFAULT \1", clause)
        if _TABLE_CONSTRAINT.match(clause):
            constraints.append(clause)
            continue
        inline_pk = re.fullmatch(
            r"(\w+ INTEGER NOT NULL) PRIMARY KEY AUTOINCREMENT", clause
        )
        if inline_pk:
            columns.append(inline_pk.group(1))
            constraints.append(f"PRIMARY KEY ({clause.split()[0]})")
            continue
        columns.append(clause)
    return name, tuple(columns), tuple(sorted(constraints))


def _schema(db_url: str) -> dict[str, Any]:
    """Every table and index, by DDL and by SQLite's own pragmas."""
    conn = _connect(db_url)
    try:
        schema: dict[str, Any] = {}
        for table in _user_tables(conn):
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()[0]
            indexes = conn.execute(f'PRAGMA index_list("{table}")').fetchall()
            schema[table] = {
                "ddl": _canonical_table_sql(sql),
                "columns": conn.execute(f'PRAGMA table_xinfo("{table}")').fetchall(),
                # Without the positional constraint id, which the reordering
                # of table constraints renumbers.
                "foreign_keys": sorted(
                    tuple(row[1:])
                    for row in conn.execute(f'PRAGMA foreign_key_list("{table}")')
                ),
                # (name, unique, origin, partial); the seq column is positional.
                "index_list": sorted(tuple(row[1:]) for row in indexes),
                "index_columns": {
                    row[1]: conn.execute(f'PRAGMA index_xinfo("{row[1]}")').fetchall()
                    for row in indexes
                },
                # Exact CREATE INDEX text, so a lost partial-index WHERE shows.
                "index_sql": sorted(
                    conn.execute(
                        "SELECT name, sql FROM sqlite_master "
                        "WHERE type = 'index' AND tbl_name = ?",
                        (table,),
                    ).fetchall()
                ),
            }
        return schema
    finally:
        conn.close()


def _raw_table_sql(db_url: str) -> dict[str, str]:
    conn = _connect(db_url)
    try:
        return {
            str(name): str(sql)
            for name, sql in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        conn.close()


def _sequences(db_url: str) -> dict[str, int]:
    conn = _connect(db_url)
    try:
        return {
            str(name): int(seq)
            for name, seq in conn.execute(
                "SELECT name, seq FROM sqlite_sequence WHERE name IN "
                "('user', 'camera', 'project', 'milestone', 'frame')"
            )
        }
    finally:
        conn.close()


def _checks(db_url: str) -> tuple[list[Any], list[Any]]:
    """(foreign_key_check rows, integrity_check rows)."""
    conn = _connect(db_url)
    try:
        return (
            conn.execute("PRAGMA foreign_key_check").fetchall(),
            conn.execute("PRAGMA integrity_check").fetchall(),
        )
    finally:
        conn.close()


def _current_revision(db_url: str) -> str:
    conn = _connect(db_url)
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        return str(row[0])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Delete-then-insert
# ---------------------------------------------------------------------------


def _insert_row(conn: sqlite3.Connection, table: str) -> int:
    statements = {
        "user": (
            "INSERT INTO user (username, username_key, auth_source, role, "
            "enabled) VALUES ('new', 'new', 'local', 'viewer', 1)"
        ),
        "camera": (
            "INSERT INTO camera (name, address, protocol) "
            "VALUES ('cam-new', '127.0.0.1', 'vapix')"
        ),
        "project": (
            "INSERT INTO project (camera_id, name, lifecycle_state, "
            "operational_status, frame_count) "
            "VALUES (1, 'new', 'active', 'idle', 0)"
        ),
        "milestone": (
            "INSERT INTO milestone (project_id, user_id, label) VALUES (1, 1, 'n')"
        ),
        "frame": (
            "INSERT INTO frame (project_id, sequence_index, capture_status, "
            "origin, lifecycle_state) VALUES (1, 99, 'captured', 'captured', "
            "'active')"
        ),
    }
    cursor = conn.execute(statements[table])
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _delete_highest_then_insert(db_url: str) -> dict[str, tuple[int, int]]:
    """For each table, delete the highest-id row and insert another.

    Children go first so RESTRICT links (project -> camera) allow the delete.
    Returns ``{table: (deleted id, new id)}``.
    """
    conn = _connect(db_url)
    try:
        result: dict[str, tuple[int, int]] = {}
        for table in ("frame", "milestone", "project", "camera", "user"):
            highest = conn.execute(f'SELECT max(id) FROM "{table}"').fetchone()[0]
            conn.execute(f'DELETE FROM "{table}" WHERE id = ?', (highest,))
            result[table] = (highest, _insert_row(conn, table))
        conn.commit()
        return result
    finally:
        conn.close()


class _WarningRecorder(logging.Filter):
    """Records WARNING-and-above messages; never drops a record."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            self.messages.append(record.getMessage())
        return True


@contextlib.contextmanager
def _record_migration_warnings() -> Iterator[list[str]]:
    """Record the warnings the migration logs, whatever state the logger is in.

    ``env.py`` runs ``fileConfig``, which replaces the root handlers that
    ``caplog`` relies on and clears the handlers of ``alembic``'s child loggers.
    It keeps their filters, so the recorder is a filter on the migration's own
    logger. ``fileConfig(disable_existing_loggers=False)`` also leaves a child
    logger's ``disabled`` flag as it found it, and a disabled logger never runs
    its filters, so an earlier test's ``disable_existing_loggers`` dictConfig on
    the same worker silently lost every warning (#126). The flag is cleared here
    and, because ``fileConfig`` only keeps it, stays cleared during the upgrade.
    """
    recorder = _WarningRecorder()
    logger = logging.getLogger("alembic.runtime.migration")
    was_disabled = logger.disabled
    logger.disabled = False
    logger.addFilter(recorder)
    try:
        yield recorder.messages
    finally:
        logger.removeFilter(recorder)
        logger.disabled = was_disabled


@pytest.fixture()
def migration_warnings() -> Iterator[list[str]]:
    """Warnings the migration logs (see :func:`_record_migration_warnings`)."""
    with _record_migration_warnings() as messages:
        yield messages


@pytest.fixture()
def at_prev(alembic_cfg: Config, tmp_db_url: str) -> str:
    """A seeded database at the revision before 031; returns its URL."""
    command.upgrade(alembic_cfg, _prev(alembic_cfg))
    _seed(tmp_db_url)
    return tmp_db_url


class TestMigration031:
    def test_reuse_happens_before_the_upgrade(self, at_prev: str) -> None:
        """Pins the bug this migration fixes (a plain rowid reuses the id)."""
        changes = _delete_highest_then_insert(at_prev)

        assert changes["project"][1] == changes["project"][0]
        assert changes["frame"][1] == changes["frame"][0]

    def test_upgrade_adds_autoincrement_to_the_five_tables_only(
        self, alembic_cfg: Config, at_prev: str
    ) -> None:
        before = _raw_table_sql(at_prev)

        command.upgrade(alembic_cfg, _REV)

        after = _raw_table_sql(at_prev)
        for table in _TABLES:
            assert "AUTOINCREMENT" not in before[table]
            assert "AUTOINCREMENT" in after[table]
        untouched = set(before) - set(_TABLES) - {"sqlite_sequence"}
        assert {t: after[t] for t in untouched} == {t: before[t] for t in untouched}

    def test_upgrade_keeps_every_row_of_every_table(
        self, alembic_cfg: Config, at_prev: str
    ) -> None:
        """Counts, FK columns and SET NULL links: all rows compare equal."""
        before = _all_rows(at_prev)

        command.upgrade(alembic_cfg, _REV)

        after = _all_rows(at_prev)
        assert after == before
        # Spelled out for the links a cascade or SET NULL would have changed.
        assert len(after["session"]) == 3
        assert len(after["frame"]) == 4
        assert len(after["milestone"]) == 2
        assert len(after["render_job"]) == 1
        conn = _connect(at_prev)
        try:
            assert conn.execute(
                "SELECT user_id, project_id, camera_id FROM report"
            ).fetchall() == [(3, 1, 1)]
            assert conn.execute("SELECT frame_id FROM exact_time_fire").fetchall() == [
                (4,)
            ]
            assert conn.execute(
                "SELECT actor_user_id, alert_cleared_by FROM event "
                "WHERE message = 'an alert'"
            ).fetchall() == [(2, 3)]
        finally:
            conn.close()

    def test_upgrade_leaves_no_dangling_reference_and_a_sound_file(
        self, alembic_cfg: Config, at_prev: str
    ) -> None:
        command.upgrade(alembic_cfg, _REV)

        foreign_key_rows, integrity_rows = _checks(at_prev)
        assert foreign_key_rows == []
        assert integrity_rows == [("ok",)]

    def test_upgrade_keeps_every_column_constraint_and_index(
        self, alembic_cfg: Config, at_prev: str
    ) -> None:
        """Full DDL of every table and index matches, apart from AUTOINCREMENT.

        The comparison would catch a lost partial-index WHERE (index SQL is
        compared verbatim, plus the pragma's ``partial`` flag), a lost CHECK or
        UNIQUE, a changed ON DELETE action and a changed server default.
        """
        before = _schema(at_prev)

        command.upgrade(alembic_cfg, _REV)

        after = _schema(at_prev)
        assert after.keys() == before.keys()
        for table in before:
            assert after[table] == before[table], table
        # 025's case-insensitive username key survives as a UNIQUE index.
        assert ("ix_user_username_key", 1, "c", 0) in after["user"]["index_list"]

    def test_sequences_are_seeded_from_believable_records_only(
        self,
        alembic_cfg: Config,
        at_prev: str,
        migration_warnings: list[str],
    ) -> None:
        command.upgrade(alembic_cfg, _REV)

        assert _sequences(at_prev) == _EXPECTED_SEQUENCE
        joined = "\n".join(migration_warnings)
        assert "ignoring project id 5000000" in joined
        assert "ignoring project id 999999999999" in joined
        assert "ignoring project id 3000000" in joined
        assert "ignoring frame id 4000000" in joined

    def test_warnings_are_recorded_after_another_test_disabled_the_logger(
        self,
        alembic_cfg: Config,
        at_prev: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#126: a leaked ``disabled`` flag must not hide the migration warnings."""
        monkeypatch.setattr(
            logging.getLogger("alembic.runtime.migration"), "disabled", True
        )
        with _record_migration_warnings() as messages:
            command.upgrade(alembic_cfg, _REV)
        assert any("ignoring project id 5000000" in m for m in messages)

    def test_deleted_highest_id_is_never_reused_after_upgrade(
        self, alembic_cfg: Config, at_prev: str
    ) -> None:
        command.upgrade(alembic_cfg, _REV)

        changes = _delete_highest_then_insert(at_prev)

        for table, (deleted, new_id) in changes.items():
            assert new_id > deleted, table
            assert new_id == _EXPECTED_SEQUENCE[table] + 1, table

    def test_dangling_reference_refuses_the_upgrade_untouched(
        self, alembic_cfg: Config, at_prev: str
    ) -> None:
        conn = sqlite3.connect(at_prev.replace("sqlite:///", ""))
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute(
                "INSERT INTO session (user_id, persistent, revoked) VALUES (99, 0, 0)"
            )
            conn.commit()
        finally:
            conn.close()
        previous = _current_revision(at_prev)

        with pytest.raises(RuntimeError, match="foreign_key_check"):
            command.upgrade(alembic_cfg, _REV)

        assert _current_revision(at_prev) == previous
        sql = _raw_table_sql(at_prev)
        assert not any("AUTOINCREMENT" in sql[table] for table in _TABLES)

    def test_downgrade_restores_schema_and_rows_and_clears_sequences(
        self, alembic_cfg: Config, at_prev: str
    ) -> None:
        schema_before = _schema(at_prev)
        rows_before = _all_rows(at_prev)
        command.upgrade(alembic_cfg, _REV)

        command.downgrade(alembic_cfg, _prev(alembic_cfg))

        sql = _raw_table_sql(at_prev)
        assert not any("AUTOINCREMENT" in sql[table] for table in _TABLES)
        assert _schema(at_prev) == schema_before
        assert _all_rows(at_prev) == rows_before
        assert _sequences(at_prev) == {}
        assert _checks(at_prev) == ([], [("ok",)])

    def test_downgrade_then_upgrade_again(
        self, alembic_cfg: Config, at_prev: str
    ) -> None:
        rows_before = _all_rows(at_prev)
        command.upgrade(alembic_cfg, _REV)
        command.downgrade(alembic_cfg, _prev(alembic_cfg))

        command.upgrade(alembic_cfg, _REV)

        assert _sequences(at_prev) == _EXPECTED_SEQUENCE
        assert _all_rows(at_prev) == rows_before
        assert _checks(at_prev) == ([], [("ok",)])
        changes = _delete_highest_then_insert(at_prev)
        assert all(new > deleted for deleted, new in changes.values())

    def test_fresh_database_starts_at_one(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        command.upgrade(alembic_cfg, "head")

        sql = _raw_table_sql(tmp_db_url)
        assert all("AUTOINCREMENT" in sql[table] for table in _TABLES)
        conn = _connect(tmp_db_url)
        try:
            assert _insert_row(conn, "user") == 1
            assert _insert_row(conn, "camera") == 1
            conn.commit()
        finally:
            conn.close()

    def test_models_declare_autoincrement_like_the_migration(
        self, tmp_path: Path
    ) -> None:
        """No drift: ``create_all`` from the models gives the same AUTOINCREMENT."""
        engine = sa.create_engine(f"sqlite:///{tmp_path / 'models.db'}")
        try:
            Base.metadata.create_all(engine)
            with engine.connect() as conn:
                rows = conn.exec_driver_sql(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'table'"
                ).all()
        finally:
            engine.dispose()
        sql = {str(name): str(ddl) for name, ddl in rows}
        for table in _TABLES:
            assert "AUTOINCREMENT" in sql[table], table
