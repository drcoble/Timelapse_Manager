"""Tests for multi-URL mirror resilience in ffmpeg-pin.json and load_ffmpeg_pin.

Covers:
- The real ffmpeg-pin.json parses with mirror_urls present and as a list.
- download_urls() returns an ordered, deduplicated list with url as fallback.
- Backward-compat: a pin with no mirror_urls field still yields [url].
- A pin with mirror_urls=[url] is deduplicated to a single entry.
- A pin with mirror_urls containing a placeholder is still valid to load
  (callers, not the loader, skip placeholders at download time).
- mirror_urls must be a JSON array; a non-array value raises FfmpegPinError.
- sha256 is the same regardless of which URL is chosen (trust anchor).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from timelapse_manager.ffmpeg_pin import FfmpegPin, FfmpegPinError, load_ffmpeg_pin

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CODE_ROOT = Path(__file__).resolve().parents[3]


def _make_pin(mirror_urls: list[str] | None = None) -> dict:
    """Return a minimal valid pin dict with optional mirror_urls."""
    base = {
        "version": "test-1.0",
        "url": "https://upstream.example.com/ffmpeg.tar.xz",
        "sha256": "a" * 64,
        "license": "GPL-3.0",
        "binaries": {"ffmpeg": "bin/ffmpeg", "ffprobe": "bin/ffprobe"},
    }
    if mirror_urls is not None:
        base["mirror_urls"] = mirror_urls
    return base


def _write_pin(tmp_path: Path, pin: dict) -> Path:
    """Write a pin dict to a temp JSON file and return its path."""
    p = tmp_path / "ffmpeg-pin.json"
    p.write_text(json.dumps(pin), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Real pin file
# ---------------------------------------------------------------------------


class TestRealPinMirrors:
    """The real ffmpeg-pin.json has a mirror_urls field."""

    def test_mirror_urls_is_present(self) -> None:
        pin = load_ffmpeg_pin()
        # mirror_urls defaults to an empty tuple when absent; the real pin
        # should have it populated.
        assert isinstance(pin.mirror_urls, tuple)
        assert len(pin.mirror_urls) >= 1, (
            "ffmpeg-pin.json should have at least one entry in mirror_urls"
        )

    def test_mirror_urls_are_strings(self) -> None:
        pin = load_ffmpeg_pin()
        for u in pin.mirror_urls:
            assert isinstance(u, str), f"mirror_urls entry is not a string: {u!r}"

    def test_download_urls_includes_primary_url(self) -> None:
        pin = load_ffmpeg_pin()
        urls = pin.download_urls()
        assert pin.url in urls, (
            f"primary url {pin.url!r} not in download_urls(): {urls}"
        )

    def test_download_urls_is_non_empty(self) -> None:
        pin = load_ffmpeg_pin()
        assert len(pin.download_urls()) >= 1

    def test_sha256_unchanged_regardless_of_url(self) -> None:
        """sha256 must be the same no matter which URL is selected."""
        pin = load_ffmpeg_pin()
        for u in pin.download_urls():
            assert pin.sha256 == load_ffmpeg_pin().sha256, (
                f"sha256 changed between calls for URL {u!r}"
            )


# ---------------------------------------------------------------------------
# download_urls() ordering and deduplication
# ---------------------------------------------------------------------------


class TestDownloadUrlsOrdering:
    """download_urls() returns an ordered, deduplicated list."""

    def _pin_from(self, **kwargs) -> FfmpegPin:
        base_url = "https://upstream.example.com/ffmpeg.tar.xz"
        return FfmpegPin(
            version="1.0",
            url=kwargs.get("url", base_url),
            sha256="a" * 64,
            license="GPL-3.0",
            binaries={"ffmpeg": "bin/ffmpeg", "ffprobe": "bin/ffprobe"},
            mirror_urls=tuple(kwargs.get("mirror_urls", [])),
        )

    def test_no_mirror_urls_returns_only_primary(self) -> None:
        pin = self._pin_from()
        assert pin.download_urls() == ["https://upstream.example.com/ffmpeg.tar.xz"]

    def test_mirror_urls_appear_before_primary(self) -> None:
        pin = self._pin_from(
            mirror_urls=[
                "https://mirror1.example.com/ffmpeg.tar.xz",
                "https://upstream.example.com/ffmpeg.tar.xz",
            ]
        )
        urls = pin.download_urls()
        assert urls[0] == "https://mirror1.example.com/ffmpeg.tar.xz"
        assert urls[-1] == "https://upstream.example.com/ffmpeg.tar.xz"

    def test_primary_appended_when_not_in_mirrors(self) -> None:
        pin = self._pin_from(mirror_urls=["https://mirror1.example.com/ffmpeg.tar.xz"])
        urls = pin.download_urls()
        assert "https://upstream.example.com/ffmpeg.tar.xz" in urls

    def test_no_duplicates_when_primary_in_mirrors(self) -> None:
        primary = "https://upstream.example.com/ffmpeg.tar.xz"
        pin = self._pin_from(
            url=primary,
            mirror_urls=[primary],
        )
        urls = pin.download_urls()
        assert urls.count(primary) == 1

    def test_order_preserved_for_multiple_mirrors(self) -> None:
        mirrors = [
            "https://mirror1.example.com/ffmpeg.tar.xz",
            "https://mirror2.example.com/ffmpeg.tar.xz",
        ]
        pin = self._pin_from(mirror_urls=mirrors)
        urls = pin.download_urls()
        assert urls[0] == mirrors[0]
        assert urls[1] == mirrors[1]

    def test_duplicate_mirrors_deduplicated(self) -> None:
        same = "https://mirror1.example.com/ffmpeg.tar.xz"
        pin = self._pin_from(mirror_urls=[same, same])
        urls = pin.download_urls()
        assert urls.count(same) == 1

    def test_placeholder_url_is_accepted_by_loader(self) -> None:
        """The loader does not skip placeholders; callers skip them at fetch time."""
        pin = self._pin_from(mirror_urls=["https://REPLACE_ME/ffmpeg.tar.xz"])
        urls = pin.download_urls()
        assert "https://REPLACE_ME/ffmpeg.tar.xz" in urls


# ---------------------------------------------------------------------------
# load_ffmpeg_pin with synthetic pin files
# ---------------------------------------------------------------------------


class TestLoadFfmpegPinMirrors:
    """load_ffmpeg_pin() handles both single-url and mirror_urls-list schemas."""

    def test_backward_compat_no_mirror_urls_field(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A pin without mirror_urls still loads; download_urls() returns [url]."""
        pin_data = _make_pin()  # no mirror_urls key
        p = _write_pin(tmp_path, pin_data)
        monkeypatch.setattr("timelapse_manager.ffmpeg_pin._pin_path", lambda: p)

        pin = load_ffmpeg_pin()
        assert pin.mirror_urls == ()
        assert pin.download_urls() == [pin.url]

    def test_mirror_urls_list_is_parsed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        mirrors = [
            "https://REPLACE_ME/ffmpeg.tar.xz",
            "https://upstream.example.com/ffmpeg.tar.xz",
        ]
        pin_data = _make_pin(mirror_urls=mirrors)
        p = _write_pin(tmp_path, pin_data)
        monkeypatch.setattr("timelapse_manager.ffmpeg_pin._pin_path", lambda: p)

        pin = load_ffmpeg_pin()
        assert list(pin.mirror_urls) == mirrors

    def test_mirror_urls_non_array_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pin_data = _make_pin()
        pin_data["mirror_urls"] = "not-a-list"
        p = _write_pin(tmp_path, pin_data)
        monkeypatch.setattr("timelapse_manager.ffmpeg_pin._pin_path", lambda: p)

        with pytest.raises(FfmpegPinError, match="mirror_urls"):
            load_ffmpeg_pin()

    def test_sha256_is_same_for_all_urls(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """sha256 is the trust anchor: the same value governs all candidate URLs."""
        mirrors = [
            "https://REPLACE_ME/ffmpeg.tar.xz",
            "https://upstream.example.com/ffmpeg.tar.xz",
        ]
        expected_sha = "b" * 64
        pin_data = _make_pin(mirror_urls=mirrors)
        pin_data["sha256"] = expected_sha
        p = _write_pin(tmp_path, pin_data)
        monkeypatch.setattr("timelapse_manager.ffmpeg_pin._pin_path", lambda: p)

        pin = load_ffmpeg_pin()
        # The sha256 is the same regardless of which download_url is used.
        for url in pin.download_urls():
            assert pin.sha256 == expected_sha, (
                f"sha256 must be invariant across URLs; got {pin.sha256!r} for {url!r}"
            )

    def test_url_field_stays_string(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """url remains a plain string even when mirror_urls is present."""
        primary = "https://upstream.example.com/ffmpeg.tar.xz"
        pin_data = _make_pin(mirror_urls=["https://REPLACE_ME/ffmpeg.tar.xz", primary])
        p = _write_pin(tmp_path, pin_data)
        monkeypatch.setattr("timelapse_manager.ffmpeg_pin._pin_path", lambda: p)

        pin = load_ffmpeg_pin()
        assert isinstance(pin.url, str)
        assert pin.url == primary
