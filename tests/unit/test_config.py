"""Tests for configuration loading and precedence resolution."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import yaml

from timelapse_manager.config.loader import (
    ConfigError,
    load_settings,
    load_settings_with_provenance,
)
from timelapse_manager.config.settings import Settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: object) -> Path:
    path.write_text(yaml.dump(data), encoding="utf-8")
    return path


def _write_json(path: Path, data: object) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Default loading (no file, no env)
# ---------------------------------------------------------------------------


class TestLoadSettingsDefaults:
    def test_load_with_no_arguments_returns_settings_instance(self) -> None:
        s = load_settings()
        assert isinstance(s, Settings)

    def test_default_http_port_is_8080(self) -> None:
        s = load_settings()
        assert s.server.http_port == 8080

    def test_default_logging_level_is_info(self) -> None:
        s = load_settings()
        assert s.logging.level == "INFO"

    def test_default_logging_format_is_json(self) -> None:
        s = load_settings()
        assert s.logging.format == "json"


# ---------------------------------------------------------------------------
# File-layer loading (YAML and JSON)
# ---------------------------------------------------------------------------


class TestLoadSettingsFromYaml:
    def test_yaml_config_file_is_loaded(self, tmp_path: Path) -> None:
        cfg = _write_yaml(tmp_path / "config.yaml", {"server": {"http_port": 9000}})
        s = load_settings(config_path=str(cfg))
        assert s.server.http_port == 9000

    def test_yml_extension_is_also_accepted(self, tmp_path: Path) -> None:
        cfg = _write_yaml(tmp_path / "config.yml", {"server": {"http_port": 9001}})
        s = load_settings(config_path=str(cfg))
        assert s.server.http_port == 9001

    def test_yaml_config_does_not_affect_unset_keys(self, tmp_path: Path) -> None:
        cfg = _write_yaml(tmp_path / "config.yaml", {"server": {"http_port": 9002}})
        s = load_settings(config_path=str(cfg))
        assert s.logging.level == "INFO"  # default remains

    def test_missing_file_raises_config_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            load_settings(config_path=str(tmp_path / "nonexistent.yaml"))

    def test_bad_yaml_raises_config_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("{{not: valid: yaml: content:", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_settings(config_path=str(bad))

    def test_non_mapping_yaml_raises_config_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "list.yaml"
        bad.write_text("- item1\n- item2\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_settings(config_path=str(bad))

    def test_empty_yaml_returns_default_settings(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.yaml"
        empty.write_text("", encoding="utf-8")
        s = load_settings(config_path=str(empty))
        assert s.server.http_port == 8080

    def test_cameras_list_parses_without_error(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "config.yaml",
            {"cameras": [{"name": "cam1", "address": "192.168.1.1"}]},
        )
        s = load_settings(config_path=str(cfg))
        assert len(s.cameras) == 1

    def test_projects_list_parses_without_error(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "config.yaml",
            {"projects": [{"name": "proj1"}]},
        )
        s = load_settings(config_path=str(cfg))
        assert len(s.projects) == 1


class TestLoadSettingsFromJson:
    def test_json_config_file_is_loaded(self, tmp_path: Path) -> None:
        cfg = _write_json(tmp_path / "config.json", {"server": {"http_port": 9100}})
        s = load_settings(config_path=str(cfg))
        assert s.server.http_port == 9100

    def test_bad_json_raises_config_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not json}", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_settings(config_path=str(bad))


class TestLoadSettingsUnsupportedExtension:
    def test_unsupported_extension_raises_config_error(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.toml"
        cfg.write_text("[server]\nhttp_port = 9999\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="Unsupported config file extension"):
            load_settings(config_path=str(cfg))


# ---------------------------------------------------------------------------
# Precedence: file < env < explicit overrides
# ---------------------------------------------------------------------------


class TestLoadSettingsPrecedence:
    def test_file_overrides_defaults(self, tmp_path: Path) -> None:
        cfg = _write_yaml(tmp_path / "config.yaml", {"server": {"http_port": 7777}})
        s = load_settings(config_path=str(cfg))
        assert s.server.http_port == 7777

    def test_env_overrides_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _write_yaml(tmp_path / "config.yaml", {"server": {"http_port": 7777}})
        monkeypatch.setenv("TLM_SERVER__HTTP_PORT", "6666")
        s = load_settings(config_path=str(cfg))
        assert s.server.http_port == 6666

    def test_env_overrides_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TLM_SERVER__HTTP_PORT", "5555")
        s = load_settings()
        assert s.server.http_port == 5555

    def test_explicit_override_beats_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TLM_SERVER__HTTP_PORT", "5555")
        s = load_settings(server={"http_port": 4444})
        assert s.server.http_port == 4444

    def test_tlm_config_env_var_selects_config_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _write_yaml(tmp_path / "config.yaml", {"server": {"http_port": 3333}})
        monkeypatch.setenv("TLM_CONFIG", str(cfg))
        s = load_settings()
        assert s.server.http_port == 3333


# ---------------------------------------------------------------------------
# Optional vs mandatory config file: env/default path is optional, an explicit
# --config path is mandatory.
# ---------------------------------------------------------------------------


class TestLoadSettingsMissingFile:
    @pytest.fixture(autouse=True)
    def _reenable_loader_logger(self) -> None:
        # A prior test that ran Alembic migrations triggers
        # ``logging.config.fileConfig(disable_existing_loggers=True)`` in
        # ``alembic/env.py``, which sets ``disabled=True`` on the already-imported
        # loader logger. A disabled logger drops records before caplog (or the
        # runtime stderr fallback) ever sees them, so re-enable it for each test
        # here. In the real entry points ``load_settings`` runs before migrations,
        # so this hazard does not occur in production.
        logging.getLogger("timelapse_manager.config.loader").disabled = False

    def test_missing_env_path_uses_defaults_without_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing = tmp_path / "nonexistent.yml"
        monkeypatch.setenv("TLM_CONFIG", str(missing))
        s = load_settings()
        assert s.server.http_port == 8080  # built-in default

    def test_missing_env_path_emits_warning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        missing = tmp_path / "nonexistent.yml"
        monkeypatch.setenv("TLM_CONFIG", str(missing))
        with caplog.at_level("WARNING"):
            load_settings()
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warnings, "expected a warning for the missing env-pointed config"
        assert str(missing) in warnings[0].getMessage()

    def test_missing_env_path_still_applies_env_overrides(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing = tmp_path / "nonexistent.yml"
        monkeypatch.setenv("TLM_CONFIG", str(missing))
        monkeypatch.setenv("TLM_SERVER__HTTP_PORT", "6543")
        s = load_settings()
        assert s.server.http_port == 6543

    def test_explicit_missing_path_raises_config_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            load_settings(config_path=str(tmp_path / "nonexistent.yaml"))

    def test_explicit_missing_path_does_not_warn_then_default(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # An explicit missing path is a hard error, not a warn-and-continue.
        with caplog.at_level("WARNING"), pytest.raises(ConfigError):
            load_settings(config_path=str(tmp_path / "nonexistent.yaml"))
        assert not [r for r in caplog.records if r.levelname == "WARNING"]

    def test_existing_env_path_is_loaded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _write_yaml(tmp_path / "config.yaml", {"server": {"http_port": 3210}})
        monkeypatch.setenv("TLM_CONFIG", str(cfg))
        s = load_settings()
        assert s.server.http_port == 3210

    def test_no_config_at_all_uses_defaults_without_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.delenv("TLM_CONFIG", raising=False)
        with caplog.at_level("WARNING"):
            s = load_settings()
        assert s.server.http_port == 8080
        assert not [r for r in caplog.records if r.levelname == "WARNING"]


# ---------------------------------------------------------------------------
# Fail-fast validation: bad values name the field path
# ---------------------------------------------------------------------------


class TestLoadSettingsValidation:
    def test_bad_port_value_raises_config_error(self) -> None:
        with pytest.raises(ConfigError):
            load_settings(server={"http_port": "not-a-number"})

    def test_bad_log_level_enum_raises_config_error(self) -> None:
        with pytest.raises(ConfigError):
            load_settings(logging={"level": "VERBOSE"})

    def test_bad_log_format_enum_raises_config_error(self) -> None:
        with pytest.raises(ConfigError):
            load_settings(logging={"format": "xml"})

    def test_config_error_names_a_field_path(self) -> None:
        with pytest.raises(ConfigError) as exc_info:
            load_settings(logging={"level": "VERBOSE"})
        assert "logging" in str(exc_info.value) or "level" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Operator-friendly list settings: a list-typed value set via an environment
# variable accepts a delimited string, not only a JSON array. Without this a
# bare ``TLM_SSRF__ALLOWED_PRIVATE_SUBNETS=10.0.0.0/24`` crashes startup with an
# opaque parse error.
# ---------------------------------------------------------------------------


class TestListSettingFromEnv:
    _ENV = "TLM_SSRF__ALLOWED_PRIVATE_SUBNETS"

    def test_single_value_string_becomes_one_element_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(self._ENV, "192.168.10.0/24")
        s = load_settings()
        assert s.ssrf.allowed_private_subnets == ["192.168.10.0/24"]

    def test_comma_separated_string_is_split(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(self._ENV, "a, b")
        s = load_settings()
        assert s.ssrf.allowed_private_subnets == ["a", "b"]

    def test_whitespace_separated_string_is_split(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(self._ENV, "10.0.0.0/24   192.168.0.0/16")
        s = load_settings()
        assert s.ssrf.allowed_private_subnets == ["10.0.0.0/24", "192.168.0.0/16"]

    def test_empty_fragments_are_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(self._ENV, "a, , b,")
        s = load_settings()
        assert s.ssrf.allowed_private_subnets == ["a", "b"]

    def test_json_array_string_still_works(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(self._ENV, '["192.168.10.0/24", "192.168.1.0/24"]')
        s = load_settings()
        assert s.ssrf.allowed_private_subnets == ["192.168.10.0/24", "192.168.1.0/24"]

    def test_actual_list_passed_in_code_is_unchanged(self) -> None:
        s = load_settings(ssrf={"allowed_private_subnets": ["a", "b"]})
        assert s.ssrf.allowed_private_subnets == ["a", "b"]

    def test_default_is_empty_list(self) -> None:
        s = load_settings()
        assert s.ssrf.allowed_private_subnets == []


# ---------------------------------------------------------------------------
# Environment provenance: which leaf values the environment actually determined.
# The accuracy contract is "never report a value the environment did not set",
# so the override case (env masked by a higher-precedence value) must resolve to
# NOT-env-effective.
# ---------------------------------------------------------------------------


class TestEnvProvenance:
    def test_value_settable_via_loader_round_trips(self) -> None:
        # The returned settings are identical to a plain load_settings().
        settings, _ = load_settings_with_provenance()
        assert isinstance(settings, Settings)

    def test_env_sourced_leaf_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TLM_SERVER__HTTP_PORT", "9123")
        settings, env_sourced = load_settings_with_provenance()
        assert settings.server.http_port == 9123
        assert "server.http_port" in env_sourced

    def test_value_not_in_env_is_not_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Clear the specific key so no ambient TLM_* pollutes the result.
        monkeypatch.delenv("TLM_SERVER__HTTP_PORT", raising=False)
        _, env_sourced = load_settings_with_provenance()
        assert "server.http_port" not in env_sourced

    def test_explicit_override_beats_env_is_not_env_effective(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Env sets the port, but a higher-precedence init/CLI override wins; the
        # effective source is therefore the override, not the environment.
        monkeypatch.setenv("TLM_SERVER__HTTP_PORT", "9123")
        settings, env_sourced = load_settings_with_provenance(
            server={"http_port": 7000}
        )
        assert settings.server.http_port == 7000
        assert "server.http_port" not in env_sourced

    def test_nested_section_leaf_uses_dotted_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TLM_SESSION__IDLE_TIMEOUT_SECONDS", "1200")
        settings, env_sourced = load_settings_with_provenance()
        assert settings.session.idle_timeout_seconds == 1200
        assert "session.idle_timeout_seconds" in env_sourced
        # A sibling leaf the environment did not set is not reported.
        assert "session.absolute_timeout_seconds" not in env_sourced
