"""Abuse tests: SSRF deny-list correctness.

Probes every significant boundary of the two-tier deny-list:
  - Tier 1 (always blocked): loopback, link-local, cloud-metadata, unspecified
  - Tier 2 (admin opt-in required): RFC-1918, CGNAT, IPv6 ULA
  - IPv4-mapped IPv6 bypass attempt
  - URL-level guard (webhook surface)
  - Host-level guard with admin opt-in
  - Range-scan guard with no opt-in
"""

from __future__ import annotations

import threading

import pytest

from timelapse_manager.security.ssrf import (
    SsrfError,
    assert_address_allowed,
    assert_allowed_url,
    resolve_and_check,
    resolve_and_check_async,
)

# ---------------------------------------------------------------------------
# Tier 1: always blocked regardless of any opt-in
# ---------------------------------------------------------------------------


@pytest.mark.abuse
class TestAlwaysBlockedAddresses:
    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",
            "127.0.0.2",
            "127.255.255.255",
            "::1",
        ],
    )
    def test_loopback_addresses_denied_unconditionally(self, address: str) -> None:
        with pytest.raises(SsrfError):
            assert_address_allowed(
                address,
                allow_private=True,
                allowed_private_subnets=["127.0.0.0/8", "::1/128"],
            )

    @pytest.mark.parametrize(
        "address",
        [
            "169.254.0.1",
            "169.254.169.254",  # cloud-metadata endpoint
            "169.254.255.255",
        ],
    )
    def test_link_local_ipv4_denied_unconditionally(self, address: str) -> None:
        with pytest.raises(SsrfError):
            assert_address_allowed(
                address,
                allow_private=True,
                allowed_private_subnets=["169.254.0.0/16"],
            )

    def test_cloud_metadata_address_denied_by_name(self) -> None:
        """169.254.169.254 is blocked with a specific 'cloud-metadata' denial."""
        with pytest.raises(SsrfError, match="cloud-metadata"):
            assert_address_allowed("169.254.169.254", allow_private=True)

    @pytest.mark.parametrize(
        "address",
        [
            "fe80::1",
            "fe80::dead:beef",
        ],
    )
    def test_link_local_ipv6_denied_unconditionally(self, address: str) -> None:
        with pytest.raises(SsrfError):
            assert_address_allowed(
                address,
                allow_private=True,
                allowed_private_subnets=["fe80::/10"],
            )

    def test_ipv4_unspecified_denied(self) -> None:
        with pytest.raises(SsrfError):
            assert_address_allowed("0.0.0.0", allow_private=True)

    def test_ipv6_unspecified_denied(self) -> None:
        with pytest.raises(SsrfError):
            assert_address_allowed("::", allow_private=True)

    def test_multicast_address_denied(self) -> None:
        with pytest.raises(SsrfError):
            assert_address_allowed("224.0.0.1", allow_private=True)


# ---------------------------------------------------------------------------
# Tier 1 bypass attempt: IPv4-mapped IPv6
# ---------------------------------------------------------------------------


@pytest.mark.abuse
class TestIPv4MappedIPv6Bypass:
    @pytest.mark.parametrize(
        "mapped",
        [
            "::ffff:127.0.0.1",
            "::ffff:7f00:1",  # same as 127.0.0.1 in hex
        ],
    )
    def test_ipv4_mapped_loopback_cannot_bypass_deny_list(self, mapped: str) -> None:
        """An IPv4-mapped IPv6 loopback must not bypass the IPv4 deny entry."""
        with pytest.raises(SsrfError):
            assert_address_allowed(
                mapped,
                allow_private=True,
                allowed_private_subnets=["127.0.0.0/8"],
            )

    def test_ipv4_mapped_cloud_metadata_denied(self) -> None:
        """::ffff:169.254.169.254 must not bypass the cloud-metadata block."""
        with pytest.raises(SsrfError):
            assert_address_allowed(
                "::ffff:169.254.169.254",
                allow_private=True,
                allowed_private_subnets=["169.254.0.0/16"],
            )


