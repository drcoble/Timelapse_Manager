"""Periodic re-evaluation of directory-backed (LDAP) sessions.

A local session is trusted for its whole lifetime: nothing about a local account
changes outside this application. A directory-backed session is different -- the
account it represents can be disabled, deleted, or moved between groups in the
directory at any time, and a long-lived "remember me" session would otherwise
keep honouring stale privileges until its absolute cap.

This module closes that gap. On a request whose session is directory-backed and
whose last re-check is older than the configured interval, it re-reads the user's
current directory state (without a password -- see
:func:`timelapse_manager.security.ldap_directory.resolve_directory_state`) and
reconciles the stored account with it:

* **Account gone or maps to no role** -> revoke the session (force re-login).
* **Role changed** -> update the stored role, keep the session.
* **Unchanged** -> keep the session.

Fail-safe on outage
-------------------
If the directory is unreachable or misconfigured at re-check time, the session is
**kept and the timestamp is not advanced**: a transient outage must never lock
out an otherwise-valid session, and not advancing the timestamp means the next
request retries promptly once the directory recovers. Only a *definitive*
negative answer from the directory (the account is gone, it is locked, or it now
maps to no mapped group) revokes a session, and so does an administrator
switching directory authentication off (#67 S10); saving the settings with LDAP off
revokes every directory session at once (:func:`revoke_directory_sessions`, #216).

Circuit breaker (issue #67, R3)
-------------------------------
Without a breaker, every due re-check (and every directory login) during an outage
waits out the connect timeout of every configured server, holding a worker thread
each. :class:`DirectoryBreaker` remembers that the directory proved unreachable
and, for ``auth.ldap_breaker_seconds``, answers "do not ask" instead:

* a re-check is skipped, the session is **kept** and ``last_revalidated_at`` is
  not advanced, so the check runs again as soon as the breaker closes;
* a directory login fails at once as "directory unavailable".

It opens **only** when no server could be reached at all
(:data:`~.ldap_directory.DETAIL_NO_SERVER_REACHABLE`). That includes a server
that accepts the connection and then goes silent before the service-account bind
completes (#202): nothing a client chose has been sent by then, so no request can
provoke it. It also includes a server that binds and then hangs on the search
(#216), but only when a *canary* search built purely from administrator
configuration stalls too: the user search carries a client-chosen name, so a slow
search alone proves nothing, while a hang that also swallows a client-independent
query is the same for every request and cannot be forced by one. A stall that the
canary does not reproduce, a protocol error, a rejected bind or a configuration
fault never opens it: those can be provoked by a request, and an open breaker
defers the revocation of deprovisioned accounts, so a request must not be able to
hold it open. Failover to another pool member is deliberately not attempted after
a stall: ldap3 fails over at connect time only, and retrying each member would
multiply the time a worker thread is held by the number of servers, which is what
the breaker exists to prevent. After the window the first caller
becomes the sole probe; everyone else stays on the open answer until the probe's
outcome is known, so the end of an outage is not a stampede of connect attempts.

An administrator saving the directory settings, or a successful connection test,
:meth:`~DirectoryBreaker.reset` it (#203): what it remembered was about the old
configuration.

The state is per database engine (one per process in production), not a bare
module global, so tests and embedded apps never share it.

The interval gate, the directory call, the breaker and the clock are all
injectable so the behaviour is unit-testable without a live directory or real
elapsed time.
"""

from __future__ import annotations

import datetime
import logging
import threading
import time
import weakref
from collections.abc import Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from ..config import SessionSettings
from ..db.models import Session as SessionRow
from ..db.models import User
from ..runtime import get_context
from .ldap_directory import (
    DETAIL_NO_SERVER_REACHABLE,
    LdapDirectoryState,
    LdapOutcome,
    map_groups_to_role,
    resolve_directory_state,
)

logger = logging.getLogger(__name__)

_LDAP_AUTH_SOURCE = "ldap"

