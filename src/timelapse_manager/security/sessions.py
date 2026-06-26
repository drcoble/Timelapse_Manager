"""Server-side login sessions.

A session is identified by a high-entropy token generated with
:func:`secrets.token_urlsafe`. Only the SHA-256 hash of that token is persisted
(in ``session.token_hash``); the raw token is returned to the caller exactly
once so it can be placed in the client cookie. Looking a session up hashes the
presented token and resolves it through the unique index -- a leak of the
database therefore discloses no usable session credential.

Lifetime is bounded on two independent axes:

* **Idle** -- the session expires once it has been untouched for longer than the
  configured idle timeout, measured from ``last_active``. This applies to every
  session, persistent ones included.
* **Creation-anchored cap** -- a non-persistent session expires once it is older
  than the absolute timeout; a persistent ("remember me") session uses the
  longer persistent timeout instead. Either way the cap is measured from
  ``created_at`` and stored in ``expires_at`` for inspection.

Every successful lookup also rejects sessions that are revoked or whose owning
user has been disabled, and refreshes ``last_active`` so an active session does
not idle out. Tokens, hashes, and CSRF secrets are never logged.
"""

from __future__ import annotations

import datetime
import hashlib
import secrets
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..config import SessionSettings
from ..db.models import Session as SessionRow
from ..db.models import User

# Bytes of entropy in a raw session token. token_urlsafe yields ~1.3 chars per
# byte, so 32 bytes is a 43-character, 256-bit token -- ample to make guessing
# and birthday collisions infeasible.
_TOKEN_BYTES = 32

# Bytes of entropy in a per-session CSRF secret (see :mod:`.csrf`).
_CSRF_SECRET_BYTES = 32

NowFn = Callable[[], datetime.datetime]


def _utcnow() -> datetime.datetime:
    """Return the current UTC time as a naive datetime.

    The schema stores naive UTC timestamps (the database clock is UTC), so we
    drop the tzinfo to compare apples to apples with stored columns.
    """
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


