"""Thumbnail generation refuses a dimension-bomb frame before spawning ffmpeg.

The thumbnail step decodes a frame into a pixel buffer; a header declaring an
enormous width/height would drive a multi-gigabyte allocation. These tests pin
that :func:`generate_thumbnail` reads the cheap header and refuses an
over-ceiling frame *before* ffmpeg is ever invoked, while a normal frame still
reaches the ffmpeg call.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import pytest

from timelapse_manager.cameras._imageinfo import ImageTooLargeError
from timelapse_manager.encode import thumbnail


def _png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


@pytest.fixture()
def no_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Replace subprocess.run with a recorder that must not fire on a bomb."""
    calls: list[list[str]] = []

    def _record(args: list[str], *a: Any, **k: Any) -> Any:
        calls.append(args)
        # A real run would write dest_path; tests that expect a call assert on
        # the recorded argv rather than a produced file.
        return None

    monkeypatch.setattr(thumbnail.subprocess, "run", _record)
    return calls


def test_oversized_frame_refused_before_ffmpeg(
    tmp_path: Path, no_ffmpeg: list[list[str]]
) -> None:
    source = tmp_path / "bomb.png"
    source.write_bytes(_png(50000, 50000))
    dest = tmp_path / "thumb.jpg"

    with pytest.raises(ImageTooLargeError):
        thumbnail.generate_thumbnail(
            "ffmpeg", str(source), dest, width=320, max_dimension_px=16000
        )

    # ffmpeg was never spawned and no thumbnail buffer was produced.
    assert no_ffmpeg == []
    assert not dest.exists()


def test_in_range_frame_reaches_ffmpeg(
    tmp_path: Path, no_ffmpeg: list[list[str]]
) -> None:
    source = tmp_path / "ok.png"
    source.write_bytes(_png(640, 480))
    dest = tmp_path / "thumb.jpg"

    thumbnail.generate_thumbnail(
        "ffmpeg", str(source), dest, width=320, max_dimension_px=16000
    )

    assert len(no_ffmpeg) == 1
    argv = no_ffmpeg[0]
    assert argv[0] == "ffmpeg"
    assert str(source) in argv


def test_limit_defaults_to_settings(
    tmp_path: Path, no_ffmpeg: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # With no explicit cap, the limit is sourced from the application settings
    # via the runtime context, mirroring the RTSP adapter's accessor use.
    class _Capture:
        max_image_dimension_px = 16000

    class _Settings:
        capture = _Capture()

    class _Context:
        settings = _Settings()

    monkeypatch.setattr("timelapse_manager.runtime.get_context", lambda: _Context())

    source = tmp_path / "bomb.png"
    source.write_bytes(_png(40000, 40000))
    dest = tmp_path / "thumb.jpg"

    with pytest.raises(ImageTooLargeError):
        thumbnail.generate_thumbnail("ffmpeg", str(source), dest, width=320)

    assert no_ffmpeg == []
