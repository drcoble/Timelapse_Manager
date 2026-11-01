"""Local-account login, first-run setup, and password changes.

This is the credential layer for *local* accounts only. Directory (LDAP) login
is a future seam and is not implemented here. The module also owns the first-run
bootstrap: deciding whether an initial administrator must still be created and
creating it.

A service ``sentinel`` user (id 1) exists from the schema's first migration so
that audit events always have a resolvable actor. The sentinel has **no**
password hash and must never be treated as a real administrator: it cannot log
in, and a database that contains only the sentinel is still considered to need
first-run setup. Passwords, hashes, and tokens are never logged.

First-run setup is gated by a one-time **setup code** (issue #67, S11): the
service writes it to ``<data_dir>/setup-code`` (owner-only) the first time the
setup page is needed, and ``POST /first-run`` must present it. A peer that can
reach the port but cannot read the service's data directory cannot claim the
first administrator. The code itself is never logged.
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import threading
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..config import AuthSettings
from ..db.models import User
from ..usernames import normalize_username, username_key
from .passwords import hash_password, needs_rehash, verify_password

logger = logging.getLogger(__name__)

_ADMIN_ROLE = "admin"
_VIEWER_ROLE = "viewer"
_LOCAL_AUTH_SOURCE = "local"

# The first-run setup code: its file name under ``data_dir``, and its length.
# ``token_urlsafe(9)`` is 12 URL-safe characters (72 random bits).
SETUP_CODE_FILENAME = "setup-code"
_SETUP_CODE_BYTES = 9
SETUP_CODE_LENGTH = 12
_SETUP_CODE_MODE = 0o600

# Serialises first-run admin creation within the process: a submit re-checks
# ``first_run_needed`` and commits the new admin while holding it, so two
# concurrent submits cannot both create one. The service is a single process
# (one app object behind both listeners), so a process-local lock suffices.
first_run_lock = threading.Lock()

# A random plaintext that nothing in the system knows or stores, used solely to
# build the timing-decoy hash below. The decoy verification always compares the
# submitted password against this hash, so it can never match a real credential
# and the discarded result can never authenticate anyone.
_DECOY_PASSWORD = secrets.token_urlsafe(32)

# Cache of decoy Argon2 hashes keyed by the cost parameters they were produced
# with -- (time_cost, memory_kib, parallelism). The real verification derives
# its hasher from those same settings, so the decoy must mirror them to take a
# comparable amount of time. A hash is computed once per distinct cost on first
# use and reused thereafter.
_decoy_hash_cache: dict[tuple[int, int, int], str] = {}


def _decoy_hash(settings: AuthSettings) -> str:
    """Return a throwaway Argon2 hash matching ``settings``' cost parameters.

    Used to equalise the timing of login failures: paths that never reach the
    real password check still run a full Argon2 verification against this hash,
    so an attacker cannot distinguish an unknown username from a real one by how
    quickly the request fails. The hash is computed once per distinct cost and
    cached; it is built from :data:`_DECOY_PASSWORD`, which nothing can supply.
    """
    key = (
        settings.argon2_time_cost,
        settings.argon2_memory_kib,
        settings.argon2_parallelism,
    )
    cached = _decoy_hash_cache.get(key)
    if cached is None:
        cached = hash_password(_DECOY_PASSWORD, settings)
        _decoy_hash_cache[key] = cached
    return cached


def authenticate_user(
    db: DbSession,
    username: str,
    password: str,
    *,
    settings: AuthSettings,
) -> User | None:
    """Return the local :class:`User` if ``password`` is correct, else ``None``.

    Handles local accounts only. Returns ``None`` -- never raises -- for every
    failure mode (unknown user, disabled user, directory account, missing or
    wrong password, the password-less sentinel), so callers can present a single
    generic "invalid credentials" result that does not reveal which condition
    failed.

    On a correct password whose stored hash is below the current Argon2 cost,
    the hash is transparently upgraded so the stronger parameters take effect
    without prompting the user.
    """
    # Case- and whitespace-insensitive (#4): ``Admin`` and `` admin`` are the
    # same local account.
    user = db.execute(
        select(User).where(User.username_key == username_key(username))
    ).scalar_one_or_none()
    if user is None or not user.enabled or user.auth_source != _LOCAL_AUTH_SOURCE:
        # An unknown username, a disabled account, and a directory account all
        # fail here before the real password check would run. Verify the
        # submitted password against a throwaway decoy hash (discarding the
        # result) so this path costs about the same as the wrong-password path,
        # denying an attacker a timing oracle for enumerating valid usernames.
        verify_password(password, _decoy_hash(settings), settings)
        return None
    # A NULL/empty hash (the sentinel, or a not-yet-provisioned account) can
    # never authenticate. Run the decoy verify here too -- verify_password would
    # otherwise short-circuit to False without any Argon2 work, making this the
    # one failure path an attacker could time -- then fail like every other.
    if not user.password_hash:
        verify_password(password, _decoy_hash(settings), settings)
        return None
    if not verify_password(password, user.password_hash, settings):
        return None
    if needs_rehash(user.password_hash, settings):
        user.password_hash = hash_password(password, settings)
        db.flush()
    return user


def first_run_needed(db: DbSession) -> bool:
    """Return True if no real administrator account exists yet.

    "Real" means an *enabled* admin that carries a password hash. The service
    sentinel (admin role, NULL hash) does not count, so a database seeded with
    only the sentinel still reports that first-run setup is required.
    """
    real_admin = db.execute(
        select(User.id).where(
            User.role == _ADMIN_ROLE,
            User.enabled.is_(True),
            User.password_hash.is_not(None),
        )
    ).first()
    return real_admin is None


def create_local_user(
    db: DbSession,
    username: str,
    password: str,
    role: str = _VIEWER_ROLE,
    *,
    settings: AuthSettings,
) -> User:
    """Create a new enabled, local account with a hashed password and ``role``.

    The single place a local account is materialised, shared by first-run admin
    setup and any other seeding path (such as a non-interactive deploy). The
    password is stored only as an Argon2 hash; the plaintext is never persisted
    or logged. ``role`` defaults to the least-privileged ``"viewer"`` so callers
    must opt in to elevated access. The caller commits the surrounding
    transaction.
    """
    user = User(
        username=normalize_username(username),
        auth_source=_LOCAL_AUTH_SOURCE,
        password_hash=hash_password(password, settings),
        role=role,
        enabled=True,
    )
    db.add(user)
    db.flush()
    return user


def create_initial_admin(
    db: DbSession,
    username: str,
    password: str,
    *,
    settings: AuthSettings,
) -> User:
    """Create the first real administrator as a new local account.

    Creates a brand-new enabled, local, admin user with a hashed password. The
    password-less sentinel (id 1) is deliberately left untouched so it remains a
    non-login service principal for audit foreign keys; the real admin is always
    a separate row. The caller commits the surrounding transaction. Delegates to
    :func:`create_local_user` so account creation lives in one place.
    """
    return create_local_user(db, username, password, _ADMIN_ROLE, settings=settings)


def change_password(
    db: DbSession,
    user: User,
    new_password: str,
    *,
    settings: AuthSettings,
) -> None:
    """Set ``user``'s password and revoke all of that user's sessions.

    A credential change must invalidate every outstanding session for the user,
    so a previously stolen session token cannot survive the change. The session
    revocation is delegated to the sessions layer to keep that policy in one
    place; this is invoked by the caller alongside the hash update.
    """
    # Imported lazily to avoid a module import cycle (sessions imports models,
    # this module is imported by the security package surface).
    from .sessions import revoke_all_user_sessions

    user.password_hash = hash_password(new_password, settings)
    db.flush()
    revoke_all_user_sessions(db, user.id)


def setup_code_path(data_dir: Path) -> Path:
    """Return where the first-run setup code lives for ``data_dir``."""
    return data_dir / SETUP_CODE_FILENAME


def ensure_setup_code(data_dir: Path) -> Path:
    """Create the first-run setup code file if it does not exist; return its path.

    The caller decides that first-run setup is still needed. The code is written
    to a private temporary file first and then hard-linked into place, so the
    file appears complete and ``0600`` or not at all: a concurrent reader never
    sees it half-written, and the link fails if another caller won the race, in
    which case that caller's code stands. Logs a WARNING naming the path (never
    the code) when a new code is created.

    :raises OSError: if ``data_dir`` is missing or not writable.
    """
    target = setup_code_path(data_dir)
    if target.exists():
        return target
    code = secrets.token_urlsafe(_SETUP_CODE_BYTES)
    temp = data_dir / f".{SETUP_CODE_FILENAME}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temp, flags, _SETUP_CODE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(code + "\n")
        try:
            os.link(temp, target)
        except FileExistsError:
            return target
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()
    logger.warning("first-run setup code written to %s", target)
    return target


def read_setup_code(data_dir: Path) -> str | None:
    """Return the stored setup code, or ``None`` if there is no usable one.

    Opens without following a symbolic link, so a link planted in the data
    directory is never read through (this also runs as root from the CLI). A
    missing, unreadable or malformed file yields ``None``.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(setup_code_path(data_dir), flags)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(256)
    except OSError:
        return None
    try:
        code = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    return code if len(code) == SETUP_CODE_LENGTH else None


def verify_setup_code(data_dir: Path, presented: str) -> bool:
    """Return whether ``presented`` matches the stored setup code.

    Fails closed: no stored code, an empty or wrong-length submission, or any
    non-ASCII input is a mismatch. The comparison is constant-time.
    """
    expected = read_setup_code(data_dir)
    if expected is None:
        return False
    candidate = presented.strip()
    if len(candidate) != SETUP_CODE_LENGTH or not candidate.isascii():
        return False
    return secrets.compare_digest(candidate.encode("ascii"), expected.encode("ascii"))


def discard_setup_code(data_dir: Path) -> None:
    """Delete the setup code once first-run setup is complete.

    A failure is logged, not raised: the admin already exists, and the code is
    only ever checked while first-run setup is still needed.
    """
    try:
        setup_code_path(data_dir).unlink(missing_ok=True)
    except OSError:
        logger.warning("could not delete the first-run setup code", exc_info=True)
