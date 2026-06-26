"""ONVIF camera discovery.

Two complementary mechanisms:

* :func:`discover_onvif` sends a WS-Discovery ``Probe`` to the standard
  multicast group ``239.255.255.250:3702`` and collects the unicast
  ``ProbeMatch`` replies. This finds ONVIF cameras on the local segment without
  knowing their addresses, but multicast does not cross routed subnets.
* :func:`scan_range` walks an explicit IP range or CIDR and sends a *unicast*
  WS-Discovery probe to each host, with bounded concurrency. This works across
  subnets where multicast does not.

Both run the blocking-socket work in a worker thread (via
:func:`asyncio.to_thread`) so they fit the asyncio capture engine without
holding the event loop. Every host address flows through
:func:`~.host_resolution.resolve_camera_host` at the top, the single seam where
a later phase can enforce an allow-list. Discovery never raises for ordinary
network errors; it logs and returns what it found.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import uuid
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx

from ..security.ssrf import SsrfError, assert_address_allowed
from . import _onvif_soap as soap
from .base import DiscoveredCamera
from .host_resolution import resolve_camera_host
from .onvif import OnvifAdapter

logger = logging.getLogger(__name__)

WS_DISCOVERY_ADDRESS = "239.255.255.250"
WS_DISCOVERY_PORT = 3702
_RECV_BUFFER = 65535


def _probe_message() -> bytes:
    """Build a WS-Discovery Probe for ONVIF NetworkVideoTransmitter devices."""
    message_id = f"uuid:{uuid.uuid4()}"
    body = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<s:Envelope xmlns:s="{soap.NS["s"]}" '
        f'xmlns:a="{soap.NS["a"]}" xmlns:d="{soap.NS["d"]}">'
        "<s:Header>"
        f"<a:MessageID>{message_id}</a:MessageID>"
        "<a:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To>"
        "<a:Action>"
        "http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe"
        "</a:Action>"
        "</s:Header>"
        "<s:Body><d:Probe>"
        '<d:Types xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        "dn:NetworkVideoTransmitter</d:Types>"
        "</d:Probe></s:Body></s:Envelope>"
    )
    return body.encode("utf-8")


def _parse_probe_match(data: bytes) -> DiscoveredCamera | None:
    """Turn a WS-Discovery ProbeMatch payload into a DiscoveredCamera, or None.

    The device service address (XAddrs) gives us the host; the scopes hint at
    the vendor. The actual snapshot/stream URIs are resolved lazily later by the
    ONVIF adapter, so they are left None here.
    """
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    xaddrs = soap.find_text(root, ".//d:XAddrs")
    if not xaddrs:
        return None
    # XAddrs may list several space-separated URLs; take the first reachable.
    first = xaddrs.split()[0]
    host = urlparse(first).hostname
    if not host:
        return None
    scopes = soap.find_text(root, ".//d:Scopes") or ""
    return DiscoveredCamera(
        address=host,
        protocol="onvif",
        snapshot_uri=None,
        stream_uri=None,
        geolocation=None,
        vendor=_vendor_from_scopes(scopes),
    )


def _vendor_from_scopes(scopes: str) -> str | None:
    """Extract a vendor/hardware name from WS-Discovery scope URIs, if present."""
    for scope in scopes.split():
        for marker in ("/name/", "/hardware/", "/mfr/"):
            if marker in scope:
                value = scope.rsplit("/", 1)[-1]
                if value:
                    return value
    return None


def _collect_multicast_replies(
    timeout_seconds: float, interface: str | None
) -> list[bytes]:
    """Send a multicast Probe and collect raw ProbeMatch datagrams."""
    replies: list[bytes] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        bind_host = interface or ""
        sock.bind((bind_host, 0))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(timeout_seconds)
        sock.sendto(_probe_message(), (WS_DISCOVERY_ADDRESS, WS_DISCOVERY_PORT))
        loop_deadline = timeout_seconds
        sock.settimeout(loop_deadline)
        while True:
            try:
                data, _addr = sock.recvfrom(_RECV_BUFFER)
            except TimeoutError:
                break
            if data:
                replies.append(data)
    except OSError as exc:
        logger.warning("ws-discovery multicast failed: %s", exc)
    finally:
        sock.close()
    return replies


def _unicast_probe(host: str, timeout_seconds: float) -> bytes | None:
    """Send a single unicast WS-Discovery probe to one host, return reply/None."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.settimeout(timeout_seconds)
        sock.sendto(_probe_message(), (host, WS_DISCOVERY_PORT))
        data, _addr = sock.recvfrom(_RECV_BUFFER)
        return data
    except (TimeoutError, OSError):
        return None
    finally:
        sock.close()


