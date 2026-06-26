"""RTSP single-frame adapter, backed by an ffmpeg subprocess.

There is no usable pure-Python RTSP/H.264 decoder in the standard library, so a
frame is grabbed by spawning ffmpeg (assumed on ``PATH``) to pull one frame from
the stream and emit a JPEG to stdout. The subprocess is always launched with an
argument *list* via :func:`asyncio.create_subprocess_exec` -- never a shell
string -- so a hostile stream URL cannot inject shell syntax.

ffmpeg's stderr is parsed to classify the common failure modes (auth refused,
host unreachable, connection timeout, undecodable codec) into the shared error
vocabulary, falling back to a generic error otherwise.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

from ..security.ssrf import SsrfError, resolve_and_check_async
from . import _imageinfo
from .base import (
    AuthCaptureError,
    CameraAdapter,
    CameraCapabilities,
    CapturedFrame,
    CaptureError,
    GeoLocation,
    OtherCaptureError,
    TimeoutCaptureError,
    UnreachableCaptureError,
    UnsupportedCodecCaptureError,
    ValidationResult,
)

logger = logging.getLogger(__name__)

FFMPEG_BINARY = "ffmpeg"

# stderr fragments (lower-cased) mapped to a failure classification. Ordered by
# specificity; the first match wins. Connection failures are checked before the
# timeout phrases because ffmpeg echoes the input URL (which carries our
# ``?timeout=`` query parameter) into many error lines -- matching a bare
# "timeout" token there would misclassify a refused connection as a timeout.
_STDERR_PATTERNS: tuple[tuple[re.Pattern[str], type[CaptureError]], ...] = (
    (re.compile(r"401 unauthorized|authentication|auth.*fail"), AuthCaptureError),
    (
        re.compile(
            r"connection refused|no route to host|name or service not known|"
            r"could not (?:resolve|connect)|network is unreachable|"
            r"unable to open|failed to (?:resolve|connect)|404 not found"
        ),
        UnreachableCaptureError,
    ),
    (
        # Specific timeout phrases only -- never the bare "timeout" query token.
        re.compile(r"connection timed out|operation timed out|timed out"),
        TimeoutCaptureError,
    ),
    (
        re.compile(r"decoder.*not found|codec.*not|unsupported|invalid data"),
        UnsupportedCodecCaptureError,
    ),
)


def build_ffmpeg_command(
    stream_url: str,
    transport: str = "tcp",
    timeout_seconds: float = 15.0,
    ffmpeg_binary: str = FFMPEG_BINARY,
) -> list[str]:
    """Build the argv list for a single-frame RTSP grab to stdout.

    The ``-i`` value is passed as its own list element, so the URL is never
    interpreted by a shell. Output is a single JPEG written to stdout (``-``).

    :param stream_url: the RTSP URL to read from.
    :param transport: RTSP lower-transport, ``tcp`` (default, most reliable)
        or ``udp``.
    :param timeout_seconds: socket I/O timeout, passed to ffmpeg in microseconds
        via ``-timeout`` (the current option name; the older ``-stimeout`` alias
        was removed in recent ffmpeg releases).
    :param ffmpeg_binary: the ffmpeg executable to invoke. Defaults to ``ffmpeg``
        on ``PATH``; a packaged release passes the bundled binary so capture and
        encode use the same ffmpeg.
    """
    socket_timeout_us = int(timeout_seconds * 1_000_000)
    return [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        transport,
        # Socket I/O timeout so a dead host fails fast instead of hanging.
        "-timeout",
        str(socket_timeout_us),
        "-i",
        stream_url,
        # Exactly one frame, encoded as a JPEG image, to stdout.
        "-frames:v",
        "1",
        "-f",
        "image2",
        "-c:v",
        "mjpeg",
        "-",
    ]


async def _guard_stream_url(stream_url: str) -> None:
    """Validate an RTSP/RTSPS stream URL's host against the camera deny-list.

    The stream URL handed to ffmpeg is user- or device-controlled (an ONVIF
    ``GetStreamUri`` response is attacker-influenceable), so its host is routed
    through the SSRF guard *before* ffmpeg dials it -- closing the hole where a
    rogue camera points the stream at loopback, the cloud-metadata endpoint, or
    an internal host. Run on every capture (the URL may be re-resolved between
    grabs), mirroring the snapshot path's re-validate-before-each-fetch.

    The host is extracted with :func:`urllib.parse.urlsplit`, which strips any
    ``user:pass@`` userinfo, so embedded credentials never reach the guard (or its
    error messages). The same camera/scan policy the device was added under is
    reused (admin private opt-in honoured; loopback/link-local/metadata never
    relaxed) so a legitimately-configured private camera still works.

    DNS resolution is off-loaded to a worker thread so a slow resolver cannot
    stall the capture event loop. A denied or unresolvable host is surfaced as
    :class:`UnreachableCaptureError` (the same family the adapter already raises)
    so it flows through the normal capture-failure handling rather than escaping
    as an unclassified error.
    """
    host = urlsplit(stream_url).hostname
    if not host:
        raise UnreachableCaptureError(
            "rtsp stream URL has no host component to validate"
        )

    from ..runtime import get_context

    ssrf = get_context().settings.ssrf
    try:
        await resolve_and_check_async(
            host,
            allow_private=True,
            allowed_private_subnets=ssrf.allowed_private_subnets,
        )
    except SsrfError as exc:
        raise UnreachableCaptureError(f"rtsp stream URL blocked: {exc}") from exc
    except socket.gaierror as exc:
        # Fail-closed: an unresolvable stream host cannot be safely dialed.
        raise UnreachableCaptureError(
            f"rtsp stream host {host!r} did not resolve"
        ) from exc


def classify_stderr(stderr_text: str) -> type[CaptureError]:
    """Map ffmpeg stderr to the most specific capture-error class."""
    lowered = stderr_text.lower()
    for pattern, error_cls in _STDERR_PATTERNS:
        if pattern.search(lowered):
            return error_cls
    return OtherCaptureError


class RtspAdapter(CameraAdapter):
    """Capture a single frame from an RTSP stream using ffmpeg."""

    def __init__(
        self,
        stream_url: str,
        credentials: tuple[str, str] | None = None,
        transport: str = "tcp",
        timeout_seconds: float = 15.0,
        ffmpeg_binary: str = FFMPEG_BINARY,
    ) -> None:
        self._stream_url = self._apply_credentials(stream_url, credentials)
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._ffmpeg_binary = ffmpeg_binary

    @staticmethod
    def _apply_credentials(url: str, credentials: tuple[str, str] | None) -> str:
        """Embed credentials in the RTSP URL userinfo if not already present.

        RTSP auth is carried in the URL (``rtsp://user:pass@host/...``). If the
        URL already has userinfo, it is left untouched. The username and password
        are percent-encoded so characters such as ``@``, ``:`` or ``/`` in a
        credential do not corrupt the URL structure.
        """
        if credentials is None or "@" in url.split("://", 1)[-1].split("/", 1)[0]:
            return url
        scheme, _, rest = url.partition("://")
        if not scheme or not rest:
            return url
        username, password = credentials
        safe_user = quote(username, safe="")
        safe_password = quote(password, safe="")
        return f"{scheme}://{safe_user}:{safe_password}@{rest}"

    async def capture(self) -> CapturedFrame:
        # SSRF guard: validate the stream host before ffmpeg opens any socket.
        # Runs on every capture so a re-resolving host is re-checked each grab.
        await _guard_stream_url(self._stream_url)
        command = build_ffmpeg_command(
            self._stream_url,
            self._transport,
            self._timeout_seconds,
            ffmpeg_binary=self._ffmpeg_binary,
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # A wall-clock guard slightly longer than ffmpeg's own socket timeout,
        # in case ffmpeg itself wedges.
        wall_timeout = self._timeout_seconds + 5.0
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=wall_timeout
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise TimeoutCaptureError(
                f"ffmpeg did not return a frame within {wall_timeout:.0f}s"
            ) from exc

        if process.returncode != 0 or not stdout:
            stderr_text = stderr.decode("utf-8", errors="replace")
            error_cls = classify_stderr(stderr_text)
            detail = stderr_text.strip().splitlines()
            message = detail[-1] if detail else "ffmpeg failed with no output"
            raise error_cls(f"rtsp capture failed: {message}")

        fmt = _imageinfo.detect_format(stdout) or "jpeg"
        dimensions = _imageinfo.read_dimensions(stdout)
        width, height = dimensions if dimensions is not None else (0, 0)
        return CapturedFrame(
            image_bytes=stdout,
            width=width,
            height=height,
            format=fmt,
            captured_at=datetime.now(UTC),
        )

    async def validate_connection(self) -> ValidationResult:
        try:
            await self.capture()
        except CaptureError as exc:
            return ValidationResult(ok=False, reason=exc.reason, message=exc.message)
        return ValidationResult(
            ok=True, reason=None, message="captured a frame from the stream"
        )

    async def get_geolocation(self) -> GeoLocation | None:
        # RTSP carries no standard geolocation metadata.
        return None

    async def capabilities(self) -> CameraCapabilities:
        # The stream does not advertise selectable resolutions.
        return CameraCapabilities(supported_resolutions=[])

    async def close(self) -> None:
        # Each capture spawns and reaps its own subprocess; nothing persists.
        return None
