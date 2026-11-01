"""Unit tests for the encode allowlist: codec, container, filter validation."""

from __future__ import annotations

import pytest

from timelapse_manager.encode.allowlist import (
    ensure_filters_allowed,
    parse_filter_names,
    parse_filter_options,
    parse_filtergraph,
    resolve_codec,
    resolve_container,
    validate_bitrate_kbps,
    validate_crf,
    validate_dimensions,
    validate_fps,
)
from timelapse_manager.encode.encoder import EncoderCapabilityError, EncoderError

# ---------------------------------------------------------------------------
# resolve_codec
# ---------------------------------------------------------------------------


class TestResolveCodec:
    def test_h264_resolves_to_libx264(self) -> None:
        assert resolve_codec("h264") == "libx264"

    def test_h265_resolves_to_libx265(self) -> None:
        assert resolve_codec("h265") == "libx265"

    def test_hevc_alias_resolves_to_libx265(self) -> None:
        assert resolve_codec("hevc") == "libx265"

    def test_libx264_passthrough(self) -> None:
        assert resolve_codec("libx264") == "libx264"

    def test_libx265_passthrough(self) -> None:
        assert resolve_codec("libx265") == "libx265"

    def test_vp9_resolves_to_libvpx_vp9(self) -> None:
        assert resolve_codec("vp9") == "libvpx-vp9"

    def test_libvpx_vp9_passthrough(self) -> None:
        assert resolve_codec("libvpx-vp9") == "libvpx-vp9"

    def test_uppercase_input_normalised(self) -> None:
        assert resolve_codec("H264") == "libx264"

    def test_unknown_codec_raises_capability_error(self) -> None:
        with pytest.raises(EncoderCapabilityError) as exc_info:
            resolve_codec("theora")
        assert exc_info.value.option == "codec"

    def test_empty_string_raises_capability_error(self) -> None:
        with pytest.raises(EncoderCapabilityError) as exc_info:
            resolve_codec("")
        assert exc_info.value.option == "codec"

    def test_codec_error_names_offending_value(self) -> None:
        with pytest.raises(EncoderCapabilityError) as exc_info:
            resolve_codec("mpeg4")
        assert "mpeg4" in str(exc_info.value)


# ---------------------------------------------------------------------------
# resolve_container
# ---------------------------------------------------------------------------


class TestResolveContainer:
    def test_mp4_resolves_to_mp4(self) -> None:
        assert resolve_container("mp4") == "mp4"

    def test_mkv_resolves_to_matroska(self) -> None:
        assert resolve_container("mkv") == "matroska"

    def test_webm_resolves_to_webm(self) -> None:
        assert resolve_container("webm") == "webm"

    def test_uppercase_normalised(self) -> None:
        assert resolve_container("MKV") == "matroska"

    def test_avi_raises_capability_error(self) -> None:
        with pytest.raises(EncoderCapabilityError) as exc_info:
            resolve_container("avi")
        assert exc_info.value.option == "container"

    def test_ts_raises_capability_error(self) -> None:
        with pytest.raises(EncoderCapabilityError) as exc_info:
            resolve_container("ts")
        assert exc_info.value.option == "container"

    def test_container_error_names_offending_value(self) -> None:
        with pytest.raises(EncoderCapabilityError) as exc_info:
            resolve_container("flv")
        assert "flv" in str(exc_info.value)


# ---------------------------------------------------------------------------
# ensure_filters_allowed
# ---------------------------------------------------------------------------


class TestEnsureFiltersAllowed:
    def test_all_allowed_filters_pass(self) -> None:
        allowed = [
            "deflicker",
            "drawtext",
            "scale",
            "fps",
            "format",
            "setpts",
            "overlay",
        ]
        ensure_filters_allowed(allowed)  # no exception

    def test_empty_list_passes(self) -> None:
        ensure_filters_allowed([])  # no exception

    def test_single_allowed_filter_passes(self) -> None:
        ensure_filters_allowed(["scale"])

    def test_disallowed_filter_raises_capability_error(self) -> None:
        with pytest.raises(EncoderCapabilityError) as exc_info:
            ensure_filters_allowed(["scale", "hflip"])
        assert exc_info.value.option == "filter"
        assert "hflip" in str(exc_info.value)

    def test_arbitrary_filter_raises_capability_error(self) -> None:
        with pytest.raises(EncoderCapabilityError) as exc_info:
            ensure_filters_allowed(["crop"])
        assert exc_info.value.option == "filter"

    def test_shell_injection_attempt_raises_capability_error(self) -> None:
        # A filter name that looks like a shell injection should be caught by the
        # allowlist.
        with pytest.raises(EncoderCapabilityError):
            ensure_filters_allowed(["scale;rm -rf /"])


