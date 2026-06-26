"""One consolidated "query this camera" call for the add/edit camera flow.

The web layer collects, in a single operator action, everything it can learn
about a camera at an address: which protocols it speaks, its device-reported
geolocation, and its network hostname. Rather than make the UI orchestrate three
separate probes, :func:`query_camera` fans them out concurrently and returns a
single :class:`QueryResult` whose granular ``error_*`` fields let the UI render an
inline state for each probe independently -- one probe failing never blanks the
others.

This mirrors the device-geolocation poll pattern: it is *confirm-before-save*.
Nothing here is persisted. The returned data is what the UI shows the operator,
who then confirms which pieces to write.

Contract -- this module performs no SSRF resolution and creates no HTTP client:

* The **caller** MUST first validate ``address`` through the camera
  host-resolution / SSRF chokepoint (``resolve_camera_host``) and reject a denied
  address *before* calling :func:`query_camera`. The protocol probe and the
  metadata reads reach the network with no further guard, so the up-front resolve
  at the call site is load-bearing for safety.
* The **caller** passes in the supervisor-owned shared
  :class:`httpx.AsyncClient`; this module never constructs its own client.
* ``credentials`` is the camera's own credential document (the same
  ``{"username": ..., "password": ...}`` mapping the ORM stores), or None.
  ``default_credentials`` is the resolved global fallback ``(username,
  password)`` pair, used only when the camera carries no credentials of its own
  -- matching how the capture path resolves credentials.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from types import SimpleNamespace

import httpx

from .base import CameraAdapter, ValidationFailure
from .geolocation import get_camera_geolocation
from .http_jpeg import credentials_from
from .onvif import OnvifAdapter
from .probing import ProtocolCandidate, detect_protocols
from .vapix import VapixAdapter

logger = logging.getLogger(__name__)

# The per-probe / per-read time budget, mirroring the probing default. The whole
# query is a single operator action, so a few seconds of fan-out is acceptable.
DEFAULT_TIMEOUT = 8.0

# The protocols that can report device metadata (geolocation/hostname). The
# best-effort protocols (rtsp/http) expose no such query and inherit the base
# no-ops, so building a metadata adapter for them would be pointless (and routing
# them through the adapter factory would demand a snapshot/stream URI we do not
# have here).
_METADATA_PROTOCOLS = ("vapix", "onvif")


@dataclass
class QueryResult:
    """The consolidated outcome of querying a camera at one address.

    Field names are stable: the UI fragment depends on them.

    :param candidates: one entry per protocol probed, responders and
        non-responders alike, in priority order (the ``detect_protocols`` shape).
    :param recommended_primary: the protocol an operator should most likely pick,
        or None when nothing responded.
    :param ok_count: how many protocol candidates responded usefully.
    :param discovered_hostname: the device-reported hostname, or None.
    :param fetched_lat: the device-reported latitude, or None.
    :param fetched_lon: the device-reported longitude, or None.
    :param error_protocol: why protocol detection found nothing usable
        (``"unreachable"`` / ``"auth_failed"`` / ``"timeout"``), or None when at
        least one protocol responded.
    :param error_hostname: why no hostname was obtained (``"no_hostname"`` /
        ``"timeout"``), or None when a hostname was obtained.
    :param error_geo: why no location was obtained (``"no_location"`` /
        ``"unreachable"``), or None when a location was obtained.
    :param auth_rejected: True when an authentication-capable protocol
        (vapix/onvif) rejected the credentials even though a connection-only
        protocol (rtsp/http) still responded. Without this, wrong credentials
        would be masked by an open RTSP port, since ``error_protocol`` is only
        set when *nothing* responds. Always False when an auth-capable protocol
        was the recommended responder (its success means the credentials work).
    """

    candidates: list[ProtocolCandidate] = field(default_factory=list)
    recommended_primary: str | None = None
    ok_count: int = 0
    discovered_hostname: str | None = None
    fetched_lat: float | None = None
    fetched_lon: float | None = None
    error_protocol: str | None = None
    error_hostname: str | None = None
    error_geo: str | None = None
    auth_rejected: bool = False


def _resolve_credentials(
    credentials: dict | None,
    default_credentials: tuple[str, str] | None,
) -> tuple[str, str] | None:
    """Resolve the ``(username, password)`` tuple the probes/adapters should use.

    The camera's own credentials win; a camera with none falls back to the global
    default. ``credentials_from`` also transparently decrypts an at-rest-encrypted
    credential document, so the rest of this module is unaware of that detail.
    """
    own = credentials_from(SimpleNamespace(credentials=credentials))
    if own is not None:
        return own
    return default_credentials


def _build_metadata_adapter(
    *,
    protocol: str,
    address: str,
    credentials: tuple[str, str] | None,
    http_client: httpx.AsyncClient,
    timeout: float,
) -> CameraAdapter | None:
    """Construct the adapter used to read geolocation/hostname, or None.

    Only the metadata-capable protocols (vapix/onvif) yield an adapter; anything
    else returns None so the caller skips the metadata reads entirely. The
    adapters are constructed directly (not via the factory) because here we have
    only an address and credentials -- no persisted snapshot/stream URI -- and the
    metadata reads compose their own URLs from the address.
    """
    if protocol == "vapix":
        return VapixAdapter(
            http_client, address=address, credentials=credentials, timeout=timeout
        )
    if protocol == "onvif":
        return OnvifAdapter(
            http_client, address=address, credentials=credentials, timeout=timeout
        )
    return None


async def _auth_capable_failure_reasons(
    *,
    address: str,
    credentials: tuple[str, str] | None,
    http_client: httpx.AsyncClient,
    timeout: float,
) -> set[ValidationFailure]:
    """Re-validate the two auth-capable protocols and return their failure reasons.

    ``detect_protocols`` discards each probe's classified failure reason into a
    free-text detail, so to recover clean codes we re-run ``vapix`` and ``onvif``
    ``validate_connection`` (which never raises and carries a
    :class:`ValidationFailure` reason). The best-effort protocols (rtsp/http) give
    no auth/timeout signal worth classifying, so they are not consulted here.
    """
    vapix = VapixAdapter(
        http_client, address=address, credentials=credentials, timeout=timeout
    )
    onvif = OnvifAdapter(
        http_client, address=address, credentials=credentials, timeout=timeout
    )
    try:
        results = await asyncio.gather(
            vapix.validate_connection(),
            onvif.validate_connection(),
        )
    finally:
        await asyncio.gather(vapix.close(), onvif.close())
    return {
        result.reason
        for result in results
        if not result.ok and result.reason is not None
    }


async def _classify_protocol_error(
    *,
    address: str,
    credentials: tuple[str, str] | None,
    http_client: httpx.AsyncClient,
    timeout: float,
) -> str:
    """Classify *why* no protocol responded, into a granular error code.

    Reached only when nothing responded. Reduce the auth-capable protocols'
    failure reasons by specificity: an auth rejection is the most actionable
    signal, then a timeout, otherwise the address is treated as unreachable.
    """
    reasons = await _auth_capable_failure_reasons(
        address=address,
        credentials=credentials,
        http_client=http_client,
        timeout=timeout,
    )
    if ValidationFailure.AUTH in reasons:
        return "auth_failed"
    if ValidationFailure.TIMEOUT in reasons:
        return "timeout"
    # OTHER / UNSUPPORTED_CODEC / a bare unreachable all collapse here: the
    # allowed code set has no "other", so an unclassified failure reads as
    # unreachable.
    return "unreachable"


async def _read_metadata(
    adapter: CameraAdapter,
) -> tuple[float | None, float | None, str | None, str | None, str | None]:
    """Read geolocation and hostname from a metadata-capable adapter.

    Each read is independent and best-effort: one failing never sinks the other.
    Returns ``(lat, lon, hostname, error_geo, error_hostname)``.

    * ``get_camera_geolocation`` already swallows every failure into None, so a
      missing location is reported as ``"no_location"`` (the camera was reachable
      enough to probe but reported no usable position, or the lookup failed).
    * ``get_device_hostname`` is best-effort by contract and returns None on any
      reachability/parse problem; we treat None as ``"no_hostname"``. A read that
      times out is reported as ``"timeout"`` so the UI can distinguish a slow
      camera from one that simply has no hostname configured.
    """
    geo, (hostname, hostname_error) = await asyncio.gather(
        get_camera_geolocation(adapter),
        _safe_hostname(adapter),
    )

    if geo is not None:
        lat: float | None = geo.latitude
        lon: float | None = geo.longitude
        error_geo: str | None = None
    else:
        lat = lon = None
        error_geo = "no_location"

    error_hostname = None if hostname is not None else hostname_error
    return lat, lon, hostname, error_geo, error_hostname


async def _safe_hostname(adapter: CameraAdapter) -> tuple[str | None, str]:
    """Read a hostname, never raising; returns ``(hostname, error_code)``.

    The error code is only meaningful when the hostname is None: a timeout reads
    as ``"timeout"`` (a slow camera), any other failure or a clean "no hostname"
    reads as ``"no_hostname"``.
    """
    try:
        hostname = await adapter.get_device_hostname()
    except TimeoutError:
        return None, "timeout"
    except Exception as exc:  # noqa: BLE001 - metadata read must never propagate
        logger.debug("device hostname lookup failed: %s", exc)
        return None, "no_hostname"
    return hostname, "no_hostname"


async def query_camera(
    *,
    address: str,
    credentials: dict | None,
    http_client: httpx.AsyncClient,
    default_credentials: tuple[str, str] | None = None,
    ffmpeg_binary: str = "ffmpeg",
    timeout: float = DEFAULT_TIMEOUT,
) -> QueryResult:
    """Probe an address for protocols, geolocation, and hostname in one call.

    See the module docstring for the SSRF/client/credentials contract: the caller
    must SSRF-resolve ``address`` first, pass the shared ``http_client``, and pass
    the camera's own credential document plus any global ``default_credentials``.
    This function persists nothing -- it returns data for the UI to confirm.

    The protocol probe always runs. The metadata reads (geolocation, hostname)
    run only for a metadata-capable recommended protocol (vapix/onvif); when
    nothing responds, or the recommended protocol exposes no metadata query, the
    metadata fields are None with a granular ``error_*`` code so the UI renders an
    inline "not available" state rather than a blank.

    :param address: the camera host/IP/URL, already SSRF-validated by the caller.
    :param credentials: the camera's own credential document, or None.
    :param http_client: the supervisor-owned shared async HTTP client.
    :param default_credentials: the resolved global fallback pair, or None.
    :param ffmpeg_binary: ffmpeg used only by the ONVIF probe's stream path.
    :param timeout: the per-probe / per-read time budget.
    """
    resolved = _resolve_credentials(credentials, default_credentials)

    outcome = await detect_protocols(
        address,
        resolved,
        http_client,
        ffmpeg_binary=ffmpeg_binary,
        timeout=timeout,
    )
    ok_count = sum(1 for candidate in outcome.candidates if candidate.ok)

    result = QueryResult(
        candidates=outcome.candidates,
        recommended_primary=outcome.recommended_primary,
        ok_count=ok_count,
    )

    if ok_count == 0:
        # Nothing responded: classify the protocol failure and report the metadata
        # probes as unavailable (there is no reachable adapter to read them from).
        result.error_protocol = await _classify_protocol_error(
            address=address,
            credentials=resolved,
            http_client=http_client,
            timeout=timeout,
        )
        result.error_hostname = "no_hostname"
        result.error_geo = "unreachable"
        return result

    adapter = _build_metadata_adapter(
        protocol=outcome.recommended_primary or "",
        address=address,
        credentials=resolved,
        http_client=http_client,
        timeout=timeout,
    )
    if adapter is None:
        # A best-effort protocol (rtsp/http) responded but exposes no metadata
        # query: protocol detection succeeded, yet there is nothing to read. It
        # also means neither auth-capable protocol won, so check whether one was
        # rejected on authentication -- otherwise wrong credentials would be
        # silently masked by the open connection-only protocol.
        reasons = await _auth_capable_failure_reasons(
            address=address,
            credentials=resolved,
            http_client=http_client,
            timeout=timeout,
        )
        result.auth_rejected = ValidationFailure.AUTH in reasons
        result.error_hostname = "no_hostname"
        result.error_geo = "no_location"
        return result

    try:
        (
            result.fetched_lat,
            result.fetched_lon,
            result.discovered_hostname,
            result.error_geo,
            result.error_hostname,
        ) = await _read_metadata(adapter)
    finally:
        await adapter.close()
    return result
