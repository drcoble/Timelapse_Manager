"""Unit tests for the key providers' cipher cache (#66 N1).

Covers: the key file / keyring is read once, not on every decrypt; rotation
swaps the cache only after the new key is stored; a failed rotation leaves the
cache and the stored key unchanged; concurrent first use creates one key; and
``invalidate_cipher`` forces a reload. The keyring is faked throughout, so the
real OS secret store is never touched.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import keyring
import pytest
from cryptography.fernet import Fernet, InvalidToken

from timelapse_manager.security.crypto import (
    decrypt_secret,
    encrypt_secret,
    set_key_provider,
)
from timelapse_manager.security.keystore import (
    KeyFileProvider,
    KeyProvider,
    OsKeystoreProvider,
)

_SERVICE = "tlm-test-cache"


def _write_key_file(path: Path, key: bytes) -> None:
    """Create an owner-only key file holding ``key``."""
    path.write_bytes(key)
    path.chmod(0o600)


@pytest.fixture
def read_counter(monkeypatch: pytest.MonkeyPatch) -> dict[Path, int]:
    """Count ``Path.read_bytes`` calls per path."""
    counts: dict[Path, int] = {}
    real_read_bytes = Path.read_bytes

    def counting_read_bytes(self: Path) -> bytes:
        counts[self] = counts.get(self, 0) + 1
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)
    return counts


class _FakeKeyring:
    """An in-memory stand-in for the ``keyring`` module's password API."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.get_calls = 0
        self.fail_set = False

    def get_password(self, service: str, account: str) -> str | None:
        self.get_calls += 1
        return self.store.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        if self.fail_set:
            raise OSError("keyring write failed")
        self.store[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        self.store.pop((service, account), None)


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyring:
    """Replace the keyring password API with an in-memory fake."""
    fake = _FakeKeyring()
    monkeypatch.setattr(keyring, "get_password", fake.get_password)
    monkeypatch.setattr(keyring, "set_password", fake.set_password)
    monkeypatch.setattr(keyring, "delete_password", fake.delete_password)
    return fake


def _stored_keyring_key(fake: _FakeKeyring) -> str:
    return fake.store[(_SERVICE, OsKeystoreProvider._ACCOUNT)]


# ---------------------------------------------------------------------------
# Read once
# ---------------------------------------------------------------------------


class TestKeyReadOnce:
    def test_key_file_is_read_once_across_decrypts(
        self, tmp_path: Path, read_counter: dict[Path, int]
    ) -> None:
        key_file = tmp_path / ".existing-key"
        _write_key_file(key_file, Fernet.generate_key())
        set_key_provider(KeyFileProvider(key_file))

        first = encrypt_secret("alpha")
        second = encrypt_secret("beta")
        assert decrypt_secret(first) == "alpha"
        assert decrypt_secret(second) == "beta"

        assert read_counter.get(key_file) == 1

    def test_keyring_is_read_once_across_decrypts(
        self, fake_keyring: _FakeKeyring
    ) -> None:
        fake_keyring.store[(_SERVICE, OsKeystoreProvider._ACCOUNT)] = (
            Fernet.generate_key().decode("ascii")
        )
        set_key_provider(OsKeystoreProvider(_SERVICE))

        first = encrypt_secret("alpha")
        assert decrypt_secret(first) == "alpha"
        assert decrypt_secret(encrypt_secret("beta")) == "beta"

        assert fake_keyring.get_calls == 1

    def test_get_cipher_returns_the_same_object(self, tmp_path: Path) -> None:
        provider = KeyFileProvider(tmp_path / ".secret-key")
        assert provider.get_cipher() is provider.get_cipher()


# ---------------------------------------------------------------------------
# Rotation keeps the cache current
# ---------------------------------------------------------------------------


class TestRotationUpdatesCache:
    def test_file_rotation_switches_new_encryptions_to_new_key(
        self, tmp_path: Path
    ) -> None:
        key_file = tmp_path / ".secret-key"
        provider = KeyFileProvider(key_file)
        set_key_provider(provider)
        stored = encrypt_secret("camera-password")
        received: list[Fernet] = []
        reencrypted: list[str] = []

        def reencrypt(old: Fernet, new: Fernet) -> None:
            received.append(old)
            raw = stored.removeprefix("enc:v1:").encode("ascii")
            token = new.encrypt(old.decrypt(raw)).decode("ascii")
            reencrypted.append(f"enc:v1:{token}")

        old_cipher = provider.get_cipher()
        provider.rotate(reencrypt)

        assert received == [old_cipher]
        new_key = key_file.read_bytes().strip()
        # The re-encrypted secret and fresh encryptions use the new key ...
        assert decrypt_secret(reencrypted[0]) == "camera-password"
        fresh = encrypt_secret("after-rotation")
        token = fresh.removeprefix("enc:v1:").encode("ascii")
        assert Fernet(new_key).decrypt(token) == b"after-rotation"
        # ... and the old ciphertext no longer decrypts.
        with pytest.raises(InvalidToken):
            decrypt_secret(stored)

    def test_keyring_rotation_switches_new_encryptions_to_new_key(
        self, fake_keyring: _FakeKeyring
    ) -> None:
        provider = OsKeystoreProvider(_SERVICE)
        set_key_provider(provider)
        stored = encrypt_secret("smtp-password")

        provider.rotate(lambda _old, _new: None)

        new_key = _stored_keyring_key(fake_keyring).encode("ascii")
        token = encrypt_secret("after").removeprefix("enc:v1:").encode("ascii")
        assert Fernet(new_key).decrypt(token) == b"after"
        with pytest.raises(InvalidToken):
            decrypt_secret(stored)

    def test_callback_may_use_the_provider(self, tmp_path: Path) -> None:
        """The cache lock is not held across the callback (no deadlock)."""
        provider = KeyFileProvider(tmp_path / ".secret-key")
        set_key_provider(provider)
        seen: list[str] = []

        def reencrypt(_old: Fernet, _new: Fernet) -> None:
            seen.append(decrypt_secret(encrypt_secret("inside")))

        provider.rotate(reencrypt)
        assert seen == ["inside"]


class TestFailedRotationKeepsCache:
    def test_file_callback_failure_keeps_cache_and_key(self, tmp_path: Path) -> None:
        key_file = tmp_path / ".secret-key"
        provider = KeyFileProvider(key_file)
        cipher = provider.get_cipher()
        key_before = key_file.read_bytes()

        def failing(_old: Fernet, _new: Fernet) -> None:
            raise RuntimeError("re-encrypt failed")

        with pytest.raises(RuntimeError):
            provider.rotate(failing)

        assert provider.get_cipher() is cipher
        assert key_file.read_bytes() == key_before

    def test_file_write_failure_keeps_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key_file = tmp_path / ".secret-key"
        provider = KeyFileProvider(key_file)
        cipher = provider.get_cipher()
        key_before = key_file.read_bytes()

        def failing_stage(_key: bytes) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(provider, "_stage_key", failing_stage)
        with pytest.raises(OSError, match="disk full"):
            provider.rotate(lambda _old, _new: None)

        assert provider.get_cipher() is cipher
        assert key_file.read_bytes() == key_before

    def test_keyring_write_failure_keeps_cache_and_key(
        self, fake_keyring: _FakeKeyring
    ) -> None:
        provider = OsKeystoreProvider(_SERVICE)
        cipher = provider.get_cipher()
        key_before = _stored_keyring_key(fake_keyring)

        fake_keyring.fail_set = True
        with pytest.raises(OSError, match="keyring write failed"):
            provider.rotate(lambda _old, _new: None)

        assert provider.get_cipher() is cipher
        assert _stored_keyring_key(fake_keyring) == key_before


# ---------------------------------------------------------------------------
# Concurrent first use
# ---------------------------------------------------------------------------


def _race_first_use(provider: KeyProvider, threads: int = 8) -> list[Fernet]:
    """Call ``get_cipher`` from ``threads`` threads released together."""
    barrier = threading.Barrier(threads)
    results: list[Fernet] = []
    errors: list[BaseException] = []
    results_lock = threading.Lock()

    def worker() -> None:
        try:
            barrier.wait()
            cipher = provider.get_cipher()
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)
            return
        with results_lock:
            results.append(cipher)

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=10)
    assert errors == []
    assert len(results) == threads
    return results


