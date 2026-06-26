"""Outbound-request guard against server-side request forgery (SSRF).

A single deny-list + resolver that every server-originated, *user-influenced*
network call routes through: camera-add probes, ONVIF/range scans, snapshot
fetches, and outbound notification webhooks. The guard answers one question --
*may the server open a connection to this target?* -- and raises before any
socket is opened when the answer is no.

Two tiers of address, and the order between them is load-bearing:

1. **Always blocked** -- loopback, link-local (including the cloud-metadata
   address ``169.254.169.254``), the unspecified range, multicast, and reserved
   space. No configuration ever relaxes these.
2. **Private / special-use that an admin may opt into** -- RFC-1918
   (``10/8``, ``172.16/12``, ``192.168/16``), CGNAT ``100.64/10``, and IPv6 ULA
   ``fc00::/7``. These are blocked by default but a camera/scan caller may pass
   ``allow_private=True`` together with the admin-configured
   ``allowed_private_subnets`` to let a *specific* subnet through. The webhook
   surface never opts in -- it always uses the full deny-list.

``allow_private=True`` does **not** mean "allow all private space"; it means
"honour ``allowed_private_subnets``". A private address that is not inside one of
the listed subnets is still rejected even with ``allow_private=True``.

Resolve-then-check: a hostname is resolved via :func:`socket.getaddrinfo` and
**every** returned A/AAAA address is validated; if *any* resolved address is
denied, the whole target is rejected. This closes the "name resolves to a public
address at check time, a private one at fetch time" trick for the check itself.

DNS-rebinding caveat: this module validates at *check* time. It does not, on its
own, pin the subsequent connection to the validated IP. A caller that hands the
original hostname to a fresh ``httpx``/``ffmpeg`` request lets the name be
re-resolved at connect time, reopening the rebinding window. Mitigations a caller
must pair with the guard: never follow redirects, keep request timeouts short,
and -- for the strongest guarantee -- connect by the validated IP with the
original Host header/SNI preserved (not implemented here; recorded as a residual
risk for the security audit).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from urllib.parse import urlsplit

# Special-use ranges that are *never* allowed, regardless of opt-in. Cloud
# metadata (169.254.169.254) is inside link-local and called out for reviewers.
_ALWAYS_DENIED: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),  # IPv4 loopback
    ipaddress.ip_network("::1/128"),  # IPv6 loopback
    ipaddress.ip_network("169.254.0.0/16"),  # IPv4 link-local (inc. metadata)
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
    ipaddress.ip_network("0.0.0.0/8"),  # "this network" / unspecified v4
    ipaddress.ip_network("::/128"),  # IPv6 unspecified
)

# The single cloud-metadata address, called out explicitly so a reviewer can
# confirm it is covered (it falls inside 169.254.0.0/16 above).
_CLOUD_METADATA_V4 = ipaddress.ip_address("169.254.169.254")

# Private / special-use ranges that an admin MAY opt into for camera/scan
# targets (never for webhooks). Blocked by default.
_PRIVATE_RANGES: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("10.0.0.0/8"),  # RFC-1918
    ipaddress.ip_network("172.16.0.0/12"),  # RFC-1918
    ipaddress.ip_network("192.168.0.0/16"),  # RFC-1918
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT (RFC-6598)
    ipaddress.ip_network("fc00::/7"),  # IPv6 unique-local (ULA)
)


class SsrfError(ValueError):
    """A target was rejected by the outbound-request guard.

    Subclasses :class:`ValueError` so existing call sites that already map a
    ``ValueError`` to an HTTP 400/422 keep working; the API layer can also catch
    this specific type to return a deny-specific response. The message never
    contains credentials -- only the host/IP that was rejected.
    """


def _normalise(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Collapse an IPv4-mapped IPv6 address to its IPv4 form.

    ``::ffff:127.0.0.1`` and ``::ffff:169.254.169.254`` are classic deny-list
    bypasses: as a v6 address they dodge the v4 ranges. Mapping them back to
    IPv4 before any membership test closes that hole.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _in_any(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    """Return whether ``ip`` is contained in any of ``networks`` (version-safe)."""
    return any(ip.version == net.version and ip in net for net in networks)


def _parse_allowed_subnets(
    allowed_private_subnets: Iterable[str],
) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse admin-configured opt-in CIDRs, skipping any malformed entry.

    A bad CIDR in configuration must never widen access, so an unparsable entry
    is simply dropped (it can match nothing) rather than raising.
    """
    parsed: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in allowed_private_subnets:
        try:
            parsed.append(ipaddress.ip_network(entry.strip(), strict=False))
        except (ValueError, AttributeError):
            continue
    return parsed


