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
:meth:`KeyProvider.rotate` generates a new key, hands both the old and new
:class:`~cryptography.fernet.Fernet` instances to a caller-supplied callback that
re-encrypts every stored secret, and only persists the new key once the callback
returns. Neither the old nor the new key material is logged.
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Callable
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

# A callback that re-encrypts all stored secrets given the old and new ciphers.
# It is handed both so it can read with the old key and write with the new one.
RotationCallback = Callable[["Fernet", "Fernet"], None]


class KeyProvider(Protocol):
    """The storage-agnostic interface the crypto layer reads the key through."""

    def get_cipher(self) -> Fernet:
        """Return a Fernet cipher built from the current key, creating one if needed."""
        ...

    def rotate(self, reencrypt: RotationCallback) -> None:
        """Generate a new key, re-encrypt secrets via the callback, then persist it."""
        ...


class KeyFileProvider:
    """Holds the encryption key in a restricted-permission file.

    On first :meth:`get_cipher` a key is generated and written owner-only; on
    later calls it is read back. The file is refused if it is group- or
    world-readable on a POSIX host (the protection POSIX modes provide is the
    whole point of the fallback).
    """

    def __init__(self, key_file: Path) -> None:
        """Create the provider for ``key_file`` (not read or created until use)."""
        self._key_file = key_file

    def get_cipher(self) -> Fernet:
        """Return a cipher from the key file, generating the key on first use."""
        return Fernet(self._load_or_create_key())

    def rotate(self, reencrypt: RotationCallback) -> None:
        """Generate a new key, re-encrypt all secrets, then atomically replace.

        The current key is loaded, a new key is generated, and the caller's
        callback re-encrypts every stored secret (old cipher in, new cipher out)
        within the caller's database transaction. Only after it returns is the
        new key persisted, so a failure mid-rotation leaves the old key in place
        and the data still decryptable. No key material is logged.
        """
        old_cipher = Fernet(self._load_or_create_key())
        new_key = Fernet.generate_key()
        new_cipher = Fernet(new_key)
        reencrypt(old_cipher, new_cipher)
        self._write_key(new_key)
        logger.info("encryption key rotated (file provider)")

    def _load_or_create_key(self) -> bytes:
        """Return the stored key bytes, creating and persisting one if absent."""
        if self._key_file.exists():
            self._verify_permissions()
            return self._key_file.read_bytes().strip()
        key = Fernet.generate_key()
        self._write_key(key)
        return key

    def _verify_permissions(self) -> None:
        """Refuse a group- or world-readable key file on a POSIX host.

        Windows does not honour POSIX modes (mirroring the local-token file), so
        the check is skipped there; on POSIX an over-permissive key file is a
        hard error with an actionable message naming the path and the fix.
        """
        if os.name == "nt":
            return
        mode = self._key_file.stat().st_mode
        if mode & _GROUP_OTHER_MASK:
            raise PermissionError(
                f"Encryption key file {self._key_file} is group- or "
                f"world-readable (mode {stat.S_IMODE(mode):#o}); refusing to "
                f"start. Restrict it with: chmod 600 {self._key_file}"
            )

    def _write_key(self, key: bytes) -> None:
        """Persist ``key`` owner-only, replacing any existing file.

        On POSIX the file is (re)created with ``O_CREAT|O_TRUNC`` and mode
        ``0600`` so it is never momentarily group/world-readable (umask only
        clears bits, so requesting ``0600`` cannot widen it). A defensive
        ``chmod`` follows in case the file pre-existed with a wider mode. On a
        platform without POSIX modes the bytes are written plainly.
        """
        self._key_file.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            self._key_file.write_bytes(key)
            return
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(self._key_file, flags, _KEY_FILE_MODE)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        # Covers the case where the file already existed with a wider mode: the
        # O_TRUNC open above does not reset permissions on an existing file.
        os.chmod(self._key_file, _KEY_FILE_MODE)


class OsKeystoreProvider:
    """Holds the encryption key in the host OS secret store via ``keyring``.

    The key is stored as a single keyring item under
    ``(service_name, service_name)``; on first use a key is generated and saved.
    Construction does no I/O -- a backend probe happens in :func:`build_key_provider`
    so a missing Secret Service degrades to the file fallback rather than raising
    here.
    """

    # A fixed account name paired with the service name for the keyring item.
    _ACCOUNT = "encryption-key"

    def __init__(self, service_name: str) -> None:
        """Create the provider for keyring service ``service_name``."""
        self._service = service_name

    def get_cipher(self) -> Fernet:
        """Return a cipher from the keyring key, generating one on first use."""
        return Fernet(self._load_or_create_key())

    def rotate(self, reencrypt: RotationCallback) -> None:
        """Generate a new key, re-encrypt all secrets, then store it. No key logged."""
        import keyring

        old_cipher = Fernet(self._load_or_create_key())
        new_key = Fernet.generate_key()
        new_cipher = Fernet(new_key)
        reencrypt(old_cipher, new_cipher)
        keyring.set_password(self._service, self._ACCOUNT, new_key.decode("ascii"))
        logger.info("encryption key rotated (OS keystore provider)")

    def _load_or_create_key(self) -> bytes:
        """Return the keyring-stored key bytes, generating and saving one if absent."""
        import keyring

        existing = keyring.get_password(self._service, self._ACCOUNT)
        if existing is not None:
            return existing.encode("ascii")
        key = Fernet.generate_key()
        keyring.set_password(self._service, self._ACCOUNT, key.decode("ascii"))
        return key


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
