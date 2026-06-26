"""Integration tests: cross-platform portability of security components.

Verifies that the encryption and SSRF layers behave identically regardless of
platform, and that the key-file provider handles cross-platform path concerns.
These tests run on all platforms in CI and must pass on Windows, macOS, Linux.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from timelapse_manager.security.crypto import (
    decrypt_secret,
    encrypt_secret,
    is_encrypted,
    set_key_provider,
)
from timelapse_manager.security.keystore import KeyFileProvider
from timelapse_manager.security.ssrf import SsrfError, assert_address_allowed

# ---------------------------------------------------------------------------
# Encryption portability
# ---------------------------------------------------------------------------


class TestEncryptionPortability:
    def test_encrypt_decrypt_works_with_tmp_path_key_file(self, tmp_path: Path) -> None:
        """Encryption works on any platform given a writable tmp dir."""
        # The autouse fixture already installs a KeyFileProvider, so these
        # calls exercise the platform's tmp-dir path handling.
        ciphertext = encrypt_secret("platform-test")
        assert is_encrypted(ciphertext)
        assert decrypt_secret(ciphertext) == "platform-test"

    def test_key_file_can_be_in_nested_directory(self, tmp_path: Path) -> None:
        """KeyFileProvider creates parent directories automatically."""
        nested_key = tmp_path / "subdir" / "nested" / ".secret-key"
        assert not nested_key.parent.exists()
        provider = KeyFileProvider(nested_key)
        provider.get_cipher()
        assert nested_key.exists()

    def test_key_file_path_with_unicode_directory(self, tmp_path: Path) -> None:
        """Key files in directories with non-ASCII characters work."""
        unicode_dir = tmp_path / "données-сamera"
        unicode_dir.mkdir()
        key_file = unicode_dir / ".secret-key"
        provider = KeyFileProvider(key_file)
        cipher = provider.get_cipher()
        token = cipher.encrypt(b"unicode-path-test")
        assert cipher.decrypt(token) == b"unicode-path-test"

    def test_ciphertext_is_ascii_safe(self) -> None:
        """Ciphertext must be ASCII-safe for portability across encodings."""
        ciphertext = encrypt_secret("some secret value")
        # Fernet produces base64url-encoded tokens; all chars are ASCII.
        assert ciphertext.isascii(), "Ciphertext must be ASCII-safe"


# ---------------------------------------------------------------------------
# SSRF portability: IP address parsing is platform-independent
# ---------------------------------------------------------------------------


class TestSsrfPortability:
    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",
            "::1",
            "169.254.169.254",
        ],
    )
    def test_always_blocked_addresses_denied_on_all_platforms(
        self, address: str
    ) -> None:
        with pytest.raises(SsrfError):
            assert_address_allowed(address, allow_private=True)

    @pytest.mark.parametrize(
        "address,subnet",
        [
            ("10.0.0.1", "10.0.0.0/8"),
            ("192.168.1.1", "192.168.0.0/16"),
        ],
    )
    def test_private_opt_in_works_on_all_platforms(
        self, address: str, subnet: str
    ) -> None:
        result = assert_address_allowed(
            address, allow_private=True, allowed_private_subnets=[subnet]
        )
        assert str(result) == address

    def test_ipv4_mapped_ipv6_normalised_on_all_platforms(self) -> None:
        """IPv4-mapped IPv6 normalisation must work regardless of socket impl."""
        with pytest.raises(SsrfError):
            assert_address_allowed(
                "::ffff:127.0.0.1",
                allow_private=True,
                allowed_private_subnets=["127.0.0.0/8"],
            )


# ---------------------------------------------------------------------------
# Key-provider provider build function (file fallback)
# ---------------------------------------------------------------------------


class TestBuildKeyProviderPortability:
    def test_build_key_provider_with_os_keystore_false_returns_file_provider(
        self, tmp_path: Path
    ) -> None:
        """build_key_provider with use_os_keystore=False returns a file provider."""
        from timelapse_manager.config.settings import SecretsSettings
        from timelapse_manager.security.keystore import (
            KeyFileProvider,
            build_key_provider,
        )

        settings = SecretsSettings(use_os_keystore=False)
        provider = build_key_provider(settings, tmp_path)
        assert isinstance(provider, KeyFileProvider)

    def test_file_provider_can_encrypt_and_decrypt(self, tmp_path: Path) -> None:
        from timelapse_manager.config.settings import SecretsSettings
        from timelapse_manager.security.keystore import build_key_provider

        settings = SecretsSettings(use_os_keystore=False)
        provider = build_key_provider(settings, tmp_path)
        set_key_provider(provider)
        try:
            ciphertext = encrypt_secret("portability-value")
            assert decrypt_secret(ciphertext) == "portability-value"
        finally:
            # Restore the autouse-installed provider (tmp_path key file).
            restore_provider = KeyFileProvider(tmp_path / ".secret-key")
            set_key_provider(restore_provider)