NowFn = Callable[[], datetime.datetime]

# A function that, given the username, returns the directory's current view of the
# account. Injected so tests can supply a deterministic resolver and the real
# wiring can bind in the settings load + bind-password decrypt. Returning
# ``None`` means "no directory configured / cannot resolve settings" and is
# treated as a fail-safe keep, exactly like an outage.
DirectoryResolver = Callable[[str], LdapDirectoryState | None]


# ``auth.ldap_breaker_seconds`` default, used when no application context is
# installed (a bare unit test).
_DEFAULT_BREAKER_SECONDS = 60


def _utcnow() -> datetime.datetime:
    """Return the current UTC time as a naive datetime (matches stored columns)."""
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


class DirectoryBreaker:
    """A thread-safe circuit breaker around "ask the directory".

    Thread-safe because every caller is a worker thread. The whole state is one
    deadline, read and written under a lock, so concurrent requests cannot see a
    torn state:

    * ``None`` -- closed: ask the directory.
    * a future deadline -- open: do not ask.
    * a past deadline -- the window is over: exactly one caller wins the probe
      (the deadline is pushed out again under the lock, so no second caller can
      also win it) and asks; the others still see "open".

    A probe that never reports back (its thread died on an unrelated error) just
    leaves the breaker open until the re-armed deadline, then a new probe is
    allowed. The deadline uses a monotonic clock, so a wall-clock step cannot
    close or pin it.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._open_until: float | None = None

    def allow(self, window_seconds: float) -> bool:
        """Return whether the caller may ask the directory (and so must report).

        ``window_seconds`` is how long the breaker stays open if this caller wins
        the post-window probe and then fails to report.
        """
        with self._lock:
            if self._open_until is None:
                return True
            now = self._clock()
            if now < self._open_until:
                return False
            self._open_until = now + window_seconds
            return True

    def record_unreachable(self, window_seconds: float) -> None:
        """Open (or re-open) the breaker for ``window_seconds``."""
        with self._lock:
            self._open_until = self._clock() + window_seconds

    def record_answered(self) -> None:
        """Close the breaker: the directory answered, or no connection was needed."""
        with self._lock:
            self._open_until = None

    def reset(self) -> None:
        """Close the breaker on an administrator's say-so (#203).

        Used when the directory settings are saved or a connection test succeeds:
        what the breaker remembered was about the *old* configuration, so the next
        login or re-check must ask the directory afresh rather than wait out the
        window. Equivalent to :meth:`record_answered`; a separate name so the call
        sites say why.
        """
        self.record_answered()

    def is_open(self) -> bool:
        """Return whether the breaker is currently refusing callers (diagnostics)."""
        with self._lock:
            return self._open_until is not None and self._clock() < self._open_until


_breakers: weakref.WeakKeyDictionary[object, DirectoryBreaker] = (
    weakref.WeakKeyDictionary()
)
_breakers_lock = threading.Lock()


def breaker_for(db: DbSession) -> DirectoryBreaker:
    """Return the breaker shared by every request on ``db``'s database engine."""
    key: object = db.get_bind()
    with _breakers_lock:
        breaker = _breakers.get(key)
        if breaker is None:
            breaker = _breakers[key] = DirectoryBreaker()
        return breaker


def breaker_window_seconds() -> int:
    """Return ``auth.ldap_breaker_seconds`` (the default without a context)."""
    try:
        return get_context().settings.auth.ldap_breaker_seconds
    except RuntimeError:
        return _DEFAULT_BREAKER_SECONDS


def directory_unreachable(outcome: LdapOutcome, detail: str) -> bool:
    """Return whether a directory result proves *no server could be reached*.

    The only result that opens the breaker. See the module docstring for why a
    protocol error or a configuration fault is deliberately not one.
    """
    return outcome is LdapOutcome.SERVER_UNREACHABLE and (
        detail == DETAIL_NO_SERVER_REACHABLE
    )


