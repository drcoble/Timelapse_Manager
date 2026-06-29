"""Unit tests for browser streamability determination."""

from __future__ import annotations

from timelapse_manager.encode.browser_streamable import is_browser_streamable


class TestIsBrowserStreamable:
    # ---------------------------------------------------------------------------
    # Streamable pairs
    # ---------------------------------------------------------------------------

    def test_h264_mp4_is_streamable(self) -> None:
        assert is_browser_streamable("h264", "mp4") is True

    def test_libx264_alias_mp4_is_streamable(self) -> None:
        assert is_browser_streamable("libx264", "mp4") is True

    # ---------------------------------------------------------------------------
    # Non-streamable pairs
    # ---------------------------------------------------------------------------

    def test_vp9_webm_is_not_streamable(self) -> None:
        assert is_browser_streamable("vp9", "webm") is False

    def test_libvpx_vp9_alias_webm_is_not_streamable(self) -> None:
        assert is_browser_streamable("libvpx-vp9", "webm") is False

    def test_h265_mp4_is_not_streamable(self) -> None:
        assert is_browser_streamable("h265", "mp4") is False

    def test_hevc_alias_mp4_is_not_streamable(self) -> None:
        assert is_browser_streamable("hevc", "mp4") is False

    def test_libx265_alias_mp4_is_not_streamable(self) -> None:
        assert is_browser_streamable("libx265", "mp4") is False

    def test_h264_mkv_is_not_streamable(self) -> None:
        assert is_browser_streamable("h264", "mkv") is False

    def test_h264_webm_is_not_streamable(self) -> None:
        assert is_browser_streamable("h264", "webm") is False

    def test_vp9_mp4_is_not_streamable(self) -> None:
        assert is_browser_streamable("vp9", "mp4") is False

    # ---------------------------------------------------------------------------
    # Case normalisation
    # ---------------------------------------------------------------------------

    def test_uppercase_codec_normalised(self) -> None:
        assert is_browser_streamable("H264", "mp4") is True

    def test_uppercase_container_normalised(self) -> None:
        assert is_browser_streamable("h264", "MP4") is True

    def test_both_uppercase_normalised(self) -> None:
        assert is_browser_streamable("H264", "MP4") is True

    # ---------------------------------------------------------------------------
    # Unknown / unsupported
    # ---------------------------------------------------------------------------

    def test_unknown_codec_is_not_streamable(self) -> None:
        assert is_browser_streamable("av1", "mp4") is False

    def test_unknown_container_is_not_streamable(self) -> None:
        assert is_browser_streamable("h264", "avi") is False

    def test_both_unknown_is_not_streamable(self) -> None:
        assert is_browser_streamable("mpeg4", "avi") is False

    def test_empty_codec_is_not_streamable(self) -> None:
        assert is_browser_streamable("", "mp4") is False

    def test_empty_container_is_not_streamable(self) -> None:
        assert is_browser_streamable("h264", "") is False
