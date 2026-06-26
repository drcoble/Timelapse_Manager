"""Configuration loading and precedence resolution.

:func:`load_settings` composes the final :class:`Settings` from four layers,
lowest to highest precedence:

1. built-in defaults,
2. an optional config file (YAML or JSON, auto-detected by extension),
3. environment variables (``TLM_`` prefix, ``__`` nesting),
4. explicit keyword overrides (typically from the CLI).

Invalid input fails fast with a :class:`ConfigError` that names the offending
field path, so a bad config is reported clearly rather than silently ignored.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from .settings import Settings

# Environment variable naming the default config file path when no explicit
# path is passed to load_settings().
_CONFIG_PATH_ENV = "TLM_CONFIG"

_YAML_SUFFIXES = {".yaml", ".yml"}
_JSON_SUFFIXES = {".json"}

_logger = logging.getLogger(__name__)


class ConfigError(ValueError):
    """Raised when configuration cannot be loaded or fails validation."""


class _FileSettingsSource(PydanticBaseSettingsSource):
    """Settings source that emits a pre-parsed config-file dictionary.

    The file is read and parsed once by :func:`load_settings`; this source just
    hands the resulting mapping to pydantic-settings at its assigned precedence
    (below environment variables).
    """

    def __init__(self, settings_cls: type[BaseSettings], data: dict[str, Any]) -> None:
        super().__init__(settings_cls)
        self._data = data

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        # The whole mapping is returned by __call__, so per-field lookup is
        # never exercised; satisfy the abstract interface explicitly.
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return self._data


def _resolve_config_path(config_path: str | None) -> Path | None:
    """Determine which config file to read, if any.

    An explicit ``config_path`` wins; otherwise the ``TLM_CONFIG`` environment
    variable is consulted. Returns ``None`` when neither is set.
    """
    candidate = (
        config_path if config_path is not None else os.environ.get(_CONFIG_PATH_ENV)
    )
    if not candidate:
        return None
    return Path(candidate)


def _read_config_file(path: Path) -> dict[str, Any]:
    """Read and parse a YAML or JSON config file into a mapping.

    Raises :class:`ConfigError` if the file is missing, unparseable, or does not
    contain a top-level mapping.
    """
    suffix = path.suffix.lower()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read config file '{path}': {exc}") from exc

    try:
        if suffix in _YAML_SUFFIXES:
            parsed = yaml.safe_load(text)
        elif suffix in _JSON_SUFFIXES:
            parsed = json.loads(text)
        else:
            raise ConfigError(
                f"Unsupported config file extension '{suffix}' for '{path}'; "
                "use .yaml, .yml, or .json."
            )
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot parse config file '{path}': {exc}") from exc

    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ConfigError(
            f"Config file '{path}' must contain a top-level mapping, "
            f"got {type(parsed).__name__}."
        )
    return parsed


def _resolve_file_data(config_path: str | None) -> dict[str, Any]:
    """Resolve the config-file layer's data, applying the missing-file policy.

    A missing file is treated differently depending on how its path was chosen.
    When the path came from the explicit ``config_path`` argument the user asked
    for that specific file, so a missing file is a hard :class:`ConfigError`.
    When the path came from the ``TLM_CONFIG`` environment variable (the packaged
    deployment's optional config) a missing file is not fatal: a warning is logged
    and loading proceeds with defaults plus any ``TLM_*`` environment overrides.
    A file that exists but is unreadable or malformed always raises, regardless of
    how its path was chosen.
    """
    explicit = config_path is not None
    resolved_path = _resolve_config_path(config_path)
    if resolved_path is None:
        # No path from either source: defaults plus env/overrides, no file layer.
        return {}
    if resolved_path.exists():
        # Present file: parse it; malformed/unreadable still raises either way.
        return _read_config_file(resolved_path)
    if explicit:
        # User named this file explicitly, so a missing one is an error. Reusing
        # _read_config_file reproduces the exact "Cannot read config file" message.
        return _read_config_file(resolved_path)
    # Path came from TLM_CONFIG (or a built-in default): the packaged deployment
    # points here optimistically, so a missing file is non-fatal.
    _logger.warning(
        "Config file '%s' (from %s) does not exist; "
        "continuing with built-in defaults and any TLM_* environment "
        "overrides.",
        resolved_path,
        _CONFIG_PATH_ENV,
    )
    return {}


def _build_settings(
    file_data: dict[str, Any], *, include_env: bool, overrides: dict[str, Any]
) -> Settings:
    """Compose a :class:`Settings` from the chosen layers.

    ``include_env`` toggles whether the environment-variable source participates.
    Suppressing it (loading with init + file + defaults only) is how provenance is
    computed: the result is diffed against the env-inclusive load to find the leaf
    values the environment actually determined. Raises :class:`ConfigError` with
    the offending field path on a validation failure, matching ``load_settings``.
    """

    class _LoadedSettings(Settings):
        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            # Order is precedence, highest first: overrides, env, then file.
            sources: tuple[PydanticBaseSettingsSource, ...] = (init_settings,)
            if include_env:
                sources += (env_settings,)
            sources += (_FileSettingsSource(settings_cls, file_data),)
            return sources

    try:
        return _LoadedSettings(**overrides)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first.get("loc", ())) or "<root>"
        raise ConfigError(
            f"Invalid configuration at '{location}': {first.get('msg', exc)}"
        ) from exc


def _leaf_paths(value: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten a settings tree into ``{dotted.path: leaf_value}`` pairs.

    Nested settings sections (pydantic models) are descended into; every
    non-model field becomes one dotted leaf. Lists and other scalars are treated
    as opaque leaves and compared by value. Used by the provenance diff so two
    loads can be compared key-by-key in a single flat space.
    """
    paths: dict[str, Any] = {}
    fields = getattr(type(value), "model_fields", None)
    if fields is None:
        paths[prefix] = value
        return paths
    for name in fields:
        child = getattr(value, name)
        dotted = f"{prefix}.{name}" if prefix else name
        if getattr(type(child), "model_fields", None) is not None:
            paths.update(_leaf_paths(child, dotted))
        else:
            paths[dotted] = child
    return paths


def load_settings(config_path: str | None = None, **overrides: Any) -> Settings:
    """Load application settings with layered precedence.

    Precedence (highest first): keyword ``overrides`` > environment variables >
    config file > built-in defaults.

    :param config_path: explicit path to a YAML/JSON config file. When omitted
        (``None``), the ``TLM_CONFIG`` environment variable is consulted; if that
        is also unset, no file layer is applied.
    :param overrides: explicit top-level setting values (typically from the CLI)
        that take the highest precedence.
    :raises ConfigError: if a config file is present but cannot be read/parsed,
        if an *explicitly* requested file is missing, or if the resolved settings
        fail validation; the message names the bad field path.
    """
    return _build_settings(
        _resolve_file_data(config_path), include_env=True, overrides=overrides
    )


def load_settings_with_provenance(
    config_path: str | None = None, **overrides: Any
) -> tuple[Settings, frozenset[str]]:
    """Load settings and report which leaf values the environment determined.

    Returns the resolved :class:`Settings` (identical to :func:`load_settings`)
    paired with a ``frozenset`` of dotted leaf paths (e.g. ``server.http_port``)
    whose *effective* value came from an environment variable.

    "Effective source is the environment" means: the value is present in the env
    layer **and** is not overridden by a higher-precedence init/CLI value. The set
    is computed by a diff that cannot produce a false positive: settings are
    loaded once normally and once with the environment source suppressed, and the
    leaf paths whose values differ are exactly those the environment determined.

    * A higher-precedence init/CLI override masks the env value, so both loads
      agree on that leaf and it is correctly **not** reported (override wins).
    * A file- or default-sourced value is identical in both loads and is **not**
      reported.
    * The only case the diff cannot see is an env value that equals the default
      (no observable difference); such a leaf is conservatively **omitted** rather
      than risk falsely marking a field as environment-controlled.
    """
    file_data = _resolve_file_data(config_path)
    with_env = _build_settings(file_data, include_env=True, overrides=overrides)
    without_env = _build_settings(file_data, include_env=False, overrides=overrides)

    env_leaves = _leaf_paths(with_env)
    base_leaves = _leaf_paths(without_env)
    env_sourced = frozenset(
        path
        for path, env_value in env_leaves.items()
        if path not in base_leaves or base_leaves[path] != env_value
    )
    return with_env, env_sourced
