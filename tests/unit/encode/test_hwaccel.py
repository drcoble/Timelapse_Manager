"""Unit tests for the hardware-encoder probing and selection module.

All tests are pure: they do not spawn any subprocess, touch the filesystem,
or require a GPU.  The injection seam (``available_hw_encoders`` kwarg on
``FfmpegEncoder``) makes every code path reachable without real hardware.
"""

from __future__ import annotations

import pytest

from timelapse_manager.encode.encoder import EncoderCapabilityError
from timelapse_manager.encode.hwaccel import (
    ALL_HW_ENCODERS,
    EncoderChoice,
    parse_hw_encoders,
    resolve_encoder,
)

# ---------------------------------------------------------------------------
# parse_hw_encoders
# ---------------------------------------------------------------------------

# Realistic sample of ``ffmpeg -hide_banner -encoders`` stdout that contains a
# mix of software encoders, hardware encoders this module knows, and hardware
# encoders this module does not know -- so we can assert only the right subset
# is returned.
_ENCODERS_SAMPLE = (
    "Encoders:\n"
    " V..... = Video\n"
    " .A.... = Audio\n"
    " ..S... = Subtitle\n"
    " ...X.. = Codec is experimental\n"
    " ....B. = Supports draw_horiz_band\n"
    " .....D = Supports direct rendering method 1\n"
    " ------\n"
    " V..... libx264              libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10\n"
    " V..... libx265              libx265 H.265 / HEVC\n"
    " V..... libsvtav1            SVT-AV1(Scalable Video Technology for AV1) encoder\n"
    " V..... libvpx-vp9           libvpx VP9\n"
    " V....D h264_nvenc           NVIDIA NVENC H.264 encoder\n"
    " V....D hevc_nvenc           NVIDIA NVENC hevc encoder\n"
    " V....D h264_qsv             H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10"
    " (Intel Quick Sync Video acceleration)\n"
    " V....D hevc_qsv             HEVC (Intel Quick Sync Video acceleration)\n"
    " V....D av1_qsv              AV1 (Intel Quick Sync Video acceleration)\n"
    " V....D h264_vaapi           H.264/AVC (VAAPI)\n"
    " V....D hevc_vaapi           HEVC (VAAPI)\n"
    " V....D av1_vaapi            AV1 (VAAPI)\n"
    " V....D h264_amf             AMD AMF H.264 encoder\n"
    " V....D hevc_amf             AMD AMF HEVC encoder\n"
)


class TestParseHwEncoders:
    def test_returns_all_known_hw_encoders_when_all_present(self) -> None:
        result = parse_hw_encoders(_ENCODERS_SAMPLE)
        assert result == ALL_HW_ENCODERS

    def test_excludes_software_encoders(self) -> None:
        result = parse_hw_encoders(_ENCODERS_SAMPLE)
        assert "libx264" not in result
        assert "libx265" not in result
        assert "libvpx-vp9" not in result
        assert "libsvtav1" not in result

    def test_excludes_unknown_hw_encoders(self) -> None:
        # h264_amf and hevc_amf are real ffmpeg encoders but not in _HW_ENCODERS
        result = parse_hw_encoders(_ENCODERS_SAMPLE)
        assert "h264_amf" not in result
        assert "hevc_amf" not in result

    def test_partial_output_returns_subset(self) -> None:
        partial = """\
 V....D h264_nvenc           NVIDIA NVENC H.264 encoder
 V....D h264_vaapi           H.264/AVC (VAAPI)
"""
        result = parse_hw_encoders(partial)
        assert result == frozenset({"h264_nvenc", "h264_vaapi"})

    def test_empty_output_returns_empty_set(self) -> None:
        assert parse_hw_encoders("") == frozenset()

    def test_only_header_lines_returns_empty(self) -> None:
        header_only = """\
Encoders:
 V..... = Video
 .A.... = Audio
 ------
"""
        assert parse_hw_encoders(header_only) == frozenset()

    def test_flag_block_must_be_exactly_six_chars(self) -> None:
        # A line with a 5-char flag block must not be parsed as an encoder row.
        ambiguous = " VAAAA h264_nvenc something\n"
        result = parse_hw_encoders(ambiguous)
        assert "h264_nvenc" not in result

    def test_return_type_is_frozenset(self) -> None:
        result = parse_hw_encoders(_ENCODERS_SAMPLE)
        assert isinstance(result, frozenset)


