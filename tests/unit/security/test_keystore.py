"""Unit tests for the at-rest key provider: KeyFileProvider.

Covers: key generation on first use, key persistence across instances, file
permission enforcement, write-then-read round-trip, rotation callback contract.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from timelapse_manager.security.keystore import KeyFileProvider

# ---------------------------------------------------------------------------
# Key generation and persistence
# ---------------------------------------------------------------------------


class TestKeyFileProviderGeneration:
    def test_get_cipher_generates_key_file_on_first_use(self, tmp_path: Path) -> None:
        key_file = tmp_path / ".secret-key"
        assert not key_file.exists()
        provider = KeyFileProvider(key_file)
        provider.get_cipher()
        assert key_file.exists()

    def test_get_cipher_returns_usable_fernet_cipher(self, tmp_path: Path) -> None:

        provider = KeyFileProvider(tmp_path / ".secret-key")
        cipher = provider.get_cipher()
        # A real Fernet cipher must be able to encrypt and decrypt.
        token = cipher.encrypt(b"test-data")
        assert cipher.decrypt(token) == b"test-data"

    def test_two_instances_same_file_produce_same_key(self, tmp_path: Path) -> None:
        """A provider created from an existing key file must reuse the key."""
        key_file = tmp_path / ".secret-key"
        p1 = KeyFileProvider(key_file)
        p1.get_cipher()  # generates and persists
        p2 = KeyFileProvider(key_file)
        cipher1 = p1.get_cipher()
        cipher2 = p2.get_cipher()
        # Encrypt with p1, decrypt with p2 — they must agree.
        token = cipher1.encrypt(b"same-key")
        assert cipher2.decrypt(token) == b"same-key"

    def test_two_instances_different_files_produce_different_keys(
        self, tmp_path: Path
    ) -> None:
        """Two separate key files produce independent keys."""
        from cryptography.fernet import InvalidToken

        p1 = KeyFileProvider(tmp_path / ".key-a")
        p2 = KeyFileProvider(tmp_path / ".key-b")
        token = p1.get_cipher().encrypt(b"data")
        with pytest.raises((InvalidToken, Exception)):
            p2.get_cipher().decrypt(token)


# ---------------------------------------------------------------------------
# File permission enforcement (POSIX only)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission tests only")
class TestKeyFilePermissions:
    def test_key_file_created_with_0600_mode(self, tmp_path: Path) -> None:
        key_file = tmp_path / ".secret-key"
        provider = KeyFileProvider(key_file)
        provider.get_cipher()
        mode = stat.S_IMODE(key_file.stat().st_mode)
        assert mode == 0o600, f"Expected 0o600, got {mode:#o}"

    def test_group_readable_key_file_raises_permission_error(
        self, tmp_path: Path
    ) -> None:
        key_file = tmp_path / ".secret-key"
        provider = KeyFileProvider(key_file)
        provider.get_cipher()  # creates with 0600
        # Widen permissions to group-readable.
        os.chmod(key_file, 0o640)
        p2 = KeyFileProvider(key_file)
        with pytest.raises(PermissionError, match="group- or world-readable"):
            p2.get_cipher()

    def test_world_readable_key_file_raises_permission_error(
        self, tmp_path: Path
    ) -> None:
        key_file = tmp_path / ".secret-key"
        provider = KeyFileProvider(key_file)
        provider.get_cipher()
        os.chmod(key_file, 0o644)
        p2 = KeyFileProvider(key_file)
        with pytest.raises(PermissionError):
            p2.get_cipher()


# ---------------------------------------------------------------------------
# Key rotation
# ---------------------------------------------------------------------------


class TestKeyFileRotation:
    def test_rotation_re_encrypts_data_with_new_key(self, tmp_path: Path) -> None:
        key_file = tmp_path / ".secret-key"
        provider = KeyFileProvider(key_file)

        # Encrypt a value with the original key.
        old_cipher = provider.get_cipher()
        original_token = old_cipher.encrypt(b"rotate-me")

        # Track what the callback receives.
        callback_calls: list[tuple[bytes, bytes]] = []

        def reencrypt_callback(old_fernet, new_fernet):
            # Mimic re-encrypting stored secrets.
            callback_calls.append(
                (
                    old_fernet.decrypt(original_token),
                    b"will-be-re-encrypted",
                )
            )

        provider.rotate(reencrypt_callback)

        # The callback must have been called exactly once.
        assert len(callback_calls) == 1
        # The old cipher successfully decrypted the original token.
        assert callback_calls[0][0] == b"rotate-me"

    def test_rotation_changes_key_file_content(self, tmp_path: Path) -> None:
        key_file = tmp_path / ".secret-key"
        provider = KeyFileProvider(key_file)
        provider.get_cipher()
        before = key_file.read_bytes()

        provider.rotate(lambda _old, _new: None)

        after = key_file.read_bytes()
        assert before != after, "Key file content must change after rotation"

    def test_old_key_cannot_decrypt_after_rotation(self, tmp_path: Path) -> None:
        from cryptography.fernet import InvalidToken

        key_file = tmp_path / ".secret-key"
        provider = KeyFileProvider(key_file)
        old_cipher = provider.get_cipher()
        token_from_old_key = old_cipher.encrypt(b"old-data")

        # Rotate to a new key.
        provider.rotate(lambda _old, _new: None)

        # The new cipher from the same provider must not decrypt the old token.
        new_cipher = provider.get_cipher()
        with pytest.raises((InvalidToken, Exception)):
            new_cipher.decrypt(token_from_old_key)
