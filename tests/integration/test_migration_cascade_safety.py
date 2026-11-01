"""Regression tests: SQLite table-rebuild migrations must not lose child rows.

SQLite cannot ALTER most columns in place, so Alembic rebuilds the table
(create-copy-drop-rename). The implicit DROP of the old table fires the
``ON DELETE CASCADE`` foreign keys on child tables and, unless foreign-key
enforcement is suspended for the rebuild, silently deletes their rows even
though the parent's own data is copied across.

Each affected migration here either widens a portable enum on ``project`` (004)
or drops columns from ``project``/``user`` on downgrade (005, 006, 010, 013).
For every one, seeding a parent plus child rows and running the migration
across the rebuild must leave the child-row counts unchanged.

A separate teeth check proves the guard is load-bearing: running the same
rebuild SQL with foreign keys left ON deletes the children, so a green result
above means the guard is doing real work and not that the cascade never fires.
"""

from __future__ import annotations

import sqlite3

import pytest
from alembic import command
from alembic.config import Config

# ---------------------------------------------------------------------------
# Seeding helpers (raw SQL at the target revision, mirroring test_migration_018)
# ---------------------------------------------------------------------------


def _path(db_url: str) -> str:
    return db_url.replace("sqlite:///", "")


def _seed_project_tree(db_url: str) -> None:
    """Seed camera + user + project + one frame/render_job/milestone child each.

    Uses only columns guaranteed present across the affected revisions, so the
    same seeding works at every migration boundary under test.
    """
    conn = sqlite3.connect(_path(db_url))
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("INSERT INTO camera (id, name) VALUES (1, 'cam')")
        conn.execute(
            "INSERT INTO user (id, username, auth_source, role, enabled) "
            "VALUES (1, 'u', 'local', 'admin', 1)"
        )
        conn.execute(
            "INSERT INTO project (id, camera_id, name, operational_status, "
            "lifecycle_state, frame_count) VALUES (1, 1, 'p', 'idle', 'active', 0)"
        )
        conn.execute(
            "INSERT INTO frame (id, project_id, sequence_index, capture_status, "
            "origin, lifecycle_state) "
            "VALUES (1, 1, 1, 'pending', 'captured', 'active')"
        )
        conn.execute(
            "INSERT INTO render_job (id, project_id, encoder_engine, kind, status) "
            "VALUES (1, 1, 'ffmpeg', 'manual', 'pending')"
        )
        conn.execute("INSERT INTO milestone (id, project_id, user_id) VALUES (1, 1, 1)")
        conn.commit()
    finally:
        conn.close()


def _seed_user_with_session(db_url: str) -> None:
    """Seed a user plus one session child (for the user-rebuild migration)."""
    conn = sqlite3.connect(_path(db_url))
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute(
            "INSERT INTO user (id, username, auth_source, role, enabled) "
            "VALUES (1, 'u', 'local', 'admin', 1)"
        )
        conn.execute(
            "INSERT INTO session (id, user_id, persistent, revoked) VALUES (1, 1, 0, 0)"
        )
        conn.commit()
    finally:
        conn.close()


def _count(db_url: str, table: str) -> int:
    conn = sqlite3.connect(_path(db_url))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Downgrade rebuilds of ``project`` -> must preserve frame/render_job/milestone
# ---------------------------------------------------------------------------

# (migration revision, the revision one step before it)
_PROJECT_DOWNGRADE_CASES = [
    ("005_add_project_campaign_bounds", "004_add_paused_lifecycle_state"),
    ("010_add_project_stream_identity", "009_add_camera_default_credentials"),
    ("013_add_project_ptz", "012_add_event_alert_lifecycle"),
]


@pytest.mark.parametrize(("revision", "prev"), _PROJECT_DOWNGRADE_CASES)
def test_project_downgrade_preserves_child_rows(
    alembic_cfg: Config, tmp_db_url: str, revision: str, prev: str
) -> None:
    """Downgrading across a ``project`` column-drop must not cascade-delete."""
    alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
    command.upgrade(alembic_cfg, revision)
    _seed_project_tree(tmp_db_url)

    command.downgrade(alembic_cfg, prev)

    assert _count(tmp_db_url, "frame") == 1
    assert _count(tmp_db_url, "render_job") == 1
    assert _count(tmp_db_url, "milestone") == 1


