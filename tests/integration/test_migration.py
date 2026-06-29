"""Integration tests for the Alembic database migration baseline.

Runs migrations against a temporary SQLite file to verify:
- migration 001 (empty baseline) applies without error and creates only alembic_version,
- migration 002 (core schema) creates the expected 10 entity tables,
- WAL journal mode is active after migration,
- downgrade from head returns the database to a clean state.

All assertions use a temp directory so no files are left in the repository.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from timelapse_manager.db.engine import create_db_engine

# Every entity table expected at the current migration head: the ten core tables
# from migration 002 plus tables added by later migrations.
_CORE_TABLES = frozenset(
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
        "exact_time_fire",
    }
)


@pytest.fixture()
def _migrated_to_001(alembic_cfg: Config) -> Config:
    """Upgrade to the empty baseline only and return the config."""
    command.upgrade(alembic_cfg, "001_empty_baseline")
    return alembic_cfg


@pytest.fixture()
def _migrated_db(alembic_cfg: Config) -> Config:
    """Run 'upgrade head' (through 002) and return the config."""
    command.upgrade(alembic_cfg, "head")
    return alembic_cfg


class TestUpgradeTo001:
    def test_upgrade_001_succeeds(self, alembic_cfg: Config) -> None:
        """Applying the baseline migration must not raise."""
        command.upgrade(alembic_cfg, "001_empty_baseline")

    def test_alembic_version_table_exists_after_001(
        self, _migrated_to_001: Config, tmp_db_url: str
    ) -> None:
        engine = create_db_engine(tmp_db_url)
        try:
            inspector = inspect(engine)
            assert "alembic_version" in inspector.get_table_names()
        finally:
            engine.dispose()

    def test_only_alembic_version_table_exists_after_001(
        self, _migrated_to_001: Config, tmp_db_url: str
    ) -> None:
        """Migration 001 (empty baseline) must create zero user tables."""
        engine = create_db_engine(tmp_db_url)
        try:
            inspector = inspect(engine)
            all_tables = {
                t for t in inspector.get_table_names() if not t.startswith("sqlite_")
            }
            assert all_tables == {"alembic_version"}
        finally:
            engine.dispose()


class TestUpgradeHead:
    def test_upgrade_head_succeeds(self, alembic_cfg: Config) -> None:
        command.upgrade(alembic_cfg, "head")

    def test_alembic_version_table_exists_after_upgrade(
        self, _migrated_db: Config, tmp_db_url: str
    ) -> None:
        engine = create_db_engine(tmp_db_url)
        try:
            inspector = inspect(engine)
            assert "alembic_version" in inspector.get_table_names()
        finally:
            engine.dispose()

    def test_all_ten_core_tables_exist_after_head(
        self, _migrated_db: Config, tmp_db_url: str
    ) -> None:
        """Upgrading to head must create exactly the expected entity tables."""
        engine = create_db_engine(tmp_db_url)
        try:
            inspector = inspect(engine)
            user_tables = {
                t
                for t in inspector.get_table_names()
                if not t.startswith("sqlite_") and t != "alembic_version"
            }
            assert user_tables == _CORE_TABLES
        finally:
            engine.dispose()

    def test_alembic_revision_is_current_head_after_head(
        self, _migrated_db: Config, tmp_db_url: str
    ) -> None:
        from alembic.migration import MigrationContext
        from alembic.script import ScriptDirectory

        head = ScriptDirectory.from_config(_migrated_db).get_current_head()
        engine = create_db_engine(tmp_db_url)
        try:
            with engine.connect() as conn:
                revision = MigrationContext.configure(conn).get_current_revision()
        finally:
            engine.dispose()
        assert revision == head


class TestWalPragma:
    def test_journal_mode_is_wal_after_migration(
        self, _migrated_db: Config, tmp_db_url: str
    ) -> None:
        """create_db_engine must enable WAL on every new connection."""
        engine = create_db_engine(tmp_db_url)
        try:
            with engine.connect() as conn:
                result = conn.execute(text("PRAGMA journal_mode")).scalar()
            assert result == "wal"
        finally:
            engine.dispose()

    def test_journal_mode_is_wal_on_fresh_engine_without_migration(
        self, tmp_db_url: str
    ) -> None:
        """WAL must be enabled by the engine itself, not just by the migration."""
        engine = create_db_engine(tmp_db_url)
        try:
            with engine.connect() as conn:
                conn.execute(
                    text("CREATE TABLE IF NOT EXISTS _probe (id INTEGER PRIMARY KEY)")
                )
                result = conn.execute(text("PRAGMA journal_mode")).scalar()
            assert result == "wal"
        finally:
            engine.dispose()

    def test_foreign_keys_pragma_is_on(
        self, _migrated_db: Config, tmp_db_url: str
    ) -> None:
        """PRAGMA foreign_keys must be 1 (on) for every new connection."""
        engine = create_db_engine(tmp_db_url)
        try:
            with engine.connect() as conn:
                result = conn.execute(text("PRAGMA foreign_keys")).scalar()
            assert result == 1
        finally:
            engine.dispose()


class TestDowngrade:
    def test_downgrade_base_succeeds(self, alembic_cfg: Config) -> None:
        """Downgrading from head back to base must not raise."""
        command.upgrade(alembic_cfg, "head")
        command.downgrade(alembic_cfg, "base")

    def test_alembic_version_table_empty_after_downgrade(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        # Alembic retains the alembic_version tracking table after downgrade
        # to base, but clears all rows from it.
        command.upgrade(alembic_cfg, "head")
        command.downgrade(alembic_cfg, "base")
        engine = create_db_engine(tmp_db_url)
        try:
            with engine.connect() as conn:
                rows = conn.execute(text("SELECT * FROM alembic_version")).fetchall()
            assert rows == [], (
                f"Expected empty alembic_version after downgrade, got: {rows}"
            )
        finally:
            engine.dispose()

    def test_no_core_tables_after_downgrade_to_base(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        """All entity tables created by 002 must be gone after downgrade to base."""
        command.upgrade(alembic_cfg, "head")
        command.downgrade(alembic_cfg, "base")
        engine = create_db_engine(tmp_db_url)
        try:
            inspector = inspect(engine)
            user_tables = {
                t
                for t in inspector.get_table_names()
                if not t.startswith("sqlite_") and t != "alembic_version"
            }
            assert user_tables == set()
        finally:
            engine.dispose()


class TestProjectStreamIdentityColumns:
    """Migration 010 adds the nullable project stream-identity columns."""

    def test_stream_columns_exist_after_head(
        self, _migrated_db: Config, tmp_db_url: str
    ) -> None:
        engine = create_db_engine(tmp_db_url)
        try:
            columns = {c["name"]: c for c in inspect(engine).get_columns("project")}
        finally:
            engine.dispose()
        assert "stream_id" in columns
        assert "stream_label" in columns
        # Both are nullable (additive, no backfill).
        assert columns["stream_id"]["nullable"] is True
        assert columns["stream_label"]["nullable"] is True

    def test_stream_columns_default_to_null_on_insert(
        self, _migrated_db: Config, tmp_db_url: str
    ) -> None:
        """A row inserted without the new columns reads them back as NULL."""
        engine = create_db_engine(tmp_db_url)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO camera (name, address, protocol) "
                        "VALUES ('cam', '192.0.2.10', 'http')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO project (camera_id, name, operational_status, "
                        "lifecycle_state, frame_count) "
                        "VALUES (1, 'proj', 'idle', 'active', 0)"
                    )
                )
                row = conn.execute(
                    text(
                        "SELECT stream_id, stream_label FROM project WHERE name='proj'"
                    )
                ).one()
            assert row.stream_id is None
            assert row.stream_label is None
        finally:
            engine.dispose()

    def test_downgrade_one_revision_drops_stream_columns(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        command.upgrade(alembic_cfg, "head")
        command.downgrade(alembic_cfg, "009_add_camera_default_credentials")
        engine = create_db_engine(tmp_db_url)
        try:
            columns = {c["name"] for c in inspect(engine).get_columns("project")}
        finally:
            engine.dispose()
        assert "stream_id" not in columns
        assert "stream_label" not in columns


class TestFrameSceneMetadataColumns:
    """Migration 011 adds the nullable frame stream-id and scene-metadata cols."""

    def test_frame_columns_exist_after_head(
        self, _migrated_db: Config, tmp_db_url: str
    ) -> None:
        engine = create_db_engine(tmp_db_url)
        try:
            columns = {c["name"]: c for c in inspect(engine).get_columns("frame")}
        finally:
            engine.dispose()
        assert "stream_id" in columns
        assert "scene_metadata" in columns
        # Both are nullable (additive, no backfill).
        assert columns["stream_id"]["nullable"] is True
        assert columns["scene_metadata"]["nullable"] is True

    def test_frame_columns_default_to_null_on_insert(
        self, _migrated_db: Config, tmp_db_url: str
    ) -> None:
        """A frame row inserted without the new columns reads them back as NULL."""
        engine = create_db_engine(tmp_db_url)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO camera (name, address, protocol) "
                        "VALUES ('cam', '192.0.2.10', 'http')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO project (camera_id, name, operational_status, "
                        "lifecycle_state, frame_count) "
                        "VALUES (1, 'proj', 'idle', 'active', 0)"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO frame (project_id, sequence_index, "
                        "capture_status, origin, lifecycle_state) "
                        "VALUES (1, 1, 'captured', 'captured', 'active')"
                    )
                )
                row = conn.execute(
                    text(
                        "SELECT stream_id, scene_metadata FROM frame "
                        "WHERE project_id=1 AND sequence_index=1"
                    )
                ).one()
            assert row.stream_id is None
            assert row.scene_metadata is None
        finally:
            engine.dispose()

    def test_downgrade_one_revision_drops_frame_columns(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        command.upgrade(alembic_cfg, "head")
        command.downgrade(alembic_cfg, "010_add_project_stream_identity")
        engine = create_db_engine(tmp_db_url)
        try:
            columns = {c["name"] for c in inspect(engine).get_columns("frame")}
        finally:
            engine.dispose()
        assert "stream_id" not in columns
        assert "scene_metadata" not in columns


class TestFrameExcludedAtColumn:
    """Migration 015 adds the nullable frame render-exclusion column."""

    def test_excluded_at_column_exists_after_head(
        self, _migrated_db: Config, tmp_db_url: str
    ) -> None:
        engine = create_db_engine(tmp_db_url)
        try:
            columns = {c["name"]: c for c in inspect(engine).get_columns("frame")}
        finally:
            engine.dispose()
        assert "excluded_at" in columns
        # Nullable (additive, no backfill: every existing frame is included).
        assert columns["excluded_at"]["nullable"] is True

    def test_excluded_at_defaults_to_null_on_insert(
        self, _migrated_db: Config, tmp_db_url: str
    ) -> None:
        """A frame inserted without the new column reads it back as NULL."""
        engine = create_db_engine(tmp_db_url)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO camera (name, address, protocol) "
                        "VALUES ('cam', '192.0.2.10', 'http')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO project (camera_id, name, operational_status, "
                        "lifecycle_state, frame_count) "
                        "VALUES (1, 'proj', 'idle', 'active', 0)"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO frame (project_id, sequence_index, "
                        "capture_status, origin, lifecycle_state) "
                        "VALUES (1, 1, 'captured', 'captured', 'active')"
                    )
                )
                row = conn.execute(
                    text(
                        "SELECT excluded_at FROM frame "
                        "WHERE project_id=1 AND sequence_index=1"
                    )
                ).one()
            assert row.excluded_at is None
        finally:
            engine.dispose()

    def test_downgrade_one_revision_drops_excluded_at(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        command.upgrade(alembic_cfg, "head")
        command.downgrade(alembic_cfg, "014_add_camera_device_hostname")
        engine = create_db_engine(tmp_db_url)
        try:
            columns = {c["name"] for c in inspect(engine).get_columns("frame")}
        finally:
            engine.dispose()
        assert "excluded_at" not in columns


class TestRenderJobExportKind:
    """Migration 017 widens the render-job ``kind`` enum to admit ``export``.

    On this SQLite database the portable enum is a plain ``VARCHAR`` with no
    ``CHECK`` constraint, so the widening is a type-metadata sync: the
    behaviour-level assertion is that an ``export`` row now round-trips through the
    migrated schema, and that the downgrade folds it back to ``manual`` so the
    narrower declared type cannot reject existing data.
    """

    def _seed_camera_project(self, conn: object) -> None:
        conn.execute(  # type: ignore[attr-defined]
            text(
                "INSERT INTO camera (name, address, protocol) "
                "VALUES ('cam', '192.0.2.10', 'http')"
            )
        )
        conn.execute(  # type: ignore[attr-defined]
            text(
                "INSERT INTO project (camera_id, name, operational_status, "
                "lifecycle_state, frame_count) "
                "VALUES (1, 'proj', 'idle', 'active', 0)"
            )
        )

    def test_export_kind_round_trips_after_head(
        self, _migrated_db: Config, tmp_db_url: str
    ) -> None:
        """An ``export`` render-job row inserts and reads back at head."""
        engine = create_db_engine(tmp_db_url)
        try:
            with engine.begin() as conn:
                self._seed_camera_project(conn)
                conn.execute(
                    text(
                        "INSERT INTO render_job "
                        "(project_id, encoder_engine, kind, status) "
                        "VALUES (1, 'ffmpeg', 'export', 'pending')"
                    )
                )
                kind = conn.execute(
                    text("SELECT kind FROM render_job WHERE project_id=1")
                ).scalar_one()
            assert kind == "export"
        finally:
            engine.dispose()

    def test_downgrade_folds_export_rows_back_to_manual(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        """Downgrading -1 leaves no ``export`` row (each is folded to ``manual``)."""
        command.upgrade(alembic_cfg, "head")
        engine = create_db_engine(tmp_db_url)
        try:
            with engine.begin() as conn:
                self._seed_camera_project(conn)
                conn.execute(
                    text(
                        "INSERT INTO render_job "
                        "(project_id, encoder_engine, kind, status) "
                        "VALUES (1, 'ffmpeg', 'export', 'done')"
                    )
                )
        finally:
            engine.dispose()

        command.downgrade(alembic_cfg, "016_add_ssrf_settings")

        engine = create_db_engine(tmp_db_url)
        try:
            with engine.begin() as conn:
                kinds = (
                    conn.execute(text("SELECT kind FROM render_job")).scalars().all()
                )
            # The row survives the table-copy (count preserved); its kind folded.
            assert kinds == ["manual"]
        finally:
            engine.dispose()


class TestHermeticity:
    def test_no_db_file_written_to_repo_root(
        self, alembic_cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Migration must not touch the repository-root timelapse.db."""
        monkeypatch.chdir(tmp_path)
        command.upgrade(alembic_cfg, "head")
        assert not (tmp_path / "timelapse.db").exists(), (
            "Migration wrote timelapse.db to the working directory"
        )
