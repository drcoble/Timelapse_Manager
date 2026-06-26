"""Abuse tests: subprocess/FFmpeg command-line hardening.

Verifies the allowlist and escaping seams that prevent hostile values from
reaching the ffmpeg subprocess:
  - Codec and container allowlist boundaries
  - Filter name allowlist
  - Numeric parameter bounds (fps, dimensions, CRF, bitrate)
  - drawtext text escaping
  - Timestamp-format escaping
  - Overlay image path confinement
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Re-export for cleaner references in the codec/container tests.
from timelapse_manager.encode.allowlist import (  # noqa: F401
    CODEC_ENCODERS,
    CONTAINER_MUXERS,
    EncoderCapabilityError,
    ensure_filters_allowed,
    resolve_codec,
    resolve_container,
    validate_bitrate_kbps,
    validate_crf,
    validate_dimensions,
    validate_fps,
)
from timelapse_manager.encode.encoder import EncoderError
from timelapse_manager.encode.overlay import (
    escape_drawtext,
    escape_path_for_filter,
    escape_timestamp_format,
    resolve_overlay_image,
)

# ---------------------------------------------------------------------------
# Codec allowlist
# ---------------------------------------------------------------------------


@pytest.mark.abuse
class TestCodecAllowlist:
    @pytest.mark.parametrize(
        "codec,expected_encoder",
        [
            ("h264", "libx264"),
            ("libx264", "libx264"),
            ("h265", "libx265"),
            ("hevc", "libx265"),
            ("libx265", "libx265"),
            ("vp9", "libvpx-vp9"),
            ("libvpx-vp9", "libvpx-vp9"),
            ("av1", "libsvtav1"),
            ("libsvtav1", "libsvtav1"),
        ],
    )
    def test_allowed_codecs_resolve(self, codec: str, expected_encoder: str) -> None:
        assert resolve_codec(codec) == expected_encoder

    @pytest.mark.parametrize(
        "codec",
        [
            "xvid",
            "mpeg2",
            "h263",
            "divx",
            "wmv",
            "theora",
            "prores",
            "",
            "h264;rm -rf /",  # injection attempt
            "h264\x00libx264",  # null byte attempt
        ],
    )
    def test_unlisted_codecs_raise(self, codec: str) -> None:
        with pytest.raises(EncoderCapabilityError):
            resolve_codec(codec)


# ---------------------------------------------------------------------------
# Container allowlist
# ---------------------------------------------------------------------------


@pytest.mark.abuse
class TestContainerAllowlist:
    @pytest.mark.parametrize(
        "container,expected_muxer",
        [
            ("mp4", "mp4"),
            ("mkv", "matroska"),
            ("webm", "webm"),
        ],
    )
    def test_allowed_containers_resolve(
        self, container: str, expected_muxer: str
    ) -> None:
        assert resolve_container(container) == expected_muxer

    @pytest.mark.parametrize(
        "container",
        [
            "avi",
            "mov",
            "ts",
            "flv",
            "m4v",
            "",
            "mp4 -vf null",  # injection attempt
        ],
    )
    def test_unlisted_containers_raise(self, container: str) -> None:
        with pytest.raises(EncoderCapabilityError):
            resolve_container(container)


# ---------------------------------------------------------------------------
# Filter name allowlist
# ---------------------------------------------------------------------------


@pytest.mark.abuse
class TestFilterAllowlist:
    @pytest.mark.parametrize(
        "filters",
        [
            ["deflicker"],
            ["drawtext"],
            ["scale"],
            ["fps"],
            ["format"],
            ["setpts"],
            ["overlay"],
            ["scale", "fps", "deflicker"],
        ],
    )
    def test_allowed_filter_lists_pass(self, filters: list[str]) -> None:
        ensure_filters_allowed(filters)  # must not raise

    @pytest.mark.parametrize(
        "filter_name",
        [
            "null",
            "crop",
            "rotate",
            "vflip",
            "hflip",
            "trim",
            "concat",
            "drawbox",
            "select",
            "yadif",
            "hue",
            "geq",  # can read arbitrary pixels
            "movie",  # reads arbitrary file
            "amovie",  # reads arbitrary file
            "zmq",  # network access
            "sendcmd",  # runtime command injection
        ],
    )
    def test_unlisted_filter_raises(self, filter_name: str) -> None:
        with pytest.raises(EncoderCapabilityError):
            ensure_filters_allowed([filter_name])

    def test_single_unlisted_filter_in_mixed_list_raises(self) -> None:
        with pytest.raises(EncoderCapabilityError):
            ensure_filters_allowed(["scale", "movie", "fps"])

    def test_empty_filter_list_passes(self) -> None:
        ensure_filters_allowed([])  # must not raise


# ---------------------------------------------------------------------------
# Numeric parameter bounds
# ---------------------------------------------------------------------------


@pytest.mark.abuse
class TestNumericBounds:
    @pytest.mark.parametrize("fps", [0.1, 1.0, 24.0, 30.0, 60.0, 240.0])
    def test_valid_fps_values_pass(self, fps: float) -> None:
        validate_fps(fps)

    @pytest.mark.parametrize("fps", [0.0, 0.09, -1.0, 240.1, 1000.0])
    def test_invalid_fps_values_raise(self, fps: float) -> None:
        with pytest.raises(EncoderCapabilityError):
            validate_fps(fps)

    @pytest.mark.parametrize("w,h", [(2, 2), (640, 480), (1920, 1080), (16384, 16384)])
    def test_valid_dimensions_pass(self, w: int, h: int) -> None:
        validate_dimensions(w, h)

    @pytest.mark.parametrize(
        "w,h",
        [
            (0, 480),  # too small
            (640, 0),  # too small
            (1, 480),  # odd width (yuv420p requirement)
            (640, 1),  # odd height
            (16385, 480),  # too large
            (-1, 480),  # negative
        ],
    )
    def test_invalid_dimensions_raise(self, w: int, h: int) -> None:
        with pytest.raises(EncoderCapabilityError):
            validate_dimensions(w, h)

    @pytest.mark.parametrize("crf", [0, 1, 23, 51, 63])
    def test_valid_crf_values_pass(self, crf: int) -> None:
        validate_crf(crf)

    @pytest.mark.parametrize("crf", [-1, 64, 100])
    def test_invalid_crf_values_raise(self, crf: int) -> None:
        with pytest.raises(EncoderCapabilityError):
            validate_crf(crf)

    @pytest.mark.parametrize("kbps", [1, 500, 8000, 1_000_000])
    def test_valid_bitrate_values_pass(self, kbps: int) -> None:
        validate_bitrate_kbps(kbps)

    @pytest.mark.parametrize("kbps", [0, -1, 1_000_001])
    def test_invalid_bitrate_values_raise(self, kbps: int) -> None:
        with pytest.raises(EncoderCapabilityError):
            validate_bitrate_kbps(kbps)


# ---------------------------------------------------------------------------
# drawtext escaping: hostile text cannot break out of the filter option
# ---------------------------------------------------------------------------


@pytest.mark.abuse
class TestEscapeDrawtext:
    def test_single_quote_replaced_with_typographic_quote(self) -> None:
        """A literal ' cannot break out of the surrounding single-quoted value.

        escape_drawtext replaces ASCII single-quote with the typographic right
        single quotation mark (U+2019) which is not a filtergraph quote delimiter.
        """
        result = escape_drawtext("it's here")
        # ASCII single quote must not appear in the output.
        assert "'" not in result
        # The typographic replacement quote must be present.
        assert "’" in result  # RIGHT SINGLE QUOTATION MARK

    def test_colon_is_escaped(self) -> None:
        """An unescaped : would end the drawtext option."""
        result = escape_drawtext("time: 12:00")
        # Each colon is prefixed with a backslash escape sequence.
        assert "\\:" in result
        # The raw colon must only appear inside the escape sequence (preceded by \\).
        import re

        bare_colons = re.findall(r"(?<!\\):", result)
        assert len(bare_colons) == 0, f"Found unescaped colon in result: {result!r}"

    def test_backslash_doubled(self) -> None:
        result = escape_drawtext("path\\to\\file")
        assert "\\\\" in result

    def test_percent_escaped_to_survive_drawtext_expansion(self) -> None:
        result = escape_drawtext("100%")
        assert "\\\\%" in result
        # No bare % that drawtext would try to expand as %{...}
        assert result.count("%") == 1  # only inside the escape sequence

    def test_newline_collapsed_to_space(self) -> None:
        result = escape_drawtext("line one\nline two")
        assert "\n" not in result

    def test_safe_text_passes_through_mostly_unchanged(self) -> None:
        result = escape_drawtext("camera 01 frame")
        assert "camera 01 frame" in result


# ---------------------------------------------------------------------------
# Timestamp-format escaping
# ---------------------------------------------------------------------------


@pytest.mark.abuse
class TestEscapeTimestampFormat:
    def test_colon_triple_escaped_for_two_parsers(self) -> None:
        """A colon in %H:%M must survive the filtergraph parser and %{...} splitter."""
        result = escape_timestamp_format("%H:%M:%S")
        # Each : must become \\\\\\: (3 backslashes + colon)
        assert "\\\\\\:" in result

    def test_backslash_doubled(self) -> None:
        result = escape_timestamp_format("literal\\path")
        assert "\\\\" in result

    def test_single_quote_escaped(self) -> None:
        """A single quote would close the outer text='...' quoting.

        escape_timestamp_format writes \\' so the filtergraph parser treats it
        as an escaped quote rather than a quote-close.
        """
        result = escape_timestamp_format("it's %H")
        # The escape sequence must be present.
        assert "\\'" in result
        # An un-escaped bare apostrophe must not appear (only the escaped form).
        import re

        bare_quotes = re.findall(r"(?<!\\)'", result)
        assert len(bare_quotes) == 0, (
            f"Found unescaped single quote in result: {result!r}"
        )

    def test_strftime_percent_directives_pass_through(self) -> None:
        """% in format strings must NOT be double-escaped (unlike drawtext)."""
        result = escape_timestamp_format("%Y-%m-%d")
        # %Y, %m, %d must survive so strftime can expand them.
        assert "%Y" in result
        assert "%m" in result
        assert "%d" in result


# ---------------------------------------------------------------------------
# Overlay image path confinement
# ---------------------------------------------------------------------------


@pytest.mark.abuse
class TestResolveOverlayImage:
    def test_path_inside_render_root_is_allowed(self, tmp_path: Path) -> None:
        render_root = tmp_path / "renders" / "project-1"
        render_root.mkdir(parents=True)
        image = render_root / "watermark.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n")  # minimal PNG header

        resolved = resolve_overlay_image(str(image), render_root)
        assert resolved.is_relative_to(render_root.resolve())

    def test_path_outside_render_root_raises(self, tmp_path: Path) -> None:
        render_root = tmp_path / "renders" / "project-1"
        render_root.mkdir(parents=True)
        outside = tmp_path / "outside.png"
        outside.write_bytes(b"\x89PNG\r\n\x1a\n")

        with pytest.raises(EncoderError, match="outside the project render root"):
            resolve_overlay_image(str(outside), render_root)

    def test_directory_traversal_blocked(self, tmp_path: Path) -> None:
        """../../etc/passwd-style traversal must be rejected."""
        render_root = tmp_path / "renders" / "project-1"
        render_root.mkdir(parents=True)
        # A real file above the root
        victim = tmp_path / "victim.png"
        victim.write_bytes(b"\x89PNG\r\n\x1a\n")

        traversal_path = str(render_root / ".." / ".." / "victim.png")
        with pytest.raises(EncoderError, match="outside the project render root"):
            resolve_overlay_image(traversal_path, render_root)

    def test_nonexistent_file_raises(self, tmp_path: Path) -> None:
        render_root = tmp_path / "renders" / "project-1"
        render_root.mkdir(parents=True)
        missing = render_root / "nonexistent.png"

        with pytest.raises(EncoderError, match="does not exist"):
            resolve_overlay_image(str(missing), render_root)

    def test_escape_path_removes_single_quotes(self) -> None:
        """A path with a single quote must have it dropped (unsupported)."""
        result = escape_path_for_filter("/fonts/my font's.ttf")
        assert "'" not in result
        assert "my fonts.ttf" in result or "my font" in result

    def test_escape_path_doubles_backslashes(self) -> None:
        """Windows path backslashes must be doubled for the filtergraph."""
        result = escape_path_for_filter("C:\\Windows\\Fonts\\arial.ttf")
        assert "\\\\" in result