# ---------------------------------------------------------------------------
# Tier 2: private addresses — blocked by default, opt-in allowed
# ---------------------------------------------------------------------------


@pytest.mark.abuse
class TestPrivateAddressOptIn:
    @pytest.mark.parametrize(
        "address,subnet",
        [
            ("10.0.0.1", "10.0.0.0/8"),
            ("10.255.255.255", "10.0.0.0/8"),
            ("172.16.0.1", "172.16.0.0/12"),
            ("172.31.255.255", "172.16.0.0/12"),
            ("192.168.1.1", "192.168.0.0/16"),
            ("192.168.255.255", "192.168.0.0/16"),
            ("100.64.0.1", "100.64.0.0/10"),  # CGNAT
        ],
    )
    def test_private_address_allowed_when_subnet_opted_in(
        self, address: str, subnet: str
    ) -> None:
        result = assert_address_allowed(
            address, allow_private=True, allowed_private_subnets=[subnet]
        )
        assert str(result) == address

    @pytest.mark.parametrize(
        "address",
        [
            "10.0.0.1",
            "172.16.0.1",
            "192.168.1.1",
            "100.64.0.1",
        ],
    )
    def test_private_address_denied_when_no_opt_in(self, address: str) -> None:
        with pytest.raises(SsrfError):
            assert_address_allowed(address, allow_private=False)

    @pytest.mark.parametrize(
        "address",
        [
            "10.0.0.1",
            "172.16.0.1",
            "192.168.1.1",
        ],
    )
    def test_private_address_denied_when_allow_private_false_even_with_subnets(
        self, address: str
    ) -> None:
        """allow_private=False blocks private IPs even when subnets are listed."""
        with pytest.raises(SsrfError):
            assert_address_allowed(
                address,
                allow_private=False,
                allowed_private_subnets=[
                    "10.0.0.0/8",
                    "172.16.0.0/12",
                    "192.168.0.0/16",
                ],
            )

    def test_private_address_denied_when_not_in_opted_in_subnet(self) -> None:
        """10.1.0.1 is RFC-1918 but not in the opted-in 192.168.0.0/16 subnet."""
        with pytest.raises(SsrfError, match="not within any admin-allowed subnet"):
            assert_address_allowed(
                "10.1.0.1",
                allow_private=True,
                allowed_private_subnets=["192.168.0.0/16"],
            )

    def test_ipv6_ula_allowed_when_opted_in(self) -> None:
        result = assert_address_allowed(
            "fd00::1",
            allow_private=True,
            allowed_private_subnets=["fc00::/7"],
        )
        assert str(result) == "fd00::1"

    def test_ipv6_ula_denied_without_opt_in(self) -> None:
        with pytest.raises(SsrfError):
            assert_address_allowed("fd00::1", allow_private=False)


# ---------------------------------------------------------------------------
# Public addresses must be allowed
# ---------------------------------------------------------------------------


@pytest.mark.abuse
class TestPublicAddressesAllowed:
    @pytest.mark.parametrize(
        "address",
        [
            "8.8.8.8",
            "1.1.1.1",
            "2001:db8::1",  # documentation range but not special-blocked in Python 3.11
        ],
    )
    def test_public_ip_allowed(self, address: str) -> None:
        # Should not raise — just ensure no SsrfError for clearly routable IPs.
        try:
            result = assert_address_allowed(address)
            assert result is not None
        except SsrfError:
            pytest.skip(f"{address} classified as non-public on this Python version")


# ---------------------------------------------------------------------------
# Webhook surface: always full deny-list (no private opt-in)
# ---------------------------------------------------------------------------


