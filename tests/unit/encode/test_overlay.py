"""Unit tests for overlay text escaping and path confinement."""

from __future__ import annotations

from pathlib import Path

import pytest

from timelapse_manager.encode.encoder import EncoderError, OverlayConfig
from timelapse_manager.encode.overlay import (
    escape_drawtext,
    escape_path_for_filter,
    escape_timestamp_format,
    has_any_overlay,
    image_position,
    resolve_overlay_image,
    text_position,
)

# ---------------------------------------------------------------------------
# escape_drawtext
# ---------------------------------------------------------------------------


class TestEscapeDrawtext:
    def test_plain_text_unchanged(self) -> None:
        assert escape_drawtext("hello world") == "hello world"

    def test_colon_escaped(self) -> None:
        result = escape_drawtext("time: 12:00")
        assert "\\:" in result
        assert ":" not in result.replace("\\:", "")

    def test_backslash_doubled(self) -> None:
        result = escape_drawtext("path\\to\\file")
        assert "\\\\" in result

    def test_percent_escaped(self) -> None:
        result = escape_drawtext("50%")
        assert "\\\\%" in result

    def test_newline_collapsed_to_space(self) -> None:
        result = escape_drawtext("line1\nline2")
        assert "\n" not in result
        assert " " in result

    def test_carriage_return_stripped(self) -> None:
        result = escape_drawtext("text\r\nhere")
        assert "\r" not in result

    def test_apostrophe_replaced_with_typographic_quote(self) -> None:
        result = escape_drawtext("it's a test")
        # Straight apostrophe is replaced with typographic right single quotation mark.
        assert "'" not in result
        assert "’" in result  # RIGHT SINGLE QUOTATION MARK

    def test_empty_string_returns_empty(self) -> None:
        assert escape_drawtext("") == ""

    def test_backslash_escaped_before_other_replacements(self) -> None:
        # A literal backslash followed by a colon: the backslash is doubled first,
        # then the colon is escaped. Result: \\\\\\: (four backslashes, escaped colon).
        result = escape_drawtext("a\\:b")
        # Backslash -> \\\\ then colon -> \\: so we end up with \\\\\\:
        assert "\\:" in result
        assert "\\\\" in result


# ---------------------------------------------------------------------------
# escape_timestamp_format
# ---------------------------------------------------------------------------


class TestEscapeTimestampFormat:
    def test_common_format_colons_escaped(self) -> None:
        result = escape_timestamp_format("%H:%M:%S")
        # Colons must be triple-backslash escaped.
        assert ":" not in result.replace("\\\\\\:", "")

    def test_percent_directives_not_escaped(self) -> None:
        # %Y, %m etc. must survive so strftime still processes them.
        result = escape_timestamp_format("%Y-%m-%d")
        assert "%Y" in result
        assert "%m" in result
        assert "%d" in result

    def test_newline_collapsed(self) -> None:
        result = escape_timestamp_format("line1\nline2")
        assert "\n" not in result

    def test_backslash_doubled(self) -> None:
        result = escape_timestamp_format("a\\b")
        assert "\\\\" in result

    def test_single_quote_escaped(self) -> None:
        result = escape_timestamp_format("fmt'here")
        assert "\\'" in result
        assert "'" not in result.replace("\\'", "")

    def test_empty_format_returns_empty(self) -> None:
        assert escape_timestamp_format("") == ""


# ---------------------------------------------------------------------------
# escape_path_for_filter
# ---------------------------------------------------------------------------


class TestEscapePathForFilter:
    def test_plain_path_unchanged(self) -> None:
        assert (
            escape_path_for_filter("/usr/share/fonts/arial.ttf")
            == "/usr/share/fonts/arial.ttf"
        )

    def test_backslash_doubled(self) -> None:
        result = escape_path_for_filter("C:\\Windows\\Fonts\\arial.ttf")
        assert "\\\\" in result

    def test_single_quote_dropped(self) -> None:
        # A path with an apostrophe: the apostrophe is dropped entirely.
        result = escape_path_for_filter("/path/to/font's.ttf")
        assert "'" not in result

    def test_empty_path_returns_empty(self) -> None:
        assert escape_path_for_filter("") == ""


# ---------------------------------------------------------------------------
# text_position and image_position
# ---------------------------------------------------------------------------