def hash_token(raw_token: str) -> str:
    """Return the hex SHA-256 digest of a raw session token.

    Used both when persisting a new session and when resolving a presented
    token, so the same one-way transform guards storage and lookup.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _absolute_cap_seconds(persistent: bool, settings: SessionSettings) -> int:
    """Return the creation-anchored lifetime cap for a session, in seconds."""
    if persistent:
        return settings.persistent_timeout_seconds
    return settings.absolute_timeout_seconds


def create_session(
    db: DbSession,
    user: User,
    *,
    remember_me: bool,
    settings: SessionSettings,
    now: NowFn = _utcnow,
) -> tuple[SessionRow, str]:
    """Create a new session for ``user`` and return ``(row, raw_token)``.

    Generates a fresh high-entropy token, persists only its hash plus a new
    per-session CSRF secret, anchors the creation-based expiry, and seeds
    ``last_active`` so the idle clock starts now. The returned raw token is the
    only copy that ever leaves this function; the caller places it in the cookie
    and must not log it.
    """
    raw_token = secrets.token_urlsafe(_TOKEN_BYTES)
    created = now()
    cap = _absolute_cap_seconds(remember_me, settings)
    row = SessionRow(
        user_id=user.id,
        persistent=remember_me,
        token_hash=hash_token(raw_token),
        csrf_secret=secrets.token_urlsafe(_CSRF_SECRET_BYTES),
        created_at=created,
        last_active=created,
        expires_at=created + datetime.timedelta(seconds=cap),
        revoked=False,
    )
    db.add(row)
    db.flush()
    return row, raw_token


def get_session_row(
    db: DbSession,
    raw_token: str,
    *,
    settings: SessionSettings,
    now: NowFn = _utcnow,
) -> SessionRow | None:
    """Resolve a raw token to a live :class:`SessionRow`, or ``None``.

    Returns ``None`` for any non-live session: not found, revoked, idle-expired,
    creation-cap-expired, or owned by a disabled user. On success the row's
    ``last_active`` is refreshed (so activity defers idle expiry) and the row is
    returned -- callers needing the user should prefer :func:`lookup_session`,
    while callers needing the per-session CSRF secret use this.
    """
    if not raw_token:
        return None
    token_hash = hash_token(raw_token)
    row = db.execute(
        select(SessionRow).where(SessionRow.token_hash == token_hash)
    ).scalar_one_or_none()
    if row is None or row.revoked:
        return None

    current = now()
    # Idle timeout: untouched for longer than the idle window. Applies to
    # persistent sessions too; persistent only lengthens the creation cap below.
    idle_deadline = (row.last_active or row.created_at) + datetime.timedelta(
        seconds=settings.idle_timeout_seconds
    )
    if current > idle_deadline:
        return None
    # Creation-anchored cap (absolute, or persistent for "remember me").
    cap = _absolute_cap_seconds(row.persistent, settings)
    if current > row.created_at + datetime.timedelta(seconds=cap):
        return None

    user = db.get(User, row.user_id)
    if user is None or not user.enabled:
        return None

    # Directory-backed sessions are periodically re-evaluated against the
    # directory so deprovisioning and role changes take effect on a live (incl.
    # "remember me") session. This is a no-op for local sessions and for LDAP
    # sessions whose re-check interval has not elapsed; only the rare due request
    # triggers a directory round-trip. A revoke (account gone / no mapped group)
    # makes the session no longer live -- treated exactly like any other dead
    # session by returning None.
    if not _revalidate(db, row, user, settings=settings, now=now):
        return None

    row.last_active = current
    db.flush()
    return row


def _revalidate(
    db: DbSession,
    row: SessionRow,
    user: User,
    *,
    settings: SessionSettings,
    now: NowFn,
) -> bool:
    """Run directory re-evaluation for a session; return False if it was revoked.

    Delegated to :mod:`.session_revalidation` (imported lazily so the session
    layer carries no import-time dependency on the LDAP connector chain). Returns
    ``True`` for every non-LDAP session and for an LDAP session that is kept.
    """
    from .session_revalidation import revalidate_ldap_session

    return revalidate_ldap_session(db, row, user, settings=settings, now=now)


def lookup_session(
    db: DbSession,
    raw_token: str,
    *,
    settings: SessionSettings,
    now: NowFn = _utcnow,
) -> User | None:
    """Resolve a raw session token to its live :class:`User`, or ``None``.

    Thin wrapper over :func:`get_session_row` for the common case of needing the
    authenticated user. Returns ``None`` on any of: token not found, revoked,
    idle-expired, creation-cap-expired, or a disabled user.
    """
    row = get_session_row(db, raw_token, settings=settings, now=now)
    if row is None:
        return None
    return db.get(User, row.user_id)


def rotate_session(
    db: DbSession,
    old_token: str,
    user: User,
    *,
    remember_me: bool,
    settings: SessionSettings,
    now: NowFn = _utcnow,
) -> tuple[SessionRow, str]:
    """Issue a fresh session and revoke the old one (login rotation).

    Mitigates session fixation: on authentication the prior session token (if
    any) is revoked and a brand-new row/token pair is minted. A missing or
    already-unknown ``old_token`` is tolerated -- the new session is still
    created. Returns ``(new_row, new_raw_token)``.
    """
    revoke_session(db, old_token, now=now)
    return create_session(db, user, remember_me=remember_me, settings=settings, now=now)


def revoke_session(
    db: DbSession,
    raw_token: str,
    *,
    now: NowFn = _utcnow,
) -> None:
    """Revoke the session identified by ``raw_token`` if it exists.

    Idempotent: an unknown or already-revoked token is a no-op. Revocation is by
    flag (not deletion) so the row remains for audit/foreign-key integrity.
    """
    if not raw_token:
        return
    token_hash = hash_token(raw_token)
    row = db.execute(
        select(SessionRow).where(SessionRow.token_hash == token_hash)
    ).scalar_one_or_none()
    if row is None or row.revoked:
        return
    row.revoked = True
    row.revoked_at = now()
    db.flush()


def revoke_all_user_sessions(
    db: DbSession,
    user_id: int,
    *,
    now: NowFn = _utcnow,
) -> None:
    """Revoke every live session belonging to ``user_id``.

    Used on password change (and available for an administrative "sign out
    everywhere") so a credential change invalidates all outstanding sessions.
    Already-revoked rows are left untouched.
    """
    rows = (
        db.execute(
            select(SessionRow).where(
                SessionRow.user_id == user_id,
                SessionRow.revoked.is_(False),
            )
        )
        .scalars()
        .all()
    )
    revoked_at = now()
    for row in rows:
        row.revoked = True
        row.revoked_at = revoked_at
    if rows:
        db.flush()
