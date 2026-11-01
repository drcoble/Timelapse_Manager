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

DNS-rebinding caveat: the resolve-then-check above validates at *check* time. A
caller that then hands the original hostname to a fresh request lets the name be
re-resolved at connect time, reopening the rebinding window between the check and
the connect. The strongest mitigation is to connect by the validated IP while
still presenting the original hostname as the ``Host`` header / TLS SNI:
:func:`resolve_and_check_pinned` (and its async variant) returns a single
validated IP literal for exactly this, and the raw-socket event path connects to
it with ``server_hostname`` set to the original host so certificate verification
and name-based virtual hosting still work. Callers that go through the shared
``httpx`` client cannot pin a single request to an IP without rebuilding the
client (and dropping its connection-pool hardening), so they retain the
check-time guard and pair it with: never follow redirects, and keep request
timeouts short.

Async URL checks and the resolution cache (#67 P6): :func:`assert_allowed_url`
resolves with the blocking :func:`socket.getaddrinfo`, so an event-loop caller
uses :func:`assert_allowed_url_async`, which resolves in a worker thread and
caches each *allowed* resolution (host and policy flags to the validated
addresses) for 60 s in a bounded LRU map. Denials and resolution failures are
never cached.

A cached allow is only safe if the connection then goes to a cached address:
a caller that lets ``httpx`` resolve the name again would reopen DNS rebinding
(public at check time, ``127.0.0.1`` at connect time). So the cache is the
*source of the connect address*, not a pass: :func:`pinned_addresses_async`
returns the validated IPs (from the cache, or a fresh resolve-and-check), and
:mod:`timelapse_manager.security.pinned_http` connects every outbound webhook
to exactly that IP while keeping the hostname for TLS SNI and ``Host``. The
check and the connect then use the same address, whatever the resolver
answers in between. :func:`resolved_ips_for` exposes the cached addresses for
other callers that pin.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
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


def _resolve_and_validate(
    host: str,
    *,
    allow_private: bool,
    allowed_private_subnets: Iterable[str],
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve ``host`` and return every validated address, in resolution order.

    A literal IP is checked directly (no DNS round-trip). A hostname is resolved
    via :func:`socket.getaddrinfo`; every A/AAAA record is validated and the call
    is rejected if *any* one is denied. Duplicate addresses are collapsed while
    the resolver's preference order is preserved, so the first element is the
    address the OS would have connected to first.
    """
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
    ordered: list[str] = []
    seen: set[str] = set()
    for info in infos:
        addr = str(info[4][0])
        if addr not in seen:
            seen.add(addr)
            ordered.append(addr)
    if not ordered:
        raise SsrfError(f"host {host!r} did not resolve to any address")
    return [
        assert_address_allowed(
            addr,
            allow_private=allow_private,
            allowed_private_subnets=allowed_private_subnets,
        )
        for addr in ordered
    ]


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
    return _resolve_and_validate(
        host,
        allow_private=allow_private,
        allowed_private_subnets=allowed_private_subnets,
    )


def resolve_and_check_pinned(
    host: str,
    *,
    allow_private: bool = False,
    allowed_private_subnets: Iterable[str] = (),
) -> str:
    """Resolve and validate ``host``, returning one validated IP to connect to.

    This is the DNS-rebinding-safe counterpart to :func:`resolve_and_check`. It
    validates **every** resolved address (rejecting the whole target if any is
    denied, identical security to :func:`resolve_and_check`) and returns the
    first one as a literal-IP string. The caller connects to that IP directly --
    so the name cannot be re-resolved to a denied address between the check and
    the connect -- while still presenting the original ``host`` as the ``Host``
    header / TLS SNI so certificate verification and virtual hosting keep working.

    :returns: a validated IP literal (an IPv4-mapped IPv6 address is normalised to
        its IPv4 form, so the returned string is always a plain v4/v6 literal).
    :raises SsrfError: if any resolved address is denied, or resolution returns no
        usable address.
    :raises socket.gaierror: if resolution fails (propagated unchanged).
    """
    validated = _resolve_and_validate(
        host,
        allow_private=allow_private,
        allowed_private_subnets=allowed_private_subnets,
    )
    return str(validated[0])


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


async def resolve_and_check_pinned_async(
    host: str,
    *,
    allow_private: bool = False,
    allowed_private_subnets: Iterable[str] = (),
) -> str:
    """Async wrapper around :func:`resolve_and_check_pinned` for loop callers.

    Off-loads the blocking :func:`socket.getaddrinfo` to a worker thread (a slow
    or wedged resolver must not stall the event loop), then returns the single
    validated IP literal the caller should connect to. The opt-in CIDR list is
    materialised before crossing the thread boundary so a lazy iterable is not
    consumed inside the worker.

    :raises SsrfError: if any resolved address is denied (propagated unchanged).
    :raises socket.gaierror: if resolution fails (propagated unchanged).
    """
    subnets = tuple(allowed_private_subnets)
    return await asyncio.to_thread(
        resolve_and_check_pinned,
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


# How long an allowed resolution is reused, and how many are kept.
_URL_CACHE_TTL_SECONDS = 60.0
_URL_CACHE_MAX_ENTRIES = 512

# (host, allow_private, allowed subnets) -> (expiry, validated addresses). The
# port is not part of the key: resolution and the verdict do not depend on it.
_CacheKey = tuple[str, bool, tuple[str, ...]]


class _ResolutionCache:
    """A bounded, thread-safe LRU of allowed resolutions with a TTL.

    The clock is injectable so tests can expire entries without sleeping.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float,
        max_entries: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self.clock = clock
        self._entries: OrderedDict[_CacheKey, tuple[float, tuple[str, ...]]] = (
            OrderedDict()
        )
        self._lock = threading.Lock()

    def get(self, key: _CacheKey) -> tuple[str, ...] | None:
        """Return the cached addresses for ``key``, or None if absent or expired."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires, addresses = entry
            if self.clock() >= expires:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return addresses

    def put(self, key: _CacheKey, addresses: tuple[str, ...]) -> None:
        """Cache ``addresses`` for ``key``, evicting the least recently used."""
        with self._lock:
            self._entries[key] = (self.clock() + self._ttl, addresses)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        """Drop every entry."""
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


_resolution_cache = _ResolutionCache(
    ttl_seconds=_URL_CACHE_TTL_SECONDS, max_entries=_URL_CACHE_MAX_ENTRIES
)


def clear_resolution_cache() -> None:
    """Drop every cached resolution (tests; or after a policy change)."""
    _resolution_cache.clear()


def _url_host(url: str) -> str:
    """Return the URL's host, rejecting a URL without a usable host or port.

    :raises SsrfError: no host, or a malformed port (``urlsplit`` raises a plain
        ``ValueError`` for one, which callers that catch only ``SsrfError``
        would miss).
    """
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise SsrfError("outbound URL is malformed") from exc
    if not host:
        raise SsrfError(f"outbound URL has no host component: {url!r}")
    return host


def _cache_key(
    host: str, allow_private: bool, allowed_private_subnets: Iterable[str]
) -> _CacheKey:
    """The cache key: the verdict depends on the policy flags, not just the host.

    The subnet list is normalised (stripped, de-duplicated, sorted) so the same
    policy given in a different order shares an entry, and a changed admin
    allow-list never reuses a verdict made under the old one.
    """
    subnets = tuple(sorted({entry.strip() for entry in allowed_private_subnets}))
    return (host.lower().rstrip("."), allow_private, subnets)


def _literal_ip(host: str) -> str | None:
    """Return ``host`` as an IP literal string, or None for a hostname.

    Only what :mod:`ipaddress` accepts is a literal (dotted-quad IPv4, IPv6,
    optionally with a zone id). Anything else, including the legacy decimal
    (``2130706433``) and octal (``0177.0.0.1``) forms, is a *hostname*: it goes
    through :func:`socket.getaddrinfo`, and the address the resolver produces is
    what is validated and what a pinned caller connects to, so the guard and
    the socket can never read the host differently.
    """
    try:
        return str(ipaddress.ip_address(host.strip().strip("[]")))
    except ValueError:
        return None


async def pinned_addresses_async(
    host: str,
    *,
    allow_private: bool = False,
    allowed_private_subnets: Iterable[str] = (),
) -> tuple[str, ...]:
    """Return every validated IP for ``host``, in resolver order, to connect to.

    A literal IP is validated as is. Otherwise a cached allowed resolution is
    reused (60 s); failing that, the host is resolved in a worker thread, every
    address validated (any denied address rejects the host), and the result
    cached. The caller connects to these addresses directly, so no later
    resolution can redirect the connection.

    :raises SsrfError: the host is denied or does not resolve.
    """
    subnets = tuple(allowed_private_subnets)
    literal = _literal_ip(host)
    if literal is not None:
        return (
            str(
                assert_address_allowed(
                    literal,
                    allow_private=allow_private,
                    allowed_private_subnets=subnets,
                )
            ),
        )
    key = _cache_key(host, allow_private, subnets)
    cached = _resolution_cache.get(key)
    if cached:
        return cached
    try:
        addresses = await resolve_and_check_async(
            host, allow_private=allow_private, allowed_private_subnets=subnets
        )
    except socket.gaierror as exc:
        raise SsrfError(f"outbound host {host!r} did not resolve") from exc
    validated = tuple(str(address) for address in addresses)
    _resolution_cache.put(key, validated)
    return validated


def pinned_addresses(
    host: str,
    *,
    allow_private: bool = False,
    allowed_private_subnets: Iterable[str] = (),
) -> tuple[str, ...]:
    """The blocking form of :func:`pinned_addresses_async`, on the same cache.

    For a caller that is already in a worker thread (credential binding) and
    must decide on the very answer a connection will dial: it reads the shared
    resolution cache and, on a miss, resolves, validates and caches exactly as
    the async form does, so the next connection under this policy reuses it
    (#248). Blocking: never call it on the event loop.

    :raises SsrfError: the host is denied or does not resolve.
    """
    subnets = tuple(allowed_private_subnets)
    literal = _literal_ip(host)
    if literal is not None:
        return (
            str(
                assert_address_allowed(
                    literal,
                    allow_private=allow_private,
                    allowed_private_subnets=subnets,
                )
            ),
        )
    key = _cache_key(host, allow_private, subnets)
    cached = _resolution_cache.get(key)
    if cached:
        return cached
    try:
        addresses = resolve_and_check(
            host, allow_private=allow_private, allowed_private_subnets=subnets
        )
    except socket.gaierror as exc:
        raise SsrfError(f"outbound host {host!r} did not resolve") from exc
    validated = tuple(str(address) for address in addresses)
    _resolution_cache.put(key, validated)
    return validated


async def pinned_address_async(
    host: str,
    *,
    allow_private: bool = False,
    allowed_private_subnets: Iterable[str] = (),
) -> str:
    """Return the first validated IP for ``host`` (see
    :func:`pinned_addresses_async`).

    :raises SsrfError: the host is denied or does not resolve.
    """
    addresses = await pinned_addresses_async(
        host,
        allow_private=allow_private,
        allowed_private_subnets=allowed_private_subnets,
    )
    return addresses[0]


async def assert_allowed_url_async(
    url: str,
    *,
    allow_private: bool = False,
    allowed_private_subnets: Iterable[str] = (),
) -> str:
    """Validate an outbound URL's host without blocking the event loop.

    The async counterpart of :func:`assert_allowed_url`, with the same verdicts:
    every resolved address is validated and a resolution failure is a denial.
    An allowed resolution is cached (see :func:`pinned_addresses_async`), so the
    pinned connection that follows normally needs no second lookup.

    This is an early, readable rejection only. It does **not** make a later
    request that re-resolves the name safe: the request must connect to the
    validated address (:mod:`timelapse_manager.security.pinned_http`).

    :returns: the URL unchanged (the caller still presents the original
        hostname for TLS and the ``Host`` header).
    :raises SsrfError: when the URL has no usable host, the host does not
        resolve, or any resolved address is denied.
    """
    host = _url_host(url)
    await pinned_addresses_async(
        host,
        allow_private=allow_private,
        allowed_private_subnets=allowed_private_subnets,
    )
    return url


def resolved_ips_for(
    url: str,
    *,
    allow_private: bool = False,
    allowed_private_subnets: Iterable[str] = (),
) -> tuple[str, ...] | None:
    """Return the validated addresses cached for ``url`` under this policy.

    For a caller that wants to connect to the IP validated by
    :func:`assert_allowed_url_async` (DNS-rebinding pinning), with the same
    policy flags it validated under. A literal-IP URL returns that IP. Returns
    ``None`` when nothing valid is cached (never validated, denied, or
    expired); this never resolves.
    """
    try:
        host = _url_host(url)
    except SsrfError:
        return None
    subnets = tuple(allowed_private_subnets)
    literal = _literal_ip(host)
    if literal is not None:
        try:
            assert_address_allowed(
                literal, allow_private=allow_private, allowed_private_subnets=subnets
            )
        except SsrfError:
            return None
        return (literal,)
    return _resolution_cache.get(_cache_key(host, allow_private, subnets))
