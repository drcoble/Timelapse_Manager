"""CLI config-file discovery (#103).

Precedence, highest first: ``--config`` flag, ``TLM_CONFIG``, the system file,
then the per-user file. Tests run ``main`` in-process against tmp paths; the real
``/etc`` and ``~/.config`` are never read.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

import timelapse_manager.cli as cli_module
from tests.unit.test_cli import _run_cli


def _cfg(path: Path, port: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump({"server": {"http_port": port}}), encoding="utf-8")
    return path


@pytest.fixture
def locations(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    system = tmp_path / "etc" / "config.yml"
    xdg = tmp_path / "xdg"
    monkeypatch.setattr(cli_module, "_SYSTEM_CONFIG_PATH", system)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.delenv("TLM_CONFIG", raising=False)
    monkeypatch.delenv("TLM_SERVER__HTTP_PORT", raising=False)
    return {
        "system": system,
        "user": xdg / "timelapse-manager" / "config.yml",
        "flag": tmp_path / "flag.yml",
        "env": tmp_path / "env.yml",
    }


def _port() -> int:
    code, out, err = _run_cli("config", "show", "--json")
    assert code == 0, err
    return int(json.loads(out)["server"]["http_port"])


class TestCliConfigDiscovery:
    def test_system_file_found_without_tlm_config(
        self, locations: dict[str, Path]
    ) -> None:
        _cfg(locations["system"], 7001)
        assert _port() == 7001

    def test_user_file_found_when_no_system_file(
        self, locations: dict[str, Path]
    ) -> None:
        _cfg(locations["user"], 7002)
        assert _port() == 7002

    def test_system_beats_user(self, locations: dict[str, Path]) -> None:
        _cfg(locations["system"], 7001)
        _cfg(locations["user"], 7002)
        assert _port() == 7001

    def test_env_beats_system(
        self, locations: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _cfg(locations["system"], 7001)
        _cfg(locations["env"], 7003)
        monkeypatch.setenv("TLM_CONFIG", str(locations["env"]))
        assert _port() == 7003

    def test_flag_beats_env_and_system(
        self, locations: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _cfg(locations["system"], 7001)
        _cfg(locations["env"], 7003)
        _cfg(locations["flag"], 7004)
        monkeypatch.setenv("TLM_CONFIG", str(locations["env"]))
        code, out, _ = _run_cli(
            "--config", str(locations["flag"]), "config", "show", "--json"
        )
        assert code == 0
        assert json.loads(out)["server"]["http_port"] == 7004

    def test_env_set_to_missing_file_does_not_fall_through(
        self, locations: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TLM_CONFIG wins even when its file is missing (loader warns, defaults)."""
        _cfg(locations["system"], 7001)
        monkeypatch.setenv("TLM_CONFIG", str(locations["env"]))
        assert _port() != 7001

    def test_nothing_found_uses_defaults(self, locations: dict[str, Path]) -> None:
        assert cli_module._resolve_cli_config_path(None) is None
        assert _port() == 8080

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can read any file")
    def test_unreadable_system_file_falls_through_to_user(
        self, locations: dict[str, Path]
    ) -> None:
        _cfg(locations["system"], 7001).chmod(0o000)
        _cfg(locations["user"], 7002)
        assert _port() == 7002

    def test_relative_xdg_config_home_is_ignored(
        self,
        locations: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """XDG says a relative value is invalid; never read from the cwd."""
        monkeypatch.setenv("XDG_CONFIG_HOME", "relative-dir")
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        _cfg(tmp_path / "home" / ".config" / "timelapse-manager" / "config.yml", 7005)
        assert _port() == 7005


class TestMissingTokenNamesTheConfig:
    """#103: a missing token says which config decided the state directory."""

    def test_no_config_found_names_the_ways_to_supply_one(
        self,
        locations: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("TLM_PATHS__DATA_DIR", str(tmp_path / "state"))
        code, _, err = _run_cli("system", "info")
        assert code == 2
        assert "token file" in err and "not found" in err
        assert "no config file was found" in err
        assert "TLM_CONFIG" in err and "--config" in err

    def test_config_in_use_is_named(
        self,
        locations: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("TLM_PATHS__DATA_DIR", str(tmp_path / "state"))
        _cfg(locations["system"], 7001)
        code, _, err = _run_cli("system", "info")
        assert code == 2
        assert f"config file in use: {locations['system']}" in err


class TestPackageShippedConfig:
    """The .deb/.rpm conffile gives an admin's shell the service's data dir (#338)."""

    def test_cli_resolves_the_data_dir_from_the_shipped_config(
        self, locations: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shipped = (
            Path(cli_module.__file__).resolve().parents[3] / "packaging" / "config.yml"
        )
        locations["system"].parent.mkdir(parents=True, exist_ok=True)
        locations["system"].write_text(
            shipped.read_text(encoding="utf-8"), encoding="utf-8"
        )
        for name in ("TLM_PATHS__DATA_DIR", "TLM_DATABASE__URL"):
            monkeypatch.delenv(name, raising=False)

        code, out, err = _run_cli("config", "show", "--json")

        assert code == 0, err
        shown = json.loads(out)
        assert shown["paths"]["data_dir"] == "/var/lib/timelapse-manager"
        assert (
            shown["database"]["url"]
            == "sqlite:////var/lib/timelapse-manager/timelapse.db"
        )

    def test_token_path_is_under_the_service_data_dir(
        self, locations: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from timelapse_manager.config import load_settings
        from timelapse_manager.security.token import local_token_path

        shipped = (
            Path(cli_module.__file__).resolve().parents[3] / "packaging" / "config.yml"
        )
        monkeypatch.delenv("TLM_PATHS__DATA_DIR", raising=False)
        settings = load_settings(str(shipped))
        assert local_token_path(settings) == Path(
            "/var/lib/timelapse-manager/.local-token"
        )