# ---------------------------------------------------------------------------
# validate_fps
# ---------------------------------------------------------------------------


class TestValidateFps:
    def test_minimum_fps_passes(self) -> None:
        validate_fps(0.1)  # no exception

    def test_maximum_fps_passes(self) -> None:
        validate_fps(240.0)  # no exception

    def test_common_fps_24_passes(self) -> None:
        validate_fps(24.0)

    def test_common_fps_30_passes(self) -> None:
        validate_fps(30.0)

    def test_fps_below_minimum_raises(self) -> None:
        with pytest.raises(EncoderCapabilityError) as exc_info:
            validate_fps(0.09)
        assert exc_info.value.option == "fps"

    def test_fps_above_maximum_raises(self) -> None:
        with pytest.raises(EncoderCapabilityError) as exc_info:
            validate_fps(241.0)
        assert exc_info.value.option == "fps"

    def test_zero_fps_raises(self) -> None:
        with pytest.raises(EncoderCapabilityError):
            validate_fps(0.0)

    def test_negative_fps_raises(self) -> None:
        with pytest.raises(EncoderCapabilityError):
            validate_fps(-1.0)


# ---------------------------------------------------------------------------
# validate_dimensions
# ---------------------------------------------------------------------------


class TestValidateDimensions:
    def test_standard_1080p_passes(self) -> None:
        validate_dimensions(1920, 1080)  # no exception

    def test_minimum_even_dimensions_pass(self) -> None:
        validate_dimensions(2, 2)

    def test_maximum_dimensions_pass(self) -> None:
        validate_dimensions(16384, 16384)

    def test_odd_width_raises(self) -> None:
        with pytest.raises(EncoderCapabilityError) as exc_info:
            validate_dimensions(1921, 1080)
        assert exc_info.value.option == "width"

    def test_odd_height_raises(self) -> None:
        with pytest.raises(EncoderCapabilityError) as exc_info:
            validate_dimensions(1920, 1081)
        assert exc_info.value.option == "height"

    def test_width_below_minimum_raises(self) -> None:
        with pytest.raises(EncoderCapabilityError) as exc_info:
            validate_dimensions(0, 480)
        assert exc_info.value.option == "width"

    def test_height_below_minimum_raises(self) -> None:
        with pytest.raises(EncoderCapabilityError) as exc_info:
            validate_dimensions(640, 0)
        assert exc_info.value.option == "height"

    def test_width_above_maximum_raises(self) -> None:
        with pytest.raises(EncoderCapabilityError) as exc_info:
            validate_dimensions(16386, 1080)
        assert exc_info.value.option == "width"

    def test_height_above_maximum_raises(self) -> None:
        with pytest.raises(EncoderCapabilityError) as exc_info:
            validate_dimensions(1920, 16386)
        assert exc_info.value.option == "height"


# ---------------------------------------------------------------------------
# validate_crf
# ---------------------------------------------------------------------------


class TestValidateCrf:
    def test_minimum_crf_passes(self) -> None:
        validate_crf(0)  # no exception

    def test_maximum_crf_passes(self) -> None:
        validate_crf(63)  # no exception

    def test_common_crf_23_passes(self) -> None:
        validate_crf(23)

    def test_negative_crf_raises(self) -> None:
        with pytest.raises(EncoderCapabilityError) as exc_info:
            validate_crf(-1)
        assert exc_info.value.option == "crf"

    def test_crf_above_maximum_raises(self) -> None:
        with pytest.raises(EncoderCapabilityError) as exc_info:
            validate_crf(64)
        assert exc_info.value.option == "crf"


# ---------------------------------------------------------------------------
# validate_bitrate_kbps
# ---------------------------------------------------------------------------


class TestValidateBitrateKbps:
    def test_minimum_bitrate_passes(self) -> None:
        validate_bitrate_kbps(1)  # no exception

    def test_maximum_bitrate_passes(self) -> None:
        validate_bitrate_kbps(1_000_000)  # no exception

    def test_common_bitrate_5000_passes(self) -> None:
        validate_bitrate_kbps(5000)

    def test_zero_bitrate_raises(self) -> None:
        with pytest.raises(EncoderCapabilityError) as exc_info:
            validate_bitrate_kbps(0)
        assert exc_info.value.option == "bitrate_kbps"

    def test_negative_bitrate_raises(self) -> None:
        with pytest.raises(EncoderCapabilityError) as exc_info:
            validate_bitrate_kbps(-100)
        assert exc_info.value.option == "bitrate_kbps"

    def test_bitrate_above_maximum_raises(self) -> None:
        with pytest.raises(EncoderCapabilityError) as exc_info:
            validate_bitrate_kbps(1_000_001)
        assert exc_info.value.option == "bitrate_kbps"


