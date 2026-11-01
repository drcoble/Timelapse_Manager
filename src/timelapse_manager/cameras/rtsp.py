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
import ipaddress
import logging
import os
import re
import signal
import socket
from datetime import UTC, datetime
from typing import NamedTuple
from urllib.parse import quote, urlsplit, urlunsplit

from ..logging import redact_text
from ..security.pinned_http import CONTAINED_MESSAGE, address_inside_subnets
from ..security.ssrf import SsrfError, pinned_addresses_async, resolve_and_check_async
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
from .host_resolution import camera_allowed_subnets

logger = logging.getLogger(__name__)

FFMPEG_BINARY = "ffmpeg"

DEFAULT_JPEG_QUALITY = 2

# Wall-clock allowance beyond ffmpeg's own socket timeout, in case ffmpeg wedges.
WALL_CLOCK_MARGIN_SECONDS = 5.0

_PROTOCOL_WHITELIST = "rtp,rtsp,tcp,udp"
# ``rtsps://`` is RTSP over TLS: ffmpeg's ``tls`` protocol wraps ``tcp``.
_RTSPS_PROTOCOL_WHITELIST = "rtp,rtsp,tcp,udp,tls"

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
    *,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    wait_for_keyframe: bool = False,
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
    :param jpeg_quality: ffmpeg ``-q:v`` for the output JPEG (2 = high quality).
    :param wait_for_keyframe: discard non-key frames before decoding
        (``-skip_frame nokey``), so the grab is a complete picture rather than a
        P-frame decoded against missing references.

    The input is constrained to the RTSP transport protocols via
    ``-protocol_whitelist``: a hostile stream's SDP cannot steer ffmpeg into
    ``file:``, ``concat:`` or ``http:`` to read local files or reach other hosts.
    An ``rtsps://`` URL additionally needs ``tls`` (and nothing else): the TLS
    wrapper carries the same RTSP stream, it is not a way to reach other hosts.

    Audio, subtitle and data streams are dropped (``-an -sn -dn``): a camera's
    audio track would otherwise be demuxed and buffered for a one-frame grab.
    """
    socket_timeout_us = int(timeout_seconds * 1_000_000)
    rtsps = _is_rtsps(stream_url)
    whitelist = _RTSPS_PROTOCOL_WHITELIST if rtsps else _PROTOCOL_WHITELIST
    skip_frame = ["-skip_frame", "nokey"] if wait_for_keyframe else []
    return [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        transport,
        # Restrict the input to RTSP's own transports so a malicious SDP cannot
        # redirect ffmpeg into file:/concat:/http: protocols. Applies to the
        # input (placed before -i) and the demuxer it spawns.
        "-protocol_whitelist",
        whitelist,
        # Socket I/O timeout so a dead host fails fast instead of hanging.
        "-timeout",
        str(socket_timeout_us),
        *skip_frame,
        "-i",
        stream_url,
        # Video only: no audio, subtitle or data streams for a one-frame grab.
        "-an",
        "-sn",
        "-dn",
        # Exactly one frame, encoded as a JPEG image, to stdout.
        "-frames:v",
        "1",
        "-f",
        "image2",
        "-c:v",
        "mjpeg",
        "-q:v",
        str(jpeg_quality),
        "-",
    ]


def _is_rtsps(stream_url: str) -> bool:
    """Whether ``stream_url`` is RTSP over TLS (case-insensitive scheme)."""
    return urlsplit(stream_url).scheme.lower() == "rtsps"


class _RtspSettings(NamedTuple):
    """The RTSP knobs of ``CaptureSettings`` that shape one grab."""

    timeout_seconds: float
    jpeg_quality: int
    wait_for_keyframe: bool


def _resolve_rtsp_settings() -> _RtspSettings:
    """Return the configured RTSP knobs, or the model defaults without a context.

    Mirrors the frame-size ceiling lookup: in a running service the context is
    always present; the fallback only matters outside a configured application.
    """
    from ..config.settings import CaptureSettings
    from ..runtime import get_context

    try:
        capture = get_context().settings.capture
    except (RuntimeError, AttributeError):
        capture = CaptureSettings()
    return _RtspSettings(
        capture.rtsp_timeout_seconds,
        capture.rtsp_jpeg_quality,
        capture.rtsp_wait_for_keyframe,
    )


def rtsp_capture_budget_seconds() -> float:
    """The whole budget of one RTSP grab: socket timeout plus the wall margin."""
    return _resolve_rtsp_settings().timeout_seconds + WALL_CLOCK_MARGIN_SECONDS


async def _kill_and_reap(process: asyncio.subprocess.Process) -> None:
    """SIGKILL ``process``'s whole group and wait for it to be reaped.

    The child is started with ``start_new_session=True``, so its process group
    id is its pid and the group never includes this application. The wait is
    shielded: a cancel arriving during teardown must not leave the child
    unreaped.
    """
    if process.returncode is None:
        # Not yet reaped, so the pid (and with it the group id) is still ours.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # already gone
        except PermissionError:  # pragma: no cover - the group is ours
            process.kill()
    await asyncio.shield(process.wait())


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


async def _contain_default_url(stream_url: str) -> str:
    """Return the URL ffmpeg may be given with the bound default login (#268).

    ffmpeg resolves the host itself, so a name could answer inside the allowed
    private subnets for the SSRF check and elsewhere for ffmpeg. The host is
    therefore resolved here (the shared validated answer the HTTP clients dial)
    and ffmpeg is handed the **IP literal** of the first address, after every
    address the host has was found inside the live subnets. An IP-literal host
    needs no rewrite. A name behind ``rtsps://`` cannot be rewritten (the
    certificate is checked against the name), so the default is refused there.

    :raises UnreachableCaptureError: the host resolves outside the subnets, or
        cannot be pinned for this scheme.
    """
    # Characters on which URL parsers disagree about the host (``urlsplit`` drops
    # tab/CR/LF silently): refuse them, as ``target_host`` does for binding.
    if any(ord(ch) < 32 or ch in "\\ \x7f" for ch in stream_url):
        raise UnreachableCaptureError(
            "rtsp stream URL blocked: it holds a control character, space or backslash"
        )
    split = urlsplit(stream_url)
    host = split.hostname
    if not host:
        raise UnreachableCaptureError(
            "rtsp stream URL has no host component to validate"
        )
    subnets = camera_allowed_subnets()
    try:
        addresses = await pinned_addresses_async(
            host, allow_private=True, allowed_private_subnets=subnets
        )
    except SsrfError as exc:
        raise UnreachableCaptureError(f"rtsp stream URL blocked: {exc}") from exc
    if not addresses or not all(
        address_inside_subnets(address, subnets) for address in addresses
    ):
        raise UnreachableCaptureError(f"rtsp stream URL blocked: {CONTAINED_MESSAGE}")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        is_literal = False
    else:
        is_literal = True  # ffmpeg does no lookup for a literal
    if not is_literal and split.scheme != "rtsp":
        raise UnreachableCaptureError(
            "rtsp stream URL blocked: the saved default login cannot be pinned "
            "to an address for this scheme; use an IP address or own credentials"
        )
    literal = f"[{addresses[0]}]" if ":" in addresses[0] else addresses[0]
    userinfo, at, _hostport = split.netloc.rpartition("@")
    try:
        port = f":{split.port}" if split.port else ""
    except ValueError as exc:  # a malformed port
        raise UnreachableCaptureError("rtsp stream URL has an invalid port") from exc
    return urlunsplit(split._replace(netloc=f"{userinfo}{at}{literal}{port}"))


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
        timeout_seconds: float | None = None,
        ffmpeg_binary: str = FFMPEG_BINARY,
        *,
        jpeg_quality: int | None = None,
        wait_for_keyframe: bool | None = None,
        contain_default: bool = False,
    ) -> None:
        """Remember the stream and the grab knobs; no I/O.

        ``timeout_seconds``, ``jpeg_quality`` and ``wait_for_keyframe`` default
        to the ``capture.rtsp_*`` settings when left unset.

        ``contain_default`` is set when ``credentials`` are the bound global
        default login. ffmpeg does its own DNS lookup, so a name could answer
        differently to it than to the check; the grab therefore dials the
        validated address instead (see :func:`_contain_default_url`).
        """
        self._contain_default = contain_default
        configured = _resolve_rtsp_settings()
        self._stream_url = self._apply_credentials(stream_url, credentials)
        self._transport = transport
        self._timeout_seconds = (
            configured.timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        self._ffmpeg_binary = ffmpeg_binary
        self._jpeg_quality = (
            configured.jpeg_quality if jpeg_quality is None else jpeg_quality
        )
        self._wait_for_keyframe = (
            configured.wait_for_keyframe
            if wait_for_keyframe is None
            else wait_for_keyframe
        )

    @property
    def capture_budget_seconds(self) -> float:
        """ffmpeg's socket timeout plus the wall margin, for the caller's bound."""
        return self._timeout_seconds + WALL_CLOCK_MARGIN_SECONDS

    @staticmethod
    def _apply_credentials(url: str, credentials: tuple[str, str] | None) -> str:
        """Embed credentials in the RTSP URL userinfo if not already present.

        RTSP auth is carried in the URL (``rtsp://user:pass@host/...``). FFmpeg's
        RTSP demuxer has no other source for it (no auth option, and ``-i`` cannot
        be read from a file), so the login is in ffmpeg's argv for the grab's
        lifetime; the mitigation is a host ``/proc`` mounted ``hidepid=2`` (#284).
        If the
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
        stream_url = self._stream_url
        if self._contain_default:
            stream_url = await _contain_default_url(stream_url)
        command = build_ffmpeg_command(
            stream_url,
            self._transport,
            self._timeout_seconds,
            ffmpeg_binary=self._ffmpeg_binary,
            jpeg_quality=self._jpeg_quality,
            wait_for_keyframe=self._wait_for_keyframe,
        )
        # Its own session, so one killpg takes ffmpeg and anything it spawned
        # and can never reach this process's group (#69 CAM-R3).
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        # A wall-clock guard slightly longer than ffmpeg's own socket timeout,
        # in case ffmpeg itself wedges. asyncio.timeout, not wait_for: on 3.11
        # wait_for can return ffmpeg's output and drop a cancel that lands at
        # the same moment (#33).
        wall_timeout = self.capture_budget_seconds
        try:
            async with asyncio.timeout(wall_timeout):
                stdout, stderr = await process.communicate()
        except TimeoutError as exc:
            await _kill_and_reap(process)
            raise TimeoutCaptureError(
                f"ffmpeg did not return a frame within {wall_timeout:.0f}s"
            ) from exc
        except BaseException:
            # The caller's budget expired, the supervisor is stopping, or the
            # task was cancelled: never leave ffmpeg running behind us.
            await _kill_and_reap(process)
            raise

        if process.returncode != 0 or not stdout:
            stderr_text = stderr.decode("utf-8", errors="replace")
            error_cls = classify_stderr(stderr_text)
            detail = stderr_text.strip().splitlines()
            # The last line can echo the stream URL, userinfo included (and a
            # legacy row's URL still carries it): redact before it is raised, so
            # no later consumer needs to remember to (#69 CAM-S2, #67-S14).
            message = redact_text(
                detail[-1] if detail else "ffmpeg failed with no output"
            )
            raise error_cls(f"rtsp capture failed: {message}")

        fmt = _imageinfo.detect_format(stdout) or "jpeg"
        dimensions = _imageinfo.read_dimensions(stdout)
        from .http_jpeg import _resolve_max_image_dimension_px, assert_frame_integrity

        width, height = assert_frame_integrity(stdout, fmt, dimensions)

        max_dimension = _resolve_max_image_dimension_px()
        if width > max_dimension or height > max_dimension:
            raise OtherCaptureError(
                f"rtsp frame dimensions {width}x{height} exceed the maximum of "
                f"{max_dimension}px per axis"
            )
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
