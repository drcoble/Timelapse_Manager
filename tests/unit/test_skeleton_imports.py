"""Verify that the package skeleton is importable and well-formed.

All top-level and subpackage imports must succeed without error, confirming
that the package structure is sound even before any implementation is added.
"""

from __future__ import annotations

import importlib

import pytest

import timelapse_manager
import timelapse_manager.app
from timelapse_manager.app import create_app

# Every submodule that must be importable as part of the package skeleton.
_SUBMODULES = [
    "timelapse_manager.api",
    "timelapse_manager.cameras",
    "timelapse_manager.capture",
    "timelapse_manager.cli",
    "timelapse_manager.config",
    "timelapse_manager.db",
    "timelapse_manager.encode",
    "timelapse_manager.security",
    "timelapse_manager.service",
    "timelapse_manager.storage",
    "timelapse_manager.web",
]


class TestPackageVersion:
    def test_version_attribute_exists(self) -> None:
        assert hasattr(timelapse_manager, "__version__")

    def test_version_is_a_string(self) -> None:
        assert isinstance(timelapse_manager.__version__, str)

    def test_version_is_non_empty(self) -> None:
        assert timelapse_manager.__version__.strip() != ""


class TestTopLevelImports:
    def test_timelapse_manager_package_importable(self) -> None:
        importlib.import_module("timelapse_manager")

    def test_app_module_importable(self) -> None:
        importlib.import_module("timelapse_manager.app")

    def test_create_app_is_callable(self) -> None:
        assert callable(create_app)


@pytest.mark.parametrize("module_name", _SUBMODULES)
def test_submodule_importable(module_name: str) -> None:
    """Each skeleton subpackage must import without raising an exception."""
    importlib.import_module(module_name)
