"""The single chokepoint through which camera/host addresses flow.

This validates a camera address against the outbound-request deny-list and
returns it **unchanged** when allowed. It must *not* rewrite the address: the
original hostname is load-bearing for TLS certificate verification and HTTP
``Host`` headers, and adapters/discovery probes pass it straight into HTTP
requests and ffmpeg URLs. So the contract is validate-and-reject, never rewrite.

Policy: the camera/scan surface uses the deny-list with the admin opt-in. An
admin may opt specific private subnets in (cameras normally live on private
LANs); loopback, link-local, and the cloud-metadata address are never relaxed.

A *literal* denied IP is rejected here. A hostname that cannot be resolved is
**allowed through** at this seam (an admin may add a camera while the camera is
offline or the name not yet in DNS); the actual SSRF enforcement for hostnames
happens fail-closed at the fetch/probe path, which re-validates before opening a
connection. A hostname that *does* resolve to a denied address is rejected.
"""

from __future__ import annotations

import socket

from ..security.ssrf import (
    SsrfError,
    pinned_addresses_async,
    resolve_and_check,
    resolve_and_check_async,
)


def resolve_camera_host(address: str) -> str:
    """Validate ``address`` against the camera deny-list; return it unchanged.

    :param address: a hostname, IP, or full URL host component as configured.
    :returns: the same value, verbatim, when allowed.
    :raises SsrfError: when the address (or a name it resolves to) is denied.
    """
    if not address:
        return address

    # Imported lazily so this module stays importable without a running app
    # context (e.g. in isolated unit tests of the pure guard).
    from ..runtime import get_context

    ssrf = get_context().settings.ssrf
    try:
        resolve_and_check(
            address,
            allow_private=True,
            allowed_private_subnets=ssrf.allowed_private_subnets,
        )
    except socket.gaierror:
        # Unresolvable at add time: defer enforcement to the fetch/probe path,
        # which re-validates fail-closed before connecting.
        return address
    return address


def camera_allowed_subnets() -> tuple[str, ...]:
    """Return the administrator's private-subnet opt-in for camera traffic.

    Read from the running application's settings **at the call**, so an edit an
    administrator makes applies to the next check and the next connection. With
    no application context installed it returns no subnets, so private
    addresses are denied (fail-closed) rather than the lookup raising.
    """
    from ..runtime import get_context

    try:
        ssrf = get_context().settings.ssrf
    except RuntimeError:
        return ()
    return tuple(ssrf.allowed_private_subnets)


async def check_camera_url_async(url: str) -> None:
    """Validate a camera URL's host under the camera policy, off the event loop.

    The coroutine counterpart of the ``assert_allowed_url`` calls the adapters
    made inline, with the same verdicts: every address the host resolves to is
    checked, and a host that does not resolve is a denial. The resolve runs in a
    worker thread and an allowed answer is cached for a short time
    (:func:`~timelapse_manager.security.ssrf.pinned_addresses_async`), which is
    also what the camera clients connect to
    (:mod:`timelapse_manager.security.pinned_http`): the address checked here is
    the address dialled, and a name whose DNS answer changes is re-resolved and
    re-checked once the cached answer expires.

    :raises SsrfError: the URL has no usable host, the host does not resolve, or
        any address it resolves to is denied.
    """
    from urllib.parse import urlsplit

    try:
        host = urlsplit(url).hostname
    except ValueError as exc:
        raise SsrfError("outbound URL is malformed") from exc
    if not host:
        raise SsrfError(f"outbound URL has no host component: {url!r}")
    await pinned_addresses_async(
        host, allow_private=True, allowed_private_subnets=camera_allowed_subnets()
    )


async def resolve_camera_host_async(address: str) -> str:
    """The coroutine form of :func:`resolve_camera_host`: same verdicts, no stall.

    Validates ``address`` and returns it unchanged. A hostname that does not
    resolve is allowed through, exactly as in the synchronous seam, so an
    administrator can still add a camera while it is offline. The resolve runs
    in a worker thread.

    :raises SsrfError: the address (or a name it resolves to) is denied.
    """
    if not address:
        return address
    try:
        await resolve_and_check_async(
            address,
            allow_private=True,
            allowed_private_subnets=camera_allowed_subnets(),
        )
    except socket.gaierror:
        return address
    return address
