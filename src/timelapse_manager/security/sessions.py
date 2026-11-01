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

Touching ``last_active`` (issue #37)
------------------------------------
The refresh is a write, and SQLite has one writer. Writing it on every request,
in the request's own transaction, made every authenticated request hold the
write lock until its endpoint returned, so the ~24 parallel requests of a
dashboard load queued behind each other on the lock. The refresh is therefore
throttled (written only once it is :func:`touch_interval` stale, so idle expiry
has that granularity) and, when it is the transaction's only write, committed at
once so the lock is held for one ``UPDATE`` rather than for the whole request.

Directory re-check bookkeeping (issue #66, C1)
----------------------------------------------
The periodic LDAP re-check writes too (``last_revalidated_at``, a changed role,
or a revoke). Its writes get the same short-commit treatment as the touch, and
the two are committed together by :func:`_commit_session_bookkeeping`. Left in
the request's transaction, the re-check held the write lock across whatever the
route did next -- on an ``async`` GET that is a camera probe of up to ~20 s --
and a revoke decided on a GET was rolled back with the ``401`` it caused, so it
was never stored.

One resolution per request (issue #67, R3)
------------------------------------------
A cookie-authenticated request used to resolve its session up to five times (the
CSRF middleware, the auth dependency, a principal gate, the CSRF token for the
template, ...), each a SELECT plus the touch and re-check bookkeeping, and each
a chance to disagree with the last. :func:`resolve_request_session` does it once
and keeps the answer, as plain values, on ``request.state``; every later caller
reads that. Nothing is shared *between* requests, so there is no cross-request
state to race on. Whatever changes the session inside a request (logout, a
password change, a login) must call :func:`invalidate_request_session`.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import Delete, delete, or_, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker

from ..config import SessionSettings
from ..db.batched_delete import prune_in_batches
from ..db.models import Session as SessionRow
from ..db.models import User

if TYPE_CHECKING:
    from starlette.requests import Request

# Bytes of entropy in a raw session token. token_urlsafe yields ~1.3 chars per
# byte, so 32 bytes is a 43-character, 256-bit token -- ample to make guessing
# and birthday collisions infeasible.
_TOKEN_BYTES = 32

# Bytes of entropy in a per-session CSRF secret (see :mod:`.csrf`).
_CSRF_SECRET_BYTES = 32

# Upper bound on how stale ``last_active`` may get before a lookup writes it
# again. A minute against the 30-minute default idle timeout keeps idle expiry
# accurate to within a minute while turning a burst of requests into reads.
_TOUCH_INTERVAL_SECONDS = 60

NowFn = Callable[[], datetime.datetime]

logger = logging.getLogger(__name__)


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
    raw_token = TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_BYTES)
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
    touch: bool = True,
) -> SessionRow | None:
    """Resolve a raw token to a live :class:`SessionRow`, or ``None``.

    Returns ``None`` for any non-live session: not found, revoked, idle-expired,
    creation-cap-expired, owned by a disabled user, or revoked by the directory
    re-check. On success the row's ``last_active`` is refreshed when it is
    :func:`touch_interval` stale (so activity defers idle expiry) and the row is
    returned -- callers needing the user should prefer :func:`lookup_session`,
    while callers needing the per-session CSRF secret use this.

    ``touch=False`` (a passive background poll, #70 F-S1) skips only the
    ``last_active`` refresh: the idle, creation-cap, disabled-user and directory
    re-check gates all still apply, so a poll on an idle session still gets
    ``None`` and a deprovisioned directory user is still revoked.

    Transaction contract (#37, #66): the bookkeeping this lookup writes -- the
    directory re-check's result and the ``last_active`` touch -- is committed
    here, in one short transaction, when ``db`` held no writes on entry, so the
    caller never carries the write lock out of this call. When ``db`` already
    held writes, the bookkeeping is only flushed and commits (or rolls back)
    with the caller's work. See :func:`_commit_session_bookkeeping`.
    """
    if not raw_token:
        return None
    token_hash = hash_token(raw_token)
    row = db.execute(
        select(SessionRow).where(SessionRow.token_hash == token_hash)
    ).scalar_one_or_none()
    if row is None or row.revoked:
        return None
    # Taken before anything below can write: whether the *caller* had written
    # decides whether the bookkeeping may be committed here (see #66).
    had_writes = _has_other_writes(db)

    current = now()
    # Idle timeout: untouched for longer than the idle window. Applies to
    # persistent sessions too; persistent only lengthens the creation cap below.
    last_touch = row.last_active or row.created_at
    idle_deadline = last_touch + datetime.timedelta(
        seconds=settings.idle_timeout_seconds
    )
    if current > idle_deadline:
        return None
    # Creation-anchored cap (absolute, or persistent for "remember me").
    cap = _absolute_cap_seconds(row.persistent, settings)
    if current > row.created_at + datetime.timedelta(seconds=cap):
        return None
    # Defence in depth: the stored deadline binds too, so a row whose
    # ``created_at`` was ever reset cannot outlive the deadline it was given.
    if row.expires_at is not None and current > row.expires_at:
        return None

    user = db.get(User, row.user_id)
    if user is None or not user.enabled:
        return None

    # A failed flush or commit expires every loaded attribute, so whatever the
    # lock-failure path needs is captured up front, and the re-check's decision
    # is captured before anything is committed.
    row_id, user_id, role_before = row.id, user.id, user.role
    decision_known = False
    kept = True
    role_changed = False
    try:
        # Directory-backed sessions are periodically re-evaluated against the
        # directory so deprovisioning and role changes take effect on a live
        # (incl. "remember me") session. This is a no-op for local sessions and
        # for LDAP sessions whose re-check interval has not elapsed; only the
        # rare due request triggers a directory round-trip. A revoke (account
        # gone / no mapped group) makes the session no longer live.
        kept = _revalidate(db, row, user, settings=settings, now=now)
        role_changed = user.role != role_before
        decision_known = True
        if touch and kept and current - last_touch >= touch_interval(settings):
            row.last_active = current
        # Commit even on a revoke, so the revoke outlives the 401 it causes.
        _commit_session_bookkeeping(db, had_writes=had_writes)
    except OperationalError as exc:
        # The re-check's own flush or the commit could not get the write lock.
        # With the caller's writes in the transaction the lock was already ours,
        # so a lock error is not bookkeeping contention: leave it to the caller.
        if had_writes or not _is_database_locked(exc):
            raise
        # Deny when the lost writes carried a security decision (a revoke or a
        # role change), or when the re-check's own flush failed before its
        # decision could be read.
        deny = not decision_known or not kept or role_changed
        return _recover_from_locked_bookkeeping(
            db, row_id=row_id, user_id=user_id, deny=deny
        )
    return row if kept else None


def touch_interval(settings: SessionSettings) -> datetime.timedelta:
    """Return how stale ``last_active`` may get before a lookup refreshes it.

    :data:`_TOUCH_INTERVAL_SECONDS`, capped at a tenth of the idle timeout so a
    short idle window keeps proportionate accuracy. A session can therefore idle
    out up to this much earlier than its true last request.
    """
    seconds = min(_TOUCH_INTERVAL_SECONDS, max(1, settings.idle_timeout_seconds // 10))
    return datetime.timedelta(seconds=seconds)


def _commit_session_bookkeeping(db: DbSession, *, had_writes: bool) -> None:
    """Publish a lookup's bookkeeping writes while holding the write lock briefly.

    The bookkeeping is whatever :func:`get_session_row` changed on the session
    row and its user: the ``last_active`` touch and the directory re-check's
    ``last_revalidated_at``, role change or revoke. The rule:

    * ``had_writes`` (the caller wrote before the lookup, so the lock is already
      this request's): **flush only**. The bookkeeping commits or rolls back with
      the caller's work, exactly as before #37/#66.
    * otherwise, if the lookup wrote anything (pending ORM changes, or the
      re-check's flush opened a driver transaction): **commit now**, so the lock
      is held for these few ``UPDATE`` statements and not for the rest of the
      request -- in particular not across an ``async`` route's camera await.
      ``expire_on_commit`` is off for these sessions, so the caller's loaded rows
      stay usable without new SQL.
    * otherwise (nothing was due): do nothing.

    A lock error (``OperationalError`` "database is locked") propagates
    unchanged; :func:`get_session_row` handles it in
    :func:`_recover_from_locked_bookkeeping`, which fails closed on any security
    decision the rolled-back writes carried.

    Later changes that add bookkeeping to the lookup must route it through this
    helper rather than committing or flushing on their own.
    """
    if had_writes:
        db.flush()
        return
    if _has_other_writes(db):
        db.commit()


def _recover_from_locked_bookkeeping(
    db: DbSession, *, row_id: int, user_id: int, deny: bool
) -> SessionRow | None:
    """Roll back bookkeeping that could not get the write lock; decide the lookup.

    Called only when the caller held no writes, so the rollback discards nothing
    but this lookup's bookkeeping. ``deny`` is the caller's verdict on what was
    lost, taken before the failed write expired the in-memory rows:

    * ``deny`` (**fail closed**) -> ``None``. The lost writes carried a security
      decision: a revoke (the directory said no, so the session is denied now
      and the revoke is simply decided again on the next request), a role change
      (the old role is still the stored one, and honouring either role would act
      on a decision that was never recorded), or a re-check whose own flush
      failed before its outcome could be read.
    * otherwise (only a missed touch or re-check timestamp) -> the re-loaded row:
      a missed touch only moves idle expiry by one interval, and an un-advanced
      re-check timestamp makes the next request re-check promptly.

    The row and user are re-loaded with ``db.get`` so nothing expired by the
    rollback is handed to a caller that may read it on the event loop.
    """
    db.rollback()
    logger.warning("session bookkeeping skipped: database is locked")
    if deny:
        return None
    row = db.get(SessionRow, row_id)
    user = db.get(User, user_id)
    if row is None or row.revoked:
        return None
    if user is None or not user.enabled:
        return None
    return row


def _is_database_locked(exc: OperationalError) -> bool:
    """Return whether ``exc`` is SQLite write contention (busy timeout elapsed)."""
    return "database is locked" in str(exc).lower()


def _has_other_writes(db: DbSession) -> bool:
    """Return whether ``db`` holds writes a commit here would publish early.

    True if the session has pending ORM changes, or its connection already has
    an open DBAPI transaction (SQLite's driver opens one only before a write,
    so an open one means this request already wrote). A driver that does not
    report its transaction state is assumed to have writes, which keeps the
    pre-#37 behaviour (flush, commit at the end of the request).
    """
    if db.new or db.dirty or db.deleted:
        return True
    driver_connection = db.connection().connection.driver_connection
    return bool(getattr(driver_connection, "in_transaction", True))


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
    touch: bool = True,
) -> User | None:
    """Resolve a raw session token to its live :class:`User`, or ``None``.

    Thin wrapper over :func:`get_session_row` for the common case of needing the
    authenticated user. Returns ``None`` on any of: token not found, revoked,
    idle-expired, creation-cap-expired, or a disabled user. ``touch`` is passed
    through unchanged (``False`` for a passive poll).
    """
    row = get_session_row(db, raw_token, settings=settings, now=now, touch=touch)
    if row is None:
        return None
    return db.get(User, row.user_id)


@dataclass(frozen=True)
class ResolvedSession:
    """The live session a request carries, as plain values.

    Plain values (no ORM objects) so the result can be cached on the request and
    read from the event loop or any worker thread without touching a database
    session. A caller that needs the :class:`User` loads it by ``user_id`` in its
    own session, so anything it then reads is the live row.
    """

    session_id: int
    user_id: int
    role: str
    csrf_secret: str
    auth_source: str
    persistent: bool


# ``request.state`` attribute holding this request's resolution. A distinct
# marker (not ``None``) records "resolved: no live session", so a dead cookie is
# not looked up again by every later caller.
_STATE_ATTR = "tlm_session"
_NO_SESSION = object()
_UNRESOLVED = object()


#: Prefix of every session token this version issues. A session minted by a
#: release before ``__Host-`` carried none; ``.`` is outside the
#: ``token_urlsafe`` alphabet, so the marker cannot collide with an old token.
TOKEN_PREFIX = "s2."


def resolve_request_session(
    request: Request,
    db: DbSession,
    *,
    settings: SessionSettings,
    now: NowFn = _utcnow,
) -> ResolvedSession | None:
    """Resolve the request's session cookie once; later calls reuse the answer.

    The first caller runs :func:`get_session_row` (the idle/absolute/disabled
    gates, the directory re-check and the touch) in its own ``db``; the result,
    including "no live session", is cached on ``request.state`` for the rest of
    the request, whichever layer asks next. ``request.state`` is backed by the
    ASGI scope, so the CSRF middleware's request and the endpoint's share it.

    The touch follows the request's passive-poll marking
    (:func:`.authz.session_touch_allowed`), computed from the same headers for
    every layer, so a passive poll still does not keep a session alive.

    A lookup that raises is not cached: the exception propagates and a later
    caller tries again (the CSRF middleware turns it into a denial).
    """
    cached = getattr(request.state, _STATE_ATTR, _UNRESOLVED)
    if cached is _NO_SESSION:
        return None
    if isinstance(cached, ResolvedSession):
        return cached

    from .authz import session_cookie_value, session_touch_allowed

    resolved: ResolvedSession | None = None
    raw_token = session_cookie_value(request, settings)
    if raw_token:
        row = get_session_row(
            db,
            raw_token,
            settings=settings,
            now=now,
            touch=session_touch_allowed(request, settings),
        )
        user = db.get(User, row.user_id) if row is not None else None
        if row is not None and user is not None:
            resolved = ResolvedSession(
                session_id=row.id,
                user_id=user.id,
                role=user.role,
                csrf_secret=row.csrf_secret or "",
                auth_source=user.auth_source,
                persistent=bool(row.persistent),
            )
    setattr(request.state, _STATE_ATTR, resolved if resolved else _NO_SESSION)
    return resolved


def live_principal_from_cache(db: DbSession, resolved: ResolvedSession) -> User | None:
    """Re-check a cached resolution against the database; return the live user.

    The CSRF middleware resolves the session *before* it reads a (possibly slow)
    body, so by the time the endpoint's gate runs, the session may have been
    revoked, its user disabled or demoted in the meantime. This keeps the cached
    CSRF secret but re-reads the facts that decide access, with primary-key reads
    only: no touch, no directory re-check, no write. ``None`` means deny.

    * the session row still exists, is not revoked and still belongs to the user;
    * the user still exists and is enabled;
    * the caller takes the role from the returned live :class:`User`, never from
      the cached ``resolved.role``.

    ``populate_existing`` makes the reads refresh any instance this session had
    already loaded. Synchronous, for the sync dependency / threadpool context.
    """
    row = db.execute(
        select(SessionRow)
        .where(SessionRow.id == resolved.session_id)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if row is None or row.revoked or row.user_id != resolved.user_id:
        return None
    user = db.execute(
        select(User)
        .where(User.id == resolved.user_id)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if user is None or not user.enabled:
        return None
    return user


def reauthorize_after_body(
    request: Request,
    db: DbSession,
    *,
    settings: SessionSettings,
    allowed_roles: tuple[str, ...],
) -> User | None:
    """Re-check a cookie caller's access once a long body has been read.

    A route that streams its body (the frame import) authorises *before* the
    body arrives, and a slow upload gives a revoke, a disable or a demotion time
    to land in between (#207). Calling this after the body and before acting
    repeats the live primary-key check of :func:`live_principal_from_cache` and
    the role test against the **live** role.

    Returns the live :class:`User`, or ``None`` for a request with no session
    cookie (the bearer-token caller, which has no session to re-check). A
    cookie that no longer resolves to a live session is ``401``, a live user
    whose role is no longer allowed ``403``.

    :raises HTTPException: ``401`` / ``403`` as above.
    """
    from fastapi import HTTPException, status

    from .authz import session_cookie_value

    # ``is None``, like the principal dependency: an empty cookie is a cookie.
    if session_cookie_value(request, settings) is None:
        return None
    resolved = resolve_request_session(request, db, settings=settings)
    user = live_principal_from_cache(db, resolved) if resolved is not None else None
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )
    if user.role not in allowed_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient privileges."
        )
    return user


def invalidate_request_session(request: Request) -> None:
    """Forget this request's cached resolution (the session just changed).

    Call after revoking, rotating or replacing the session inside a request, so
    nothing later in the same request acts on the old answer.
    """
    # ``State`` has no pop; a missing attribute is the normal case.
    if hasattr(request.state, _STATE_ATTR):
        delattr(request.state, _STATE_ATTR)


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


#: How long a revoked or expired session row is kept before it is purged.
STALE_SESSION_RETENTION = datetime.timedelta(days=7)


def purge_stale_sessions(
    session_factory: sessionmaker[DbSession],
    *,
    now: datetime.datetime | None = None,
    older_than: datetime.timedelta = STALE_SESSION_RETENTION,
    batch_size: int = 5000,
    max_batches: int = 20,
) -> int:
    """Delete session rows that were revoked or expired more than ``older_than`` ago.

    Without this the ``session`` table only grows: logout and expiry mark a row,
    nothing removes it (#67 R12). A row goes when its ``revoked_at`` or its
    ``expires_at`` is older than the cutoff; a live session, or one that expired or
    was revoked more recently, is untouched. Deletes run in batches, each its own
    short write transaction (``prune_in_batches``),
    so the write lock is never held for long. There is no index on either column;
    the table is small, so each batch scans it.

    Synchronous: call it off the event loop. Returns the number of rows deleted.
    """
    cutoff = (now if now is not None else _utcnow()) - older_than

    def build(limit: int) -> Delete:
        stale = (
            select(SessionRow.id)
            .where(or_(SessionRow.revoked_at < cutoff, SessionRow.expires_at < cutoff))
            .order_by(SessionRow.id)
            .limit(limit)
        )
        return (
            delete(SessionRow)
            .where(SessionRow.id.in_(stale))
            .execution_options(synchronize_session=False)
        )

    deleted = prune_in_batches(session_factory, build, batch_size, max_batches)
    if deleted:
        logger.info("purged %d stale session row(s)", deleted)
    return deleted