class InvalidScanRange(ValueError):
    """The scan input is neither a CIDR, an ``a-b`` range, nor a single host."""


class ScanRangeTooLarge(ValueError):
    """The scan input is valid but expands to more hosts than the cap allows.

    Carries the computed ``host_count`` and the ``max_hosts`` cap so a caller can
    surface both numbers to the operator. Subclasses :class:`ValueError` so older
    callers that catch ``ValueError`` keep working; the message contains
    ``"over the limit"`` so callers matching on that phrasing still match.
    """

    def __init__(self, spec: str, host_count: int, max_hosts: int) -> None:
        self.spec = spec
        self.host_count = host_count
        self.max_hosts = max_hosts
        super().__init__(
            f"scan range {spec!r} expands to {host_count} hosts, over the limit of "
            f"{max_hosts}; narrow the range or raise the cap"
        )


def count_hosts(cidr_or_range: str) -> int:
    """Count the hosts a CIDR or ``start-end`` range would expand to.

    Mirrors :func:`_hosts_from_spec` exactly (CIDR uses host enumeration, which
    for IPv4 drops the network/broadcast addresses except for ``/31`` and
    ``/32``), but computes the count arithmetically so a wide range is never
    materialised just to size it. This lets a cap be enforced *before* a large
    range is enumerated.

    :raises InvalidScanRange: when the input is not a CIDR, ``a-b`` range, or
        single host.
    """
    spec = cidr_or_range.strip()
    try:
        if "/" in spec:
            network = ipaddress.ip_network(spec, strict=False)
            # ``network.hosts()`` drops the network and broadcast address for a
            # block wider than a point-to-point link; ``/31`` and ``/32`` yield
            # every address. ``num_addresses`` counts all addresses, so subtract
            # the two endpoints only when there is room for them.
            total = network.num_addresses
            if network.prefixlen < network.max_prefixlen - 1:
                return total - 2
            return total
        if "-" in spec:
            start_str, _, end_str = spec.partition("-")
            start = ipaddress.ip_address(start_str.strip())
            end = ipaddress.ip_address(end_str.strip())
            if int(end) < int(start):
                raise InvalidScanRange(f"range end precedes start: {spec}")
            return int(end) - int(start) + 1
        # A single bare address.
        ipaddress.ip_address(spec)
        return 1
    except InvalidScanRange:
        raise
    except ValueError as exc:
        raise InvalidScanRange(f"not a valid CIDR, range, or address: {spec}") from exc


def check_scan_range(cidr_or_range: str, max_hosts: int) -> int:
    """Validate a scan spec and confirm it fits within the host cap.

    The single seam the web and API discovery paths share so their validation,
    host counting, and cap enforcement can never drift apart. Counts the hosts
    arithmetically (never enumerating a wide range) and compares the count to the
    cap, so an oversized range is rejected before any scan begins.

    :param cidr_or_range: a CIDR, ``a-b`` range, or single host address.
    :param max_hosts: the largest number of hosts a single scan may target.
    :returns: the host count, when the spec is valid and within the cap.
    :raises InvalidScanRange: when the input is malformed.
    :raises ScanRangeTooLarge: when the input is valid but over the cap.
    """
    host_count = count_hosts(cidr_or_range)
    if host_count > max_hosts:
        raise ScanRangeTooLarge(cidr_or_range.strip(), host_count, max_hosts)
    return host_count