class TestConcurrentFirstUse:
    def test_file_provider_creates_one_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = KeyFileProvider(tmp_path / ".secret-key")
        real_create = provider._create_key
        writes: list[bytes] = []

        def slow_create() -> bytes:
            # Widen the window so an unguarded first use would race.
            time.sleep(0.05)
            key = real_create()
            writes.append(key)
            return key

        monkeypatch.setattr(provider, "_create_key", slow_create)
        results = _race_first_use(provider)

        assert len(writes) == 1
        assert all(cipher is results[0] for cipher in results)

    def test_keyring_provider_creates_one_key(
        self, fake_keyring: _FakeKeyring, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sets: list[str] = []
        real_set = fake_keyring.set_password

        def slow_set(service: str, account: str, value: str) -> None:
            time.sleep(0.05)
            sets.append(value)
            real_set(service, account, value)

        monkeypatch.setattr(keyring, "set_password", slow_set)
        provider = OsKeystoreProvider(_SERVICE)
        results = _race_first_use(provider)

        assert len(sets) == 1
        assert all(cipher is results[0] for cipher in results)


# ---------------------------------------------------------------------------
# Invalidation hook (for key rotation / fingerprint paths, #67 S8)
# ---------------------------------------------------------------------------


class TestInvalidateCipher:
    def test_cached_cipher_outlives_key_file_swap_until_invalidated(
        self, tmp_path: Path
    ) -> None:
        key_file = tmp_path / ".existing-key"
        _write_key_file(key_file, Fernet.generate_key())
        provider = KeyFileProvider(key_file)
        original = provider.get_cipher()

        replacement = Fernet.generate_key()
        _write_key_file(key_file, replacement)
        # Without invalidation the cached cipher is still served.
        assert provider.get_cipher() is original

        provider.invalidate_cipher()
        token = provider.get_cipher().encrypt(b"x")
        assert Fernet(replacement).decrypt(token) == b"x"

    def test_invalidate_rechecks_key_file_permissions(self, tmp_path: Path) -> None:
        key_file = tmp_path / ".existing-key"
        _write_key_file(key_file, Fernet.generate_key())
        provider = KeyFileProvider(key_file)
        provider.get_cipher()

        key_file.chmod(0o644)
        provider.invalidate_cipher()
        with pytest.raises(PermissionError, match="chmod 600"):
            provider.get_cipher()

    def test_keyring_reloads_after_invalidate(self, fake_keyring: _FakeKeyring) -> None:
        provider = OsKeystoreProvider(_SERVICE)
        original = provider.get_cipher()
        replacement = Fernet.generate_key()
        fake_keyring.store[(_SERVICE, OsKeystoreProvider._ACCOUNT)] = (
            replacement.decode("ascii")
        )
        assert provider.get_cipher() is original

        provider.invalidate_cipher()
        token = provider.get_cipher().encrypt(b"y")
        assert Fernet(replacement).decrypt(token) == b"y"
        assert fake_keyring.get_calls == 2

    def test_invalidate_before_first_use_is_harmless(self, tmp_path: Path) -> None:
        provider = KeyFileProvider(tmp_path / ".secret-key")
        provider.invalidate_cipher()
        assert isinstance(provider.get_cipher(), Fernet)
