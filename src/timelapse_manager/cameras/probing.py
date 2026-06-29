"""Multi-protocol detection probe for a camera at a given address.

Given one address and an optional credential pair, this probes every supported
camera protocol *concurrently* and returns the full set of responders -- not
just the first match. A camera (Axis devices are the norm) commonly answers on
several protocols at once; the caller surfaces all of them and lets the operator
pick, with a recommended primary suggested in priority order.

Two protocols are *truly detected* because the device self-reports a working
snapshot/stream URI: ONVIF (via its media service over SOAP) and VAPIX (via the
Axis snapshot CGI). The remaining two are *best-effort*: RTSP is a TCP
reachability check on the control port plus a default stream path, and HTTP
GETs a small set of common snapshot paths looking for an image response. Both
best-effort probes carry lower confidence and may yield no confirmed URI.

Contract -- this module performs no SSRF resolution and creates no HTTP client:

* The **caller** MUST first validate the address through the camera
  host-resolution / SSRF chokepoint (and reject a denied address) *before*
  calling :func:`detect_protocols`. ONVIF SOAP is not re-guarded at fetch time,
  so the up-front resolve at the call site is load-bearing for safety.
* The **caller** passes in the supervisor's shared :class:`httpx.AsyncClient`;
  this module never constructs its own client.
* Credentials are used verbatim. The caller is responsible for substituting any
  saved global default when the supplied credentials are blank -- this module
  takes no database session and makes no such decision.

For example, a caller resolves ``192.0.2.10`` through the guard, then calls
``await detect_protocols("192.0.2.10", ("user", "pass"), client,
ffmpeg_binary=..., timeout=...)``.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlsplit

import httpx

from .base import CaptureError
from .http_jpeg import frame_from_bytes, http_get_image
from .onvif import OnvifAdapter
from .vapix import VapixAdapter, build_snapshot_url

logger = logging.getLogger(__name__)

# Recommended-primary priority: a truly-detected, snapshot-capable protocol is
# preferred over the best-effort ones. VAPIX leads because, where present, it is
# the most direct and reliable Axis snapshot path.
PROTOCOL_PRIORITY = ("vapix", "onvif", "rtsp", "http")

# Best-effort RTSP control port.
RTSP_PORT = 554

# Best-effort HTTP snapshot paths probed in order; the first that returns an
# image wins. Kept short on purpose -- this is a hint, not an exhaustive scan.
HTTP_SNAPSHOT_PATHS = (
    "/snapshot.jpg",
    "/image.jpg",
    "/snapshot.cgi",
    "/cgi-bin/snapshot.cgi",
)


class Confidence(str, Enum):  # noqa: UP042 - stable str-mixin enum
    """How sure a candidate is, mirroring the truly-detected vs best-effort split.

    A ``str``-mixin enum (not :class:`enum.StrEnum`) so the value both *is* a
    ``str`` for serialisation and keeps predictable cross-version ``repr``,
    matching the convention the adapter vocabulary uses.
    """

    #: Device self-reported a working URI (ONVIF / VAPIX).
    HIGH = "high"
    #: Port/path was reachable but no URI was confirmed (RTSP / HTTP).
    LOW = "low"


@dataclass
class ProtocolCandidate:
    """One protocol's detection result for an address.

    :param protocol: the protocol family, e.g. ``"vapix"``.
    :param ok: True when the protocol responded usefully.
    :param snapshot_uri: a confirmed snapshot URL when detected, else None.
    :param stream_uri: a confirmed stream URL when detected, else None.
    :param confidence: :class:`Confidence` -- ``HIGH`` for truly-detected
        protocols, ``LOW`` for the best-effort ones.
    :param detail: a short, secret-free explanation safe to surface in the UI.
    """

    protocol: str
    ok: bool
    snapshot_uri: str | None = None
    stream_uri: str | None = None
    confidence: Confidence = Confidence.LOW
    detail: str = ""


@dataclass
class DetectionOutcome:
    """The full result of probing an address across all protocols.

    :param candidates: one entry per protocol probed, responders and
        non-responders alike, in :data:`PROTOCOL_PRIORITY` order.
    :param recommended_primary: the protocol an operator should most likely pick
        -- the first present of :data:`PROTOCOL_PRIORITY` among the ``ok``
        candidates -- or None when nothing responded.
    """

    candidates: list[ProtocolCandidate] = field(default_factory=list)
    recommended_primary: str | None = None


def _base_address(address: str) -> str:
    """Return ``scheme://host[:port]`` for an address, defaulting to http.

    Used to compose best-effort HTTP snapshot URLs from a bare address while
    honouring an explicit scheme/port the operator may have typed. Any userinfo
    (``user:pass@``) the operator may have embedded in the address is dropped --
    the composed URL must never carry credentials, since it is surfaced in the
    detection result and persisted as the snapshot URI.
    """
    if address.startswith(("http://", "https://")):
        split = urlsplit(address)
        host = split.hostname or ""
        if ":" in host:  # IPv6 literal -- restore the brackets a netloc needs.
            host = f"[{host}]"
        netloc = f"{host}:{split.port}" if split.port else host
        return f"{split.scheme}://{netloc}"
    return f"http://{address}"


def _host_of(address: str) -> str:
    """Return the bare host of an address (strips scheme, port, and path)."""
    if address.startswith(("http://", "https://", "rtsp://", "rtsps://")):
        return urlsplit(address).hostname or address
    # A bare ``host`` or ``host:port`` form.
    return address.split("/", 1)[0].split(":", 1)[0]


async def _probe_vapix(
    address: str,
    credentials: tuple[str, str] | None,
    http_client: httpx.AsyncClient,
    timeout: float,
) -> ProtocolCandidate:
    """Probe the Axis VAPIX snapshot CGI; truly-detected on a returned image."""
    adapter = VapixAdapter(
        http_client, address=address, credentials=credentials, timeout=timeout
    )
    try:
        result = await adapter.validate_connection()
    finally:
        await adapter.close()
    if result.ok:
        # The CGI path is composed from the address only -- no credentials are
        # embedded in this URL (VAPIX auth is sent in headers), so it is safe to
        # surface verbatim.
        snapshot_uri = build_snapshot_url(address)
        return ProtocolCandidate(
            protocol="vapix",
            ok=True,
            snapshot_uri=snapshot_uri,
            confidence=Confidence.HIGH,
            detail="Axis VAPIX snapshot CGI responded with an image.",
        )
    return ProtocolCandidate(
        protocol="vapix",
        ok=False,
        confidence=Confidence.HIGH,
        detail=f"No VAPIX response: {result.message}",
    )


async def _probe_onvif(
    address: str,
    credentials: tuple[str, str] | None,
    http_client: httpx.AsyncClient,
    timeout: float,
    ffmpeg_binary: str,
) -> ProtocolCandidate:
    """Query the ONVIF media service; truly-detected with self-reported URIs.

    The snapshot URI comes straight from the device's SOAP response and carries
    no embedded credentials. The stream URI is resolved through the adapter's
    own SSRF-guarded resolver but is surfaced *without* credentials embedded
    (the adapter only embeds them when it later hands the URL to ffmpeg), so the
    value returned here is safe to display.
    """
    adapter = OnvifAdapter(
        http_client,
        address=address,
        credentials=credentials,
        timeout=timeout,
        ffmpeg_binary=ffmpeg_binary,
    )
    try:
        snapshot_uri: str | None = None
        stream_uri: str | None = None
        try:
            snapshot_uri = await adapter._resolve_snapshot_uri()
        except CaptureError as exc:
            logger.debug("onvif snapshot resolution failed for %s: %s", address, exc)
        try:
            stream_uri = await adapter._resolve_stream_uri()
        except CaptureError as exc:
            logger.debug("onvif stream resolution failed for %s: %s", address, exc)
    finally:
        await adapter.close()

    if snapshot_uri is not None or stream_uri is not None:
        return ProtocolCandidate(
            protocol="onvif",
            ok=True,
            snapshot_uri=snapshot_uri,
            stream_uri=stream_uri,
            confidence=Confidence.HIGH,
            detail="ONVIF media service returned a media profile.",
        )
    return ProtocolCandidate(
        protocol="onvif",
        ok=False,
        confidence=Confidence.HIGH,
        detail="No ONVIF media profile resolved.",
    )


async def _probe_rtsp(address: str, timeout: float) -> ProtocolCandidate:
    """Best-effort RTSP check: is the control port reachable?

    A successful TCP connect to the RTSP control port is the most we can confirm
    without credentials and a live stream URI, so this is a low-confidence
    candidate with no guaranteed stream URI. The connection attempt runs in a
    worker thread so a slow/blocked connect cannot stall the event loop, and it
    is independently time-boxed by the caller.
    """
    host = _host_of(address)

    def _connect() -> None:
        with socket.create_connection((host, RTSP_PORT), timeout=timeout):
            return None

    try:
        await asyncio.to_thread(_connect)
    except OSError as exc:
        return ProtocolCandidate(
            protocol="rtsp",
            ok=False,
            confidence=Confidence.LOW,
            detail=f"RTSP control port {RTSP_PORT} not reachable: {exc}",
        )
    return ProtocolCandidate(
        protocol="rtsp",
        ok=True,
        # A reasonable default stream path; the operator confirms/edits it.
        stream_uri=f"rtsp://{host}:{RTSP_PORT}/",
        confidence=Confidence.LOW,
        detail=(
            f"RTSP control port {RTSP_PORT} is reachable; the stream path is a "
            "guess to confirm."
        ),
    )


async def _probe_http(
    address: str,
    credentials: tuple[str, str] | None,
    http_client: httpx.AsyncClient,
    timeout: float,
) -> ProtocolCandidate:
    """Best-effort HTTP snapshot probe over a few common paths.

    GETs each candidate path in turn and accepts the first that returns a
    decodable image. Low confidence because a bare HTTP endpoint self-reports
    nothing -- a 200 image is the only signal.
    """
    base = _base_address(address)
    for path in HTTP_SNAPSHOT_PATHS:
        url = f"{base}{path}"
        try:
            image_bytes = await http_get_image(http_client, url, credentials, timeout)
            frame_from_bytes(image_bytes)
        except CaptureError as exc:
            logger.debug("http snapshot probe %s failed: %s", url, exc)
            continue
        return ProtocolCandidate(
            protocol="http",
            ok=True,
            snapshot_uri=url,
            confidence=Confidence.LOW,
            detail=f"HTTP snapshot found at {path}.",
        )
    return ProtocolCandidate(
        protocol="http",
        ok=False,
        confidence=Confidence.LOW,
        detail="No common HTTP snapshot path returned an image.",
    )


async def _run_probe(coro: object, protocol: str, timeout: float) -> ProtocolCandidate:
    """Time-box one probe and turn any failure into a non-ok candidate.

    Wrapping each probe here means one protocol that hangs, times out, or raises
    can never sink the others: it degrades to a non-ok candidate while the rest
    complete. ``timeout`` is the per-probe budget; it is deliberately a little
    longer than the inner adapter/socket timeout so the adapter's own classified
    failure surfaces first when possible.
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout)  # type: ignore[arg-type]
    except TimeoutError:
        return ProtocolCandidate(
            protocol=protocol,
            ok=False,
            confidence=(
                Confidence.HIGH if protocol in ("vapix", "onvif") else Confidence.LOW
            ),
            detail=f"{protocol} probe timed out after {timeout:.0f}s.",
        )
    except Exception as exc:  # noqa: BLE001 - one probe must never sink the rest
        logger.debug("%s probe raised: %s", protocol, exc)
        return ProtocolCandidate(
            protocol=protocol,
            ok=False,
            confidence=(
                Confidence.HIGH if protocol in ("vapix", "onvif") else Confidence.LOW
            ),
            detail=f"{protocol} probe failed: {exc}",
        )


