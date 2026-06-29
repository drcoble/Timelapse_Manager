"""Symmetric encryption for credentials stored at rest.

A thin wrapper over :class:`cryptography.fernet.Fernet` that the persistence
layer uses to protect stored secrets (SMTP / webhook / camera / LDAP). The key
is obtained from the :mod:`.keystore` provider, not passed in, so call sites stay
free of key-management concerns.

Versioned ciphertext
--------------------
Every encrypted value carries a ``enc:v1:`` prefix. This lets a read distinguish
ciphertext this layer produced from a **legacy plaintext** value written before
encryption existed: :func:`decrypt_secret` returns a value lacking the prefix
unchanged. That is the migration strategy -- no schema change and no bulk
data-migration pass; a legacy value is silently re-encrypted the next time its
field is written through the normal write path.

Fernet ciphertext is non-deterministic (each token embeds a random IV and a
timestamp), so two encryptions of the same plaintext differ. Never compare
ciphertexts for equality to infer anything about the plaintext.

Provider seam
-------------
The key provider is installed once at startup via :func:`set_key_provider` (which
the application lifespan calls with the provider chosen by
:func:`.keystore.build_key_provider`). Tests plug in a fake the same way. There is
no implicit fallback: a call that reaches encryption before a provider is
installed raises a clear error rather than silently inventing or persisting a key
(which would write key material to disk as a side effect and could mask a wiring
bug). The module thus has no import-time dependency on a live ``Settings``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cryptography.fernet import InvalidToken

if TYPE_CHECKING:
    from .keystore import KeyProvider

# Marks a value this layer encrypted, and its format version. A value without
# this prefix is treated as legacy plaintext on read.
_PREFIX = "enc:v1:"

# The process-wide key provider, installed at startup (or in tests). ``None``
# means no provider is installed yet -- a hard error if encryption is attempted,
# never a silent fallback (which would persist key material as a side effect).
_provider: KeyProvider | None = None


def set_key_provider(provider: KeyProvider | None) -> None:
    """Install (or clear) the process-wide key provider.

    Startup wiring installs the provider selected by
    :func:`.keystore.build_key_provider`; tests inject a fake. Passing ``None``
    clears it (the application's ``dispose`` does this on teardown), after which
    any encryption attempt raises until a provider is installed again.
    """
    global _provider
    _provider = provider


def _get_provider() -> KeyProvider:
    """Return the installed key provider, or raise if none is installed.

    There is deliberately no lazy fallback: building a provider here would read
    the application context and could persist a key file as a side effect,
    masking a startup-wiring bug. In production the provider is always installed
    by the lifespan before any secret is read or written; a caller reaching this
    without one has bypassed startup (typically a test missing the provider
    fixture), which this surfaces as a clear, actionable error.
    """
    if _provider is None:
        raise RuntimeError(
            "No encryption key provider is installed. Startup wiring must call "
            "set_key_provider() (with build_key_provider(...)) before any stored "
            "secret is encrypted or decrypted."
        )
    return _provider


def is_encrypted(value: str) -> bool:
    """Return True if ``value`` is a ciphertext this layer produced."""
    return value.startswith(_PREFIX)


def encrypt_secret(plaintext: str) -> str:
    """Encrypt ``plaintext`` and return a prefixed, versioned ciphertext string.

    The returned value is ``enc:v1:<fernet-token>``. An empty string encrypts to
    an empty string so an absent secret stays absent (callers store ``None``/``""``
    to mean "no secret", and this preserves that without producing a token).
    """
    if plaintext == "":
        return ""
    cipher = _get_provider().get_cipher()
    token = cipher.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return f"{_PREFIX}{token}"


def decrypt_secret(token: str) -> str:
    """Return the plaintext for a stored secret, tolerating legacy plaintext.

    A value carrying the ``enc:v1:`` prefix is decrypted. A value lacking it is a
    legacy plaintext written before encryption existed and is returned unchanged
    (the documented migration path). An empty string returns empty.

    :raises InvalidToken: if a prefixed value fails to decrypt (wrong key or
        corrupt ciphertext) -- surfaced rather than silently returning garbage.
    """
    if token == "":
        return ""
    if not is_encrypted(token):
        return token  # legacy plaintext passthrough
    cipher = _get_provider().get_cipher()
    raw = token[len(_PREFIX) :]
    return cipher.decrypt(raw.encode("ascii")).decode("utf-8")


def encrypt_credentials(
    credentials: dict[str, object] | None,
) -> dict[str, object] | None:
    """Return a copy of a camera credential document with secret fields encrypted.

    The camera credential document is a small JSON object (e.g.
    ``{"username": ..., "password": ...}``). Only secret-bearing keys
    (``password``/``secret``/``token``) are encrypted; non-secret fields such as
    the username pass through so the record stays readable for display. ``None``
    and an empty document pass through unchanged.

    This helper exists for the camera persistence boundary to adopt; it is the
    counterpart of :func:`decrypt_credentials`.
    """
    if not credentials:
        return credentials
    return {
        key: (encrypt_secret(str(value)) if _is_secret_field(key) and value else value)
        for key, value in credentials.items()
    }


def decrypt_credentials(
    credentials: dict[str, object] | None,
) -> dict[str, object] | None:
    """Return a copy of a camera credential document with secret fields decrypted.

    Inverse of :func:`encrypt_credentials`; legacy plaintext fields pass through
    via :func:`decrypt_secret`'s passthrough. ``None`` and empty pass through.
    """
    if not credentials:
        return credentials
    return {
        key: (decrypt_secret(str(value)) if _is_secret_field(key) and value else value)
        for key, value in credentials.items()
    }


def _is_secret_field(key: str) -> bool:
    """Return True if a credential-document key names a secret to encrypt."""
    lowered = key.lower()
    return "password" in lowered or "secret" in lowered or "token" in lowered


# Re-exported so callers can catch a decrypt failure without importing cryptography.
__all__ = [
    "InvalidToken",
    "decrypt_credentials",
    "decrypt_secret",
    "encrypt_credentials",
    "encrypt_secret",
    "is_encrypted",
    "set_key_provider",
]
