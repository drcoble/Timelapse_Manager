"""Unit tests for the camera host-resolution seam.

The function validates a camera address against the outbound-request deny-list
and returns it unchanged when allowed, or raises SsrfError when denied.

These tests patch timelapse_manager.runtime.get_context so the guard's
SSRF-settings lookup has a stub settings object without requiring a full
database or lifespan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

from timelapse_manager.cameras.host_resolution import resolve_camera_host
from timelapse_manager.security.ssrf import SsrfError

# ---------------------------------------------------------------------------
# Helpers: minimal stub context
# ---------------------------------------------------------------------------


@dataclass
class _StubSsrfSettings:
    allowed_private_subnets: list[str] = field(default_factory=list)


def _stub_context(allowed_private_subnets: list[str] | None = None) -> MagicMock:
    """Return a minimal mock AppContext with configurable SSRF settings."""
    ssrf = _StubSsrfSettings(allowed_private_subnets=allowed_private_subnets or [])
    ctx = MagicMock()
    ctx.settings.ssrf = ssrf
    return ctx


def _patch_context(allowed_private_subnets: list[str] | None = None):
    """Return a context manager patching get_context in timelapse_manager.runtime."""
    ctx = _stub_context(allowed_private_subnets)
    return patch("timelapse_manager.runtime.get_context", return_value=ctx)


# ---------------------------------------------------------------------------
# Tests: addresses that are always allowed (public routable space)
# ---------------------------------------------------------------------------


class TestResolveCameraHostAllowed:
    def test_public_ip_returned_unchanged(self) -> None:
        # 8.8.8.8 is a well-known public IP (not private, not reserved in Python 3.11).
        with _patch_context():
            assert resolve_camera_host("8.8.8.8") == "8.8.8.8"

    def test_empty_string_returned_unchanged_without_context(self) -> None:
        # Empty address short-circuits before the context lookup.
        assert resolve_camera_host("") == ""

    def test_unresolvable_hostname_allowed_through(self) -> None:
        """A hostname that does not resolve is allowed through at add-time.

        Enforcement is deferred to the fetch path (fail-closed) so an admin
        can add a camera while it is temporarily offline or not yet in DNS.
        """
        with _patch_context():
            result = resolve_camera_host("camera.invalid.does.not.resolve.example")
        assert result == "camera.invalid.does.not.resolve.example"


# ---------------------------------------------------------------------------
# Tests: private addresses require an opt-in subnet
# ---------------------------------------------------------------------------


class TestResolveCameraHostPrivate:
    def test_rfc1918_address_allowed_when_subnet_opted_in(self) -> None:
        with _patch_context(["10.0.0.0/8"]):
            assert resolve_camera_host("10.0.0.1") == "10.0.0.1"

    def test_192_168_allowed_when_subnet_opted_in(self) -> None:
        with _patch_context(["192.168.0.0/16"]):
            assert resolve_camera_host("192.168.100.200") == "192.168.100.200"

    def test_rfc1918_address_denied_when_no_subnet_opted_in(self) -> None:
        with _patch_context([]), pytest.raises(SsrfError):
            resolve_camera_host("10.0.0.1")

    def test_rfc1918_address_denied_when_wrong_subnet_opted_in(self) -> None:
        # 10.x is not within the opted-in 192.168.x subnet.
        with _patch_context(["192.168.0.0/16"]), pytest.raises(SsrfError):
            resolve_camera_host("10.0.0.1")


# ---------------------------------------------------------------------------
# Tests: always-denied addresses (loopback, link-local)
# ---------------------------------------------------------------------------


class TestResolveCameraHostAlwaysDenied:
    def test_loopback_ipv4_always_denied(self) -> None:
        """127.0.0.1 is in the always-blocked tier regardless of opt-in."""
        with _patch_context(["127.0.0.0/8"]), pytest.raises(SsrfError):
            resolve_camera_host("127.0.0.1")

    def test_loopback_ipv6_always_denied(self) -> None:
        """::1 is in the always-blocked tier; opt-in cannot relax it."""
        with _patch_context(["::1/128"]), pytest.raises(SsrfError):
            resolve_camera_host("::1")

    def test_link_local_always_denied(self) -> None:
        """169.254.x.x (link-local / cloud-metadata range) is always blocked."""
        with _patch_context(["169.254.0.0/16"]), pytest.raises(SsrfError):
            resolve_camera_host("169.254.1.1")

    def test_cloud_metadata_address_always_denied(self) -> None:
        """The cloud-metadata endpoint is explicitly always blocked."""
        with _patch_context(["169.254.0.0/16"]), pytest.raises(SsrfError):
            resolve_camera_host("169.254.169.254")

    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",
            "127.255.255.255",
            "::1",
            "169.254.1.1",
            "169.254.169.254",
        ],
    )
    def test_always_blocked_ranges_cannot_be_opted_in(self, address: str) -> None:
        """No opt-in config can relax the always-blocked ranges."""
        with (
            _patch_context(["127.0.0.0/8", "::1/128", "169.254.0.0/16", "fe80::/10"]),
            pytest.raises(SsrfError),
        ):
            resolve_camera_host(address)