class TestTextPosition:
    def test_top_left_returns_margin_coordinates(self) -> None:
        x, y = text_position("top_left")
        assert "10" in x
        assert "10" in y

    def test_top_right_uses_width_minus_text_width(self) -> None:
        x, y = text_position("top_right")
        assert "w" in x
        assert "tw" in x

    def test_bottom_left_uses_height_minus_text_height(self) -> None:
        x, y = text_position("bottom_left")
        assert "h" in y
        assert "th" in y

    def test_bottom_right_combines_both(self) -> None:
        x, y = text_position("bottom_right")
        assert "w" in x
        assert "h" in y

    def test_unknown_placement_falls_back_to_top_left(self) -> None:
        x, y = text_position("center")
        top_left_x, top_left_y = text_position("top_left")
        assert x == top_left_x
        assert y == top_left_y


class TestImagePosition:
    def test_top_left_returns_margin_coordinates(self) -> None:
        x, y = image_position("top_left")
        assert "10" in x
        assert "10" in y

    def test_top_right_uses_main_minus_overlay_width(self) -> None:
        x, y = image_position("top_right")
        assert "W" in x
        assert "w" in x

    def test_bottom_left_uses_main_minus_overlay_height(self) -> None:
        x, y = image_position("bottom_left")
        assert "H" in y
        assert "h" in y

    def test_unknown_placement_falls_back_to_top_left(self) -> None:
        x, y = image_position("bogus")
        top_left_x, top_left_y = image_position("top_left")
        assert x == top_left_x
        assert y == top_left_y


# ---------------------------------------------------------------------------
# resolve_overlay_image: path confinement
# ---------------------------------------------------------------------------


class TestResolveOverlayImage:
    def test_image_inside_render_root_returns_path(self, tmp_path: Path) -> None:
        render_root = tmp_path / "renders"
        render_root.mkdir()
        image = render_root / "logo.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)

        result = resolve_overlay_image(str(image), render_root)
        assert result == image.resolve()

    def test_path_traversal_outside_root_raises(self, tmp_path: Path) -> None:
        render_root = tmp_path / "renders"
        render_root.mkdir()
        evil_path = str(render_root / ".." / "etc" / "passwd")

        with pytest.raises(EncoderError):
            resolve_overlay_image(evil_path, render_root)

    def test_absolute_path_outside_root_raises(self, tmp_path: Path) -> None:
        render_root = tmp_path / "renders"
        render_root.mkdir()

        with pytest.raises(EncoderError):
            resolve_overlay_image("/etc/passwd", render_root)

    def test_nonexistent_file_inside_root_raises(self, tmp_path: Path) -> None:
        render_root = tmp_path / "renders"
        render_root.mkdir()
        missing = render_root / "missing.png"

        with pytest.raises(EncoderError):
            resolve_overlay_image(str(missing), render_root)

    def test_image_path_uses_is_relative_to_not_string_prefix(
        self, tmp_path: Path
    ) -> None:
        # Regression: "/tmp/renders/../other" must be rejected, not accepted via
        # string prefix match.
        render_root = tmp_path / "renders"
        render_root.mkdir()
        sibling = tmp_path / "other"
        sibling.mkdir()
        sibling_file = sibling / "secret.png"
        sibling_file.write_bytes(b"fake")

        # This path starts with render_root/ but resolves outside it.
        traversal = str(render_root / ".." / "other" / "secret.png")
        with pytest.raises(EncoderError):
            resolve_overlay_image(traversal, render_root)


# ---------------------------------------------------------------------------
# has_any_overlay
# ---------------------------------------------------------------------------


class TestHasAnyOverlay:
    def test_all_disabled_returns_false(self) -> None:
        cfg = OverlayConfig(
            timestamp_enabled=False,
            text_enabled=False,
            image_enabled=False,
        )
        assert not has_any_overlay(cfg)

    def test_timestamp_enabled_returns_true(self) -> None:
        cfg = OverlayConfig(timestamp_enabled=True)
        assert has_any_overlay(cfg)

    def test_text_enabled_returns_true(self) -> None:
        cfg = OverlayConfig(text_enabled=True)
        assert has_any_overlay(cfg)

    def test_image_enabled_returns_true(self) -> None:
        cfg = OverlayConfig(image_enabled=True)
        assert has_any_overlay(cfg)
