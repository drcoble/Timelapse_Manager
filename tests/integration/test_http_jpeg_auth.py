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

from timelapse_manager.cameras.base import AuthCaptureError
from timelapse_manager.cameras.http_jpeg import _auth_from_challenge, http_get_image

_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 16 + b"\xff\xd9"
_NOOP_GUARD = "timelapse_manager.cameras.http_jpeg._guard_snapshot_url"


class TestAuthFromChallenge:
    def test_digest_challenge_selects_digest_auth(self) -> None:
        auth = _auth_from_challenge('Digest realm="cam", nonce="x"', "u", "p")
        assert isinstance(auth, httpx.DigestAuth)

    def test_basic_challenge_selects_basic_auth(self) -> None:
        auth = _auth_from_challenge('Basic realm="cam"', "u", "p")
        assert isinstance(auth, httpx.BasicAuth)

    def test_missing_challenge_defaults_to_basic(self) -> None:
        assert isinstance(_auth_from_challenge("", "u", "p"), httpx.BasicAuth)

    def test_leading_whitespace_digest_is_recognised(self) -> None:
        auth = _auth_from_challenge("  Digest realm=x", "u", "p")
        assert isinstance(auth, httpx.DigestAuth)


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
                    client, "http://10.0.0.9/snap", ("admin", "pw"), timeout=5.0
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
                    client, "http://10.0.0.9/snap", ("admin", "wrong"), timeout=5.0
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
