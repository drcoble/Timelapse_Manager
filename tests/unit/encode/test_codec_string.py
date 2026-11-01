"""Unit tests for the RFC 6381 codec-string rules (#25).

The table cases run against ffprobe output recorded from the pinned n9 FFmpeg
build for every software encoder the app offers
(``tests/fixtures/ffprobe/n9_streams.json``). The runner has no hardware HEVC
encoder, so those cases are hand-made from the documented output shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from timelapse_manager.encode.codec_string import (
    codec_label,
    codec_string,
    container_label,
    media_type_for_container,
    media_type_for_path,
    playback_label,
)

_FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures" / "ffprobe" / "n9_streams.json"
)


def _recorded() -> dict[str, dict[str, Any]]:
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert data["ffmpeg_version"].startswith("n9.")
    streams: dict[str, dict[str, Any]] = data["streams"]
    return streams


# encoder/container[/hvc1] -> the codec string the browser is asked about.
_EXPECTED: dict[str, str] = {
    "libx264/mp4": "avc1.64001F",
    "libx264/mkv": "avc1.64001F",
    "libx265/mp4/hvc1": "hvc1.1.6.L93.B0",
    "libx265/mp4": "hev1.1.6.L93.B0",
    "libvpx-vp9/webm": "vp09.00.10.08",
    "libvpx-vp9/mp4": "vp09.00.10.08",
    "libsvtav1/mp4": "av01.0.05M.08",
    "libsvtav1/mkv": "av01.0.05M.08",
}


class TestRecordedN9Output:
    def test_fixture_covers_every_offered_encoder(self) -> None:
        encoders = {key.split("/")[0] for key in _recorded()}
        assert encoders == {"libx264", "libx265", "libvpx-vp9", "libsvtav1"}
        assert set(_recorded()) == set(_EXPECTED)

    @pytest.mark.parametrize("key", sorted(_EXPECTED))
    def test_recorded_stream_maps_to_codec_string(self, key: str) -> None:
        assert codec_string(_recorded()[key]) == _EXPECTED[key]


class TestH264:
    @pytest.mark.parametrize(
        ("profile", "level", "expected"),
        [
            ("High", 31, "avc1.64001F"),
            ("High", 40, "avc1.640028"),
            ("Main", 30, "avc1.4D001E"),
            ("Constrained Baseline", 30, "avc1.42E01E"),
            ("Baseline", 21, "avc1.420015"),
            ("High 10", 51, "avc1.6E0033"),
        ],
    )
    def test_profile_and_level(self, profile: str, level: int, expected: str) -> None:
        stream = {"codec_name": "h264", "profile": profile, "level": level}
        assert codec_string(stream) == expected

    def test_unknown_profile_is_none(self) -> None:
        assert (
            codec_string({"codec_name": "h264", "profile": "Odd", "level": 31}) is None
        )

    def test_missing_level_is_none(self) -> None:
        assert codec_string({"codec_name": "h264", "profile": "High"}) is None


class TestHevc:
    def test_hardware_encoder_tagged_hvc1(self) -> None:
        # Hand-made: hevc_nvenc/qsv/vaapi report Main with a level_idc, and with
        # -tag:v hvc1 the stream carries the hvc1 tag.
        stream = {
            "codec_name": "hevc",
            "profile": "Main",
            "level": 120,
            "pix_fmt": "yuv420p",
            "codec_tag_string": "hvc1",
        }
        assert codec_string(stream) == "hvc1.1.6.L120.B0"

    def test_main10(self) -> None:
        stream = {
            "codec_name": "hevc",
            "profile": "Main 10",
            "level": 150,
            "codec_tag_string": "hvc1",
        }
        assert codec_string(stream) == "hvc1.2.4.L150.B0"

    def test_untagged_stream_in_mkv_uses_hvc1(self) -> None:
        stream = {
            "codec_name": "hevc",
            "profile": "Main",
            "level": 93,
            "codec_tag_string": "[0][0][0][0]",
        }
        assert codec_string(stream) == "hvc1.1.6.L93.B0"


class TestVp9AndAv1:
    def test_vp9_known_level_is_used(self) -> None:
        stream = {"codec_name": "vp9", "profile": "Profile 0", "level": 31}
        assert codec_string(stream) == "vp09.00.31.08"

    def test_vp9_10_bit(self) -> None:
        stream = {
            "codec_name": "vp9",
            "profile": "Profile 2",
            "level": -99,
            "pix_fmt": "yuv420p10le",
        }
        assert codec_string(stream) == "vp09.02.10.10"

    def test_av1_level_is_two_digits(self) -> None:
        stream = {"codec_name": "av1", "profile": "Main", "level": 12}
        assert codec_string(stream) == "av01.0.12M.08"

    def test_av1_unknown_level_is_none(self) -> None:
        stream = {"codec_name": "av1", "profile": "Main", "level": -99}
        assert codec_string(stream) is None


class TestUnknown:
    @pytest.mark.parametrize(
        "stream",
        [
            {},
            {"codec_name": "mpeg4", "profile": "Simple Profile", "level": 1},
            {"codec_name": "h264", "profile": "High", "level": "not-a-number"},
            {"codec_name": "vp9", "profile": "Profile x", "level": 10},
            {"codec_name": "h264", "profile": "High", "level": True},
        ],
    )
    def test_unrecognised_or_malformed_is_none(self, stream: dict[str, Any]) -> None:
        assert codec_string(stream) is None


class TestMediaTypes:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("render-1.mp4", "video/mp4"),
            ("render-1.MP4", "video/mp4"),
            ("render-1.webm", "video/webm"),
            ("render-1.mkv", "video/x-matroska"),
            ("render-1.zip", "application/octet-stream"),
        ],
    )
    def test_path(self, name: str, expected: str) -> None:
        assert media_type_for_path(Path("/renders") / name) == expected

    def test_container(self) -> None:
        assert media_type_for_container("webm") == "video/webm"
        assert media_type_for_container(None) is None
        assert media_type_for_container("avi") is None

    def test_codec_label(self) -> None:
        assert codec_label("libsvtav1") == "AV1"
        assert codec_label("hevc") == "H.265"
        assert codec_label(None) == "this codec"
        assert codec_label("prores") == "prores"

    @pytest.mark.parametrize(
        ("container", "expected"),
        [
            ("mp4", "MP4"),
            ("mkv", "MKV"),
            ("webm", "WebM"),
            ("WEBM", "WebM"),
            (".mkv", "MKV"),
            ("avi", "AVI"),
            ("", None),
            (".", None),
            (None, None),
        ],
    )
    def test_container_label(self, container: str | None, expected: str | None) -> None:
        assert container_label(container) == expected

    @pytest.mark.parametrize(
        ("codec", "container", "expected"),
        [
            # #36: Safari plays H.264 in MP4 but not in MKV; name both.
            ("libx264", "mkv", "H.264 in MKV"),
            ("h264", ".mp4", "H.264 in MP4"),
            ("libvpx-vp9", "webm", "VP9 in WebM"),
            ("libsvtav1", "mp4", "AV1 in MP4"),
            ("libx265", "mkv", "H.265 in MKV"),
            # No container known: the codec alone, as before.
            ("libsvtav1", None, "AV1"),
            ("libsvtav1", "", "AV1"),
            (None, "mkv", "this codec in MKV"),
        ],
    )
    def test_playback_label(
        self, codec: str | None, container: str | None, expected: str
    ) -> None:
        assert playback_label(codec, container) == expected