def record_directory_result(
    breaker: DirectoryBreaker, outcome: LdapOutcome, detail: str
) -> None:
    """Report a directory call's result to ``breaker`` (open or close it)."""
    if directory_unreachable(outcome, detail):
        breaker.record_unreachable(breaker_window_seconds())
    else:
        breaker.record_answered()


def _default_directory_resolver(db: DbSession) -> DirectoryResolver:
    """Build the production resolver: load settings, decrypt bind pw, re-search.

    Bound lazily against the request's DB session. Returns a resolver that, for a
    username, loads the current LDAP settings view and the decrypted service-bind
    password and asks the connector for the account's present state. If LDAP is
    not enabled the connector itself returns the ``DISABLED`` outcome, which the
    caller treats as a fail-safe keep.
    """
    # Imported here (not at module load) so the session layer's import graph does
    # not pull in the settings-service/crypto chain unless re-eval actually runs.
    from .ldap_settings_service import load_settings, resolve_bind_password

    def resolve(username: str) -> LdapDirectoryState | None:
        view = load_settings(db)
        bind_password = resolve_bind_password(db)
        return resolve_directory_state(
            settings=view,
            username=username,
            bind_password=bind_password,
        )

    return resolve


def revalidate_ldap_session(
    db: DbSession,
    row: SessionRow,
    user: User,
    *,
    settings: SessionSettings,
    resolver: DirectoryResolver | None = None,
    now: NowFn = _utcnow,
    breaker: DirectoryBreaker | None = None,
) -> bool:
    """Re-evaluate a directory-backed session against the directory if due.

    Returns ``True`` if the session remains valid (kept, possibly with an updated
    role) and ``False`` if it was revoked and must no longer be honoured.

    A no-op that returns ``True`` for:

    * a local session (``auth_source != "ldap"``) -- never re-evaluated;
    * a directory session whose last re-check is within the configured interval.

    When a re-check is due, the injected ``resolver`` is asked for the account's
    current directory state and the outcome is applied:

    * account gone (``NO_SUCH_USER``), locked in the directory (``LOCKED``, #67
      S10), directory authentication switched off in the settings (``DISABLED``)
      or present-but-no-mapped-group -> revoke, return ``False``;
    * role changed -> update ``user.role``, advance the timestamp, return ``True``;
    * unchanged -> advance the timestamp, return ``True``;
    * directory unreachable / misconfigured, or no resolver available
      -> **fail safe**: keep the session, do **not** advance the timestamp,
      return ``True``.

    While the directory circuit breaker is open the resolver is not called at
    all and the session is kept exactly as for an unreachable directory (the
    timestamp is not advanced, so the check is due again once it closes). An
    outage therefore never *advances* a session's re-check or extends it past
    its own idle and absolute limits; it only defers a revocation until the
    directory answers. ``breaker`` defaults to the engine's shared one.
    """
    if user.auth_source != _LDAP_AUTH_SOURCE:
        return True

    current = now()
    if not _revalidation_due(row, settings, current):
        return True

    if breaker is None:
        breaker = breaker_for(db)
    if not breaker.allow(breaker_window_seconds()):
        # The directory was just found unreachable: do not queue behind another
        # connect timeout. Keep the session, leave the timestamp, ask again later.
        logger.debug("LDAP directory breaker open; skipping session re-evaluation")
        return True

    if resolver is None:
        resolver = _default_directory_resolver(db)
    state = resolver(user.username)

    # No resolver answer at all (settings unresolvable) -> fail safe, keep. No
    # connection was attempted, so it says nothing about reachability.
    if state is None:
        breaker.record_answered()
        return True
    record_directory_result(breaker, state.outcome, state.detail)

    if state.outcome is LdapOutcome.NO_SUCH_USER:
        # Definitive: the account has been deprovisioned. Revoke now so a
        # "remember me" cookie cannot outlive removal.
        _revoke(row, current)
        logger.info("Revoked LDAP session: account no longer in directory")
        return False

    if state.outcome is LdapOutcome.LOCKED:
        # Definitive: the directory disabled the account. Same as removal.
        _revoke(row, current)
        logger.info("Revoked LDAP session: account is locked in the directory")
        return False

    if state.outcome is LdapOutcome.DISABLED:
        # An administrator switched directory authentication off: a directory
        # session must not outlive it (a deliberate decision, not an outage).
        _revoke(row, current)
        logger.info("Revoked LDAP session: directory authentication is disabled")
        return False

    if state.outcome is not LdapOutcome.AUTHENTICATED:
        # SERVER_UNREACHABLE / CONFIG_ERROR: cannot decide. Fail safe --
        # keep the session and leave last_revalidated_at unchanged so the next
        # request retries promptly once the directory recovers.
        logger.warning(
            "LDAP re-evaluation could not reach the directory; keeping session"
        )
        return True

    # The account is present: recompute the role from current group membership.
    # The role group DNs live in the settings row, not in the directory state, so
    # they are read directly here.
    from .ldap_settings_service import load_settings

    view = load_settings(db)
    new_role = map_groups_to_role(
        state.groups,
        admin_group_dn=view.admin_group_dn or None,
        operator_group_dn=view.operator_group_dn or None,
        viewer_group_dn=view.viewer_group_dn or None,
    )
    if new_role is None:
        # Present but in no mapped group: access is no longer granted. Definitive
        # negative -> revoke.
        _revoke(row, current)
        logger.info("Revoked LDAP session: user maps to no role in directory")
        return False

    if new_role != user.role:
        user.role = new_role
        logger.info("Updated LDAP session role from directory group membership")

    row.last_revalidated_at = current
    db.flush()
    return True


