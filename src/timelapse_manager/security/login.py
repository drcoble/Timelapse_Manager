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
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..config import AuthSettings
from ..db.models import User
from .passwords import hash_password, needs_rehash, verify_password

_ADMIN_ROLE = "admin"
_VIEWER_ROLE = "viewer"
_LOCAL_AUTH_SOURCE = "local"


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
    user = db.execute(
        select(User).where(User.username == username)
    ).scalar_one_or_none()
    if user is None or not user.enabled:
        return None
    if user.auth_source != _LOCAL_AUTH_SOURCE:
        return None
    # A NULL/empty hash (the sentinel, or a not-yet-provisioned account) can
    # never authenticate; verify_password returns False for it.
    if not verify_password(password, user.password_hash, settings):
        return None
    if user.password_hash is not None and needs_rehash(user.password_hash, settings):
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
        username=username,
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
