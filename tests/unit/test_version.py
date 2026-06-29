"""Tests for component version probing."""

from __future__ import annotations

import subprocess
import sys
import types

import pytest

import timelapse_manager
from timelapse_manager.version import (
    get_app_version,
    get_build_info,
    probe_ffmpeg_version,
)


class TestGetAppVersion:
    def test_returns_string(self) -> None:
        assert isinstance(get_app_version(), str)

    def test_returns_non_empty_string(self) -> None:
        assert get_app_version().strip() != ""

    def test_matches_package_version(self) -> None:
        assert get_app_version() == timelapse_manager.__version__


class TestGetBuildInfo:
    _MODULE = "timelapse_manager._build_info"

    def test_returns_unknown_fallback_when_module_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force the generated module to be unimportable even if a local
        # `make release` left one on disk: the None sentinel in sys.modules
        # makes import_module raise ImportError deterministically.
        monkeypatch.setitem(sys.modules, self._MODULE, None)
        assert get_build_info() == {"sha": "unknown", "date": "unknown"}

    def test_returns_real_values_when_module_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = types.ModuleType(self._MODULE)
        fake.BUILD_SHA = "abc1234"  # type: ignore[attr-defined]
        fake.BUILD_DATE = "2026-06-17"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, self._MODULE, fake)
        assert get_build_info() == {"sha": "abc1234", "date": "2026-06-17"}

    def test_missing_attributes_fall_back_to_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A generated module that somehow lacks one field still degrades safely.
        fake = types.ModuleType(self._MODULE)
        fake.BUILD_SHA = "deadbeef"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, self._MODULE, fake)
        assert get_build_info() == {"sha": "deadbeef", "date": "unknown"}

    def test_returns_string_values(self) -> None:
        info = get_build_info()
        assert isinstance(info["sha"], str)
        assert isinstance(info["date"], str)


class TestProbeFfmpegVersion:
    def test_returns_string(self) -> None:
        result = probe_ffmpeg_version()
        assert isinstance(result, str)

    def test_returns_non_empty_string(self) -> None:
        result = probe_ffmpeg_version()
        assert result.strip() != ""

    def test_returns_unavailable_when_binary_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "timelapse_manager.version.subprocess.run",
            lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
        )
        assert probe_ffmpeg_version() == "unavailable"

    def test_returns_unavailable_on_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "timelapse_manager.version.subprocess.run",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(cmd="ffmpeg", timeout=5)
            ),
        )
        assert probe_ffmpeg_version() == "unavailable"

    def test_returns_unavailable_on_nonzero_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        completed = subprocess.CompletedProcess(
            args=["ffmpeg", "-version"], returncode=1, stdout="", stderr=""
        )
        monkeypatch.setattr(
            "timelapse_manager.version.subprocess.run",
            lambda *args, **kwargs: completed,
        )
        assert probe_ffmpeg_version() == "unavailable"

    def test_returns_unavailable_on_empty_stdout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        completed = subprocess.CompletedProcess(
            args=["ffmpeg", "-version"], returncode=0, stdout="", stderr=""
        )
        monkeypatch.setattr(
            "timelapse_manager.version.subprocess.run",
            lambda *args, **kwargs: completed,
        )
        assert probe_ffmpeg_version() == "unavailable"

    def test_returns_first_line_of_stdout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        expected = "ffmpeg version 6.1 Copyright (c) 2000-2024 the FFmpeg developers"
        completed = subprocess.CompletedProcess(
            args=["ffmpeg", "-version"],
            returncode=0,
            stdout=expected + "\nextra line",
            stderr="",
        )
        monkeypatch.setattr(
            "timelapse_manager.version.subprocess.run",
            lambda *args, **kwargs: completed,
        )
        assert probe_ffmpeg_version() == expected

    def test_returns_unavailable_on_os_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "timelapse_manager.version.subprocess.run",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("permission denied")),
        )
        assert probe_ffmpeg_version() == "unavailable"
