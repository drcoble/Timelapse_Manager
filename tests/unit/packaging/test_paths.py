"""Tests for the frozen-aware path helpers in paths.py.

Covers:
- is_frozen() returns False in the test environment.
- default_state_dir() is absolute and not cwd-relative.
- default_database_url() uses sqlite:/// and is not cwd-relative.
- Settings env-override: TLM_PATHS__DATA_DIR and TLM_DATABASE__URL are honoured.
- alembic_config_path() resolves to a real file regardless of cwd.
- alembic_script_location() resolves to a real directory regardless of cwd.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from timelapse_manager.paths import (
    alembic_config_path,
    alembic_script_location,
    bundle_root,
    default_database_url,
    default_state_dir,
    is_frozen,
    resource_path,
)

# ---------------------------------------------------------------------------
# is_frozen
# ---------------------------------------------------------------------------


class TestIsFrozen:
    def test_returns_false_in_test_environment(self) -> None:
        """Tests must not run from a frozen bundle."""
        assert not is_frozen()

    def test_returns_true_when_meipass_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        assert is_frozen()

    def test_returns_true_when_frozen_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        assert is_frozen()

    def test_returns_false_when_frozen_is_falsy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        assert not is_frozen()


# ---------------------------------------------------------------------------
# bundle_root / resource_path
# ---------------------------------------------------------------------------


class TestBundleRoot:
    def test_returns_path_in_dev(self) -> None:
        root = bundle_root()
        assert isinstance(root, Path)
        assert root.is_absolute()

    def test_returns_meipass_when_frozen(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        assert bundle_root() == tmp_path

    def test_resource_path_joins_parts(self) -> None:
        result = resource_path("templates", "index.html")
        root = bundle_root()
        assert result == root / "templates" / "index.html"


# ---------------------------------------------------------------------------
# default_state_dir
# ---------------------------------------------------------------------------


class TestDefaultStateDir:
    def test_returns_absolute_path(self) -> None:
        assert default_state_dir().is_absolute()

    def test_is_not_cwd_relative(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Changing cwd must not change the result."""
        monkeypatch.chdir(tmp_path)
        state_dir = default_state_dir()
        # Result must not be a descendant of tmp_path (which is our fake cwd).
        assert not str(state_dir).startswith(str(tmp_path))

    def test_ends_with_app_dir_name(self) -> None:
        assert default_state_dir().name == "timelapse-manager"

    def test_os_appropriate_parent(self) -> None:
        state_dir = default_state_dir()
        if sys.platform == "darwin":
            assert "Library" in str(state_dir) and "Application Support" in str(
                state_dir
            )
        elif sys.platform.startswith("win"):
            assert "timelapse-manager" in str(state_dir)
        else:
            # Linux/POSIX: under XDG_DATA_HOME or ~/.local/share
            assert ".local" in str(state_dir) or "XDG" in os.environ.get(
                "XDG_DATA_HOME", ""
            )


# ---------------------------------------------------------------------------
# default_database_url
# ---------------------------------------------------------------------------


class TestDefaultDatabaseUrl:
    def test_starts_with_sqlite_scheme(self) -> None:
        url = default_database_url()
        assert url.startswith("sqlite:///")

    def test_path_is_absolute(self) -> None:
        url = default_database_url()
        # sqlite:/// + absolute path = sqlite:////abs/path on POSIX
        db_path = url[len("sqlite:///") :]
        assert Path(db_path).is_absolute()

    def test_is_not_cwd_relative(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        url = default_database_url()
        db_path = url[len("sqlite:///") :]
        assert not str(db_path).startswith(str(tmp_path))

    def test_db_file_named_timelapse_db(self) -> None:
        url = default_database_url()
        assert url.endswith("timelapse.db")


# ---------------------------------------------------------------------------
# Settings env overrides
# ---------------------------------------------------------------------------


class TestSettingsEnvOverrides:
    """Environment variables override the defaults at Settings construction time."""

    def test_tlm_paths_data_dir_overrides_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        custom = tmp_path / "custom_data"
        monkeypatch.setenv("TLM_PATHS__DATA_DIR", str(custom))
        from timelapse_manager.config.settings import Settings

        s = Settings()
        assert s.paths.data_dir == custom

    def test_tlm_database_url_overrides_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        custom_url = f"sqlite:///{tmp_path}/override.db"
        monkeypatch.setenv("TLM_DATABASE__URL", custom_url)
        from timelapse_manager.config.settings import Settings

        s = Settings()
        assert s.database.url == custom_url

    def test_tlm_paths_data_dir_not_inherited_across_instances(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Each Settings() construction reads env fresh (no module-level caching)."""
        custom = tmp_path / "per_test"
        monkeypatch.setenv("TLM_PATHS__DATA_DIR", str(custom))
        from timelapse_manager.config.settings import Settings

        s1 = Settings()
        s2 = Settings()
        assert s1.paths.data_dir == s2.paths.data_dir == custom


# ---------------------------------------------------------------------------
# alembic_config_path / alembic_script_location
# ---------------------------------------------------------------------------


class TestAlembicPaths:
    def test_alembic_config_path_points_to_real_file(self) -> None:
        path = alembic_config_path()
        assert path.is_file(), f"alembic.ini not found at {path}"

    def test_alembic_config_path_is_ini_file(self) -> None:
        path = alembic_config_path()
        assert path.suffix == ".ini"

    def test_alembic_config_path_cwd_independent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        path = alembic_config_path()
        assert path.is_file(), f"alembic.ini not found when cwd={tmp_path}"

    def test_alembic_script_location_points_to_real_dir(self) -> None:
        path = alembic_script_location()
        assert path.is_dir(), f"alembic migrations dir not found at {path}"

    def test_alembic_script_location_contains_versions_subdir(self) -> None:
        path = alembic_script_location()
        assert (path / "versions").is_dir(), f"No versions/ subdir in {path}"

    def test_alembic_script_location_cwd_independent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        path = alembic_script_location()
        assert path.is_dir(), f"alembic migrations dir not found when cwd={tmp_path}"
