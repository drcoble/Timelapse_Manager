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

from .base import (
    CameraAdapter,
    GeoLocation,
    GeolocationAccessDenied,
    ValidationFailure,
)
from .clients import contained_client
from .geolocation import get_camera_geolocation
from .http_auth import BasicPolicy, CameraAuthState
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
        ``"access_denied"`` / ``"unreachable"``), or None when a location was
        obtained. ``"access_denied"`` means the device refused the read for
        this account; ``geo_required_role`` then names the role that can read it.
    :param geo_required_role: the device role that can read the location
        (e.g. ``"Administrator"``) when ``error_geo`` is ``"access_denied"``,
        else None.
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
    geo_required_role: str | None = None
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
    allow_basic_over_http: bool = False,
) -> CameraAdapter | None:
    """Construct the adapter used to read geolocation/hostname, or None.

    Only the metadata-capable protocols (vapix/onvif) yield an adapter; anything
    else returns None so the caller skips the metadata reads entirely. The
    adapters are constructed directly (not via the factory) because here we have
    only an address and credentials -- no persisted snapshot/stream URI -- and the
    metadata reads compose their own URLs from the address.
    """
    state = (
        CameraAuthState(BasicPolicy.for_camera(True)) if allow_basic_over_http else None
    )
    if protocol == "vapix":
        return VapixAdapter(
            http_client,
            address=address,
            credentials=credentials,
            timeout=timeout,
            auth_state=state,
        )
    if protocol == "onvif":
        return OnvifAdapter(
            http_client,
            address=address,
            credentials=credentials,
            timeout=timeout,
            auth_state=state,
        )
    return None


def _auth_capable_failure_reasons(
    candidates: list[ProtocolCandidate],
) -> set[ValidationFailure]:
    """Return the classified failure reasons of the two auth-capable probes.

    The protocol probe already recorded why ``vapix`` and ``onvif`` failed on
    each candidate, so nothing is sent to the camera again: a re-probe would
    cost a camera given the wrong password a second failed login per protocol
    and bring an Axis brute-force lockout closer (#69 CAM-P5). The best-effort
    protocols (rtsp/http) give no auth/timeout signal worth classifying, so
    they are not consulted here.
    """
    return {
        candidate.reason
        for candidate in candidates
        if candidate.protocol in _METADATA_PROTOCOLS
        and not candidate.ok
        and candidate.reason is not None
    }


def _classify_protocol_error(candidates: list[ProtocolCandidate]) -> str:
    """Classify *why* no protocol responded, into a granular error code.

    Reached only when nothing responded. Reduce the auth-capable protocols'
    failure reasons by specificity: an auth rejection is the most actionable
    signal, then a timeout, otherwise the address is treated as unreachable.
    """
    reasons = _auth_capable_failure_reasons(candidates)
    if ValidationFailure.AUTH in reasons:
        return "auth_failed"
    if ValidationFailure.TIMEOUT in reasons:
        return "timeout"
    # OTHER / UNSUPPORTED_CODEC / a bare unreachable all collapse here: the
    # allowed code set has no "other", so an unclassified failure reads as
    # unreachable.
    return "unreachable"


@dataclass
class _Metadata:
    """The device metadata one adapter yielded, with a reason for each gap."""

    latitude: float | None
    longitude: float | None
    hostname: str | None
    error_geo: str | None
    error_hostname: str | None
    geo_required_role: str | None


async def _read_metadata(adapter: CameraAdapter) -> _Metadata:
    """Read geolocation and hostname from a metadata-capable adapter.

    Each read is independent and best-effort: one failing never sinks the other,
    because both helpers below report their failure as a code instead of raising.

    * A missing location is ``"no_location"`` (the camera was reachable enough to
      probe but reported no usable position, or the lookup failed), or
      ``"access_denied"`` when the device refused the read for this account.
    * ``get_device_hostname`` is best-effort by contract and returns None on any
      reachability/parse problem; we treat None as ``"no_hostname"``. A read that
      times out is reported as ``"timeout"`` so the UI can distinguish a slow
      camera from one that simply has no hostname configured.
    """
    (geo, error_geo, required_role), (hostname, hostname_error) = await asyncio.gather(
        _safe_geolocation(adapter),
        _safe_hostname(adapter),
    )
    return _Metadata(
        latitude=geo.latitude if geo is not None else None,
        longitude=geo.longitude if geo is not None else None,
        hostname=hostname,
        error_geo=error_geo,
        error_hostname=None if hostname is not None else hostname_error,
        geo_required_role=required_role,
    )


async def _safe_geolocation(
    adapter: CameraAdapter,
) -> tuple[GeoLocation | None, str | None, str | None]:
    """Read a location, never raising; returns ``(geo, error_code, role)``.

    ``error_code`` is None when a location was read, ``"access_denied"`` when the
    device refused this account (``role`` then names the role that can read it),
    and ``"no_location"`` otherwise -- the resolver already folds every other
    lookup failure into None.
    """
    try:
        geo = await get_camera_geolocation(adapter)
    except GeolocationAccessDenied as exc:
        return None, "access_denied", exc.required_role
    if geo is None:
        return None, "no_location", None
    return geo, None, None


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
    allow_basic_over_http: bool = False,
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
    :param allow_basic_over_http: the form's opt-in to HTTP Basic over plain
        ``http://`` (#69 CAM-S1, OD-3), applied to the protocol probes and the
        metadata reads exactly as capture applies a saved camera's flag.
    """
    resolved = _resolve_credentials(credentials, default_credentials)
    if resolved is not None and resolved == default_credentials:
        # The global default login is contained to the allowed private subnets
        # at connect time (#268); the camera's own credentials are not.
        own = credentials_from(SimpleNamespace(credentials=credentials))
        if own is None:
            http_client = contained_client(http_client)

    outcome = await detect_protocols(
        address,
        resolved,
        http_client,
        ffmpeg_binary=ffmpeg_binary,
        timeout=timeout,
        allow_basic_over_http=allow_basic_over_http,
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
        result.error_protocol = _classify_protocol_error(outcome.candidates)
        result.error_hostname = "no_hostname"
        result.error_geo = "unreachable"
        return result

    adapter = _build_metadata_adapter(
        protocol=outcome.recommended_primary or "",
        address=address,
        credentials=resolved,
        http_client=http_client,
        timeout=timeout,
        allow_basic_over_http=allow_basic_over_http,
    )
    if adapter is None:
        # A best-effort protocol (rtsp/http) responded but exposes no metadata
        # query: protocol detection succeeded, yet there is nothing to read. It
        # also means neither auth-capable protocol won, so check whether one was
        # rejected on authentication -- otherwise wrong credentials would be
        # silently masked by the open connection-only protocol.
        reasons = _auth_capable_failure_reasons(outcome.candidates)
        result.auth_rejected = ValidationFailure.AUTH in reasons
        result.error_hostname = "no_hostname"
        result.error_geo = "no_location"
        return result

    try:
        metadata = await _read_metadata(adapter)
    finally:
        await adapter.close()
    result.fetched_lat = metadata.latitude
    result.fetched_lon = metadata.longitude
    result.discovered_hostname = metadata.hostname
    result.error_geo = metadata.error_geo
    result.error_hostname = metadata.error_hostname
    result.geo_required_role = metadata.geo_required_role
    return result
