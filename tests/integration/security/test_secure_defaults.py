"""Integration tests: secure defaults on startup.

Verifies that an application started with minimal / default settings does not
surface insecure defaults: SSRF guard is active, private addresses are blocked
by default, and settings whose defaults control a security surface have the
expected safe values.
"""

from __future__ import annotations

import pytest

from timelapse_manager.config.settings import (
    SecretsSettings,
    Settings,
    SsrfSettings,
)

# ---------------------------------------------------------------------------
# SSRF settings defaults
# ---------------------------------------------------------------------------


class TestSsrfDefaults:
    def test_default_allowed_private_subnets_is_empty(self) -> None:
        """Out of the box, no private subnets are opted in (all private blocked)."""
        s = SsrfSettings()
        assert s.allowed_private_subnets == [], (
            "Default allowed_private_subnets must be empty so private space is "
            "blocked without explicit admin configuration."
        )

    def test_default_max_scan_hosts_is_positive(self) -> None:
        """A default scan cap must exist to prevent unbounded network sweeps."""
        s = SsrfSettings()
        assert s.max_scan_hosts > 0

    def test_default_max_scan_hosts_is_reasonable(self) -> None:
        """The default scan cap should be at most 4096 hosts."""
        s = SsrfSettings()
        assert s.max_scan_hosts <= 4096


# ---------------------------------------------------------------------------
# Secrets settings defaults
# ---------------------------------------------------------------------------


class TestSecretsDefaults:
    def test_default_service_name_is_set(self) -> None:
        s = SecretsSettings()
        assert s.keystore_service_name, "keystore_service_name must have a default"

    def test_default_key_file_is_none(self) -> None:
        """key_file defaults to None so the data_dir default path is used."""
        s = SecretsSettings()
        assert s.key_file is None


# ---------------------------------------------------------------------------
# Full Settings integration
# ---------------------------------------------------------------------------


class TestFullSettingsDefaults:
    def test_ssrf_section_present_in_settings(self) -> None:
        s = Settings()
        assert hasattr(s, "ssrf")
        assert isinstance(s.ssrf, SsrfSettings)

    def test_secrets_section_present_in_settings(self) -> None:
        s = Settings()
        assert hasattr(s, "secrets")
        assert isinstance(s.secrets, SecretsSettings)

    def test_default_ssrf_blocks_private_space(self) -> None:
        from timelapse_manager.security.ssrf import SsrfError, assert_address_allowed

        s = Settings()
        for private_ip in ["10.0.0.1", "192.168.1.1", "172.16.0.1"]:
            with pytest.raises(SsrfError):
                assert_address_allowed(
                    private_ip,
                    allow_private=True,
                    allowed_private_subnets=s.ssrf.allowed_private_subnets,
                )

    def test_default_ssrf_always_blocks_loopback(self) -> None:
        from timelapse_manager.security.ssrf import SsrfError, assert_address_allowed

        with pytest.raises(SsrfError):
            assert_address_allowed(
                "127.0.0.1",
                allow_private=True,
                allowed_private_subnets=["127.0.0.0/8"],
            )