# ---------------------------------------------------------------------------
# parse_filter_names / parse_filtergraph / parse_filter_options (#67 S4)
# ---------------------------------------------------------------------------


class TestParseFilterNames:
    def test_simple_chain(self) -> None:
        assert parse_filter_names("setpts=N/24/TB,fps=24,format=yuv420p") == [
            "setpts",
            "fps",
            "format",
        ]

    def test_filter_without_options(self) -> None:
        assert parse_filter_names("deflicker,hwupload") == ["deflicker", "hwupload"]

    def test_quoted_comma_and_semicolon_do_not_split(self) -> None:
        graph = "drawtext=text='a,b;c[d]e',scale=2:2"
        assert parse_filter_names(graph) == ["drawtext", "scale"]

    def test_backslash_escaped_quote_does_not_open_quoting(self) -> None:
        # Outside quotes "\'" is a literal quote, so the comma after it splits.
        graph = "drawtext=text=it\\'s,movie=x"
        assert parse_filter_names(graph) == ["drawtext", "movie"]

    def test_backslash_inside_quotes_is_literal(self) -> None:
        # Inside quotes a backslash does not escape: "\'" closes the quote, so
        # the old timestamp escaping let the next comma start a new filter.
        graph = "drawtext=text='x\\',movie=/etc/passwd'"
        assert parse_filter_names(graph) == ["drawtext", "movie"]

    def test_escaped_separator_outside_quotes(self) -> None:
        assert parse_filter_names("drawtext=text=a\\,b,fps=1") == ["drawtext", "fps"]

    def test_pad_labels_are_stripped(self) -> None:
        graph = "[0:v]setpts=PTS/TB,fps=24[v];[v][1:v]overlay=10:10[out]"
        assert parse_filter_names(graph) == ["setpts", "fps", "overlay"]

    def test_unquoted_bracket_ends_the_options(self) -> None:
        # "[a]" after options is an output label, then ";" starts a chain.
        assert parse_filter_names("drawtext=text=x[a];movie=y") == [
            "drawtext",
            "movie",
        ]

    def test_quoted_filter_name_is_unquoted(self) -> None:
        assert parse_filter_names("'movie'=x") == ["movie"]

    def test_instance_name_is_kept(self) -> None:
        assert parse_filter_names("drawtext@x=text=a") == ["drawtext@x"]

    def test_whitespace_around_filters(self) -> None:
        assert parse_filter_names("  fps=1 ,\n scale=2:2 ") == ["fps", "scale"]

    @pytest.mark.parametrize(
        "graph",
        [
            "",
            "   ",
            ",fps=1",
            "fps=1,,scale=2:2",
            "[v]",
            "[]fps=1",
            "[v fps=1",
            "fps=1[out]x",
            "fps=1]",
        ],
    )
    def test_unparseable_graph_raises(self, graph: str) -> None:
        with pytest.raises(EncoderError, match="unparseable filtergraph"):
            parse_filter_names(graph)

    def test_options_are_unquoted_once(self) -> None:
        (parsed,) = parse_filtergraph("drawtext=fontfile='/a\\:b':text='%{pts\\:x}'")
        assert parsed.args == "fontfile=/a\\:b:text=%{pts\\:x}"


class TestParseFilterOptions:
    def test_keyed_options(self) -> None:
        assert parse_filter_options("fontfile=/a\\:b:text=%{pts\\:x}:x=1") == [
            ("fontfile", "/a:b"),
            ("text", "%{pts:x}"),
            ("x", "1"),
        ]

    def test_shorthand_options_have_no_key(self) -> None:
        assert parse_filter_options("640:480") == [(None, "640"), (None, "480")]

    def test_escaped_colon_stays_in_the_value(self) -> None:
        assert parse_filter_options("text=a\\:textfile=/etc/passwd") == [
            ("text", "a:textfile=/etc/passwd")
        ]

    def test_unescaped_colon_starts_a_new_option(self) -> None:
        assert parse_filter_options("text=a:textfile=/etc/passwd") == [
            ("text", "a"),
            ("textfile", "/etc/passwd"),
        ]

    def test_empty_args(self) -> None:
        assert parse_filter_options("") == []