def _hosts_from_spec(cidr_or_range: str) -> list[str]:
    """Expand a CIDR or ``start-end`` range into a list of host IP strings.

    :raises ValueError: when the input is neither a CIDR nor an ``a-b`` range.
    """
    spec = cidr_or_range.strip()
    if "/" in spec:
        network = ipaddress.ip_network(spec, strict=False)
        return [str(ip) for ip in network.hosts()]
    if "-" in spec:
        start_str, _, end_str = spec.partition("-")
        start = ipaddress.ip_address(start_str.strip())
        end = ipaddress.ip_address(end_str.strip())
        if int(end) < int(start):
            raise ValueError(f"range end precedes start: {spec}")
        return [
            str(ipaddress.ip_address(value))
            for value in range(int(start), int(end) + 1)
        ]
    # A single bare address.
    return [str(ipaddress.ip_address(spec))]


async def discover_onvif(
    timeout_seconds: float = 2.0, interface: str | None = None
) -> list[DiscoveredCamera]:
    """Discover ONVIF cameras on the local segment via WS-Discovery multicast.

    Never raises for network errors; returns an empty list and logs instead.

    :param timeout_seconds: how long to listen for replies.
    :param interface: local IP to bind/send from, or None for the default.
    """
    from ..runtime import get_context

    ssrf = get_context().settings.ssrf
    if interface is not None:
        interface = resolve_camera_host(interface)
    try:
        replies = await asyncio.to_thread(
            _collect_multicast_replies, timeout_seconds, interface
        )
    except OSError as exc:
        logger.warning("ws-discovery failed: %s", exc)
        return []

    discovered: dict[str, DiscoveredCamera] = {}
    for data in replies:
        camera = _parse_probe_match(data)
        if camera is None:
            continue
        # A device may advertise a denied address (e.g. 127.0.0.1) in its
        # XAddrs; never surface such a result as an addable camera.
        if not _discovered_address_allowed(camera.address, ssrf):
            logger.debug(
                "discovery dropping denied advertised address %s", camera.address
            )
            continue
        discovered[camera.address] = camera
    return list(discovered.values())


def _discovered_address_allowed(address: str, ssrf: Any) -> bool:
    """Return whether a discovery-advertised address passes the camera deny-list.

    A bare hostname (rare in XAddrs, which usually carries an IP) that does not
    parse as an IP is left to the fetch-time guard; only a literal denied IP is
    dropped here.
    """
    try:
        assert_address_allowed(
            address,
            allow_private=True,
            allowed_private_subnets=ssrf.allowed_private_subnets,
        )
    except SsrfError as exc:
        # A hostname (not a literal IP) is deferred to the fetch-time guard;
        # any other deny reason means the advertised address is blocked.
        return "not a valid IP" in str(exc)
    return True


async def scan_range(
    cidr_or_range: str,
    timeout_seconds: float = 2.0,
    max_concurrent: int = 10,
    per_host_timeout: float = 1.0,
) -> list[DiscoveredCamera]:
    """Unicast-probe each host in a CIDR/range for ONVIF, bounded concurrency.

    Useful across routed subnets where multicast discovery does not reach.
    Strictly bounded to the entered CIDR/range and capped at the configured
    ``max_scan_hosts`` so a scan cannot be turned into a wide network sweep. Each
    per-host probe target is validated against the camera deny-list (with the
    admin opt-in), so a probe never targets loopback/link-local/metadata and only
    reaches opted-in private space. Never raises for per-host network errors.

    :param cidr_or_range: ``"192.168.1.0/24"`` or ``"192.168.1.10-192.168.1.20"``.
    :param timeout_seconds: overall budget is governed by per-host timeout and
        concurrency; retained for signature symmetry with multicast discovery.
    :param max_concurrent: maximum simultaneous probes.
    :param per_host_timeout: socket timeout for each unicast probe.
    :raises ValueError: when the range exceeds the configured ``max_scan_hosts``.
    """
    from ..runtime import get_context

    ssrf = get_context().settings.ssrf
    # Validate, count, and cap-check through the one shared seam (which also backs
    # the web/API discovery handlers), so a cap change can never make the
    # pre-scan check and this check disagree. A malformed spec is logged and
    # yields no results; an over-cap spec raises, as before.
    try:
        check_scan_range(cidr_or_range, ssrf.max_scan_hosts)
    except InvalidScanRange as exc:
        logger.warning("invalid scan range %r: %s", cidr_or_range, exc)
        return []

    hosts = _hosts_from_spec(cidr_or_range)

    # Validate every target up front and drop denied addresses, so a probe is
    # never sent to a loopback/link-local/metadata/non-opted-in private host.
    allowed_hosts: list[str] = []
    for host in hosts:
        try:
            assert_address_allowed(
                host,
                allow_private=True,
                allowed_private_subnets=ssrf.allowed_private_subnets,
            )
        except SsrfError:
            logger.debug("scan skipping denied host %s", host)
            continue
        allowed_hosts.append(host)

    semaphore = asyncio.Semaphore(max(1, max_concurrent))

    async def probe(host: str) -> DiscoveredCamera | None:
        async with semaphore:
            data = await asyncio.to_thread(_unicast_probe, host, per_host_timeout)
        if data is None:
            return None
        camera = _parse_probe_match(data)
        if camera is not None:
            # Trust the probed (already-validated) address over a possibly
            # different XAddrs host.
            camera.address = host
        return camera

    results = await asyncio.gather(*(probe(host) for host in allowed_hosts))
    return [camera for camera in results if camera is not None]


