"""Tests for the FFmpeg binary resolver and pin-file loader.

Covers:
- resolve_ffmpeg_binary: explicit-knob mode, frozen-success mode,
  frozen-fail-loud mode, and dev/unfrozen fallback mode.
- load_ffmpeg_pin: parses the real ffmpeg-pin.json; required keys present;
  resolves relative to the package, not cwd.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from timelapse_manager.config.settings import RenderSettings, Settings
from timelapse_manager.ffmpeg_pin import (
    FfmpegResolutionError,
    load_ffmpeg_pin,
    resolve_ffmpeg_binary,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings_with_knob(path: str) -> Settings:
    """Return a minimal Settings whose render.ffmpeg_binary is set to *path*."""
    return Settings(render=RenderSettings(ffmpeg_binary=path, autostart=False))


def _settings_no_knob() -> Settings:
    """Return a minimal Settings with no explicit ffmpeg_binary."""
    return Settings(render=RenderSettings(ffmpeg_binary=None, autostart=False))


# ---------------------------------------------------------------------------
# resolve_ffmpeg_binary – explicit-knob mode
# ---------------------------------------------------------------------------


class TestResolverKnobMode:
    """When render.ffmpeg_binary is set the resolver returns it verbatim."""

    def test_returns_knob_path_verbatim(self) -> None:
        settings = _settings_with_knob("/opt/ffmpeg/bin/ffmpeg")
        result = resolve_ffmpeg_binary(settings)
        assert result == "/opt/ffmpeg/bin/ffmpeg"

    def test_knob_does_not_require_file_to_exist(self) -> None:
        # The resolver must not stat the knob path; it trusts the operator.
        settings = _settings_with_knob("/nonexistent/bin/ffmpeg")
        result = resolve_ffmpeg_binary(settings)
        assert result == "/nonexistent/bin/ffmpeg"

    def test_ffprobe_derived_beside_ffmpeg_knob(self) -> None:
        settings = _settings_with_knob("/opt/ffmpeg/bin/ffmpeg")
        result = resolve_ffmpeg_binary(settings, name="ffprobe")
        assert result == "/opt/ffmpeg/bin/ffprobe"

    def test_knob_wins_even_when_frozen(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Explicit knob takes precedence over the frozen-bundle path."""
        # Simulate a frozen process with a valid bundle structure.
        bundle = tmp_path / "bundle"
        ffmpeg_dir = bundle / "ffmpeg"
        ffmpeg_dir.mkdir(parents=True)
        (ffmpeg_dir / "ffmpeg").write_bytes(b"")
        monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)

        settings = _settings_with_knob("/opt/static/ffmpeg")
        result = resolve_ffmpeg_binary(settings)
        assert result == "/opt/static/ffmpeg"


# ---------------------------------------------------------------------------
# resolve_ffmpeg_binary – frozen mode (success)
# ---------------------------------------------------------------------------


class TestResolverFrozenSuccess:
    """When frozen and no knob the bundled binary is returned."""

    def test_returns_bundled_binary_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        bundle = tmp_path / "bundle"
        ffmpeg_dir = bundle / "ffmpeg"
        ffmpeg_dir.mkdir(parents=True)
        bundled_bin = ffmpeg_dir / "ffmpeg"
        bundled_bin.write_bytes(b"")  # file must exist

        monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)

        settings = _settings_no_knob()
        result = resolve_ffmpeg_binary(settings)
        assert result == str(bundled_bin)

    def test_bundled_path_is_inside_bundle_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        bundle = tmp_path / "bundle"
        (bundle / "ffmpeg").mkdir(parents=True)
        (bundle / "ffmpeg" / "ffmpeg").write_bytes(b"")
        monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)

        settings = _settings_no_knob()
        result = resolve_ffmpeg_binary(settings)
        # The resolved path must start with the bundle root.
        assert result.startswith(str(bundle))

    def test_bundled_ffprobe_resolved(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        bundle = tmp_path / "bundle"
        (bundle / "ffmpeg").mkdir(parents=True)
        (bundle / "ffmpeg" / "ffmpeg").write_bytes(b"")
        (bundle / "ffmpeg" / "ffprobe").write_bytes(b"")
        monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)

        settings = _settings_no_knob()
        result = resolve_ffmpeg_binary(settings, name="ffprobe")
        assert result.endswith("ffprobe")
        assert Path(result).is_file()


# ---------------------------------------------------------------------------
# resolve_ffmpeg_binary – frozen mode (fail-loud)
# ---------------------------------------------------------------------------


