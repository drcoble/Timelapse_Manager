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
import ipaddress
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlsplit

import httpx

from ..security.ssrf import SsrfError, pinned_addresses_async
from .base import CaptureError, ValidationFailure
from .host_resolution import camera_allowed_subnets
from .http_auth import BasicPolicy, BasicRefusedError, CameraAuthState
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
    :param reason: why a non-ok probe failed, classified (``AUTH``,
        ``TIMEOUT``, ...), or ``None`` for a responder or an unclassified miss.
        Callers that need the failure category (the camera query's error codes,
        the HTTP probe's credential gate) read it here instead of probing the
        camera again (#69 CAM-P5).
    """

    protocol: str
    ok: bool
    snapshot_uri: str | None = None
    stream_uri: str | None = None
    confidence: Confidence = Confidence.LOW
    detail: str = ""
    reason: ValidationFailure | None = None


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
    """Return the bare host of an address (strips scheme, port, path and brackets).

    Handles a bare ``host``, ``host:port``, a URL, a bracketed IPv6 literal
    (``[fd00::1]``, ``[fd00::1]:554``) and a bare IPv6 literal (``fd00::1``,
    whose colons are not a port separator; #249). Brackets are not part of the
    returned host, so it can go straight to a resolver.
    """
    text = address.strip()
    if "://" not in text:
        authority = text.split("/", 1)[0]
        try:
            ipaddress.ip_address(authority)
        except ValueError:
            pass
        else:
            return authority
        text = f"//{text}"
    try:
        return urlsplit(text).hostname or address
    except ValueError:
        return address


def _uri_host(host: str) -> str:
    """Return ``host`` as it must appear in a URI: an IPv6 literal is bracketed."""
    return f"[{host}]" if ":" in host else host


def _probe_auth_state(allow_basic_over_http: bool) -> CameraAuthState | None:
    """A fresh auth state carrying the caller's Basic-over-HTTP opt-in.

    ``None`` (no opt-in) leaves each adapter on its own probe-policy state, which
    never sends Basic over plain HTTP. With the opt-in the state uses the same
    policy capture applies to a saved camera (#69 CAM-S1, OD-3): Basic over
    ``http://`` is allowed, https is unaffected, and nothing is ever downgraded
    from https. One state per probe: concurrent probes never share a nonce count.
    """
    if not allow_basic_over_http:
        return None
    return CameraAuthState(BasicPolicy.for_camera(True))


async def _probe_vapix(
    address: str,
    credentials: tuple[str, str] | None,
    http_client: httpx.AsyncClient,
    timeout: float,
    auth_state: CameraAuthState | None = None,
) -> ProtocolCandidate:
    """Probe the Axis VAPIX snapshot CGI; truly-detected on a returned image."""
    adapter = VapixAdapter(
        http_client,
        address=address,
        credentials=credentials,
        timeout=timeout,
        auth_state=auth_state,
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
        reason=result.reason,
    )


async def _probe_onvif(
    address: str,
    credentials: tuple[str, str] | None,
    http_client: httpx.AsyncClient,
    timeout: float,
    ffmpeg_binary: str,
    auth_state: CameraAuthState | None = None,
) -> ProtocolCandidate:
    """Query the ONVIF media service; truly-detected with self-reported URIs.

    The snapshot URI comes straight from the device's SOAP response and carries
    no embedded credentials. The stream URI is resolved through the adapter's
    own SSRF-guarded resolver but is surfaced *without* credentials embedded
    (the adapter only embeds them when it later hands the URL to ffmpeg), so the
    value returned here is safe to display.

    The media profile is resolved once, first: when that call fails nothing
    else is sent, so a camera that rejects the credentials sees one attempt
    from this probe, not one per URI (#69 CAM-P5). The failure's classified
    reason is kept on the candidate.
    """
    adapter = OnvifAdapter(
        http_client,
        address=address,
        credentials=credentials,
        timeout=timeout,
        ffmpeg_binary=ffmpeg_binary,
        auth_state=auth_state,
    )
    reason: ValidationFailure | None = None
    snapshot_uri: str | None = None
    stream_uri: str | None = None
    try:
        try:
            await adapter._resolve_profile_token()
        except CaptureError as exc:
            logger.debug("onvif profile resolution failed for %s: %s", address, exc)
            reason = exc.reason
        else:
            try:
                snapshot_uri = await adapter._resolve_snapshot_uri()
            except CaptureError as exc:
                logger.debug(
                    "onvif snapshot resolution failed for %s: %s", address, exc
                )
                reason = exc.reason
            try:
                stream_uri = await adapter._resolve_stream_uri()
            except CaptureError as exc:
                logger.debug("onvif stream resolution failed for %s: %s", address, exc)
                reason = reason or exc.reason
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
        reason=reason,
    )


async def _probe_rtsp(address: str, timeout: float) -> ProtocolCandidate:
    """Best-effort RTSP check: is the control port reachable?

    A successful TCP connect to the RTSP control port is the most we can confirm
    without credentials and a live stream URI, so this is a low-confidence
    candidate with no guaranteed stream URI. The connect is a plain asyncio
    connection under :func:`asyncio.timeout` (no worker thread, so a blackholed
    port holds nothing but the timer), to an address the SSRF guard validated
    (each tried in resolver order, so a dual-stack camera whose first answer is
    unreachable still connects over the other family).
    """
    host = _host_of(address)
    failure: Exception | None = None
    try:
        async with asyncio.timeout(timeout):
            addresses = await pinned_addresses_async(
                host,
                allow_private=True,
                allowed_private_subnets=camera_allowed_subnets(),
            )
            for ip in addresses:
                try:
                    _reader, writer = await asyncio.open_connection(ip, RTSP_PORT)
                except OSError as exc:
                    failure = exc
                    continue
                writer.close()
                failure = None
                break
    except TimeoutError as exc:
        failure = exc
    except SsrfError as exc:
        failure = exc
    if failure is not None:
        return ProtocolCandidate(
            protocol="rtsp",
            ok=False,
            confidence=Confidence.LOW,
            detail=f"RTSP control port {RTSP_PORT} not reachable: {failure}",
            reason=(
                ValidationFailure.TIMEOUT
                if isinstance(failure, TimeoutError)
                else ValidationFailure.UNREACHABLE
            ),
        )
    return ProtocolCandidate(
        protocol="rtsp",
        ok=True,
        # A reasonable default stream path; the operator confirms/edits it.
        stream_uri=f"rtsp://{_uri_host(host)}:{RTSP_PORT}/",
        confidence=Confidence.LOW,
        detail=(
            f"RTSP control port {RTSP_PORT} is reachable; the stream path is a "
            "guess to confirm."
        ),
    )


CredentialGate = Callable[[], Awaitable[bool]]


async def _probe_http(
    address: str,
    credentials: tuple[str, str] | None,
    http_client: httpx.AsyncClient,
    timeout: float,
    *,
    credentials_allowed: CredentialGate | None = None,
    auth_state: CameraAuthState | None = None,
) -> ProtocolCandidate:
    """Best-effort HTTP snapshot probe over a few common paths.

    GETs each candidate path in turn and accepts the first that returns a
    decodable image. Low confidence because a bare HTTP endpoint self-reports
    nothing -- a 200 image is the only signal.

    Each path is asked anonymously first. Credentials are sent only when a path
    answers ``401``/``403`` *and* ``credentials_allowed`` says so -- in
    :func:`detect_protocols`, when neither VAPIX nor ONVIF rejected them -- and
    only until one credentialed request is rejected. A camera given the wrong
    password therefore sees at most one failed login from this probe, not one
    per path, which keeps the probe from tripping a camera's brute-force lockout
    (#69 CAM-P5). ``credentials_allowed=None`` allows them.
    """
    base = _base_address(address)
    # None: undecided until a path asks for a login; then the gate decides once.
    send_credentials: bool | None = None if credentials is not None else False
    reason: ValidationFailure | None = None
    refusal: str | None = None
    for path in HTTP_SNAPSHOT_PATHS:
        url = f"{base}{path}"
        found, failure, refused = await _http_probe_get(
            http_client,
            url,
            credentials if send_credentials else None,
            timeout,
            auth_state,
        )
        if failure is ValidationFailure.AUTH and send_credentials is None:
            send_credentials = (
                credentials_allowed is None or await credentials_allowed()
            )
            if send_credentials:
                found, failure, refused = await _http_probe_get(
                    http_client, url, credentials, timeout, auth_state
                )
        # Basic over plain HTTP is refused before the password is sent (#69
        # CAM-S1, OD-3); the result says so instead of a bare "no image".
        refusal = refused or refusal
        if found:
            return ProtocolCandidate(
                protocol="http",
                ok=True,
                snapshot_uri=url,
                confidence=Confidence.LOW,
                detail=f"HTTP snapshot found at {path}.",
            )
        if failure is ValidationFailure.AUTH and send_credentials:
            # The camera rejected the login: send no more credentials.
            send_credentials = False
        if failure is not None and reason is not ValidationFailure.AUTH:
            reason = failure
    detail = "No common HTTP snapshot path returned an image."
    if refusal is not None:
        detail = f"{detail} The {refusal}."
    return ProtocolCandidate(
        protocol="http",
        ok=False,
        confidence=Confidence.LOW,
        detail=detail,
        reason=reason,
    )


async def _http_probe_get(
    http_client: httpx.AsyncClient,
    url: str,
    credentials: tuple[str, str] | None,
    timeout: float,
    auth_state: CameraAuthState | None = None,
) -> tuple[bool, ValidationFailure | None, str | None]:
    """GET one probe path: ``(found an image, why it failed, Basic refusal)``.

    A response that is not a decodable image is ``(False, None, None)``; a
    request error is ``(False, reason, None)``, ``AUTH`` for a 401/403. A
    Basic-only offer over plain HTTP is ``(False, AUTH, message)``: the
    password was never sent. Never raises a :class:`CaptureError`.
    """
    try:
        image_bytes = await http_get_image(
            http_client, url, credentials, timeout, auth_state=auth_state
        )
    except BasicRefusedError as exc:
        logger.debug("http snapshot probe %s refused Basic: %s", url, exc)
        return False, exc.reason, exc.message
    except CaptureError as exc:
        logger.debug("http snapshot probe %s failed: %s", url, exc)
        return False, exc.reason, None
    try:
        frame_from_bytes(image_bytes)
    except CaptureError as exc:
        logger.debug("http snapshot probe %s returned no image: %s", url, exc)
        return False, None, None
    return True, None, None


async def _run_probe(
    coro: Awaitable[ProtocolCandidate], protocol: str, timeout: float
) -> ProtocolCandidate:
    """Time-box one probe and turn any failure into a non-ok candidate.

    Wrapping each probe here means one protocol that hangs, times out, or raises
    can never sink the others: it degrades to a non-ok candidate while the rest
    complete. ``timeout`` is the per-probe budget; it is deliberately a little
    longer than the inner adapter/socket timeout so the adapter's own classified
    failure surfaces first when possible. The degraded candidate carries the
    failure's reason (``TIMEOUT`` for the budget, the capture error's own
    reason, else ``OTHER``).
    """
    confidence = Confidence.HIGH if protocol in ("vapix", "onvif") else Confidence.LOW
    try:
        async with asyncio.timeout(timeout):
            return await coro
    except TimeoutError:
        return ProtocolCandidate(
            protocol=protocol,
            ok=False,
            confidence=confidence,
            detail=f"{protocol} probe timed out after {timeout:.0f}s.",
            reason=ValidationFailure.TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 - one probe must never sink the rest
        logger.debug("%s probe raised: %s", protocol, exc)
        return ProtocolCandidate(
            protocol=protocol,
            ok=False,
            confidence=confidence,
            detail=f"{protocol} probe failed: {exc}",
            reason=(
                exc.reason if isinstance(exc, CaptureError) else ValidationFailure.OTHER
            ),
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
    allow_basic_over_http: bool = False,
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
    :data:`PROTOCOL_PRIORITY` order among the ``ok`` candidates. A non-ok
    candidate carries its classified ``reason``.

    The best-effort HTTP probe sends the credentials only when a path asks for
    a login and neither VAPIX nor ONVIF rejected them (see :func:`_probe_http`),
    so wrong credentials cost the camera one failed login per auth-capable
    protocol and none on the HTTP paths (#69 CAM-P5).

    :param address: the camera host/IP/URL, already SSRF-validated by the caller.
    :param credentials: the ``(username, password)`` to probe with, or None.
    :param http_client: the shared async HTTP client the probes borrow.
    :param ffmpeg_binary: ffmpeg used only by the ONVIF adapter's stream path.
    :param timeout: the per-probe time budget.
    :param allow_basic_over_http: the caller's opt-in to HTTP Basic over plain
        ``http://`` (#69 CAM-S1, OD-3), the same flag capture honours for a saved
        camera. Off by default: a probe never sends Basic over plain HTTP.
    """
    vapix = asyncio.ensure_future(
        _run_probe(
            _probe_vapix(
                address,
                credentials,
                http_client,
                timeout,
                _probe_auth_state(allow_basic_over_http),
            ),
            "vapix",
            timeout,
        )
    )
    onvif = asyncio.ensure_future(
        _run_probe(
            _probe_onvif(
                address,
                credentials,
                http_client,
                timeout,
                ffmpeg_binary,
                _probe_auth_state(allow_basic_over_http),
            ),
            "onvif",
            timeout,
        )
    )

    async def credentials_allowed() -> bool:
        """Whether VAPIX and ONVIF both finished without rejecting the login.

        Awaited by the HTTP probe only when a path asks for a login. Waiting
        never cancels the two probes; the HTTP probe's own budget bounds it.
        """
        await asyncio.wait((vapix, onvif))
        return all(
            task.result().reason is not ValidationFailure.AUTH
            for task in (vapix, onvif)
        )

    try:
        results = await asyncio.gather(
            vapix,
            onvif,
            _run_probe(_probe_rtsp(address, timeout), "rtsp", timeout),
            _run_probe(
                _probe_http(
                    address,
                    credentials,
                    http_client,
                    timeout,
                    credentials_allowed=credentials_allowed,
                    auth_state=_probe_auth_state(allow_basic_over_http),
                ),
                "http",
                timeout,
            ),
        )
    finally:
        # A caller's outer bound cancelling the gather must not leave the two
        # probe tasks running on.
        for task in (vapix, onvif):
            task.cancel()
    # Present in priority order so the UI lists the most-preferred first.
    order = {protocol: index for index, protocol in enumerate(PROTOCOL_PRIORITY)}
    candidates = sorted(results, key=lambda c: order.get(c.protocol, len(order)))
    return DetectionOutcome(
        candidates=candidates,
        recommended_primary=_recommend(candidates),
    )
