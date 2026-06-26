"""Integration tests for the core schema migration (002_create_core_schema).

Verifies that upgrading to head creates the full 10-table schema, that the
WAL and foreign-key pragmas are active on new connections, and that
downgrade to base cleanly removes all entity tables.
"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from timelapse_manager.db.engine import create_db_engine

# Every entity table expected at the current migration head: the ten core tables
# from migration 002 plus tables added by later migrations.
_EXPECTED_TABLES = frozenset(
    {
        "camera",
        "project",
        "frame",
        "render_job",
        "milestone",
        "user",
        "session",
        "ldap_settings",
        "notification_settings",
        "event",
        "camera_default_credentials",
        "ssrf_settings",
    }
)


@pytest.fixture()
def _migrated(alembic_cfg: Config) -> Config:
    """Upgrade to head and yield the config for assertions."""
    command.upgrade(alembic_cfg, "head")
    return alembic_cfg


class TestUpgradeCreatesSchema:
    def test_upgrade_head_succeeds_without_error(self, alembic_cfg: Config) -> None:
        command.upgrade(alembic_cfg, "head")

    def test_alembic_version_table_exists_after_upgrade(
        self, _migrated: Config, tmp_db_url: str
    ) -> None:
        engine = create_db_engine(tmp_db_url)
        try:
            assert "alembic_version" in inspect(engine).get_table_names()
        finally:
            engine.dispose()

    def test_all_ten_entity_tables_exist_after_upgrade(
        self, _migrated: Config, tmp_db_url: str
    ) -> None:
        engine = create_db_engine(tmp_db_url)
        try:
            user_tables = {
                t
                for t in inspect(engine).get_table_names()
                if t != "alembic_version" and not t.startswith("sqlite_")
            }
            assert user_tables == _EXPECTED_TABLES
        finally:
            engine.dispose()

    def test_alembic_revision_is_current_head_after_head(
        self, _migrated: Config, tmp_db_url: str
    ) -> None:
        from alembic.script import ScriptDirectory

        head = ScriptDirectory.from_config(_migrated).get_current_head()
        engine = create_db_engine(tmp_db_url)
        try:
            with engine.connect() as conn:
                revision = MigrationContext.configure(conn).get_current_revision()
        finally:
            engine.dispose()
        assert revision == head


class TestPragmasAfterMigration:
    def test_wal_mode_is_active_on_new_connection(
        self, _migrated: Config, tmp_db_url: str
    ) -> None:
        engine = create_db_engine(tmp_db_url)
        try:
            with engine.connect() as conn:
                mode = conn.execute(text("PRAGMA journal_mode")).scalar()
        finally:
            engine.dispose()
        assert mode == "wal"

    def test_foreign_keys_are_on_on_new_connection(
        self, _migrated: Config, tmp_db_url: str
    ) -> None:
        engine = create_db_engine(tmp_db_url)
        try:
            with engine.connect() as conn:
                fk = conn.execute(text("PRAGMA foreign_keys")).scalar()
        finally:
            engine.dispose()
        assert fk == 1


class TestDowngradeRemovesSchema:
    def test_downgrade_to_base_succeeds(self, alembic_cfg: Config) -> None:
        command.upgrade(alembic_cfg, "head")
        command.downgrade(alembic_cfg, "base")

    def test_entity_tables_are_gone_after_downgrade_to_base(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        command.upgrade(alembic_cfg, "head")
        command.downgrade(alembic_cfg, "base")
        engine = create_db_engine(tmp_db_url)
        try:
            user_tables = {
                t
                for t in inspect(engine).get_table_names()
                if t != "alembic_version" and not t.startswith("sqlite_")
            }
            assert user_tables == set()
        finally:
            engine.dispose()

    def test_alembic_version_is_empty_after_downgrade_to_base(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        command.upgrade(alembic_cfg, "head")
        command.downgrade(alembic_cfg, "base")
        engine = create_db_engine(tmp_db_url)
        try:
            with engine.connect() as conn:
                rows = conn.execute(text("SELECT * FROM alembic_version")).fetchall()
            assert rows == []
        finally:
            engine.dispose()

    def test_roundtrip_upgrade_after_downgrade_succeeds(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        """upgrade → downgrade → upgrade must succeed (no residual state)."""
        command.upgrade(alembic_cfg, "head")
        command.downgrade(alembic_cfg, "base")
        command.upgrade(alembic_cfg, "head")
        engine = create_db_engine(tmp_db_url)
        try:
            user_tables = {
                t
                for t in inspect(engine).get_table_names()
                if t != "alembic_version" and not t.startswith("sqlite_")
            }
            assert user_tables == _EXPECTED_TABLES
        finally:
            engine.dispose()
