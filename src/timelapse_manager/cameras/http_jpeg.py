"""HTTP/JPEG snapshot adapter.

Captures a single still by issuing an HTTP ``GET`` against a snapshot URL and
treating the response body as an encoded image. Supports both HTTP Basic and
Digest authentication: the adapter probes once unauthenticated and, on a ``401``
challenge, retries with the scheme the server advertised. This keeps the shared
``httpx.AsyncClient`` free of any per-camera auth state.

The snapshot URL is user-supplied and fetched repeatedly, so it is re-validated
against the camera deny-list immediately before each request (fail-closed): the
URL is stored unvalidated at camera-create time, and re-checking here -- rather
than only at create time -- closes the window where a name later resolves to an
internal address. Redirects are not followed (the shared client defaults to
``follow_redirects=False``), so a 30x cannot bounce the fetch to a denied host.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from ..security.crypto import decrypt_credentials
from ..security.ssrf import SsrfError, assert_allowed_url
from . import _imageinfo
from .base import (
    AuthCaptureError,
    CameraAdapter,
    CameraCapabilities,
    CapturedFrame,
    GeoLocation,
    OtherCaptureError,
    TimeoutCaptureError,
    UnreachableCaptureError,
    ValidationFailure,
    ValidationResult,
)

logger = logging.getLogger(__name__)


def credentials_from(camera: Any) -> tuple[str, str] | None:
    """Extract ``(username, password)`` from a camera's credentials mapping.

    Returns None when no usable username is present. A missing password is
    treated as empty string, matching how many cameras accept user-only auth.
    """
    creds = getattr(camera, "credentials", None)
    if not isinstance(creds, dict):
        return None
    # Stored credential documents have their secret fields encrypted at rest;
    # decrypt here -- the single point every adapter reads credentials through --
    # so the rest of the capture path is unaware of the at-rest encryption.
    # Legacy plaintext fields pass through unchanged.
    creds = decrypt_credentials(creds) or {}
    username = creds.get("username")
    if not username:
        return None
    password = creds.get("password") or ""
    return (str(username), str(password))


def classify_http_status(status_code: int) -> ValidationFailure | None:
    """Map an HTTP status to a failure category, or None if it is a success.

    ``2xx`` returns None. ``401``/``403`` are auth failures; everything else is
    classified as ``OTHER`` so the caller can surface the raw status.
    """
    if 200 <= status_code < 300:
        return None
    if status_code in (401, 403):
        return ValidationFailure.AUTH
    return ValidationFailure.OTHER


def _guard_snapshot_url(url: str) -> None:
    """Validate a snapshot URL against the camera deny-list before fetching.

    Uses the camera/scan policy (admin private opt-in honoured, loopback/
    link-local/metadata never relaxed). A denied target is surfaced as
    :class:`UnreachableCaptureError` so it flows through the adapter's existing
    failure handling instead of raising an unclassified error into the engine.
    """
    from ..runtime import get_context

    ssrf = get_context().settings.ssrf
    try:
        assert_allowed_url(
            url,
            allow_private=True,
            allowed_private_subnets=ssrf.allowed_private_subnets,
        )
    except SsrfError as exc:
        raise UnreachableCaptureError(f"snapshot URL blocked: {exc}") from exc


def _auth_from_challenge(challenge: str, username: str, password: str) -> httpx.Auth:
    """Pick a matching httpx auth handler from a ``WWW-Authenticate`` header."""
    if challenge.lower().lstrip().startswith("digest"):
        return httpx.DigestAuth(username, password)
    return httpx.BasicAuth(username, password)


async def http_get_image(
    client: httpx.AsyncClient,
    url: str,
    credentials: tuple[str, str] | None,
    timeout: float,
) -> bytes:
    """GET a URL, handling Basic/Digest auth, and return the response body.

    Issues an unauthenticated request first. On a ``401`` with credentials in
    hand, it retries using the scheme advertised in ``WWW-Authenticate``.

    :raises AuthCaptureError: on a ``401``/``403`` that auth could not satisfy.
    :raises UnreachableCaptureError: on connection/transport errors, or when the
        URL targets a denied address (SSRF guard).
    :raises TimeoutCaptureError: when the request times out.
    :raises OtherCaptureError: on any other non-success status.
    """
    _guard_snapshot_url(url)
    try:
        response = await client.get(url, timeout=timeout)
        if response.status_code == 401 and credentials is not None:
            challenge = response.headers.get("www-authenticate", "")
            auth = _auth_from_challenge(challenge, *credentials)
            response = await client.get(url, timeout=timeout, auth=auth)
    except httpx.TimeoutException as exc:
        raise TimeoutCaptureError(f"timed out requesting {url}") from exc
    except httpx.TransportError as exc:
        raise UnreachableCaptureError(f"cannot reach {url}: {exc}") from exc

    failure = classify_http_status(response.status_code)
    if failure is ValidationFailure.AUTH:
        raise AuthCaptureError(
            f"authentication rejected ({response.status_code}) for {url}"
        )
    if failure is not None:
        raise OtherCaptureError(f"unexpected status {response.status_code} from {url}")
    return response.content


def frame_from_bytes(image_bytes: bytes) -> CapturedFrame:
    """Build a :class:`CapturedFrame`, parsing dimensions from the bytes.

    :raises OtherCaptureError: when the body is empty or not a known image.
    """
    if not image_bytes:
        raise OtherCaptureError("camera returned an empty response body")
    fmt = _imageinfo.detect_format(image_bytes)
    if fmt is None:
        raise OtherCaptureError("camera response was not a recognised image")
    dimensions = _imageinfo.read_dimensions(image_bytes)
    width, height = dimensions if dimensions is not None else (0, 0)
    return CapturedFrame(
        image_bytes=image_bytes,
        width=width,
        height=height,
        format=fmt,
        captured_at=datetime.now(UTC),
    )


class HttpJpegAdapter(CameraAdapter):
    """Capture stills from a plain HTTP snapshot endpoint."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        snapshot_url: str,
        credentials: tuple[str, str] | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._client = client
        self._snapshot_url = snapshot_url
        self._credentials = credentials
        self._timeout = timeout

    async def capture(self) -> CapturedFrame:
        image_bytes = await http_get_image(
            self._client, self._snapshot_url, self._credentials, self._timeout
        )
        return frame_from_bytes(image_bytes)

    async def validate_connection(self) -> ValidationResult:
        try:
            await self.capture()
        except (
            AuthCaptureError,
            UnreachableCaptureError,
            TimeoutCaptureError,
            OtherCaptureError,
        ) as exc:
            return ValidationResult(ok=False, reason=exc.reason, message=exc.message)
        return ValidationResult(
            ok=True, reason=None, message="snapshot retrieved successfully"
        )

    async def get_geolocation(self) -> GeoLocation | None:
        # A generic HTTP snapshot endpoint exposes no device metadata.
        return None

    async def capabilities(self) -> CameraCapabilities:
        # No capability query exists for a bare snapshot URL.
        return CameraCapabilities(supported_resolutions=[])

    async def close(self) -> None:
        # The HTTP client is owned by the caller; nothing to release here.
        return None