def assert_address_allowed(
    address: str,
    *,
    allow_private: bool = False,
    allowed_private_subnets: Iterable[str] = (),
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Validate a single *literal* IP address against the deny-list.

    :param address: a literal IPv4/IPv6 address (not a hostname).
    :param allow_private: when ``True``, a private address is permitted *only* if
        it is inside one of ``allowed_private_subnets``. Loopback/link-local/
        metadata/unspecified/multicast/reserved are never relaxed.
    :param allowed_private_subnets: admin opt-in CIDRs (camera/scan surface only).
    :returns: the parsed, normalised address when allowed.
    :raises SsrfError: when the address is denied or not a valid IP.
    """
    try:
        ip = _normalise(ipaddress.ip_address(address.strip()))
    except ValueError as exc:
        raise SsrfError(f"not a valid IP address: {address!r}") from exc

    # Tier 1: unconditional denies. Order matters -- these run before any opt-in.
    # The cloud-metadata address is checked by name first so the deny reason is
    # unambiguous in logs/audits (it also falls inside 169.254.0.0/16 below).
    if ip == _CLOUD_METADATA_V4:
        raise SsrfError(f"target {ip} is the cloud-metadata endpoint")
    if _in_any(ip, _ALWAYS_DENIED):
        raise SsrfError(
            f"target {ip} is in an always-blocked range (loopback/"
            "link-local/metadata/unspecified)"
        )
    if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
        raise SsrfError(f"target {ip} is loopback/link-local/unspecified")
    if ip.is_multicast:
        raise SsrfError(f"target {ip} is a multicast address")
    if ip.is_reserved:
        raise SsrfError(f"target {ip} is in reserved address space")

    # Tier 2: private / special-use. Allowed only via an explicit opt-in subnet.
    if _in_any(ip, _PRIVATE_RANGES) or ip.is_private:
        if allow_private:
            allowed = _parse_allowed_subnets(allowed_private_subnets)
            if _in_any(ip, allowed):
                return ip
            raise SsrfError(
                f"private target {ip} is not within any admin-allowed subnet"
            )
        raise SsrfError(f"target {ip} is in private/special-use space")

    return ip


def resolve_and_check(
    host: str,
    *,
    allow_private: bool = False,
    allowed_private_subnets: Iterable[str] = (),
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve ``host`` and validate **every** resolved address.

    A literal IP is checked directly. A hostname is resolved via
    :func:`socket.getaddrinfo`; every A/AAAA record returned is validated and the
    call is rejected if *any* one is denied (so a name that resolves to one
    public and one private address is rejected).

    :raises SsrfError: if any resolved address is denied, or resolution returns
        no usable address. A resolution *failure* (NXDOMAIN/no network) is
        surfaced to the caller as :class:`socket.gaierror`; callers that probe at
        fetch time decide whether that means "deny" or "defer".
    """
    # A literal IP needs no DNS round-trip.
    try:
        literal = ipaddress.ip_address(host.strip())
    except ValueError:
        literal = None
    if literal is not None:
        return [
            assert_address_allowed(
                host,
                allow_private=allow_private,
                allowed_private_subnets=allowed_private_subnets,
            )
        ]

    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    addresses = {str(info[4][0]) for info in infos}
    if not addresses:
        raise SsrfError(f"host {host!r} did not resolve to any address")

    validated: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for addr in addresses:
        validated.append(
            assert_address_allowed(
                addr,
                allow_private=allow_private,
                allowed_private_subnets=allowed_private_subnets,
            )
        )
    return validated


async def resolve_and_check_async(
    host: str,
    *,
    allow_private: bool = False,
    allowed_private_subnets: Iterable[str] = (),
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Async wrapper around :func:`resolve_and_check` for event-loop callers.

    :func:`resolve_and_check` calls the blocking :func:`socket.getaddrinfo`, which
    would stall the event loop if invoked directly from a coroutine (a slow or
    wedged resolver blocks *every* task). The capture path and the webhook
    dispatch run on the loop, so they must off-load the resolution to a worker
    thread. The synchronous API is left intact for non-async callers.

    The opt-in CIDR list is materialised before crossing the thread boundary so a
    lazy/once-only iterable is not consumed inside the worker.

    :raises SsrfError: if any resolved address is denied (propagated unchanged).
    :raises socket.gaierror: if resolution fails (propagated unchanged); callers
        decide whether that means "deny" or "defer".
    """
    subnets = tuple(allowed_private_subnets)
    return await asyncio.to_thread(
        resolve_and_check,
        host,
        allow_private=allow_private,
        allowed_private_subnets=subnets,
    )


def assert_allowed_url(
    url: str,
    *,
    allow_private: bool = False,
    allowed_private_subnets: Iterable[str] = (),
) -> str:
    """Validate the host of an outbound URL, returning the URL unchanged.

    Extracts the host component, resolves it, and validates every resolved
    address. The URL is returned verbatim so the caller still presents the
    original hostname (load-bearing for TLS verification and the ``Host``
    header); this guard does not rewrite the target.

    :raises SsrfError: when the URL has no host, the host resolves to a denied
        address, or (for the webhook surface) targets private space.
    """
    parsed = urlsplit(url)
    host = parsed.hostname
    if not host:
        raise SsrfError(f"outbound URL has no host component: {url!r}")
    try:
        resolve_and_check(
            host,
            allow_private=allow_private,
            allowed_private_subnets=allowed_private_subnets,
        )
    except socket.gaierror as exc:
        # An unresolvable webhook/snapshot host cannot be connected to anyway;
        # treat a resolution failure as a denial for URL targets (fail-closed).
        raise SsrfError(f"outbound URL host {host!r} did not resolve") from exc
    return url