class TestResolverFrozenFailLoud:
    """When frozen and the bundled binary is absent the resolver raises loudly."""

    def test_raises_resolution_error_when_binary_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        bundle = tmp_path / "empty_bundle"
        bundle.mkdir()
        monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)

        settings = _settings_no_knob()
        with pytest.raises(FfmpegResolutionError):
            resolve_ffmpeg_binary(settings)

    def test_error_message_mentions_binary_name(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        bundle = tmp_path / "empty_bundle"
        bundle.mkdir()
        monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)

        settings = _settings_no_knob()
        with pytest.raises(FfmpegResolutionError, match="ffmpeg"):
            resolve_ffmpeg_binary(settings)

    def test_error_message_is_actionable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Error message must mention reinstall or the env-knob, not a PATH fallback."""
        bundle = tmp_path / "empty_bundle"
        bundle.mkdir()
        monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)

        settings = _settings_no_knob()
        with pytest.raises(FfmpegResolutionError) as exc_info:
            resolve_ffmpeg_binary(settings)
        msg = str(exc_info.value)
        # Must direct the user to a remediation action.
        assert (
            "TLM_RENDER__FFMPEG_BINARY" in msg
            or "Reinstall" in msg
            or "reinstall" in msg
        )

    def test_does_not_silently_fall_back_to_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The frozen resolver must raise, never return the bare 'ffmpeg' name."""
        bundle = tmp_path / "empty_bundle"
        bundle.mkdir()
        monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)

        settings = _settings_no_knob()
        with pytest.raises(FfmpegResolutionError):
            resolve_ffmpeg_binary(settings)
        # The raises() check above already ensures no silent PATH fallback.

    def test_frozen_flag_alone_also_triggers_resolution(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """sys.frozen=True without _MEIPASS should still trigger the frozen path."""
        # Remove _MEIPASS in case it exists, then set sys.frozen.
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        monkeypatch.setattr(sys, "frozen", True, raising=False)

        settings = _settings_no_knob()
        # bundle_root() falls back to the package dir when _MEIPASS is absent;
        # the bundled binary almost certainly won't exist there either.
        # This exercises the frozen branch without a silent PATH fall-through.
        # We accept either FfmpegResolutionError or a path-string (if somehow present).
        try:
            result = resolve_ffmpeg_binary(settings)
            # If it returns a path, it must not be the bare name "ffmpeg".
            assert result != "ffmpeg"
        except FfmpegResolutionError:
            pass  # expected


# ---------------------------------------------------------------------------
# resolve_ffmpeg_binary – dev/unfrozen fallback
# ---------------------------------------------------------------------------


class TestResolverDevMode:
    """When not frozen and no knob the resolver returns the bare 'ffmpeg' name."""

    def test_returns_bare_ffmpeg_in_dev(self) -> None:
        # Ensure we are not accidentally frozen (no _MEIPASS or sys.frozen).
        assert not hasattr(sys, "_MEIPASS") or getattr(sys, "frozen", False) is False
        from timelapse_manager.paths import is_frozen

        assert not is_frozen(), "Tests should not run from a frozen bundle"

        settings = _settings_no_knob()
        result = resolve_ffmpeg_binary(settings)
        assert result == "ffmpeg"

    def test_returns_bare_ffprobe_in_dev(self) -> None:
        from timelapse_manager.paths import is_frozen

        assert not is_frozen()

        settings = _settings_no_knob()
        result = resolve_ffmpeg_binary(settings, name="ffprobe")
        assert result == "ffprobe"


# ---------------------------------------------------------------------------
# load_ffmpeg_pin
# ---------------------------------------------------------------------------


class TestLoadFfmpegPin:
    """load_ffmpeg_pin() reads the real ffmpeg-pin.json and validates it."""

    def test_parses_without_error(self) -> None:
        pin = load_ffmpeg_pin()
        assert pin is not None

    def test_version_is_non_empty_string(self) -> None:
        pin = load_ffmpeg_pin()
        assert isinstance(pin.version, str)
        assert pin.version.strip() != ""

    def test_url_is_non_empty_string(self) -> None:
        pin = load_ffmpeg_pin()
        assert isinstance(pin.url, str)
        assert pin.url.startswith("https://")

    def test_sha256_is_non_empty_string(self) -> None:
        pin = load_ffmpeg_pin()
        assert isinstance(pin.sha256, str)
        assert pin.sha256.strip() != ""

    def test_license_is_non_empty_string(self) -> None:
        pin = load_ffmpeg_pin()
        assert isinstance(pin.license, str)
        assert pin.license.strip() != ""

    def test_binaries_contains_ffmpeg_key(self) -> None:
        pin = load_ffmpeg_pin()
        assert "ffmpeg" in pin.binaries

    def test_binaries_contains_ffprobe_key(self) -> None:
        pin = load_ffmpeg_pin()
        assert "ffprobe" in pin.binaries

    def test_binary_values_are_non_empty_strings(self) -> None:
        pin = load_ffmpeg_pin()
        for name, path in pin.binaries.items():
            assert isinstance(path, str), f"binaries[{name!r}] is not a string"
            assert path.strip() != "", f"binaries[{name!r}] is empty"

    def test_resolves_independent_of_cwd(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """pin resolution is keyed off __file__, not the working directory."""
        monkeypatch.chdir(tmp_path)
        # If load_ffmpeg_pin were CWD-relative it would raise FfmpegPinError here.
        pin = load_ffmpeg_pin()
        assert pin.version.strip() != ""

    def test_raises_on_missing_pin_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from timelapse_manager.ffmpeg_pin import FfmpegPinError

        # Make the resolved pin path point at a nonexistent file.
        fake_path = tmp_path / "no_such_pin.json"
        monkeypatch.setattr("timelapse_manager.ffmpeg_pin._pin_path", lambda: fake_path)
        with pytest.raises(FfmpegPinError, match="Cannot read"):
            load_ffmpeg_pin()

    def test_raises_on_invalid_json(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from timelapse_manager.ffmpeg_pin import FfmpegPinError

        bad_json = tmp_path / "bad.json"
        bad_json.write_text("not valid json", encoding="utf-8")
        monkeypatch.setattr("timelapse_manager.ffmpeg_pin._pin_path", lambda: bad_json)
        with pytest.raises(FfmpegPinError, match="Cannot parse"):
            load_ffmpeg_pin()

    def test_raises_on_missing_required_field(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import json

        from timelapse_manager.ffmpeg_pin import FfmpegPinError

        incomplete = tmp_path / "incomplete.json"
        # Missing 'binaries' key.
        incomplete.write_text(
            json.dumps(
                {"version": "1.0", "url": "http://x", "sha256": "abc", "license": "MIT"}
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "timelapse_manager.ffmpeg_pin._pin_path", lambda: incomplete
        )
        with pytest.raises(FfmpegPinError):
            load_ffmpeg_pin()
