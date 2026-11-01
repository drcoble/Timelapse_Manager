"""HTTP/JPEG snapshot adapter.

Captures a single still by issuing an HTTP ``GET`` against a snapshot URL and
treating the response body as an encoded image. Authentication is negotiated
through a :class:`~.http_auth.CameraAuthState` (#69 CAM-S1, CAM-P1): the first
request to a camera is answered from its ``401`` challenge (Digest preferred,
Basic only where the policy allows), and later requests send the remembered
scheme up front, so a snapshot costs one request. The shared
``httpx.AsyncClient`` itself carries no per-camera auth state.

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
from ..security.ssrf import SsrfError
from . import _imageinfo
from ._bounded import read_capped_body
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
from .clients import pool_exhausted_error, request_timeout
from .host_resolution import check_camera_url_async
from .http_auth import CameraAuthState

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


def strip_url_userinfo(url: str) -> str:
    """Return ``url`` without any ``user:pass@`` part.

    httpx turns URL userinfo into a preemptive ``Authorization: Basic`` on
    every request, bypassing the negotiation in :mod:`.http_auth` (so Basic
    over plain HTTP, and to whichever host the URL names). A legacy row can
    still hold such a URL; the login it carried is moved into the credential
    document at startup, and what is fetched -- and named in an error -- is the
    URL without it. An unparsable URL is returned unchanged (the request then
    fails on its own, naming nothing new).
    """
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, ValueError):
        return url
    if not parsed.userinfo:
        return url
    return str(parsed.copy_with(userinfo=b""))


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


async def _guard_snapshot_url(url: str) -> None:
    """Validate a snapshot URL against the camera deny-list before fetching.

    Uses the camera/scan policy (admin private opt-in honoured, loopback/
    link-local/metadata never relaxed). A denied target is surfaced as
    :class:`UnreachableCaptureError` so it flows through the adapter's existing
    failure handling instead of raising an unclassified error into the engine.
    The lookup runs off the event loop; the camera client then dials the address
    it validated (see :mod:`.clients`).
    """
    try:
        await check_camera_url_async(url)
    except SsrfError as exc:
        raise UnreachableCaptureError(f"snapshot URL blocked: {exc}") from exc


def _resolve_max_image_bytes() -> int:
    """Return the configured ceiling on a snapshot response body, in bytes."""
    from ..runtime import get_context

    return get_context().settings.capture.max_image_bytes


def _resolve_max_image_dimension_px() -> int:
    """Return the configured ceiling on either pixel dimension of a frame.

    Falls back to the model default when no runtime context is installed, so the
    ceiling is always enforced rather than silently disabled. In a running
    service the context is always present; the fallback only matters for frame
    parsing exercised outside a configured application context.
    """
    from ..runtime import get_context

    try:
        return get_context().settings.capture.max_image_dimension_px
    except (RuntimeError, AttributeError):
        from ..config.settings import CaptureSettings

        default = CaptureSettings.model_fields["max_image_dimension_px"].default
        assert isinstance(default, int)
        return default


async def _read_capped_body(
    response: httpx.Response, url: str, max_bytes: int
) -> bytes:
    """Stream a snapshot body, aborting if it would exceed ``max_bytes``.

    Camera responses are untrusted, so the body is read in chunks against a
    running total rather than buffered whole (see
    :func:`._bounded.read_capped_body`: a declared ``Content-Length`` over the
    ceiling is rejected up front, and the connection is released on the
    over-cap path so a hostile peer cannot pin it open).
    """
    return await read_capped_body(
        response, max_bytes=max_bytes, label="snapshot", url=url
    )


async def http_get_image(
    client: httpx.AsyncClient,
    url: str,
    credentials: tuple[str, str] | None,
    timeout: float,
    *,
    max_bytes: int | None = None,
    auth_state: CameraAuthState | None = None,
) -> bytes:
    """GET a URL, handling Basic/Digest auth, and return the response body.

    With credentials, the request carries whatever ``auth_state`` negotiated
    with this origin before (one round trip). On a ``401`` it answers the
    challenge in that response, once: the strongest offered scheme, and Basic
    only where the state's policy allows it. The body is streamed and capped so
    an untrusted, oversized response cannot be buffered whole into memory.

    :param max_bytes: the body ceiling. ``None`` (an image fetch) uses the
        configured ``max_image_bytes``; a caller reading a small text or XML
        document passes a much smaller cap, such as
        :data:`._bounded.TEXT_RESPONSE_MAX_BYTES`.
    :param auth_state: the camera's negotiation state. ``None`` uses a fresh
        one with the probe policy (never Basic over plain HTTP).

    :raises AuthCaptureError: on a ``401``/``403`` that auth could not satisfy,
        a challenge in an unsupported scheme, or Basic over plain HTTP the
        policy refuses.
    :raises UnreachableCaptureError: on connection/transport errors, or when the
        URL targets a denied address (SSRF guard).
    :raises TimeoutCaptureError: when the request times out.
    :raises OtherCaptureError: on any other non-success status (a redirect to
        ``https://`` on the same host says to change the address), or when the
        body exceeds the configured size ceiling; with ``local=True`` when no
        pooled connection became free in time (the camera was never contacted).
    """
    url = strip_url_userinfo(url)
    await _guard_snapshot_url(url)
    if max_bytes is None:
        max_bytes = _resolve_max_image_bytes()
    state = auth_state if auth_state is not None else CameraAuthState()
    # The pool wait keeps the client's own (shorter) bound, so an exhausted
    # local pool is reported as such before a capture budget of ``timeout``.
    per_request = request_timeout(client, timeout)
    try:
        preemptive = state.preemptive("GET", url, credentials)
        headers = {"Authorization": preemptive} if preemptive else None
        async with client.stream(
            "GET", url, timeout=per_request, headers=headers
        ) as response:
            if response.status_code == 401 and credentials is not None:
                challenges = response.headers.get_list("www-authenticate")
                # Release the 401 before the retry so its body does not stay
                # buffered while the authenticated GET runs.
                await response.aclose()
                answer = state.respond(challenges, "GET", url, credentials)
                async with client.stream(
                    "GET",
                    url,
                    timeout=per_request,
                    headers={"Authorization": answer},
                ) as retry:
                    if retry.status_code == 401:
                        state.forget(url)
                    _raise_for_capture_status(retry, url)
                    return await _read_capped_body(retry, url, max_bytes)
            _raise_for_capture_status(response, url)
            return await _read_capped_body(response, url, max_bytes)
    except httpx.PoolTimeout as exc:
        # Before TimeoutException (its base): no free local connection is not
        # a camera failure (#69 CAM-P2).
        raise pool_exhausted_error(url) from exc
    except httpx.TimeoutException as exc:
        raise TimeoutCaptureError(f"timed out requesting {url}") from exc
    except httpx.TransportError as exc:
        raise UnreachableCaptureError(f"cannot reach {url}: {exc}") from exc


_REDIRECT_STATUSES = frozenset({301, 302, 307, 308})


def https_redirect_hint(url: str, status_code: int, location: str | None) -> str | None:
    """Explain a same-host redirect from ``http://`` to ``https://``, or None.

    Redirects are never followed (a ``30x`` could bounce the request past the
    SSRF guard), so a camera that has been switched to HTTPS-only would just
    fail with a bare status. This names the fix instead (#69 CAM-S1).
    """
    if status_code not in _REDIRECT_STATUSES or not location:
        return None
    try:
        source = httpx.URL(url)
        target = source.join(location)
    except (httpx.InvalidURL, ValueError):
        return None
    if source.scheme != "http" or target.scheme != "https":
        return None
    if target.host.lower() != source.host.lower():
        return None
    host = f"[{target.host}]" if ":" in target.host else target.host
    netloc = host if target.port in (None, 443) else f"{host}:{target.port}"
    return f"camera redirects to HTTPS; change the address to https://{netloc}"


def _raise_for_capture_status(response: httpx.Response, url: str) -> None:
    """Map a non-success response to the matching capture error, else return."""
    status_code = response.status_code
    failure = classify_http_status(status_code)
    if failure is ValidationFailure.AUTH:
        raise AuthCaptureError(f"authentication rejected ({status_code}) for {url}")
    if failure is not None:
        hint = https_redirect_hint(url, status_code, response.headers.get("location"))
        if hint is not None:
            raise OtherCaptureError(hint)
        raise OtherCaptureError(f"unexpected status {status_code} from {url}")


def assert_frame_integrity(
    image_bytes: bytes, fmt: str | None, dimensions: tuple[int, int] | None
) -> tuple[int, int]:
    """Return ``(width, height)`` for a whole image, or raise.

    Shared by every adapter that builds a frame from raw bytes (HTTP snapshot,
    VAPIX, ONVIF snapshot and RTSP), so a truncated or undecodable body is never
    written as a frame (#69 CAM-R11). The message carries the body size, so the
    capture-failure log shows how much arrived.

    :raises OtherCaptureError: when the dimensions are unreadable or zero, or a
        JPEG has no End-Of-Image marker.
    """
    problem = _imageinfo.frame_integrity_problem(image_bytes, fmt, dimensions)
    if problem is not None or dimensions is None:
        raise OtherCaptureError(
            f"truncated or undecodable image ({problem}; {len(image_bytes)} bytes)"
        )
    return dimensions


def frame_from_bytes(image_bytes: bytes) -> CapturedFrame:
    """Build a :class:`CapturedFrame`, parsing dimensions from the bytes.

    :raises OtherCaptureError: when the body is empty, not a known image,
        truncated or undecodable (see :func:`assert_frame_integrity`), or larger
        than the configured dimension ceiling.
    """
    if not image_bytes:
        raise OtherCaptureError("camera returned an empty response body")
    fmt = _imageinfo.detect_format(image_bytes)
    if fmt is None:
        raise OtherCaptureError("camera response was not a recognised image")
    dimensions = _imageinfo.read_dimensions(image_bytes)
    width, height = assert_frame_integrity(image_bytes, fmt, dimensions)
    max_dimension = _resolve_max_image_dimension_px()
    if width > max_dimension or height > max_dimension:
        raise OtherCaptureError(
            f"camera image dimensions {width}x{height} exceed the maximum of "
            f"{max_dimension}px per axis"
        )
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
        auth_state: CameraAuthState | None = None,
    ) -> None:
        self._client = client
        self._snapshot_url = snapshot_url
        self._credentials = credentials
        self._timeout = timeout
        # The camera session's negotiation state, so a capture reuses the
        # scheme (and Digest nonce) the last one negotiated; a fresh one with
        # the probe policy when none is given.
        self._auth_state = auth_state if auth_state is not None else CameraAuthState()

    async def capture(self) -> CapturedFrame:
        image_bytes = await http_get_image(
            self._client,
            self._snapshot_url,
            self._credentials,
            self._timeout,
            auth_state=self._auth_state,
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