# ---------------------------------------------------------------------------
# resolve_encoder — decision table
# ---------------------------------------------------------------------------

# The full set of known hardware encoders, used when a test needs "all
# available" without listing them by name.
_ALL_AVAILABLE = ALL_HW_ENCODERS


class TestResolveEncoderSoftwarePath:
    def test_disabled_returns_software_no_reason(self) -> None:
        choice = resolve_encoder(
            "h264",
            hwaccel_enabled=False,
            hwaccel_api="nvenc",
            available=_ALL_AVAILABLE,
        )
        assert choice.encoder_name == "libx264"
        assert choice.hwaccel_api is None
        assert choice.fallback_reason is None
        assert not choice.is_hardware

    def test_disabled_vp9_returns_software(self) -> None:
        choice = resolve_encoder(
            "vp9",
            hwaccel_enabled=False,
            hwaccel_api="nvenc",
            available=_ALL_AVAILABLE,
        )
        assert choice.encoder_name == "libvpx-vp9"
        assert not choice.is_hardware

    def test_disabled_av1_returns_software(self) -> None:
        choice = resolve_encoder(
            "av1",
            hwaccel_enabled=False,
            hwaccel_api="nvenc",
            available=_ALL_AVAILABLE,
        )
        assert choice.encoder_name == "libsvtav1"
        assert not choice.is_hardware

    def test_unknown_codec_raises(self) -> None:
        with pytest.raises(EncoderCapabilityError, match="unsupported codec"):
            resolve_encoder(
                "notacodec",
                hwaccel_enabled=False,
                hwaccel_api=None,
                available=frozenset(),
            )

    def test_unknown_codec_raises_even_with_hw_enabled(self) -> None:
        with pytest.raises(EncoderCapabilityError):
            resolve_encoder(
                "notacodec",
                hwaccel_enabled=True,
                hwaccel_api="nvenc",
                available=_ALL_AVAILABLE,
            )


class TestResolveEncoderUnknownApi:
    def test_unknown_api_falls_back_with_reason(self) -> None:
        choice = resolve_encoder(
            "h264",
            hwaccel_enabled=True,
            hwaccel_api="unknown_api",
            available=_ALL_AVAILABLE,
        )
        assert not choice.is_hardware
        assert choice.fallback_reason is not None
        assert "unknown_api" in choice.fallback_reason

    def test_none_api_falls_back_with_reason(self) -> None:
        choice = resolve_encoder(
            "h264",
            hwaccel_enabled=True,
            hwaccel_api=None,
            available=_ALL_AVAILABLE,
        )
        assert not choice.is_hardware
        assert choice.fallback_reason is not None


class TestResolveEncoderNvenc:
    def test_h264_nvenc_selects_hardware(self) -> None:
        choice = resolve_encoder(
            "h264",
            hwaccel_enabled=True,
            hwaccel_api="nvenc",
            available=frozenset({"h264_nvenc"}),
        )
        assert choice.encoder_name == "h264_nvenc"
        assert choice.hwaccel_api == "nvenc"
        assert choice.is_hardware
        assert choice.fallback_reason is None

    def test_h265_nvenc_selects_hardware(self) -> None:
        choice = resolve_encoder(
            "h265",
            hwaccel_enabled=True,
            hwaccel_api="nvenc",
            available=frozenset({"hevc_nvenc"}),
        )
        assert choice.encoder_name == "hevc_nvenc"
        assert choice.is_hardware

    def test_vp9_nvenc_falls_back_software(self) -> None:
        # VP9 has no hardware encoder on any API.
        choice = resolve_encoder(
            "vp9",
            hwaccel_enabled=True,
            hwaccel_api="nvenc",
            available=_ALL_AVAILABLE,
        )
        assert choice.encoder_name == "libvpx-vp9"
        assert not choice.is_hardware
        assert choice.fallback_reason is not None

    def test_av1_nvenc_falls_back_software(self) -> None:
        # AV1 has no NVENC encoder -- the NVENC map has h264/h265 only.
        choice = resolve_encoder(
            "av1",
            hwaccel_enabled=True,
            hwaccel_api="nvenc",
            available=_ALL_AVAILABLE,
        )
        assert choice.encoder_name == "libsvtav1"
        assert not choice.is_hardware
        assert choice.fallback_reason is not None

    def test_missing_nvenc_encoder_falls_back(self) -> None:
        # Even though nvenc is the requested API, the encoder is not available
        # in this ffmpeg build.
        choice = resolve_encoder(
            "h264",
            hwaccel_enabled=True,
            hwaccel_api="nvenc",
            available=frozenset(),  # nothing available
        )
        assert choice.encoder_name == "libx264"
        assert not choice.is_hardware
        assert choice.fallback_reason is not None
        assert "h264_nvenc" in choice.fallback_reason


