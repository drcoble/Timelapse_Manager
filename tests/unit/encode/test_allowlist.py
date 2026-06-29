"""Unit tests for the encode allowlist: codec, container, filter validation."""

from __future__ import annotations

import pytest

from timelapse_manager.encode.allowlist import (
    ensure_filters_allowed,
    resolve_codec,
    resolve_container,
    validate_bitrate_kbps,
    validate_crf,
    validate_dimensions,
    validate_fps,
)
from timelapse_manager.encode.encoder import EncoderCapabilityError

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
