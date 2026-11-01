"""Unit tests for overlay text escaping and path confinement."""

from __future__ import annotations

from pathlib import Path

import pytest

from timelapse_manager.encode.encoder import EncoderError, OverlayConfig
from timelapse_manager.encode.overlay import (
    DEFAULT_TIMESTAMP_FORMAT,
    TIMESTAMP_FORMAT_MAX_LENGTH,
    escape_drawtext,
    escape_path_for_filter,
    escape_timestamp_format,
    has_any_overlay,
    image_position,
    resolve_overlay_image,
    text_position,
    validate_timestamp_format,
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

    def test_backslash_quadrupled_for_option_and_expansion_levels(self) -> None:
        # Two levels consume a backslash each: the option parser and drawtext's
        # own expansion. Doubling only (the old behaviour) let "\\%{...}" in a
        # caption reach drawtext as a live %{...} function.
        assert escape_drawtext("a\\b") == "a\\\\\\\\b"
        assert escape_drawtext("\\%{x}") == "\\\\\\\\\\\\%{x}"

    def test_nul_and_control_characters_dropped(self) -> None:
        # A NUL cannot be carried in an argv element at all.
        assert escape_drawtext("a\x00b\x07c\x1bd\x7fe") == "abcde"

    def test_tab_becomes_space(self) -> None:
        assert escape_drawtext("a\tb") == "a b"

    def test_non_ascii_passes_through(self) -> None:
        assert escape_drawtext("é 日本 🎥") == "é 日本 🎥"

    def test_backslash_escaped_before_other_replacements(self) -> None:
        # A literal backslash followed by a colon: the backslash is doubled first,
        # then the colon is escaped. Result: \\\\\\: (four backslashes, escaped colon).
        result = escape_drawtext("a\\:b")
        # Backslash -> \\\\ then colon -> \\: so we end up with \\\\\\:
        assert "\\:" in result
        assert "\\\\" in result


# ---------------------------------------------------------------------------
# validate_timestamp_format (#67 S4)
# ---------------------------------------------------------------------------


class TestValidateTimestampFormat:
    @pytest.mark.parametrize(
        "fmt",
        [
            "'",
            "',null",
            "x',movie=/etc/passwd",
            "',drawtext=textfile=/etc/passwd",
            "[a];movie=x",
            "%{eif:1:d}",
            "%{localtime}",
            "\\",
            "%H\\:%M",
            "a,b",
            "a;b",
            "[x]",
            "{",
            '"',
            "%H\n",
            "%H\r",
            "\n",
            "%H\x00",
            "%H\t%M",
            "é%Y",
            "%Y\u00a0%m",
            "%",
            "%Y%",
            "%-d",
            "%Q",
            "%e",
            "%s",
            "%%%",
            "",
            "a" * 65,
            "%Y" * 33,
        ],
    )
    def test_hostile_or_malformed_format_rejected(self, fmt: str) -> None:
        with pytest.raises(ValueError, match="timestamp_format"):
            validate_timestamp_format(fmt)

    @pytest.mark.parametrize("value", [None, 5, b"%Y", ["%Y"]])
    def test_non_string_rejected(self, value: object) -> None:
        with pytest.raises(ValueError, match="must be a string"):
            validate_timestamp_format(value)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "fmt",
        [
            DEFAULT_TIMESTAMP_FORMAT,
            "%Y-%m-%d %H:%M",
            "%H:%M",
            "%d/%m/%Y %I:%M %p",
            "%a %A %b %B %j %y %z %Z",
            "100%%",
            "%%Y",
            "-%Y_%m.%d",
            "Day %j",
            "a" * 64,
        ],
    )
    def test_normal_format_accepted_unchanged(self, fmt: str) -> None:
        assert validate_timestamp_format(fmt) == fmt

    def test_max_length_is_sixty_four(self) -> None:
        assert TIMESTAMP_FORMAT_MAX_LENGTH == 64


# ---------------------------------------------------------------------------
# escape_timestamp_format
# ---------------------------------------------------------------------------


class TestEscapeTimestampFormat:
    def test_common_format_colons_escaped(self) -> None:
        result = escape_timestamp_format("%H:%M:%S")
        # Colons must be triple-backslash escaped.
        assert result == "%H\\\\\\:%M\\\\\\:%S"

    def test_percent_directives_not_escaped(self) -> None:
        # %Y, %m etc. must survive so strftime still processes them.
        assert escape_timestamp_format("%Y-%m-%d") == "%Y-%m-%d"

    @pytest.mark.parametrize(
        "fmt", ["it's %H", "a\\b", "line1\nline2", "", "%{eif:1:d}", "%H'"]
    )
    def test_invalid_format_raises_encoder_error(self, fmt: str) -> None:
        # Validation is re-run here: an unchecked value never reaches the graph.
        with pytest.raises(EncoderError, match="invalid timestamp overlay format"):
            escape_timestamp_format(fmt)

    def test_never_produces_a_quote(self) -> None:
        result = escape_timestamp_format("%Y-%m-%d %H:%M:%S %p %Z")
        assert "'" not in result


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

    def test_colon_escaped_for_the_option_parser(self) -> None:
        # Inside fontfile='...' the graph parser keeps "\\:"; the option parser
        # then reads it as a literal colon. A bare colon would end the option.
        assert escape_path_for_filter("/a:b/f.ttf") == "/a\\:b/f.ttf"
        assert (
            escape_path_for_filter("C:\\Windows\\arial.ttf")
            == "C\\:\\\\Windows\\\\arial.ttf"
        )


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
