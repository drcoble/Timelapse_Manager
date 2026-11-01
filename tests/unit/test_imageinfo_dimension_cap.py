"""Dimension-ceiling enforcement in the hand-rolled image header reader.

A rogue or malfunctioning camera can declare an enormous width/height in its
image header; decoding such a frame would allocate gigabytes. These tests pin
that :func:`read_dimensions_bounded` refuses an over-ceiling header (raising
:class:`ImageTooLargeError`) while leaving the never-raise
:func:`read_dimensions` contract -- and ordinary in-range images -- untouched.
"""

from __future__ import annotations

import struct

import pytest

from timelapse_manager.cameras._imageinfo import (
    ImageTooLargeError,
    read_dimensions,
    read_dimensions_bounded,
)


def _png(width: int, height: int) -> bytes:
    """A structurally-minimal PNG IHDR declaring the given dimensions."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


def _jpeg(width: int, height: int) -> bytes:
    """A structurally-minimal JPEG SOF0 declaring the given dimensions."""
    sof = (
        b"\xff\xc0"
        + struct.pack(">H", 17)
        + b"\x08"
        + struct.pack(">H", height)
        + struct.pack(">H", width)
        + b"\x01\x01\x11\x00"
    )
    return b"\xff\xd8" + sof + b"\xff\xd9"


class TestReadDimensionsBounded:
    def test_in_range_png_returned(self) -> None:
        assert read_dimensions_bounded(_png(320, 240), 16000) == (320, 240)

    def test_in_range_jpeg_returned(self) -> None:
        assert read_dimensions_bounded(_jpeg(640, 480), 16000) == (640, 480)

    def test_dimension_at_ceiling_is_allowed(self) -> None:
        # The ceiling itself is in range; only strictly-greater is rejected.
        assert read_dimensions_bounded(_png(16000, 16000), 16000) == (16000, 16000)

    def test_oversized_width_png_rejected(self) -> None:
        with pytest.raises(ImageTooLargeError):
            read_dimensions_bounded(_png(20000, 100), 16000)

    def test_oversized_height_png_rejected(self) -> None:
        with pytest.raises(ImageTooLargeError):
            read_dimensions_bounded(_png(100, 20000), 16000)

    def test_oversized_jpeg_rejected(self) -> None:
        # JPEG SOF stores each dimension as 16-bit, so 60000 is the realistic
        # upper end -- still far above the 16000 ceiling.
        with pytest.raises(ImageTooLargeError):
            read_dimensions_bounded(_jpeg(60000, 60000), 16000)

    def test_rejection_does_not_require_full_image(self) -> None:
        # Only the tiny header is present -- no pixel payload -- yet the giant
        # dimensions are caught, proving the refusal precedes any decode/alloc.
        bomb = _png(2_000_000_000, 2_000_000_000)
        assert len(bomb) < 64
        with pytest.raises(ImageTooLargeError):
            read_dimensions_bounded(bomb, 16000)

    def test_unreadable_input_returns_none(self) -> None:
        # An unparseable header has no dimensions to bound -> None, never raises.
        assert read_dimensions_bounded(b"not an image", 16000) is None

    def test_error_is_a_value_error(self) -> None:
        assert issubclass(ImageTooLargeError, ValueError)


class TestReadDimensionsStillNeverRaises:
    def test_oversized_image_does_not_raise_in_plain_reader(self) -> None:
        # The original reader keeps its never-raise contract: callers that only
        # need metadata still get the dimensions back without an exception.
        assert read_dimensions(_png(20000, 20000)) == (20000, 20000)
