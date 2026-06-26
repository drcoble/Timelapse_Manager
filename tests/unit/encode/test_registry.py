"""Unit tests for the encoder engine-selection seam."""

from __future__ import annotations

import pytest

from timelapse_manager.encode import EncoderError, FfmpegEncoder, build_encoder


class TestBuildEncoder:
    def test_default_resolves_to_ffmpeg(self) -> None:
        assert isinstance(build_encoder("ffmpeg"), FfmpegEncoder)

    def test_none_resolves_to_ffmpeg(self) -> None:
        assert isinstance(build_encoder(None), FfmpegEncoder)

    def test_name_is_case_insensitive(self) -> None:
        assert isinstance(build_encoder("FFmpeg"), FfmpegEncoder)

    def test_unknown_engine_fails_loudly(self) -> None:
        with pytest.raises(EncoderError) as excinfo:
            build_encoder("gstreamer")
        # The error names the offending engine and the supported set.
        assert "gstreamer" in str(excinfo.value)
        assert "ffmpeg" in str(excinfo.value)

    def test_passes_ffmpeg_binary_and_font(self) -> None:
        enc = build_encoder(
            "ffmpeg", ffmpeg_binary="/opt/ff/ffmpeg", font_path="/f.ttf"
        )
        assert isinstance(enc, FfmpegEncoder)
