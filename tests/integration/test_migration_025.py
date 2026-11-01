"""Migration test for revision 025_add_user_username_key (issue #4).

Verifies:
- upgrade adds ``user.username_key`` (backfilled, NOT NULL) and the unique index
- directory (LDAP) duplicates that differ only by case/whitespace are merged into
  the oldest row: highest role kept, every foreign key re-pointed, extras deleted
- local duplicates (or a local/LDAP clash) stop the upgrade with a clear error
- downgrade drops the index and column
"""

from __future__ import annotations

import sqlite3

import pytest
from alembic import command
from alembic.config import Config

_REV = "025_add_user_username_key"
_PREV = "024_add_login_attempt"


def _connect(db_url: str) -> sqlite3.Connection:
    return sqlite3.connect(db_url.replace("sqlite:///", ""))


def _seed_users(db_url: str, users: list[tuple[int, str, str, str, int]]) -> None:
    conn = _connect(db_url)
    try:
        conn.executemany(
            'INSERT INTO "user" (id, username, auth_source, role, enabled, '
            "theme_preference) VALUES (?, ?, ?, ?, ?, 'system')",
            users,
        )
        conn.commit()
    finally:
        conn.close()


class TestMigration025:
    def test_upgrade_backfills_key_and_creates_unique_index(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        command.upgrade(alembic_cfg, _PREV)
        _seed_users(
            tmp_db_url,
            [
                (1, "system", "local", "admin", 1),
                (2, " Alice.Smith ", "ldap", "admin", 1),
            ],
        )
        command.upgrade(alembic_cfg, _REV)
        conn = _connect(tmp_db_url)
        try:
            keys = dict(conn.execute('SELECT id, username_key FROM "user"').fetchall())
            assert keys == {1: "system", 2: "alice.smith"}
            info = {r[1]: r for r in conn.execute('PRAGMA table_info("user")')}
            assert info["username_key"][3] == 1  # NOT NULL
            index = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='ix_user_username_key'"
            ).fetchone()
            assert index is not None and "UNIQUE" in index[0].upper()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    'INSERT INTO "user" (username, username_key, auth_source, role, '
                    "enabled, theme_preference) VALUES "
                    "('ALICE.SMITH', 'alice.smith', 'ldap', 'viewer', 1, 'system')"
                )
        finally:
            conn.close()

    def test_ldap_duplicates_are_merged_with_foreign_keys_repointed(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        command.upgrade(alembic_cfg, _PREV)
        _seed_users(
            tmp_db_url,
            [
                (1, "system", "local", "admin", 1),
                (2, "svc_bot", "ldap", "viewer", 1),
                (3, "Alice.smith", "ldap", "operator", 1),
                (4, "SVC_Bot", "ldap", "admin", 1),
                (5, "alice.smith", "ldap", "admin", 1),
            ],
        )
        conn = _connect(tmp_db_url)
        try:
            conn.execute("INSERT INTO camera (id, name) VALUES (1, 'c')")
            conn.execute(
                "INSERT INTO project (id, camera_id, name, operational_status, "
                "lifecycle_state, frame_count) VALUES (1, 1, 'p', 'idle', 'active', 0)"
            )
            conn.executemany(
                "INSERT INTO session (user_id, token_hash, persistent, revoked) "
                "VALUES (?, ?, 0, 0)",
                [(4, "t4"), (5, "t5"), (2, "t2")],
            )
            conn.execute(
                "INSERT INTO event (scope, level, message, actor_user_id, "
                "alert_cleared_by) VALUES ('system', 'info', 'm', 4, 5)"
            )
            conn.execute("INSERT INTO milestone (project_id, user_id) VALUES (1, 5)")
            conn.commit()
        finally:
            conn.close()

        command.upgrade(alembic_cfg, _REV)

        conn = _connect(tmp_db_url)
        try:
            users = {
                r[0]: r[1:]
                for r in conn.execute(
                    'SELECT id, username, role, enabled FROM "user" ORDER BY id'
                )
            }
            assert users == {
                1: ("system", "admin", 1),
                2: ("svc_bot", "admin", 1),  # oldest kept, highest role
                3: ("Alice.smith", "admin", 1),
            }
            sessions = sorted(conn.execute("SELECT token_hash, user_id FROM session"))
            assert sessions == [("t2", 2), ("t4", 2), ("t5", 3)]
            assert conn.execute(
                "SELECT actor_user_id, alert_cleared_by FROM event"
            ).fetchone() == (2, 3)
            assert conn.execute("SELECT user_id FROM milestone").fetchone() == (3,)
        finally:
            conn.close()

    @pytest.mark.parametrize(
        "users",
        [
            [(2, "admin", "local", "admin", 1), (3, "Admin", "local", "viewer", 1)],
            [(2, "bob", "local", "viewer", 1), (3, "BOB", "ldap", "admin", 1)],
        ],
        ids=["two-local", "local-and-ldap"],
    )
    def test_local_duplicates_stop_the_upgrade(
        self,
        alembic_cfg: Config,
        tmp_db_url: str,
        users: list[tuple[int, str, str, str, int]],
    ) -> None:
        command.upgrade(alembic_cfg, _PREV)
        _seed_users(tmp_db_url, [(1, "system", "local", "admin", 1), *users])
        with pytest.raises(RuntimeError, match="differ only by letter case"):
            command.upgrade(alembic_cfg, _REV)
        conn = _connect(tmp_db_url)
        try:
            assert conn.execute('SELECT COUNT(*) FROM "user"').fetchone()[0] == 3
            cols = {r[1] for r in conn.execute('PRAGMA table_info("user")')}
            assert "username_key" not in cols  # schema untouched by the refusal
            # The administrator resolves it; the retry then succeeds.
            conn.execute('DELETE FROM "user" WHERE id = 3')
            conn.commit()
        finally:
            conn.close()
        command.upgrade(alembic_cfg, _REV)

    def test_downgrade_drops_index_and_column(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        command.upgrade(alembic_cfg, _PREV)
        _seed_users(tmp_db_url, [(1, "system", "local", "admin", 1)])
        conn = _connect(tmp_db_url)
        conn.execute(
            "INSERT INTO session (user_id, token_hash, persistent, revoked) "
            "VALUES (1, 'keep', 0, 0)"
        )
        conn.commit()
        conn.close()
        command.upgrade(alembic_cfg, _REV)
        command.downgrade(alembic_cfg, _PREV)
        conn = _connect(tmp_db_url)
        try:
            cols = {r[1] for r in conn.execute('PRAGMA table_info("user")')}
            assert "username_key" not in cols
            assert (
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE name='ix_user_username_key'"
                ).fetchone()
                is None
            )
            # The rebuild must not cascade-delete sessions.
            assert conn.execute("SELECT COUNT(*) FROM session").fetchone()[0] == 1
        finally:
            conn.close()
        command.upgrade(alembic_cfg, _REV)  # round-trip
