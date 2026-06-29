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

from ..security.ssrf import resolve_and_check


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