class TestResolveEncoderQsv:
    def test_h264_qsv_selects_hardware(self) -> None:
        choice = resolve_encoder(
            "h264",
            hwaccel_enabled=True,
            hwaccel_api="qsv",
            available=frozenset({"h264_qsv"}),
        )
        assert choice.encoder_name == "h264_qsv"
        assert choice.hwaccel_api == "qsv"
        assert choice.is_hardware

    def test_av1_qsv_selects_hardware(self) -> None:
        choice = resolve_encoder(
            "av1",
            hwaccel_enabled=True,
            hwaccel_api="qsv",
            available=frozenset({"av1_qsv"}),
        )
        assert choice.encoder_name == "av1_qsv"
        assert choice.is_hardware

    def test_vp9_qsv_falls_back_software(self) -> None:
        choice = resolve_encoder(
            "vp9",
            hwaccel_enabled=True,
            hwaccel_api="qsv",
            available=_ALL_AVAILABLE,
        )
        assert choice.encoder_name == "libvpx-vp9"
        assert not choice.is_hardware

    def test_missing_qsv_encoder_falls_back(self) -> None:
        choice = resolve_encoder(
            "h264",
            hwaccel_enabled=True,
            hwaccel_api="qsv",
            available=frozenset(),
        )
        assert not choice.is_hardware
        assert choice.fallback_reason is not None


class TestResolveEncoderVaapi:
    def test_h264_vaapi_selects_hardware(self) -> None:
        choice = resolve_encoder(
            "h264",
            hwaccel_enabled=True,
            hwaccel_api="vaapi",
            available=frozenset({"h264_vaapi"}),
        )
        assert choice.encoder_name == "h264_vaapi"
        assert choice.hwaccel_api == "vaapi"
        assert choice.is_hardware

    def test_av1_vaapi_selects_hardware(self) -> None:
        choice = resolve_encoder(
            "av1",
            hwaccel_enabled=True,
            hwaccel_api="vaapi",
            available=frozenset({"av1_vaapi"}),
        )
        assert choice.encoder_name == "av1_vaapi"
        assert choice.is_hardware

    def test_vp9_vaapi_falls_back_software(self) -> None:
        choice = resolve_encoder(
            "vp9",
            hwaccel_enabled=True,
            hwaccel_api="vaapi",
            available=_ALL_AVAILABLE,
        )
        assert choice.encoder_name == "libvpx-vp9"
        assert not choice.is_hardware

    def test_missing_vaapi_encoder_falls_back(self) -> None:
        choice = resolve_encoder(
            "h264",
            hwaccel_enabled=True,
            hwaccel_api="vaapi",
            available=frozenset(),
        )
        assert not choice.is_hardware
        assert choice.fallback_reason is not None


# ---------------------------------------------------------------------------
# EncoderChoice dataclass
# ---------------------------------------------------------------------------


class TestEncoderChoice:
    def test_is_hardware_true_when_api_set(self) -> None:
        choice = EncoderChoice(encoder_name="h264_nvenc", hwaccel_api="nvenc")
        assert choice.is_hardware is True

    def test_is_hardware_false_when_api_none(self) -> None:
        choice = EncoderChoice(encoder_name="libx264", hwaccel_api=None)
        assert choice.is_hardware is False

    def test_fallback_reason_defaults_to_none(self) -> None:
        choice = EncoderChoice(encoder_name="libx264", hwaccel_api=None)
        assert choice.fallback_reason is None

    def test_frozen_rejects_mutation(self) -> None:
        choice = EncoderChoice(encoder_name="libx264", hwaccel_api=None)
        with pytest.raises((AttributeError, TypeError)):
            choice.encoder_name = "libx265"  # type: ignore[misc]
