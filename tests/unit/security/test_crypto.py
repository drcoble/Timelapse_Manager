"""Unit tests for the at-rest encryption module.

Covers: encrypt/decrypt round-trips, legacy plaintext passthrough, empty-string
handling, key-provider requirement, non-determinism, credential document helpers.
"""

from __future__ import annotations

import pytest

from timelapse_manager.security.crypto import (
    InvalidToken,
    decrypt_credentials,
    decrypt_secret,
    encrypt_credentials,
    encrypt_secret,
    is_encrypted,
    set_key_provider,
)
from timelapse_manager.security.keystore import KeyFileProvider

_PREFIX = "enc:v1:"


# ---------------------------------------------------------------------------
# Basic encryption / decryption round-trip
# ---------------------------------------------------------------------------


class TestEncryptDecryptRoundTrip:
    def test_short_string_round_trips(self) -> None:
        token = encrypt_secret("hunter2")
        assert decrypt_secret(token) == "hunter2"

    def test_long_string_round_trips(self) -> None:
        plaintext = "A" * 10_000
        assert decrypt_secret(encrypt_secret(plaintext)) == plaintext

    def test_unicode_content_round_trips(self) -> None:
        plaintext = "p@$$w0rd–café–日本語"
        assert decrypt_secret(encrypt_secret(plaintext)) == plaintext

    def test_binary_like_characters_round_trip(self) -> None:
        plaintext = "\x00\x01\x02\x03\xff"
        assert decrypt_secret(encrypt_secret(plaintext)) == plaintext

    def test_encrypted_value_has_version_prefix(self) -> None:
        ciphertext = encrypt_secret("secret")
        assert ciphertext.startswith(_PREFIX)

    def test_is_encrypted_true_for_ciphertext(self) -> None:
        ciphertext = encrypt_secret("secret")
        assert is_encrypted(ciphertext)

    def test_is_encrypted_false_for_plaintext(self) -> None:
        assert not is_encrypted("plain-password")

    def test_is_encrypted_false_for_empty(self) -> None:
        assert not is_encrypted("")


# ---------------------------------------------------------------------------
# Non-determinism (Fernet uses a random IV)
# ---------------------------------------------------------------------------


class TestNonDeterminism:
    def test_two_encryptions_of_same_plaintext_differ(self) -> None:
        a = encrypt_secret("same-value")
        b = encrypt_secret("same-value")
        assert a != b, "Fernet encryption must be non-deterministic"

    def test_both_ciphertexts_decrypt_to_same_plaintext(self) -> None:
        a = encrypt_secret("same-value")
        b = encrypt_secret("same-value")
        assert decrypt_secret(a) == decrypt_secret(b) == "same-value"


# ---------------------------------------------------------------------------
# Empty string handling
# ---------------------------------------------------------------------------


class TestEmptyStringHandling:
    def test_empty_plaintext_encrypts_to_empty_string(self) -> None:
        assert encrypt_secret("") == ""

    def test_empty_ciphertext_decrypts_to_empty_string(self) -> None:
        assert decrypt_secret("") == ""

    def test_empty_is_not_treated_as_ciphertext(self) -> None:
        assert not is_encrypted("")


# ---------------------------------------------------------------------------
# Legacy plaintext passthrough
# ---------------------------------------------------------------------------


class TestLegacyPlaintextPassthrough:
    def test_value_without_prefix_returned_unchanged(self) -> None:
        """A value that has no enc:v1: prefix is legacy plaintext and passes through."""
        legacy = "plain-old-password"
        assert decrypt_secret(legacy) == legacy

    def test_partial_prefix_not_treated_as_ciphertext(self) -> None:
        partial = "enc:v0:something"
        assert decrypt_secret(partial) == partial

    def test_prefix_only_value_would_fail_decrypt(self) -> None:
        """A prefixed value that is not valid Fernet data raises InvalidToken."""
        with pytest.raises((InvalidToken, Exception)):
            decrypt_secret("enc:v1:not-valid-fernet-data")


# ---------------------------------------------------------------------------
# Key provider requirement
# ---------------------------------------------------------------------------


class TestKeyProviderRequired:
    def test_encrypt_without_provider_raises_runtime_error(self, tmp_path) -> None:
        """After clearing the provider, encryption must raise immediately."""
        set_key_provider(None)
        try:
            with pytest.raises(RuntimeError, match="No encryption key provider"):
                encrypt_secret("sensitive")
        finally:
            # Restore a valid provider (the autouse fixture will re-install on
            # the next test, but we need it for any decrypt calls in teardown).
            provider = KeyFileProvider(tmp_path / ".restore-key")
            set_key_provider(provider)

    def test_decrypt_without_provider_raises_for_prefixed_value(self, tmp_path) -> None:
        """Decrypting a prefixed value requires a provider."""
        # Encrypt first while a provider is installed.
        ciphertext = encrypt_secret("value")
        # Now clear the provider.
        set_key_provider(None)
        try:
            with pytest.raises(RuntimeError, match="No encryption key provider"):
                decrypt_secret(ciphertext)
        finally:
            provider = KeyFileProvider(tmp_path / ".restore-key2")
            set_key_provider(provider)


# ---------------------------------------------------------------------------
# Credential document helpers
# ---------------------------------------------------------------------------


class TestEncryptCredentials:
    def test_password_field_encrypted(self) -> None:
        creds = {"username": "admin", "password": "hunter2"}
        encrypted = encrypt_credentials(creds)
        assert encrypted is not None
        assert encrypted["username"] == "admin"
        assert is_encrypted(str(encrypted["password"]))

    def test_token_field_encrypted(self) -> None:
        creds = {"api_token": "tok123"}
        encrypted = encrypt_credentials(creds)
        assert encrypted is not None
        assert is_encrypted(str(encrypted["api_token"]))

    def test_secret_field_encrypted(self) -> None:
        creds = {"client_secret": "abc"}
        encrypted = encrypt_credentials(creds)
        assert encrypted is not None
        assert is_encrypted(str(encrypted["client_secret"]))

    def test_non_secret_fields_pass_through(self) -> None:
        creds = {"username": "admin", "host": "camera.local"}
        encrypted = encrypt_credentials(creds)
        assert encrypted is not None
        assert encrypted["username"] == "admin"
        assert encrypted["host"] == "camera.local"

    def test_none_credentials_pass_through(self) -> None:
        assert encrypt_credentials(None) is None

    def test_empty_credentials_pass_through(self) -> None:
        assert encrypt_credentials({}) == {}

    def test_round_trip_via_decrypt_credentials(self) -> None:
        creds = {"username": "admin", "password": "secret", "protocol": "vapix"}
        encrypted = encrypt_credentials(creds)
        decrypted = decrypt_credentials(encrypted)
        assert decrypted is not None
        assert decrypted["password"] == "secret"
        assert decrypted["username"] == "admin"
        assert decrypted["protocol"] == "vapix"

    def test_empty_password_value_not_encrypted(self) -> None:
        """An empty password value must not be encrypted (stays empty)."""
        creds = {"username": "admin", "password": ""}
        encrypted = encrypt_credentials(creds)
        assert encrypted is not None
        # Empty stays empty — no ciphertext for absent credentials.
        assert encrypted["password"] == ""