def revoke_directory_sessions(db: DbSession, *, now: NowFn = _utcnow) -> int:
    """Revoke every live directory-backed session; return how many were revoked.

    Called when an administrator saves the directory settings switched off, so a
    "remember me" session cannot outlive the decision until its next due re-check
    (which only revokes on a ``DISABLED`` answer, and never while the breaker is
    open). One conditional bulk ``UPDATE`` in the caller's transaction: the revoke
    commits with the settings change or not at all, and an in-flight request's
    live re-check (#207) sees the flag. Local sessions are untouched.
    """
    ldap_user_ids = select(User.id).where(User.auth_source == _LDAP_AUTH_SOURCE)
    result = db.execute(
        update(SessionRow)
        .where(SessionRow.user_id.in_(ldap_user_ids), SessionRow.revoked.is_(False))
        .values(revoked=True, revoked_at=now())
    )
    count = int(getattr(result, "rowcount", 0) or 0)
    if count:
        logger.info(
            "Revoked %d LDAP session(s): directory authentication disabled", count
        )
    return count


def _revalidation_due(
    row: SessionRow, settings: SessionSettings, current: datetime.datetime
) -> bool:
    """Return whether this session's directory re-check interval has elapsed.

    A session whose ``last_revalidated_at`` is unset is due immediately -- but the
    login path seeds it at session creation, so in practice a freshly minted LDAP
    session is not re-checked until a full interval has passed.
    """
    last = row.last_revalidated_at
    if last is None:
        return True
    interval = datetime.timedelta(seconds=settings.ldap_revalidation_interval_seconds)
    return current - last >= interval


def _revoke(row: SessionRow, when: datetime.datetime) -> None:
    """Mark a session row revoked (by flag, preserving the row for audit)."""
    row.revoked = True
    row.revoked_at = when


__all__ = [
    "DirectoryBreaker",
    "DirectoryResolver",
    "breaker_for",
    "breaker_window_seconds",
    "directory_unreachable",
    "record_directory_result",
    "revalidate_ldap_session",
    "revoke_directory_sessions",
]