# ---------------------------------------------------------------------------
# Migration 004: enum widen on ``project`` rebuilds the table on BOTH directions
# ---------------------------------------------------------------------------


def test_004_upgrade_preserves_child_rows(alembic_cfg: Config, tmp_db_url: str) -> None:
    """The enum-widen upgrade rebuilds ``project`` and must keep child rows."""
    alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
    command.upgrade(alembic_cfg, "003_session_auth_columns")
    _seed_project_tree(tmp_db_url)

    command.upgrade(alembic_cfg, "004_add_paused_lifecycle_state")

    assert _count(tmp_db_url, "frame") == 1
    assert _count(tmp_db_url, "render_job") == 1
    assert _count(tmp_db_url, "milestone") == 1


def test_004_downgrade_preserves_child_rows(
    alembic_cfg: Config, tmp_db_url: str
) -> None:
    """The enum-narrow downgrade rebuilds ``project`` and must keep child rows."""
    alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
    command.upgrade(alembic_cfg, "004_add_paused_lifecycle_state")
    _seed_project_tree(tmp_db_url)

    command.downgrade(alembic_cfg, "003_session_auth_columns")

    assert _count(tmp_db_url, "frame") == 1
    assert _count(tmp_db_url, "render_job") == 1
    assert _count(tmp_db_url, "milestone") == 1


# ---------------------------------------------------------------------------
# Migration 006: downgrade rebuilds ``user`` -> must preserve ``session`` rows
# ---------------------------------------------------------------------------


def test_006_downgrade_preserves_session_rows(
    alembic_cfg: Config, tmp_db_url: str
) -> None:
    """Downgrading across the ``user`` column-drop must not cascade-delete."""
    alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
    command.upgrade(alembic_cfg, "006_add_user_preferences")
    _seed_user_with_session(tmp_db_url)

    command.downgrade(alembic_cfg, "005_add_project_campaign_bounds")

    assert _count(tmp_db_url, "session") == 1


# ---------------------------------------------------------------------------
# Teeth check: the guard is load-bearing
# ---------------------------------------------------------------------------


def test_cascade_fires_without_the_guard(alembic_cfg: Config, tmp_db_url: str) -> None:
    """Sanity: with foreign keys left ON, a ``project`` rebuild DOES delete
    child rows. This proves the passing tests above exercise a real cascade
    rather than a path where no cascade could ever fire.
    """
    alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
    command.upgrade(alembic_cfg, "013_add_project_ptz")
    _seed_project_tree(tmp_db_url)

    # Reproduce the batch rebuild Alembic performs (create-copy-drop-rename)
    # for a column drop, but WITHOUT suspending foreign keys.
    conn = sqlite3.connect(_path(tmp_db_url))
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.executescript(
            """
            CREATE TABLE _rebuild_project (
                id INTEGER NOT NULL PRIMARY KEY,
                camera_id INTEGER NOT NULL,
                name VARCHAR NOT NULL,
                operational_status VARCHAR NOT NULL,
                lifecycle_state VARCHAR NOT NULL,
                frame_count INTEGER NOT NULL
            );
            INSERT INTO _rebuild_project
                SELECT id, camera_id, name, operational_status,
                       lifecycle_state, frame_count FROM project;
            DROP TABLE project;
            ALTER TABLE _rebuild_project RENAME TO project;
            """
        )
        conn.commit()
        remaining = conn.execute("SELECT COUNT(*) FROM frame").fetchone()[0]
    finally:
        conn.close()

    # The DROP of the old ``project`` cascaded into ``frame``: the child is gone.
    assert remaining == 0


# ---------------------------------------------------------------------------
# Migration 033 (#66): index-only on ``event``; no rebuild, no cascade
# ---------------------------------------------------------------------------

_033 = "033_event_hot_path_indexes"
_033_PREV = "032_render_job_codec"
_033_TABLES = ("camera", "project", "frame", "event", "exact_time_fire", "milestone")


