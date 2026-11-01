"""Frame integrity checks on captured image bytes (#69 CAM-R11).

A body cut short in transfer, or one whose dimensions cannot be read, must fail
the capture instead of being written as a frame. The HTTP snapshot path
(``frame_from_bytes``, shared by the HTTP, VAPIX and ONVIF adapters) and the
RTSP path share one helper.
"""

from __future__ import annotations

import struct
from typing import Any

import pytest

from tests.conftest import make_jpeg
from timelapse_manager.cameras import _imageinfo
from timelapse_manager.cameras.base import OtherCaptureError
from timelapse_manager.cameras.http_jpeg import frame_from_bytes
from timelapse_manager.cameras.rtsp import RtspAdapter


def _png(width: int, height: int) -> bytes:
    ihdr = struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(ihdr))
        + b"IHDR"
        + ihdr
        + b"\x00\x00\x00\x00"
    )


class TestFrameFromBytes:
    def test_whole_jpeg_is_accepted(self) -> None:
        frame = frame_from_bytes(make_jpeg(64, 48))
        assert (frame.width, frame.height) == (64, 48)
        assert frame.capture_notes == []

    def test_padding_after_eoi_is_tolerated(self) -> None:
        frame = frame_from_bytes(make_jpeg(64, 48) + b"\x00" * 8)
        assert (frame.width, frame.height) == (64, 48)

    def test_bare_soi_is_rejected(self) -> None:
        with pytest.raises(OtherCaptureError, match="truncated or undecodable"):
            frame_from_bytes(b"\xff\xd8")

    def test_jpeg_truncated_before_eoi_is_rejected(self) -> None:
        whole = make_jpeg(64, 48)
        truncated = whole[:-2] + b"\x00" * 20  # EOI lost, then more bytes
        with pytest.raises(OtherCaptureError, match="end-of-image") as info:
            frame_from_bytes(truncated)
        # The message carries the size that arrived, for the failure log.
        assert f"{len(truncated)} bytes" in str(info.value)

    def test_jpeg_without_dimensions_is_rejected(self) -> None:
        no_sof = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 16 + b"\xff\xd9"
        with pytest.raises(OtherCaptureError, match="no readable dimensions"):
            frame_from_bytes(no_sof)

    def test_zero_by_zero_png_is_rejected(self) -> None:
        with pytest.raises(OtherCaptureError, match="zero dimension 0x0"):
            frame_from_bytes(_png(0, 0))

    def test_zero_width_jpeg_is_rejected(self) -> None:
        with pytest.raises(OtherCaptureError, match="zero dimension"):
            frame_from_bytes(make_jpeg(0, 48))

    def test_png_with_dimensions_is_accepted(self) -> None:
        frame = frame_from_bytes(_png(4, 3))
        assert (frame.format, frame.width, frame.height) == ("png", 4, 3)


class TestHasJpegEoi:
    def test_eoi_at_end(self) -> None:
        assert _imageinfo.has_jpeg_eoi(make_jpeg(1, 1))

    def test_eoi_inside_window(self) -> None:
        padded = make_jpeg(1, 1) + b"\x00" * (_imageinfo.JPEG_EOI_WINDOW - 2)
        assert _imageinfo.has_jpeg_eoi(padded)

    def test_eoi_outside_window(self) -> None:
        padded = make_jpeg(1, 1) + b"\x00" * _imageinfo.JPEG_EOI_WINDOW
        assert not _imageinfo.has_jpeg_eoi(padded)

    def test_empty(self) -> None:
        assert not _imageinfo.has_jpeg_eoi(b"")


class _FakeFfmpeg:
    """A stand-in ffmpeg process whose stdout is the given bytes."""

    def __init__(self, stdout: bytes) -> None:
        self._stdout = stdout
        self.returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b""

    def kill(self) -> None:  # pragma: no cover - not reached
        return None

    async def wait(self) -> int:  # pragma: no cover - not reached
        return 0


class TestRtspFrameIntegrity:
    """The RTSP frame build shares the helper."""

    @pytest.fixture()
    def _allow_private(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        ssrf = SimpleNamespace(allowed_private_subnets=["10.0.0.0/8"])
        ctx = SimpleNamespace(settings=SimpleNamespace(ssrf=ssrf))
        monkeypatch.setattr("timelapse_manager.runtime.get_context", lambda: ctx)

    async def _capture(self, monkeypatch: pytest.MonkeyPatch, stdout: bytes) -> Any:
        async def _spawn(*_args: Any, **_kwargs: Any) -> _FakeFfmpeg:
            return _FakeFfmpeg(stdout)

        monkeypatch.setattr(
            "timelapse_manager.cameras.rtsp.asyncio.create_subprocess_exec", _spawn
        )
        return await RtspAdapter("rtsp://10.0.0.5/stream").capture()

    @pytest.mark.usefixtures("_allow_private")
    async def test_whole_frame_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        frame = await self._capture(monkeypatch, make_jpeg(32, 24))
        assert (frame.width, frame.height) == (32, 24)

    @pytest.mark.usefixtures("_allow_private")
    async def test_truncated_frame_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(OtherCaptureError, match="truncated or undecodable"):
            await self._capture(monkeypatch, make_jpeg(32, 24)[:-2])
