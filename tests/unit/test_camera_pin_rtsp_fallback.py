"""A pinned ONVIF camera never falls back to RTSP (#228).

The ONVIF adapter grabs a frame over RTSP through ffmpeg when the device has no
snapshot endpoint. RTSP is not covered by a certificate pin and the grab carries
the camera's credentials, so a pinned camera refuses the fallback; an unpinned
camera is unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from timelapse_manager.cameras import build_adapter
from timelapse_manager.cameras.base import OtherCaptureError
from timelapse_manager.cameras.onvif import OnvifAdapter


class _Rtsp:
    built: list[tuple[Any, ...]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        _Rtsp.built.append(args)

    async def capture(self) -> Any:
        return "rtsp-frame"


def _camera(pin: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        protocol="onvif",
        address="https://10.0.16.9",
        credentials=None,
        credentials_inherit_default=False,
        snapshot_uri=None,
        stream_uri=None,
        default_resolution=None,
        allow_basic_over_http=False,
        tls_pinned_cert_pem=pin,
    )


async def _capture(pin: str | None) -> Any:
    _Rtsp.built = []
    adapter = build_adapter(
        _camera(pin),
        httpx.AsyncClient(),
        credentials=("user", "secret"),
    )
    assert isinstance(adapter, OnvifAdapter)

    async def no_snapshot(self: OnvifAdapter) -> str:
        raise OtherCaptureError("no snapshot endpoint")

    async def stream(self: OnvifAdapter) -> str:
        return "rtsp://10.0.16.9/stream1"

    with (
        patch.object(OnvifAdapter, "_resolve_snapshot_uri", no_snapshot),
        patch.object(OnvifAdapter, "_resolve_stream_uri", stream),
        patch("timelapse_manager.cameras.onvif.RtspAdapter", _Rtsp),
    ):
        return await adapter.capture()


async def test_a_pinned_camera_does_not_run_rtsp_with_credentials() -> None:
    with pytest.raises(OtherCaptureError, match="no snapshot endpoint"):
        await _capture("PIN")
    assert _Rtsp.built == []


async def test_an_unpinned_camera_keeps_the_rtsp_fallback() -> None:
    assert await _capture(None) == "rtsp-frame"
    assert len(_Rtsp.built) == 1
