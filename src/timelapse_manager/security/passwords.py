"""Password hashing with Argon2id.

Passwords are never stored or compared in plaintext. They are hashed with
Argon2id -- a memory-hard function resistant to GPU/ASIC offline attacks -- via
``argon2-cffi``. The encoded hash carries its own algorithm, version, and cost
parameters, so verification is self-describing and parameters can be raised over
time with :func:`needs_rehash` driving a transparent upgrade on next login.

Cost parameters are read from :class:`AuthSettings` so they can be tuned per
deployment (kept modest enough for a small single-board computer by default).
Never log a password, a hash, or any derived secret.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2 import exceptions as argon2_exceptions

from ..config import AuthSettings


def _hasher(settings: AuthSettings) -> PasswordHasher:
    """Build a :class:`PasswordHasher` from the configured Argon2 cost."""
    return PasswordHasher(
        time_cost=settings.argon2_time_cost,
        memory_cost=settings.argon2_memory_kib,
        parallelism=settings.argon2_parallelism,
    )


def hash_password(plain: str, settings: AuthSettings) -> str:
    """Return an Argon2id hash of ``plain`` using the configured cost.

    The returned string is the self-describing PHC-format encoding (algorithm,
    version, parameters, salt, and digest), suitable for storage and later
    verification with :func:`verify_password`.
    """
    return _hasher(settings).hash(plain)


def verify_password(plain: str, hashed: str | None, settings: AuthSettings) -> bool:
    """Return True if ``plain`` matches ``hashed``; False otherwise.

    The underlying Argon2 verification is constant-time with respect to the
    secret. A missing or empty stored hash (for example the non-login service
    sentinel, or a directory account that carries no local password) never
    verifies, so it cannot be used to authenticate. Any malformed hash or
    mismatch yields False rather than raising.
    """
    if not hashed:
        return False
    try:
        return _hasher(settings).verify(hashed, plain)
    except (
        argon2_exceptions.VerifyMismatchError,
        argon2_exceptions.VerificationError,
        argon2_exceptions.InvalidHashError,
    ):
        return False


def needs_rehash(hashed: str, settings: AuthSettings) -> bool:
    """Return True if ``hashed`` was produced with weaker-than-current cost.

    Lets a successful login transparently re-hash a password whose stored cost
    is below the deployment's current Argon2 parameters. A hash that cannot be
    parsed is treated as needing a rehash so it is replaced on next login.
    """
    try:
        return _hasher(settings).check_needs_rehash(hashed)
    except argon2_exceptions.InvalidHashError:
        return True