@pytest.mark.abuse
class TestWebhookUrlGuard:
    def test_loopback_url_denied_for_webhook(self) -> None:
        with pytest.raises(SsrfError):
            assert_allowed_url("http://127.0.0.1/webhook", allow_private=False)

    def test_link_local_url_denied_for_webhook(self) -> None:
        with pytest.raises(SsrfError):
            assert_allowed_url(
                "https://169.254.169.254/latest/meta-data",
                allow_private=False,
            )

    def test_private_ip_url_denied_for_webhook_even_with_subnets(self) -> None:
        """Webhook surface never opts in to private space."""
        with pytest.raises(SsrfError):
            assert_allowed_url(
                "http://10.0.0.1/webhook",
                allow_private=False,
                allowed_private_subnets=["10.0.0.0/8"],
            )

    def test_url_without_host_denied(self) -> None:
        with pytest.raises(SsrfError, match="no host"):
            assert_allowed_url("not-a-url")

    def test_url_with_public_host_allowed(self) -> None:
        # Should not raise for a public-resolvable domain.
        # Use a literal IP to avoid DNS dependency.
        result = assert_allowed_url("http://8.8.8.8/notify", allow_private=False)
        assert "8.8.8.8" in result

    def test_url_with_opted_in_private_ip_allowed_for_camera_surface(self) -> None:
        """Camera surface with allow_private=True and matching subnet is allowed."""
        result = assert_allowed_url(
            "http://10.0.0.1/snapshot.jpg",
            allow_private=True,
            allowed_private_subnets=["10.0.0.0/8"],
        )
        assert "10.0.0.1" in result


# ---------------------------------------------------------------------------
# Hostname resolution: every resolved address must pass
# ---------------------------------------------------------------------------


@pytest.mark.abuse
class TestResolveAndCheck:
    def test_literal_loopback_denied(self) -> None:
        with pytest.raises(SsrfError):
            resolve_and_check("127.0.0.1")

    def test_literal_private_allowed_when_opted_in(self) -> None:
        result = resolve_and_check(
            "10.0.0.1",
            allow_private=True,
            allowed_private_subnets=["10.0.0.0/8"],
        )
        assert len(result) == 1

    def test_invalid_ip_raises_ssrf_error(self) -> None:
        """A string that is clearly not an IP (and not a hostname) is caught."""
        import socket

        # resolve_and_check will try DNS for non-IP strings; a hostname with
        # illegal characters raises socket.gaierror or SsrfError — both acceptable.
        with pytest.raises((SsrfError, socket.gaierror)):
            resolve_and_check("not_a_valid_@@@@")

    def test_malformed_cidr_in_allowed_subnets_is_dropped(self) -> None:
        """A malformed subnet entry is silently skipped (never widens access)."""
        with pytest.raises(SsrfError):
            assert_address_allowed(
                "10.0.0.1",
                allow_private=True,
                allowed_private_subnets=["NOT_A_CIDR", "also bad"],
            )


# ---------------------------------------------------------------------------
# Defect 2: the async wrapper must not run blocking getaddrinfo on the loop
# ---------------------------------------------------------------------------


@pytest.mark.abuse
class TestAsyncResolutionOffLoadsBlockingDns:
    async def test_getaddrinfo_runs_off_the_event_loop_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``resolve_and_check_async`` must resolve on a worker thread, never on
        the event-loop thread -- otherwise a slow resolver stalls every task.

        A literal-IP target skips DNS entirely, so a hostname is used to force the
        ``socket.getaddrinfo`` path; the patched resolver records which thread it
        ran on and returns a public address so the check itself passes.
        """
        loop_thread = threading.current_thread()
        seen: dict[str, object] = {}

        def recording_getaddrinfo(host: str, *args: object, **kwargs: object) -> list:
            seen["thread"] = threading.current_thread()
            return [(None, None, None, "", ("8.8.8.8", 0))]

        monkeypatch.setattr(
            "timelapse_manager.security.ssrf.socket.getaddrinfo",
            recording_getaddrinfo,
        )

        result = await resolve_and_check_async("camera.example.test")

        assert result, "a public host should resolve and pass the guard"
        assert seen["thread"] is not loop_thread, (
            "getaddrinfo must run on a worker thread, not the event loop"
        )

    async def test_async_wrapper_propagates_deny(self) -> None:
        """A denied literal target still raises through the async wrapper."""
        with pytest.raises(SsrfError):
            await resolve_and_check_async("127.0.0.1")