def _seed_033_tree(db_url: str) -> None:
    """Seed camera, project, frames, events and fire rows at revision 032.

    Self-contained because ``user.username_key`` (025) is NOT NULL, which the
    older seeders above predate.
    """
    conn = sqlite3.connect(_path(db_url))
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute(
            "INSERT INTO user (id, username, username_key, auth_source, role, "
            "enabled) VALUES (1, 'u', 'u', 'local', 'admin', 1)"
        )
        conn.execute("INSERT INTO camera (id, name) VALUES (1, 'cam')")
        conn.execute(
            "INSERT INTO project (id, camera_id, name, operational_status, "
            "lifecycle_state, frame_count) VALUES (1, 1, 'p', 'idle', 'active', 0)"
        )
        conn.execute(
            "INSERT INTO frame (project_id, sequence_index, capture_status, "
            "origin, lifecycle_state) VALUES (1, 1, 'pending', 'captured', 'active')"
        )
        conn.execute("INSERT INTO milestone (project_id, user_id) VALUES (1, 1)")
        conn.execute(
            "INSERT INTO exact_time_fire (project_id, anchor_id, local_date, "
            "status, fired_at) VALUES (1, 'noon', '2026-10-01', 'fired', "
            "'2026-10-01 12:00:00')"
        )
        conn.execute(
            "INSERT INTO event (scope, scope_id, level, message, actor_user_id) "
            "VALUES ('project', 1, 'warning', 'e', 1)"
        )
        conn.commit()
    finally:
        conn.close()


def test_033_round_trip_preserves_rows(alembic_cfg: Config, tmp_db_url: str) -> None:
    """Creating and dropping the event indexes leaves every row in place."""
    alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
    command.upgrade(alembic_cfg, _033_PREV)
    _seed_033_tree(tmp_db_url)
    before = {table: _count(tmp_db_url, table) for table in _033_TABLES}
    assert all(count == 1 for count in before.values())

    command.upgrade(alembic_cfg, _033)
    assert {table: _count(tmp_db_url, table) for table in _033_TABLES} == before

    command.downgrade(alembic_cfg, _033_PREV)
    assert {table: _count(tmp_db_url, table) for table in _033_TABLES} == before


# ---------------------------------------------------------------------------
# Migration 034 (#69): native ADD COLUMN on ``exact_time_fire``; no rebuild
# ---------------------------------------------------------------------------

_034 = "034_exact_time_fire_retry"


def test_034_round_trip_preserves_rows(alembic_cfg: Config, tmp_db_url: str) -> None:
    """Adding and dropping the retry columns leaves every row in place.

    A rebuild of ``exact_time_fire`` would not cascade itself, but the rule is
    the same for every migration: native DDL only, proven by unchanged counts.
    """
    alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
    command.upgrade(alembic_cfg, _033)
    _seed_033_tree(tmp_db_url)
    before = {table: _count(tmp_db_url, table) for table in _033_TABLES}
    assert all(count == 1 for count in before.values())

    command.upgrade(alembic_cfg, _034)
    assert {table: _count(tmp_db_url, table) for table in _033_TABLES} == before

    command.downgrade(alembic_cfg, _033)
    assert {table: _count(tmp_db_url, table) for table in _033_TABLES} == before


# ---------------------------------------------------------------------------
# Migration 035 (#66): index-only on ``frame``; no rebuild, no cascade
# ---------------------------------------------------------------------------

_035 = "035_frame_read_path_indexes"


def test_035_round_trip_preserves_rows(alembic_cfg: Config, tmp_db_url: str) -> None:
    """Creating and dropping the frame read-path indexes leaves every row in place."""
    alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
    command.upgrade(alembic_cfg, _033)
    _seed_033_tree(tmp_db_url)
    command.upgrade(alembic_cfg, _034)
    before = {table: _count(tmp_db_url, table) for table in _033_TABLES}
    assert all(count == 1 for count in before.values())

    command.upgrade(alembic_cfg, _035)
    assert {table: _count(tmp_db_url, table) for table in _033_TABLES} == before

    command.downgrade(alembic_cfg, _034)
    assert {table: _count(tmp_db_url, table) for table in _033_TABLES} == before


# ---------------------------------------------------------------------------
# Migration 036 (#67): a new table only; no rebuild, no cascade
# ---------------------------------------------------------------------------

_036 = "036_notification_cursor"


def test_036_round_trip_preserves_rows(alembic_cfg: Config, tmp_db_url: str) -> None:
    """Creating and dropping the notification cursor leaves every row in place."""
    alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
    command.upgrade(alembic_cfg, _033)
    _seed_033_tree(tmp_db_url)
    command.upgrade(alembic_cfg, _035)
    before = {table: _count(tmp_db_url, table) for table in _033_TABLES}
    assert all(count == 1 for count in before.values())

    command.upgrade(alembic_cfg, _036)
    assert {table: _count(tmp_db_url, table) for table in _033_TABLES} == before

    command.downgrade(alembic_cfg, _035)
    assert {table: _count(tmp_db_url, table) for table in _033_TABLES} == before


