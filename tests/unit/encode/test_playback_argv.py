"""Argv tests for the browser-playback encode flags (#25).

``-movflags +faststart`` on every MP4 output, and ``-tag:v hvc1`` for every
HEVC encoder (software and hardware) in MP4. ``_build_argv`` is called
directly, so no ffmpeg is spawned; the hardware path is reached through the
``available_hw_encoders`` injection seam.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.encode.test_hwaccel_argv import _build_spec, _call_build_argv
from timelapse_manager.encode import ffmpeg_impl
from timelapse_manager.encode.allowlist import CONTAINER_MUXERS, HW_ENCODER_CODECS
from timelapse_manager.encode.codec_string import CONTAINER_MEDIA_TYPES
from timelapse_manager.encode.ffmpeg_impl import FfmpegEncoder


def _argv(
    tmp_path: Path,
    *,
    codec: str,
    container: str,
    hw_encoder: str | None = None,
) -> list[str]:
    out = tmp_path / f"out.{container}"
    spec = _build_spec(tmp_path, out, codec=codec, container=container)
    if hw_encoder is None:
        enc = FfmpegEncoder(hwaccel_enabled=False)
    else:
        enc = FfmpegEncoder(
            hwaccel_enabled=True,
            hwaccel_api=hw_encoder.rsplit("_", 1)[1],
            available_hw_encoders=frozenset({hw_encoder}),
        )
    return _call_build_argv(spec, tmp_path, enc)


def _flag(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


class TestFaststart:
    def test_faststart_muxers_are_allowlisted_containers_with_a_mime(self) -> None:
        """No dead entries: every faststart muxer is one the app can produce
        and serve (MP4 today; there is no MOV container)."""
        muxers = ffmpeg_impl._FASTSTART_MUXERS
        assert muxers == {"mp4"}
        containers = {c for c, m in CONTAINER_MUXERS.items() if m in muxers}
        assert containers and containers <= set(CONTAINER_MEDIA_TYPES)

    @pytest.mark.parametrize("codec", ["h264", "h265", "av1"])
    def test_every_mp4_gets_faststart(self, tmp_path: Path, codec: str) -> None:
        argv = _argv(tmp_path, codec=codec, container="mp4")
        assert _flag(argv, "-movflags") == "+faststart"
        # An output option: it sits before the muxer and the output path.
        assert argv.index("-movflags") < argv.index("-f", argv.index("-c:v"))

    @pytest.mark.parametrize(
        ("codec", "container"), [("h264", "mkv"), ("vp9", "webm"), ("av1", "mkv")]
    )
    def test_other_containers_get_no_movflags(
        self, tmp_path: Path, codec: str, container: str
    ) -> None:
        argv = _argv(tmp_path, codec=codec, container=container)
        assert "-movflags" not in argv
        assert "-tag:v" not in argv


class TestHvc1Tag:
    def test_libx265_in_mp4_is_tagged_hvc1(self, tmp_path: Path) -> None:
        argv = _argv(tmp_path, codec="h265", container="mp4")
        assert _flag(argv, "-c:v") == "libx265"
        assert _flag(argv, "-tag:v") == "hvc1"

    def test_libx265_in_mkv_is_not_tagged(self, tmp_path: Path) -> None:
        argv = _argv(tmp_path, codec="h265", container="mkv")
        assert "-tag:v" not in argv

    @pytest.mark.parametrize("codec", ["h264", "av1"])
    def test_non_hevc_mp4_is_not_tagged(self, tmp_path: Path, codec: str) -> None:
        assert "-tag:v" not in _argv(tmp_path, codec=codec, container="mp4")

    @pytest.mark.parametrize(
        "hw_encoder",
        sorted(name for name, codec in HW_ENCODER_CODECS.items() if codec == "h265"),
    )
    def test_every_hardware_hevc_encoder_in_mp4_is_tagged(
        self, tmp_path: Path, hw_encoder: str
    ) -> None:
        argv = _argv(tmp_path, codec="h265", container="mp4", hw_encoder=hw_encoder)
        assert _flag(argv, "-c:v") == hw_encoder
        assert _flag(argv, "-tag:v") == "hvc1"
        assert _flag(argv, "-movflags") == "+faststart"

    def test_hardware_h264_in_mp4_is_not_tagged(self, tmp_path: Path) -> None:
        argv = _argv(tmp_path, codec="h264", container="mp4", hw_encoder="h264_nvenc")
        assert _flag(argv, "-c:v") == "h264_nvenc"
        assert "-tag:v" not in argv
        assert _flag(argv, "-movflags") == "+faststart"