async def resolve_discovered_uris(
    cameras: list[DiscoveredCamera],
    credentials: tuple[str, str] | None,
    http_client: httpx.AsyncClient,
    *,
    ffmpeg_binary: str = "ffmpeg",
    timeout: float = 5.0,
    max_concurrent: int = 10,
) -> list[DiscoveredCamera]:
    """Best-effort enrich discovered ONVIF cameras with their media URIs.

    Discovery only finds devices; the snapshot/stream URIs of an ONVIF camera
    live behind its media service and must be resolved over SOAP with the right
    credentials. For each ``"onvif"`` camera still missing a snapshot and/or
    stream URI this re-validates the address through the SSRF chokepoint (defence
    in depth -- a device that advertised an allowed host could still be denied on
    re-check), builds an :class:`~.onvif.OnvifAdapter` with the supplied
    ``credentials`` and the shared ``http_client``, and time-boxes a URI
    resolution. A resolved URI fills only a field that was ``None`` -- an
    operator-confirmed value is never overwritten.

    Like :func:`~.probing.detect_protocols` this is pure: it opens no HTTP client
    of its own, touches no database, and does not resolve the default credential
    itself -- the caller resolves the effective credential and passes the shared
    client. A camera that fails, times out, or is denied is returned unchanged
    (its URIs stay ``None``); one camera never raises for, or blocks, the others.

    :param cameras: the discovered cameras to enrich, mutated in place.
    :param credentials: the ``(username, password)`` to resolve with, or None.
    :param http_client: the shared async HTTP client the adapters borrow.
    :param ffmpeg_binary: ffmpeg used only on the ONVIF adapter's stream path.
    :param timeout: per-camera resolution budget.
    :param max_concurrent: maximum simultaneous resolutions.
    :returns: the same list, with resolvable ONVIF URIs filled in.
    """
    semaphore = asyncio.Semaphore(max(1, max_concurrent))

    async def enrich(camera: DiscoveredCamera) -> None:
        if camera.protocol != "onvif":
            return
        if camera.snapshot_uri is not None and camera.stream_uri is not None:
            return
        # Defence in depth: a discovery-advertised address that passed the
        # discovery-time check is re-validated here before any fetch is issued.
        try:
            resolve_camera_host(camera.address)
        except SsrfError:
            logger.debug("skipping enrichment for denied address %s", camera.address)
            return
        adapter = OnvifAdapter(
            http_client,
            address=camera.address,
            credentials=credentials,
            timeout=timeout,
            ffmpeg_binary=ffmpeg_binary,
        )
        try:
            async with semaphore:
                snapshot_uri, stream_uri = await asyncio.wait_for(
                    adapter.resolve_uris(), timeout
                )
        except Exception as exc:  # noqa: BLE001 - one camera must never sink the rest
            # A slow, refused, or otherwise-failing device leaves its URIs None;
            # it must not raise out and sink the rest of the scan.
            logger.debug("enrichment failed for %s: %s", camera.address, exc)
            return
        finally:
            await adapter.close()
        if camera.snapshot_uri is None and snapshot_uri is not None:
            camera.snapshot_uri = snapshot_uri
        if camera.stream_uri is None and stream_uri is not None:
            camera.stream_uri = stream_uri

    await asyncio.gather(*(enrich(camera) for camera in cameras))
    return cameras
