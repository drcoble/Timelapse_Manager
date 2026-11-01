"""Bind camera credentials to the hosts they were configured for (#69, #67-S3).

Two kinds of credential can reach a camera request:

* a camera's **own** credentials, which an operator typed for that camera. The
  edit route clears them when an edit moves the camera to a host it did not
  have before and the password is not retyped (:func:`introduces_new_host`),
  so a stored password never follows an address change to a new machine.
* the **global default**, which an inheriting camera receives without the
  operator typing anything. Left unbound, editing a camera's address to a
  public host (or running Detect against one) would send the fleet default
  there. :func:`bound_default_credentials` releases the default only when
  **every** host the camera's credentials can reach resolves wholly inside
  ``ssrf.allowed_private_subnets`` (owner decision OD-1; the cameras' own
  LAN), and fails closed on anything it cannot parse or resolve.

The host a request really connects to is the one ``httpx`` parses, so every
host here is extracted with :class:`httpx.URL`, after rejecting the characters
(whitespace, control characters, a backslash) on which URL parsers disagree.

The DNS lookups are blocking: call these helpers from a worker thread, after
the database session has been released.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from typing import Any
from urllib.parse import unquote

import httpx

from .ssrf import (
    SsrfError,
    _in_any,
    _normalise,
    _parse_allowed_subnets,
    pinned_addresses,
    pinned_addresses_async,
)

__all__ = [
    "DEFAULT_WITHHELD_HINT",
    "bound_default_credentials",
    "camera_credential_targets",
    "current_allowed_subnets",
    "default_still_bound",
    "default_withheld",
    "introduces_new_host",
    "may_substitute_default",
    "split_userinfo",
    "target_host",
    "uses_bound_default",
]

#: Shown beside a probe's result when the saved default login was held back
#: because the probed address is outside the allowed private subnets.
DEFAULT_WITHHELD_HINT = (
    "The saved default login is sent only to addresses inside the allowed "
    "private subnets, so this address was tried without it. Enter a username "
    "and password to test it."
)

# Characters on which URL parsers disagree about where the host ends.
_UNSAFE_URL_CHARS = frozenset("\\ \t\r\n")


def target_host(value: str | None, *, allow_userinfo: bool = True) -> str | None:
    """Return the lower-cased host a request to ``value`` would connect to.

    ``value`` is a camera address (a bare host, ``host:port`` or a URL) or a
    snapshot/stream URI. Returns ``None`` -- never a guess -- for an empty
    value, one with whitespace, a control character or a backslash, one that
    ``httpx`` cannot parse, or one with no host. A trailing dot is dropped and
    an IPv6 literal comes back without brackets. ``allow_userinfo=False`` also
    refuses a value with a ``user:pass@`` part (a URI a device returns has none).
    """
    if not value:
        return None
    text = value.strip()
    if not text or any(ch in _UNSAFE_URL_CHARS or ord(ch) < 32 for ch in text):
        return None
    if "://" not in text:
        text = f"http://{text}"
    try:
        url = httpx.URL(text)
        host = url.host
        userinfo = url.userinfo
    except (httpx.InvalidURL, ValueError):
        return None
    if userinfo and not allow_userinfo:
        return None
    host = host.rstrip(".") if host else ""
    return host or None


def split_userinfo(value: str) -> tuple[str, tuple[str, str] | None]:
    """Split ``user:pass@`` off a URL or ``host`` value.

    Returns ``(value without userinfo, (username, password) | None)``. The
    userinfo ends at the **last** ``@`` of the authority (the part before the
    first ``/``, ``?`` or ``#``), so a password holding an unescaped ``@`` is
    taken whole, and both halves are percent-decoded. A value with no userinfo
    comes back unchanged.
    """
    scheme, sep, rest = value.partition("://")
    if not sep:
        scheme, rest = "", value
    cut = len(rest)
    for delimiter in "/?#":
        index = rest.find(delimiter)
        if index != -1:
            cut = min(cut, index)
    authority, tail = rest[:cut], rest[cut:]
    userinfo, at, host = authority.rpartition("@")
    if not at:
        return value, None
    username, _, password = userinfo.partition(":")
    clean = f"{scheme}://{host}{tail}" if sep else f"{host}{tail}"
    return clean, (unquote(username), unquote(password))


def camera_credential_targets(camera: Any) -> list[str | None]:
    """Return the values whose hosts can receive the camera's credentials.

    The address plus any stored snapshot and stream URI: an adapter sends its
    credentials to whichever of them it fetches (``http`` cameras use only the
    snapshot URI, ``rtsp`` only the stream URI, ``vapix`` and ``onvif`` the
    address and an operator-stored URI). Binding all three is conservative: a
    stale URI on another host withholds the default rather than risking it.
    """
    return [
        getattr(camera, "address", None),
        getattr(camera, "snapshot_uri", None),
        getattr(camera, "stream_uri", None),
    ]


def _as_ips(
    addresses: Iterable[str],
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Parse validated address strings; empty if any is not an IP literal."""
    found: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for address in addresses:
        try:
            found.append(_normalise(ipaddress.ip_address(address)))
        except ValueError:
            return []
    return found


def _resolve_ips(
    host: str, allowed_private_subnets: Iterable[str]
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve ``host`` to every address it has; empty when it has none.

    Resolves through the SSRF layer's shared resolution cache under the camera
    policy (#248), so the answer decided on here is the one the camera clients
    dial while it is cached, instead of a separate lookup an attacker's DNS
    could answer differently. A host the policy denies (loopback, link-local,
    metadata) or that does not resolve yields nothing, so it is never bound.
    """
    try:
        return _as_ips(
            pinned_addresses(
                host,
                allow_private=True,
                allowed_private_subnets=tuple(allowed_private_subnets),
            )
        )
    except (SsrfError, UnicodeError):
        return []


async def _resolve_ips_async(
    host: str, allowed_private_subnets: Iterable[str]
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """:func:`_resolve_ips` for the event loop (same cache, resolve in a thread)."""
    try:
        return _as_ips(
            await pinned_addresses_async(
                host,
                allow_private=True,
                allowed_private_subnets=tuple(allowed_private_subnets),
            )
        )
    except (SsrfError, UnicodeError):
        return []


def may_substitute_default(
    hosts: Iterable[str | None], allowed_private_subnets: Iterable[str]
) -> bool:
    """Return whether the global default may be sent to every host in ``hosts``.

    True only when there is at least one host and **each** resolves, with every
    address it has, inside ``allowed_private_subnets``. A name with one address
    inside and one outside, an unresolvable name, an unparsable value and an
    empty subnet list all give ``False``. Blocking (DNS); call from a thread.

    :param hosts: values as :func:`target_host` accepts them; ``None`` entries
        (an unset field) are skipped, an unparsable one is a refusal.
    """
    policy = list(allowed_private_subnets)
    subnets = _parse_allowed_subnets(policy)
    if not subnets:
        return False
    checked = 0
    for value in hosts:
        if value is None or not value.strip():
            continue
        host = target_host(value)
        if host is None:
            return False
        addresses = _resolve_ips(host, policy)
        if not addresses or not all(_in_any(ip, subnets) for ip in addresses):
            return False
        checked += 1
    return checked > 0


def uses_bound_default(config: Any) -> bool:
    """Return whether ``config`` holds the global default as its credentials.

    True for a camera configuration that inherits the default, has no
    credentials of its own and was handed a (bound) default by the loader. Such
    a session depends on the camera's hosts staying inside the allowed private
    subnets; one with its own credentials, or none, does not.
    """
    own = getattr(config, "credentials", None)
    has_own = isinstance(own, dict) and bool(own.get("username"))
    return (
        not has_own
        and bool(getattr(config, "credentials_inherit_default", False))
        and getattr(config, "default_credentials", None) is not None
    )


async def default_still_bound(
    config: Any, allowed_private_subnets: Iterable[str]
) -> bool:
    """Return whether a session holding the bound default may still use it.

    The use-time half of :func:`bound_default_credentials` (#248, #223): the
    same rule -- every host the camera's credentials can reach resolves, with
    every address, inside ``allowed_private_subnets`` -- decided for the policy
    and the DNS answer **now**, through the shared resolution cache the camera
    clients dial from. A session cached when its hosts were inside the subnets
    therefore stops using the default as soon as an administrator narrows the
    list, or the name starts answering outside it, instead of at the next
    camera edit or restart. Runs on the event loop (the lookup, on a cache
    miss, runs in a worker thread).
    """
    policy = list(allowed_private_subnets)
    subnets = _parse_allowed_subnets(policy)
    if not subnets:
        return False
    checked = 0
    for value in camera_credential_targets(config):
        if value is None or not value.strip():
            continue
        host = target_host(value)
        if host is None:
            return False
        addresses = await _resolve_ips_async(host, policy)
        if not addresses or not all(_in_any(ip, subnets) for ip in addresses):
            return False
        checked += 1
    return checked > 0


def default_withheld(config: Any) -> bool:
    """Return whether ``config`` is an inheriting camera whose default was withheld.

    The loader marks it (``CaptureCameraConfig.default_withheld``) when a
    default is saved but binding held it back. Such a session holds no secret,
    so the registry asks :func:`default_still_bound` whether the verdict has
    changed (#269).
    """
    own = getattr(config, "credentials", None)
    has_own = isinstance(own, dict) and bool(own.get("username"))
    return (
        not has_own
        and bool(getattr(config, "credentials_inherit_default", False))
        and bool(getattr(config, "default_withheld", False))
    )


def _configured_subnets() -> list[str]:
    """The live ``ssrf.allowed_private_subnets`` (config plus admin list)."""
    from ..runtime import get_context

    return list(get_context().settings.ssrf.allowed_private_subnets)


def current_allowed_subnets() -> list[str]:
    """The live allowed private subnets, from the policy binding itself reads.

    The registry's use-time check and the loader's binding must decide against
    one source, so this is that source (looked up at each call). With no
    application context it returns no subnets (fail closed: the default is then
    withheld) rather than raising into a capture.
    """
    try:
        return _configured_subnets()
    except RuntimeError:
        return []


def bound_default_credentials(
    targets: Iterable[str | None],
    default: tuple[str, str] | None,
    allowed_private_subnets: Iterable[str] | None = None,
) -> tuple[str, str] | None:
    """Return ``default`` only if it may be sent to every target, else ``None``.

    The one rule for the global default credentials (owner decision OD-1): they
    go only to targets inside the allowed private subnets. Every place that
    resolves the default for a saved camera or an ad-hoc probe passes the
    result through here. Blocking (DNS); call from a worker thread, after the
    database session is released.

    :param targets: the camera's :func:`camera_credential_targets`, or the one
        address a probe is aimed at.
    :param default: the resolved ``(username, password)``, or ``None``.
    :param allowed_private_subnets: the policy; ``None`` reads the running
        application's setting.
    """
    if default is None:
        return None
    subnets = (
        _configured_subnets()
        if allowed_private_subnets is None
        else list(allowed_private_subnets)
    )
    return default if may_substitute_default(targets, subnets) else None


def introduces_new_host(
    before: Iterable[str | None], after: Iterable[str | None]
) -> bool:
    """Return whether ``after`` names a host that ``before`` did not.

    Used by the edit route: stored own credentials are kept across an edit only
    while every host that can receive them was already one of the camera's.
    Fails closed: an ``after`` value that cannot be parsed counts as new.
    """
    known = {host for host in map(target_host, before) if host is not None}
    for value in after:
        if value is None or not value.strip():
            continue
        host = target_host(value)
        if host is None or host not in known:
            return True
    return False
