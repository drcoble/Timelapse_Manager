"""Trusted-proxy network parsing shared by the web layer and the token policy.

One implementation, so the login throttle / effective-client resolution
(:mod:`..web.middleware`) and the bearer-token peer policy
(:mod:`.token`) can never disagree about which peers are trusted proxies
(#174). Entries are parsed once per distinct list (an LRU cache) and each
malformed entry is logged once, as a WARNING, when that list is first used
(#67 R17). Malformed entries are dropped rather than raised so one bad config
value cannot wedge request handling.
"""

from __future__ import annotations

import functools
import ipaddress
import logging
from collections.abc import Sequence

logger = logging.getLogger(__name__)

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

TRUSTED_PROXIES_SETTING = "server.trusted_proxies"


def parse_networks(
    entries: Sequence[str], setting: str = TRUSTED_PROXIES_SETTING
) -> list[IpNetwork]:
    """Parse address / CIDR entries into networks, dropping malformed ones.

    Bare addresses become host networks (``/32`` or ``/128``); CIDR blocks are
    taken as written. ``setting`` only names the config key in the one-time
    warning for a dropped entry.
    """
    return list(parse_networks_cached(tuple(entries), setting))


@functools.lru_cache(maxsize=16)
def parse_networks_cached(
    entries: tuple[str, ...], setting: str = TRUSTED_PROXIES_SETTING
) -> tuple[IpNetwork, ...]:
    """Parse ``entries`` once; see :func:`parse_networks`."""
    networks: list[IpNetwork] = []
    for entry in entries:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            # The entry is operator configuration, not request data; ``%r``
            # keeps any control characters in it from forging log lines.
            logger.warning(
                "%s entry %r is not an IP address or network; it is ignored",
                setting,
                entry,
            )
    return tuple(networks)


def parse_address(value: str | None) -> IpAddress | None:
    """Parse ``value`` into an IP address, normalising IPv4-mapped IPv6 forms.

    Returns ``None`` for anything that is not a valid IP literal. An
    IPv4-mapped IPv6 address (``::ffff:a.b.c.d``) is folded to its IPv4 form so
    it matches IPv4 trusted-proxy entries and produces a stable client key.
    """
    if not value:
        return None
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def in_networks(address: IpAddress, networks: Sequence[IpNetwork]) -> bool:
    """Return whether ``address`` falls in any of ``networks`` (version-aware)."""
    return any(
        address.version == network.version and address in network
        for network in networks
    )