def _recommend(candidates: list[ProtocolCandidate]) -> str | None:
    """Return the first :data:`PROTOCOL_PRIORITY` protocol that is ``ok``."""
    ok_protocols = {c.protocol for c in candidates if c.ok}
    for protocol in PROTOCOL_PRIORITY:
        if protocol in ok_protocols:
            return protocol
    return None


async def detect_protocols(
    address: str,
    credentials: tuple[str, str] | None,
    http_client: httpx.AsyncClient,
    *,
    ffmpeg_binary: str = "ffmpeg",
    timeout: float = 8.0,
) -> DetectionOutcome:
    """Probe all supported protocols at ``address`` and return every responder.

    See the module docstring for the SSRF/client/credentials contract: the
    caller must SSRF-resolve ``address`` first, pass the shared ``http_client``,
    and substitute any default credentials before calling. This function adds no
    safety of its own beyond per-probe isolation and time-boxing.

    Each protocol is probed concurrently and independently time-boxed, so a
    slow or failing probe never blocks or sinks the others. The returned
    :class:`DetectionOutcome` lists one candidate per protocol (responders and
    non-responders) and names a recommended primary in
    :data:`PROTOCOL_PRIORITY` order among the ``ok`` candidates.

    :param address: the camera host/IP/URL, already SSRF-validated by the caller.
    :param credentials: the ``(username, password)`` to probe with, or None.
    :param http_client: the shared async HTTP client the probes borrow.
    :param ffmpeg_binary: ffmpeg used only by the ONVIF adapter's stream path.
    :param timeout: the per-probe time budget.
    """
    probes: list[tuple[str, object]] = [
        (
            "vapix",
            _probe_vapix(address, credentials, http_client, timeout),
        ),
        (
            "onvif",
            _probe_onvif(address, credentials, http_client, timeout, ffmpeg_binary),
        ),
        ("rtsp", _probe_rtsp(address, timeout)),
        ("http", _probe_http(address, credentials, http_client, timeout)),
    ]
    results = await asyncio.gather(
        *(_run_probe(coro, protocol, timeout) for protocol, coro in probes)
    )
    # Present in priority order so the UI lists the most-preferred first.
    order = {protocol: index for index, protocol in enumerate(PROTOCOL_PRIORITY)}
    candidates = sorted(results, key=lambda c: order.get(c.protocol, len(order)))
    return DetectionOutcome(
        candidates=candidates,
        recommended_primary=_recommend(candidates),
    )
