"""Live-capture dimension-ceiling enforcement in ``frame_from_bytes``.

A rogue or malfunctioning camera can return a small image whose header declares
an enormous width/height; decoding it later (thumbnail/render) would allocate
gigabytes. ``frame_from_bytes`` -- the shared bytes->frame converter used by the
HTTP/ONVIF/VAPIX capture paths -- rejects an over-ceiling frame before it is ever
persisted, so no oversized frame can enter storage.
"""

from __future__ import annotations

import struct

import pytest

from timelapse_manager.cameras.base import OtherCaptureError
from timelapse_manager.cameras.http_jpeg import (
    _resolve_max_image_dimension_px,
    frame_from_bytes,
)


def _png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


def test_in_range_frame_is_accepted() -> None:
    frame = frame_from_bytes(_png(640, 480))
    assert (frame.width, frame.height) == (640, 480)


def test_oversized_frame_rejected_without_context() -> None:
    # No runtime context installed: the resolver falls back to the model default,
    # so the ceiling is still enforced.
    cap = _resolve_max_image_dimension_px()
    with pytest.raises(OtherCaptureError, match="dimensions|maximum"):
        frame_from_bytes(_png(cap + 1, 100))
    with pytest.raises(OtherCaptureError, match="dimensions|maximum"):
        frame_from_bytes(_png(100, cap + 1))
