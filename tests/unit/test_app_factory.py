"""Tests for the create_app() application factory."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI

import timelapse_manager
from timelapse_manager.app import create_app, create_app_from_env
from timelapse_manager.config.settings import Settings


class TestCreateAppReturnType:
    def test_returns_fastapi_instance(self, settings: Settings) -> None:
        app = create_app(settings)
        assert isinstance(app, FastAPI)

    def test_app_title_is_set(self, settings: Settings) -> None:
        app = create_app(settings)
        assert app.title == "Timelapse Manager"

    def test_app_version_matches_package_version(self, settings: Settings) -> None:
        app = create_app(settings)
        assert app.version == timelapse_manager.__version__


class TestCreateAppIdempotency:
    def test_two_calls_return_distinct_objects(self, settings: Settings) -> None:
        app_a = create_app(settings)
        app_b = create_app(settings)
        assert app_a is not app_b

    def test_two_apps_have_independent_route_tables(self, settings: Settings) -> None:
        app_a = create_app(settings)
        app_b = create_app(settings)
        # Mutating one app's routes must not affect the other.
        original_count = len(list(app_b.routes))
        app_a.routes.clear()
        assert len(list(app_b.routes)) == original_count

    def test_healthz_route_registered_on_each_instance(
        self, settings: Settings
    ) -> None:
        for _ in range(2):
            app = create_app(settings)
            paths = [getattr(r, "path", None) for r in app.routes]
            assert "/healthz" in paths


class TestCreateAppSideEffects:
    def test_no_db_file_created_when_app_constructed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Constructing the app must not create or touch the SQLite database."""
        from timelapse_manager.config.settings import (
            DatabaseSettings,
            LoggingSettings,
            PathsSettings,
        )

        monkeypatch.chdir(tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        s = Settings(
            database=DatabaseSettings(url=f"sqlite:///{tmp_path / 'test.db'}"),
            logging=LoggingSettings(level="WARNING", format="text"),
            paths=PathsSettings(
                data_dir=data_dir,
                frames_root=data_dir / "frames",
                token_file=data_dir / ".local-token",
            ),
        )
        create_app(s)
        db_files = list(tmp_path.glob("*.db"))
        assert db_files == [], f"Unexpected DB files created: {db_files}"

    def test_importing_app_module_has_no_db_side_effects(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Calling create_app must not write files to the working directory."""
        from timelapse_manager.config.settings import (
            DatabaseSettings,
            LoggingSettings,
            PathsSettings,
        )

        monkeypatch.chdir(tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        s = Settings(
            database=DatabaseSettings(url=f"sqlite:///{tmp_path / 'test.db'}"),
            logging=LoggingSettings(level="WARNING", format="text"),
            paths=PathsSettings(
                data_dir=data_dir,
                frames_root=data_dir / "frames",
                token_file=data_dir / ".local-token",
            ),
        )
        create_app(s)
        # The only file allowed is what was already in data_dir; the cwd (tmp_path)
        # itself should be empty (no db files, no token files, no log files).
        created = [p for p in tmp_path.iterdir() if p.name != "data"]
        assert created == [], f"Unexpected files created in cwd: {created}"


class TestCreateAppFromEnv:
    """The zero-argument factory used by ``uvicorn --factory`` in the container.

    It resolves Settings from the environment, configures logging, and builds the
    app -- without binding a socket, generating certs, or running the lifespan.
    """

    def test_builds_app_from_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        # No explicit config file: rely on environment + built-in defaults, which
        # requires load_settings() to treat a missing/unset TLM_CONFIG as optional.
        monkeypatch.delenv("TLM_CONFIG", raising=False)
        monkeypatch.setenv("TLM_PATHS__DATA_DIR", str(data_dir))
        monkeypatch.setenv("TLM_DATABASE__URL", f"sqlite:///{tmp_path / 'env.db'}")

        app = create_app_from_env()

        assert isinstance(app, FastAPI)
        assert app.title == "Timelapse Manager"
        paths = [getattr(r, "path", None) for r in app.routes]
        assert "/healthz" in paths

    def test_resolves_settings_from_environment_into_app_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The env-derived settings reach the app, not just any defaults."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        db_url = f"sqlite:///{tmp_path / 'env.db'}"
        monkeypatch.delenv("TLM_CONFIG", raising=False)
        monkeypatch.setenv("TLM_PATHS__DATA_DIR", str(data_dir))
        monkeypatch.setenv("TLM_DATABASE__URL", db_url)

        app = create_app_from_env()

        assert app.state.settings.database.url == db_url
        assert app.state.settings.paths.data_dir == data_dir

    def test_constructing_does_not_create_database(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Constructing via the env factory must not touch the SQLite database.

        The lifespan -- not the factory -- creates the engine, so no ``.db`` file
        should appear from a bare construction (no socket is bound either).
        """
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("TLM_CONFIG", raising=False)
        monkeypatch.setenv("TLM_PATHS__DATA_DIR", str(data_dir))
        monkeypatch.setenv("TLM_DATABASE__URL", f"sqlite:///{tmp_path / 'env.db'}")

        create_app_from_env()

        assert list(tmp_path.glob("*.db")) == []
