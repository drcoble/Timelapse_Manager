"""HTTP camera auth: Basic/Digest challenge selection and the 401-retry path.

These exercise ``http_get_image``'s authentication handling -- the unauthenticated
first request, the ``WWW-Authenticate`` scheme selection, and the authenticated
retry -- which the snapshot-server fixture (it never returns 401) does not. The
SSRF guard is patched to a no-op here; its behaviour is covered by the abuse suite.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from timelapse_manager.cameras.base import AuthCaptureError, OtherCaptureError
from timelapse_manager.cameras.http_auth import BasicPolicy, CameraAuthState
from timelapse_manager.cameras.http_jpeg import http_get_image

_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 16 + b"\xff\xd9"
_NOOP_GUARD = "timelapse_manager.cameras.http_jpeg._guard_snapshot_url"
_CAP_RESOLVER = "timelapse_manager.cameras.http_jpeg._resolve_max_image_bytes"


@pytest.fixture(autouse=True)
def _stub_image_cap():
    """Resolve the body-size cap without a live app context.

    ``http_get_image`` reads the ceiling from the runtime context; these auth
    tests construct no context, so the resolver is stubbed to a large default.
    Individual tests override it to exercise the cap itself.
    """
    with patch(_CAP_RESOLVER, return_value=64 * 1024 * 1024):
        yield


class TestBasicAuthRetry:
    async def test_401_then_authenticated_retry_returns_body(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if "authorization" not in request.headers:
                return httpx.Response(
                    401, headers={"WWW-Authenticate": 'Basic realm="cam"'}
                )
            return httpx.Response(
                200, content=_JPEG, headers={"content-type": "image/jpeg"}
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            with patch(_NOOP_GUARD):
                body = await http_get_image(
                    client, "https://10.0.0.9/snap", ("admin", "pw"), timeout=5.0
                )

        assert body == _JPEG
        assert len(requests) == 2  # unauthenticated probe, then authenticated retry
        assert requests[0].headers.get("authorization") is None
        assert requests[1].headers["authorization"].startswith("Basic ")

    async def test_persistent_401_raises_auth_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401, headers={"WWW-Authenticate": 'Basic realm="cam"'}
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            with patch(_NOOP_GUARD), pytest.raises(AuthCaptureError):
                await http_get_image(
                    client,
                    "http://10.0.0.9/snap",
                    ("admin", "wrong"),
                    timeout=5.0,
                    auth_state=CameraAuthState(BasicPolicy.for_camera(True)),
                )

    async def test_no_credentials_no_retry(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                401, headers={"WWW-Authenticate": 'Basic realm="cam"'}
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            with patch(_NOOP_GUARD), pytest.raises(AuthCaptureError):
                await http_get_image(client, "http://10.0.0.9/snap", None, timeout=5.0)

        # Without credentials there is nothing to retry with: one request only.
        assert len(requests) == 1


class TestBodySizeCap:
    async def test_oversize_content_length_rejected_up_front(self) -> None:
        # A response whose declared Content-Length exceeds the cap is rejected
        # before its body is read.
        cap = 1024

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=b"\x00" * (cap * 4),
                headers={"content-type": "image/jpeg"},
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            with (
                patch(_NOOP_GUARD),
                patch(_CAP_RESOLVER, return_value=cap),
                pytest.raises(OtherCaptureError, match="limit"),
            ):
                await http_get_image(client, "http://10.0.0.9/snap", None, timeout=5.0)

    async def test_oversize_streamed_body_rejected(self) -> None:
        # A body that overshoots the cap while streaming (no Content-Length) is
        # caught by the running total, not just the up-front header check.
        cap = 1024

        async def stream_body():
            for _ in range(8):
                yield b"\x00" * 512  # 4 KiB total, over the 1 KiB cap

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=stream_body(), headers={"content-type": "image/jpeg"}
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            with (
                patch(_NOOP_GUARD),
                patch(_CAP_RESOLVER, return_value=cap),
                pytest.raises(OtherCaptureError, match="limit"),
            ):
                await http_get_image(client, "http://10.0.0.9/snap", None, timeout=5.0)

    async def test_body_within_cap_returned(self) -> None:
        # A body at or under the cap is returned intact.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=_JPEG, headers={"content-type": "image/jpeg"}
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            with patch(_NOOP_GUARD), patch(_CAP_RESOLVER, return_value=64 * 1024):
                body = await http_get_image(
                    client, "http://10.0.0.9/snap", None, timeout=5.0
                )
        assert body == _JPEG
