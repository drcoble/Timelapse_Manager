"""The at-rest encryption key provider: OS secret store with a file fallback.

Stored credentials (SMTP / webhook / camera / LDAP) are encrypted at rest with a
single symmetric key. This module owns *where that key lives* and presents it
behind one small interface (:class:`KeyProvider`) so the rest of the application
is storage-agnostic.

Selection
---------
Two providers exist and are chosen automatically:

* :class:`OsKeystoreProvider` -- stores the key in the host OS secret store
  (macOS Keychain, Windows Credential Manager, Linux Secret Service) via the
  ``keyring`` library, keyed by ``settings.secrets.keystore_service_name``.
* :class:`KeyFileProvider` -- a restricted-permission (``0600``) key file used
  when no OS secret store is reachable (the common case for headless Linux and
  Docker, where no Secret Service runs) or when ``use_os_keystore`` is ``False``.

:func:`build_key_provider` tries the OS store first (when enabled) and **falls
back to the file provider rather than crashing** when no keyring backend is
available. The file provider refuses to start if the key file is found group- or
world-readable -- a misconfigured key is a hard error, not a warning.

Key hygiene
-----------
The key is never written to a log line and never committed (the default file
lives under the gitignored data directory). On first use a fresh key is
generated with :meth:`cryptography.fernet.Fernet.generate_key` and persisted; on
POSIX the file is opened with an explicit ``0600`` mode (umask only clears bits,
so it cannot widen) so the key is never momentarily group/world-readable.

Rotation
--------
:meth:`KeyProvider.rotate` **stages** a new key beside the current one, hands both
:class:`~cryptography.fernet.Fernet` instances to a caller-supplied callback that
re-encrypts every stored secret and commits, and only then **promotes** the
staged key (``<key file>.next`` renamed over the key file, or the keyring's
``-next`` item copied over the main one). The old key stays authoritative until
the database holds data the new key can read; a crash in between is recovered
from the staged key at the next start. Neither key's material is logged.

Key integrity
-------------
The provider and a fingerprint of its key are recorded in ``secrets_meta`` (see
:mod:`.key_integrity`). :func:`key_fingerprint` is the one-way check value; the
non-creating :meth:`KeyProvider.load_existing_key` lets startup test the key
without ever minting a replacement for one that has gone missing.

Cipher cache
------------
Each provider builds its :class:`~cryptography.fernet.Fernet` cipher once, on
the first :meth:`KeyProvider.get_cipher`, and serves every later encrypt and
decrypt from memory. A decrypt therefore never re-reads the key file or calls
into the keyring (blocking I/O that would otherwise run on the event loop for
every stored secret). First use is guarded by a lock, so concurrent first
callers load or create exactly one key.

The cache is replaced only after :meth:`KeyProvider.rotate` has persisted the
new key; a rotation that fails leaves the cached cipher unchanged. Because the
key is read once, replacing the key out of band (restoring a different key
file, or editing the keyring item) takes effect only after a service restart,
as for any other secret change. The key file's permission check likewise runs
on that first load. Code that changes the stored key in-process must call
:meth:`KeyProvider.invalidate_cipher` so the next :meth:`~KeyProvider.get_cipher`
reloads it.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import logging
import os
import stat
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from cryptography.fernet import Fernet

if TYPE_CHECKING:
    from ..config.settings import SecretsSettings

logger = logging.getLogger(__name__)

# Owner read/write only. The key decrypts every stored credential, so it must
# never be group- or world-readable.
_KEY_FILE_MODE = 0o600

# POSIX permission bits that must be clear on the key file: any group or other
# access. Used to refuse an over-permissive key file.
_GROUP_OTHER_MASK = 0o077

# The fixed label the key fingerprint is an HMAC of, and how much of it an
# operator is ever shown.
_FINGERPRINT_LABEL = b"tlm-key-check"
_FINGERPRINT_PREFIX_LENGTH = 8

# How long a starter that lost the O_EXCL create race waits for the winner to
# finish writing the key.
_RACE_READ_ATTEMPTS = 20
_RACE_READ_PAUSE_SECONDS = 0.05

# A callback that re-encrypts all stored secrets given the old and new ciphers.
# It is handed both so it can read with the old key and write with the new one.
RotationCallback = Callable[["Fernet", "Fernet"], None]


class KeyProvider(Protocol):
    """The storage-agnostic interface the crypto layer reads the key through."""

    #: The name recorded in ``secrets_meta.provider`` (``file`` or ``os_keystore``).
    name: str

    def get_cipher(self) -> Fernet:
        """Return a Fernet cipher built from the current key, creating one if needed."""
        ...

    def rotate(self, reencrypt: RotationCallback) -> None:
        """Stage a new key, re-encrypt secrets via the callback, then promote it."""
        ...

    def invalidate_cipher(self) -> None:
        """Drop the cached cipher so the next :meth:`get_cipher` reloads the key."""
        ...

    def describe(self) -> str:
        """Where the key lives, for operator messages (a path or item name)."""
        ...

    def load_existing_key(self) -> bytes | None:
        """Return the stored key, or ``None`` when absent. Never creates one."""
        ...

    def fingerprint(self) -> str | None:
        """Fingerprint of the stored key, or ``None`` when no key exists."""
        ...

    def read_staged_key(self) -> bytes | None:
        """Return the key a rotation staged but did not promote, if any."""
        ...

    def pending_fingerprint(self) -> str:
        """Fingerprint of the staged key (valid inside a rotation callback)."""
        ...

    def promote_staged_key(self) -> None:
        """Make the staged key the stored key and drop the cached cipher."""
        ...

    def discard_staged_key(self) -> None:
        """Remove a staged key that no committed row refers to."""
        ...

    def replace_key(self, key: bytes) -> None:
        """Store ``key`` in place of the current one (``secrets reset`` only)."""
        ...


def key_fingerprint(key: bytes) -> str:
    """Return the hex HMAC-SHA256 of a fixed label under ``key``.

    A one-way check value: it identifies the key without revealing it. Only an
    8-character prefix is ever shown to an operator; neither the key nor the
    full fingerprint is logged.
    """
    return hmac.new(key.strip(), _FINGERPRINT_LABEL, hashlib.sha256).hexdigest()


def fingerprint_prefix(fingerprint: str) -> str:
    """The short form of a fingerprint that is safe to print."""
    return fingerprint[:_FINGERPRINT_PREFIX_LENGTH]


class _CachedCipherProvider:
    """The cipher cache shared by the key providers.

    Subclasses supply :meth:`_read_key`, :meth:`_create_key` and the staging
    hooks. :meth:`get_cipher` loads the key once, under a lock, and caches the
    resulting cipher; :meth:`_install_cipher` swaps the cache after a rotation
    has persisted its key; and :meth:`invalidate_cipher` is the hook for any
    path that changes the stored key in-process (key rotation, key fingerprint
    checks).
    """

    name = ""

    def __init__(self) -> None:
        """Start with an empty cache; nothing is read until first use."""
        self._cipher: Fernet | None = None
        self._cipher_lock = threading.Lock()

    def get_cipher(self) -> Fernet:
        """Return the cached cipher, loading (or creating) the key on first use."""
        cipher = self._cipher
        if cipher is not None:
            return cipher
        with self._cipher_lock:
            if self._cipher is None:
                self._cipher = Fernet(self._load_or_create_key())
            return self._cipher

    def invalidate_cipher(self) -> None:
        """Drop the cached cipher so the next :meth:`get_cipher` reloads the key.

        Any code that replaces the stored key while the process runs (rotation,
        a key fingerprint check that swaps keys) must call this, or keep the
        cache current via :meth:`_install_cipher`. Out-of-band key changes are
        not detected; they need a restart.
        """
        with self._cipher_lock:
            self._cipher = None

    def _install_cipher(self, cipher: Fernet) -> None:
        """Replace the cached cipher; call only after the matching key is stored."""
        with self._cipher_lock:
            self._cipher = cipher

    def _load_or_create_key(self) -> bytes:
        """Return the stored key bytes, creating and persisting one if absent."""
        existing = self._read_key()
        # An empty file is another starter's key mid-write: it created the file
        # (O_EXCL) and has not written yet. Treat it as absent so ``_create_key``
        # waits for the content, instead of building a cipher from ``b""`` (#189).
        if existing:
            return existing
        return self._create_key()

    def load_existing_key(self) -> bytes | None:
        """Return the stored key, or ``None`` when absent. Never creates one."""
        return self._read_key()

    def fingerprint(self) -> str | None:
        """Fingerprint of the stored key, or ``None`` when no key exists."""
        key = self._read_key()
        return None if key is None else key_fingerprint(key)

    def pending_fingerprint(self) -> str:
        """Fingerprint of the staged key (valid inside a rotation callback)."""
        staged = self.read_staged_key()
        if staged is None:
            raise RuntimeError("no staged key: rotate() has not staged one")
        return key_fingerprint(staged)

    def rotate(self, reencrypt: RotationCallback) -> None:
        """Stage a new key, re-encrypt all secrets, then promote it.

        The order is what makes a crash survivable (#67 S8):

        1. the new key is **staged** beside the current one, which stays in
           place and stays authoritative;
        2. the caller's callback re-encrypts every stored secret (old cipher in,
           new cipher out) and records the new fingerprint, **committing** its
           database transaction before it returns;
        3. only then is the staged key **promoted** to be the stored key.

        A failure before step 3 leaves the old key stored and the staged key is
        discarded. A crash between the commit and the promotion leaves a staged
        key whose fingerprint matches the committed row, which startup (or the
        next ``secrets`` command) promotes. No key material is logged.

        The cache switches to the new cipher only after the promotion; the cache
        lock is not held across the callback, so the callback may itself encrypt
        or decrypt through this provider.
        """
        old_cipher = self.get_cipher()
        new_key = Fernet.generate_key()
        new_cipher = Fernet(new_key)
        self._stage_key(new_key)
        try:
            reencrypt(old_cipher, new_cipher)
        except BaseException:
            self.discard_staged_key()
            raise
        self.promote_staged_key()
        self._install_cipher(new_cipher)
        logger.info("encryption key rotated (%s provider)", self.name)

    def promote_staged_key(self) -> None:
        """Make the staged key the stored key and drop the cached cipher."""
        self._promote()
        self.invalidate_cipher()

    # -- subclass hooks -----------------------------------------------------

    def describe(self) -> str:
        raise NotImplementedError

    def read_staged_key(self) -> bytes | None:
        raise NotImplementedError

    def discard_staged_key(self) -> None:
        raise NotImplementedError

    def replace_key(self, key: bytes) -> None:
        raise NotImplementedError

    def _read_key(self) -> bytes | None:
        raise NotImplementedError

    def _create_key(self) -> bytes:
        raise NotImplementedError

    def _stage_key(self, key: bytes) -> None:
        raise NotImplementedError

    def _promote(self) -> None:
        raise NotImplementedError


class KeyFileProvider(_CachedCipherProvider):
    """Holds the encryption key in a restricted-permission file.

    On first :meth:`get_cipher` the key is read back, or generated and written
    owner-only if the file is absent; the cipher is then cached for the life of
    the provider. The file is refused if it is group- or world-readable on a
    POSIX host (the protection POSIX modes provide is the whole point of the
    fallback).

    A key is only ever created with ``O_EXCL``, so two starters racing on a fresh
    install end with one key. A rotation stages the new key in
    ``<key file>.next`` and renames it over the key file once the database holds
    the re-encrypted data.
    """

    name = "file"

    def __init__(self, key_file: Path) -> None:
        """Create the provider for ``key_file`` (not read or created until use)."""
        super().__init__()
        self._key_file = key_file
        self._next_file = key_file.with_name(key_file.name + ".next")

    def describe(self) -> str:
        """The key file path."""
        return str(self._key_file)

    def read_staged_key(self) -> bytes | None:
        """Return the key staged in ``<key file>.next``, or ``None``."""
        return self._read_file(self._next_file)

    def discard_staged_key(self) -> None:
        """Remove ``<key file>.next`` if present."""
        self._next_file.unlink(missing_ok=True)

    def replace_key(self, key: bytes) -> None:
        """Store ``key``, keeping any previous key file as ``.replaced-<time>``.

        Used only by ``secrets reset``, which is replacing a key that cannot
        decrypt the data. The old file is kept (owner-only, as it was) rather
        than destroyed in case it was the wrong file rather than the wrong key.
        """
        if self._key_file.exists():
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            self._key_file.rename(
                self._key_file.with_name(f"{self._key_file.name}.replaced-{stamp}")
            )
        self._write_exclusive(self._key_file, key)
        self.invalidate_cipher()

    def _read_key(self) -> bytes | None:
        """Return the key file's content, or ``None`` if it does not exist."""
        return self._read_file(self._key_file)

    def _read_file(self, path: Path) -> bytes | None:
        """Read a key file after the permission check; ``None`` when absent.

        An empty file counts as absent: :meth:`_write_exclusive` creates the
        file before it writes the key, so a concurrent reader can see it empty,
        and ``b""`` is never a key. Every caller (key loading, the fingerprint,
        the staged key) therefore sees "no key" rather than an empty one (#189).
        """
        try:
            self._verify_permissions(path)
            return path.read_bytes().strip() or None
        except FileNotFoundError:
            return None

    def _create_key(self) -> bytes:
        """Create the key file exclusively; read the winner's key if we lose."""
        key = Fernet.generate_key()
        try:
            self._write_exclusive(self._key_file, key)
        except FileExistsError:
            # Another starter created it between our read and our create. Its
            # writer may not have finished, so wait for the content.
            for _ in range(_RACE_READ_ATTEMPTS):
                existing = self._read_key()
                if existing:
                    return existing
                time.sleep(_RACE_READ_PAUSE_SECONDS)
            raise
        return key

    def _verify_permissions(self, path: Path) -> None:
        """Refuse a group- or world-readable key file on a POSIX host.

        Windows does not honour POSIX modes (mirroring the local-token file), so
        the check is skipped there; on POSIX an over-permissive key file is a
        hard error with an actionable message naming the path and the fix.
        """
        if os.name == "nt":
            return
        mode = path.stat().st_mode
        if mode & _GROUP_OTHER_MASK:
            raise PermissionError(
                f"Encryption key file {path} is group- or "
                f"world-readable (mode {stat.S_IMODE(mode):#o}); refusing to "
                f"start. Restrict it with: chmod 600 {path}"
            )

    def _stage_key(self, key: bytes) -> None:
        """Write ``<key file>.next`` exclusively (a leftover is an error)."""
        self._key_file.parent.mkdir(parents=True, exist_ok=True)
        self._write_exclusive(self._next_file, key)

    def _promote(self) -> None:
        """Atomically rename ``<key file>.next`` over the key file."""
        os.replace(self._next_file, self._key_file)
        _fsync_directory(self._key_file.parent)

    @staticmethod
    def _write_exclusive(path: Path, key: bytes) -> None:
        """Create ``path`` owner-only with ``O_EXCL`` and write ``key``.

        On POSIX the file is created with ``O_CREAT|O_EXCL`` and mode ``0600``
        so it is never momentarily group/world-readable (umask only clears bits,
        so requesting ``0600`` cannot widen it) and never replaces an existing
        file. :class:`FileExistsError` propagates. On a platform without POSIX
        modes the bytes are written plainly.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            with path.open("xb") as handle:
                handle.write(key)
            return
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _KEY_FILE_MODE)
        try:
            os.write(fd, key)
            os.fsync(fd)
        finally:
            os.close(fd)


class OsKeystoreProvider(_CachedCipherProvider):
    """Holds the encryption key in the host OS secret store via ``keyring``.

    The key is stored as a single keyring item under
    ``(service_name, service_name)``; on first use a key is generated and saved.
    The cipher is cached after the first :meth:`get_cipher`, so the keyring is
    not consulted again for each decrypt.
    Construction does no I/O -- a backend probe happens in :func:`build_key_provider`
    so a missing Secret Service degrades to the file fallback rather than raising
    here.

    A rotation stages the new key under a second item (``encryption-key-next``)
    and copies it over the main item once the database holds the re-encrypted
    data, mirroring the file provider.
    """

    name = "os_keystore"

    # A fixed account name paired with the service name for the keyring item.
    _ACCOUNT = "encryption-key"
    _NEXT_ACCOUNT = "encryption-key-next"

    def __init__(self, service_name: str) -> None:
        """Create the provider for keyring service ``service_name``."""
        super().__init__()
        self._service = service_name

    def describe(self) -> str:
        """The keyring service and account."""
        return f"OS keystore item {self._service}/{self._ACCOUNT}"

    def read_staged_key(self) -> bytes | None:
        """Return the key staged under the ``-next`` item, or ``None``."""
        return self._get(self._NEXT_ACCOUNT)

    def discard_staged_key(self) -> None:
        """Remove the ``-next`` item if present (best effort)."""
        import keyring

        with contextlib.suppress(Exception):
            keyring.delete_password(self._service, self._NEXT_ACCOUNT)

    def replace_key(self, key: bytes) -> None:
        """Store ``key`` as the main item (``secrets reset`` only)."""
        self._set(self._ACCOUNT, key)
        self.invalidate_cipher()

    def _get(self, account: str) -> bytes | None:
        import keyring

        value = keyring.get_password(self._service, account)
        return None if value is None else value.encode("ascii").strip()

    def _set(self, account: str, key: bytes) -> None:
        import keyring

        keyring.set_password(self._service, account, key.decode("ascii"))

    def _read_key(self) -> bytes | None:
        """Return the keyring-stored key bytes, or ``None`` if absent."""
        return self._get(self._ACCOUNT)

    def _create_key(self) -> bytes:
        """Generate and store a key (the keyring has no exclusive create)."""
        key = Fernet.generate_key()
        self._set(self._ACCOUNT, key)
        return key

    def _stage_key(self, key: bytes) -> None:
        self._set(self._NEXT_ACCOUNT, key)

    def _promote(self) -> None:
        staged = self._get(self._NEXT_ACCOUNT)
        if staged is None:
            raise RuntimeError("no staged key to promote")
        self._set(self._ACCOUNT, staged)
        self.discard_staged_key()


def _fsync_directory(directory: Path) -> None:
    """Flush a directory entry change to disk (best effort; not on Windows)."""
    if os.name == "nt":
        return
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _default_key_file(settings: SecretsSettings, data_dir: Path) -> Path:
    """Resolve the fallback key file: ``key_file`` if set, else data_dir/.secret-key."""
    if settings.key_file is not None:
        return settings.key_file
    return data_dir / ".secret-key"


def _os_keystore_available(service_name: str) -> bool:
    """Return True if a usable keyring backend can store and read a probe value.

    Headless Linux and Docker commonly have no Secret Service: ``keyring`` then
    exposes a null/fail backend that raises on use. Rather than inspect backend
    classes, this performs a real round-trip on a throwaway probe item and treats
    any failure (no backend, locked store, transient error) as "unavailable" so
    the caller falls back to the file provider. The probe item is removed.
    """
    try:
        import keyring
        from keyring.errors import KeyringError
    except Exception:  # noqa: BLE001 - keyring import itself failing => unavailable
        return False

    probe_account = "backend-probe"
    try:
        keyring.set_password(service_name, probe_account, "probe")
        ok = keyring.get_password(service_name, probe_account) == "probe"
        keyring.delete_password(service_name, probe_account)
        return ok
    except (KeyringError, RuntimeError, OSError):
        return False
    except Exception:  # noqa: BLE001 - any backend fault means "use the file"
        return False


def provider_for_name(
    name: str, settings: SecretsSettings, data_dir: Path
) -> KeyProvider | None:
    """Build the provider a ``secrets_meta`` row names, without probing.

    Returns ``None`` for ``os_keystore`` when no keyring backend round-trips (the
    recorded store is gone, which the caller reports) and for an unknown name.
    Unlike :func:`build_key_provider` this never falls back to the other store:
    a key found in the wrong place is a different key.
    """
    if name == KeyFileProvider.name:
        return KeyFileProvider(_default_key_file(settings, data_dir))
    if name == OsKeystoreProvider.name and _os_keystore_available(
        settings.keystore_service_name
    ):
        return OsKeystoreProvider(settings.keystore_service_name)
    return None


def build_key_provider(settings: SecretsSettings, data_dir: Path) -> KeyProvider:
    """Select the key provider: OS keystore when reachable, else the file fallback.

    When ``use_os_keystore`` is set and a keyring backend round-trips a probe, the
    OS keystore provider is returned; otherwise (the headless/Docker case, or an
    explicit opt-out) the restricted-permission file provider is used. Selection
    is logged at INFO; the key itself is never logged.

    :param settings: the secrets configuration section.
    :param data_dir: the data directory, used to derive the default key file when
        ``settings.key_file`` is unset.
    """
    if settings.use_os_keystore and _os_keystore_available(
        settings.keystore_service_name
    ):
        logger.info("using OS keystore for at-rest encryption key")
        return OsKeystoreProvider(settings.keystore_service_name)
    key_file = _default_key_file(settings, data_dir)
    logger.info("using key-file fallback for at-rest encryption key")
    return KeyFileProvider(key_file)