# ---------------------------------------------------------------------------
# Migration 037 (#66): index-only on ``render_job``; no rebuild, no cascade
# ---------------------------------------------------------------------------

_037 = "037_render_job_hot_path_indexes"
_037_TABLES = (*_033_TABLES, "render_job")


def test_037_round_trip_preserves_rows(alembic_cfg: Config, tmp_db_url: str) -> None:
    """Creating and dropping the render_job indexes leaves every row in place."""
    alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
    command.upgrade(alembic_cfg, _033)
    _seed_033_tree(tmp_db_url)
    command.upgrade(alembic_cfg, _036)
    conn = sqlite3.connect(_path(tmp_db_url))
    try:
        conn.execute(
            "INSERT INTO render_job (project_id, encoder_engine, kind, status) "
            "VALUES (1, 'ffmpeg', 'scheduled', 'pending')"
        )
        conn.commit()
    finally:
        conn.close()
    before = {table: _count(tmp_db_url, table) for table in _037_TABLES}
    assert all(count == 1 for count in before.values())

    command.upgrade(alembic_cfg, _037)
    assert {table: _count(tmp_db_url, table) for table in _037_TABLES} == before

    command.downgrade(alembic_cfg, _036)
    assert {table: _count(tmp_db_url, table) for table in _037_TABLES} == before


# ---------------------------------------------------------------------------
# Migration 038 (#69): native ADD COLUMN on ``camera``; no rebuild, no cascade
# ---------------------------------------------------------------------------

_038 = "038_camera_allow_basic_over_http"


def test_038_round_trip_preserves_rows(alembic_cfg: Config, tmp_db_url: str) -> None:
    """Adding and dropping the camera Basic-over-HTTP flag leaves every row in place.

    ``camera`` is the parent of every project (and so of every frame, event and
    fire row): a rebuild with foreign keys on would cascade through all of them.
    """
    alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
    command.upgrade(alembic_cfg, _033)
    _seed_033_tree(tmp_db_url)
    command.upgrade(alembic_cfg, _036)
    conn = sqlite3.connect(_path(tmp_db_url))
    try:
        conn.execute(
            "INSERT INTO render_job (project_id, encoder_engine, kind, status) "
            "VALUES (1, 'ffmpeg', 'scheduled', 'pending')"
        )
        conn.commit()
    finally:
        conn.close()
    command.upgrade(alembic_cfg, _037)
    tables = _037_TABLES  # camera included
    before = {table: _count(tmp_db_url, table) for table in tables}
    assert all(count == 1 for count in before.values())

    command.upgrade(alembic_cfg, _038)
    assert {table: _count(tmp_db_url, table) for table in tables} == before

    command.downgrade(alembic_cfg, _037)
    assert {table: _count(tmp_db_url, table) for table in tables} == before


# ---------------------------------------------------------------------------
# Migration 039 (#69): native ADD COLUMN on ``camera``; no rebuild, no cascade
# ---------------------------------------------------------------------------

_039 = "039_camera_tls_pinned_cert"


def test_039_round_trip_preserves_rows(alembic_cfg: Config, tmp_db_url: str) -> None:
    """Adding and dropping the camera pin column leaves every row in place."""
    alembic_cfg.set_main_option("sqlalchemy.url", tmp_db_url)
    command.upgrade(alembic_cfg, _033)
    _seed_033_tree(tmp_db_url)
    command.upgrade(alembic_cfg, _036)
    conn = sqlite3.connect(_path(tmp_db_url))
    try:
        conn.execute(
            "INSERT INTO render_job (project_id, encoder_engine, kind, status) "
            "VALUES (1, 'ffmpeg', 'scheduled', 'pending')"
        )
        conn.commit()
    finally:
        conn.close()
    command.upgrade(alembic_cfg, _038)
    tables = _037_TABLES  # camera included
    before = {table: _count(tmp_db_url, table) for table in tables}
    assert all(count == 1 for count in before.values())

    command.upgrade(alembic_cfg, _039)
    assert {table: _count(tmp_db_url, table) for table in tables} == before

    command.downgrade(alembic_cfg, _038)
    assert {table: _count(tmp_db_url, table) for table in tables} == before
