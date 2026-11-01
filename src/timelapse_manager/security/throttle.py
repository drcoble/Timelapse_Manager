"""Brute-force login throttling.

Repeated failed logins are slowed to make online password guessing impractical
without turning login into an account-enumeration or denial-of-service oracle.

Design constraints:

* **Per-IP is the primary limit.** A single source that exceeds the failure
  budget within the sliding window is throttled regardless of which usernames
  it targets, which is what actually bounds a guessing attack.
* **No per-username lockout.** The refusal decision never depends on a
  username's own failure tally. A per-username lockout would let anyone who can
  send login requests deny a victim access, and -- if it triggered only for real
  accounts -- confirm which usernames exist. The submitted username is recorded
  (whether or not the account exists, so recording it leaks nothing) but is used
  only to scope the per-account success reset below, never to refuse a login.
* **Success forgives one account, not a whole IP.** A genuine login clears only
  the failures for that exact ``(ip, username)`` pair, so a legitimate user is
  not penalised for an earlier typo -- while an attacker who holds one valid
  credential cannot reset an in-progress spray against other usernames by simply
  logging into the account they already own.

The throttle records only login attempts -- never the attempted password. State
lives in the ``login_attempt`` table rather than in memory, so the decision
survives a process restart and cannot be reset by clearing in-process state; the
login path is not hot, so a synchronous write per attempt is cheap.

Reserve, then release (#67 S7)
------------------------------
:meth:`BruteForceThrottle.try_reserve` checks the budget and records the attempt
as a failure **before** the password is checked, in one short transaction under
a process-wide lock. Parallel attempts from one source are therefore counted
before any of them authenticates, so a burst cannot slip past the check. The
caller then either keeps the row (a failed attempt, including a directory
outage), or releases it on success (:meth:`~BruteForceThrottle.release` with
``succeeded=True``, which also writes the success row). A plain release, which
forgives nothing else, is available for an attempt that must not count.

Per-username delay, not lockout
-------------------------------
A username's own failures never refuse a login. After
``auth.local_backoff_after_failures`` live failures for one username, from any
source, the next attempt is only *delayed* (1 s, doubling, at most 30 s) before
the password is checked. A correct password still succeeds after the delay, so
no third party can lock the local break-glass administrator out. The delay
depends only on the submitted name's failure history, never on whether an
account holds the name or where it comes from (local or directory): anything
else would let response timing tell local, directory and unknown names apart.
Above ``auth.throttle_global_max_failures`` failures in the window, from all
sources together, every username is delayed by at least the first step.

Keys
----
* The source key is :func:`throttle_ip_key`: IPv4 as is, IPv6 grouped by its
  /64 (one host usually holds a whole /64), IPv4-mapped IPv6 folded to IPv4.
* The password-change key is :func:`password_change_key` (``account:<id>``),
  which can never equal a source key. Those rows count only toward that key:
  they add nothing to the per-username delay or to the global ceiling.
* The username is always :func:`~timelapse_manager.usernames.username_key`, the
  same key the account lookup uses (#4), so ``Admin``, `` admin `` and its
  full-width form share one counter.
"""

from __future__ import annotations

import datetime
import ipaddress
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, aliased, sessionmaker

from ..config import AuthSettings
from ..db.engine import run_with_write_retry
from ..db.models import LoginAttempt
from ..db.session import session_scope
from ..runtime import get_context
from ..usernames import username_key

NowFn = Callable[[], datetime.datetime]


# The longest a per-username delay grows to, in seconds.
LOCAL_BACKOFF_MAX_SECONDS = 30.0

# IPv6 sources are keyed by this prefix: a single host usually controls a whole
# /64, so per-address keys would hand it 2**64 separate budgets.
IPV6_KEY_PREFIX = 64

# Prefix of the password-change key. No source key can start with it: a source
# key is an IP address, an IPv6 network, or the server's own peer placeholder.
_PASSWORD_CHANGE_PREFIX = "account:"

# Serialises check-and-insert in :meth:`BruteForceThrottle.try_reserve` within
# the process, so concurrent attempts cannot all pass the check before any of
# them is counted. Only ``try_reserve`` takes it; it is never held while the
# caller's request transaction is open.
_reserve_lock = threading.Lock()


def _utcnow() -> datetime.datetime:
    """Return the current time as a naive UTC datetime (the column convention)."""
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


def throttle_ip_key(value: str) -> str:
    """Return the throttle key for the client address ``value``.

    An IPv4 address is returned in canonical form; an IPv4-mapped IPv6 address
    is folded to its IPv4 form; any other IPv6 address becomes its /64 network
    (``2001:db8:1:2::/64``). A value that is not an IP literal (a test client's
    placeholder name, ``unknown``) is returned unchanged.
    """
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return value
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        network = ipaddress.IPv6Network(f"{address}/{IPV6_KEY_PREFIX}", strict=False)
        return str(network)
    return str(address)


def password_change_key(user_id: int) -> str:
    """Return the throttle key for user ``user_id``'s password-change attempts."""
    return f"{_PASSWORD_CHANGE_PREFIX}{user_id}"


def backoff_seconds(prior_failures: int, *, after: int) -> float:
    """Return the delay before an attempt, given the username's prior failures.

    ``prior_failures`` counts the live failures for the username recorded before
    this attempt. Below ``after`` there is no delay; from there it is
    ``2 ** (prior_failures - after)`` seconds, capped at
    :data:`LOCAL_BACKOFF_MAX_SECONDS`.
    """
    if prior_failures < after:
        return 0.0
    exponent = min(prior_failures - after, 16)
    return min(float(2**exponent), LOCAL_BACKOFF_MAX_SECONDS)


@dataclass(frozen=True)
class LoginReservation:
    """One attempt counted in advance by :meth:`BruteForceThrottle.try_reserve`.

    :param attempt_id: the ``login_attempt`` row recorded for the attempt.
    :param ip: the source key the attempt was counted against.
    :param username: the username key the attempt was counted against.
    :param delay_seconds: how long the caller waits before checking the password.
    """

    attempt_id: int
    ip: str
    username: str
    delay_seconds: float


class BruteForceThrottle:
    """Sliding-window failed-login counter, keyed per source IP.

    Counts are persisted in the ``login_attempt`` table and pruned to the
    configured window on the write path. Construct one per process and share it
    across login requests; because the state is in the database, two instances
    (e.g. across a restart) observe the same history.

    The session factory defaults to the process-wide one obtained from the
    application context -- the same source other non-request database users read
    -- and is resolved lazily per call so the throttle can be constructed before
    startup wiring completes. It may be injected for testing.
    """

    def __init__(
        self,
        settings: AuthSettings,
        *,
        session_factory: sessionmaker[Session] | None = None,
        now: NowFn = _utcnow,
    ) -> None:
        self._max_failures = settings.throttle_max_failures
        self._window = datetime.timedelta(seconds=settings.throttle_window_seconds)
        self._global_max_failures = settings.throttle_global_max_failures
        self._backoff_after = settings.local_backoff_after_failures
        self._session_factory = session_factory
        self._now = now

    def _factory(self) -> sessionmaker[Session]:
        """Return the injected session factory, or the process-wide default."""
        return self._session_factory or get_context().session_factory

    @contextmanager
    def _session(self, db: Session | None) -> Iterator[Session]:
        """Yield a session to work in.

        When the caller supplies its request-scoped ``db`` the throttle's read
        and write ride that same session -- and therefore the same SQLite
        connection and transaction -- so an attempt recorded mid-login cannot
        deadlock against the request's own uncommitted write (SQLite permits a
        single writer). The caller owns the commit in that case. With no session
        supplied (background/unit use) the throttle opens and commits its own
        short scope.
        """
        if db is not None:
            yield db
        else:
            with session_scope(self._factory()) as owned:
                yield owned

    def is_throttled(
        self, *, ip: str, username: str, db: Session | None = None
    ) -> bool:
        """Return True if login from ``ip`` should be refused.

        The decision is **per source IP**: an IP whose live failure count reaches
        the ceiling is slowed regardless of which usernames it targeted, which is
        what actually bounds a guessing/spray attack. There is deliberately **no
        per-username refusal** -- a failure tally for a username, which any
        unauthenticated party can run up from anywhere, must never lock a victim
        out of their account nor confirm that the account exists. ``username`` is
        accepted for interface symmetry (and used only to scope the per-account
        success reset, below); the refusal itself never depends on it.
        """
        cutoff = self._now() - self._window
        with self._session(db) as session:
            ip_hits = self._recent_ip_failures(session, ip, cutoff)
        return ip_hits >= self._max_failures

    def try_reserve(
        self, *, ip: str, username: str, backoff: bool = True
    ) -> LoginReservation | None:
        """Count one attempt in advance, or return ``None`` if ``ip`` is throttled.

        Under :data:`_reserve_lock`, in one short write transaction of its own
        (committed before returning, so the next reservation sees it), this:

        * refuses, recording nothing, when ``ip`` already holds the maximum live
          failures;
        * otherwise inserts a failure row for ``(ip, username)`` and returns it.

        ``username`` is the name as submitted; it is reduced to its
        :func:`~timelapse_manager.usernames.username_key` here. With ``backoff``
        the returned reservation carries the per-username delay (see the module
        docstring); without it the delay is zero.

        Call it before the request's own session writes anything: it writes on a
        separate connection, and SQLite allows one writer.
        """
        key = username_key(username)

        def _reserve() -> LoginReservation | None:
            now = self._now()
            cutoff = now - self._window
            with session_scope(self._factory()) as session:
                self._prune(session, cutoff)
                if self._recent_ip_failures(session, ip, cutoff) >= self._max_failures:
                    return None
                delay = 0.0
                if backoff:
                    delay = self._backoff_delay(session, key, cutoff)
                row = LoginAttempt(
                    ip=ip, username=key, attempted_at=now, succeeded=False
                )
                session.add(row)
                session.flush()
                return LoginReservation(
                    attempt_id=int(row.id),
                    ip=ip,
                    username=key,
                    delay_seconds=delay,
                )

        with _reserve_lock:
            return run_with_write_retry(_reserve)

    def release(
        self,
        reservation: LoginReservation,
        *,
        succeeded: bool = False,
        db: Session | None = None,
    ) -> None:
        """Withdraw a reservation, so the attempt no longer counts as a failure.

        With ``succeeded`` the attempt authenticated, and a success row is
        written as :meth:`record_success` does, forgiving that pair's earlier
        failures. Without it only the reservation row is removed and nothing
        else is forgiven.

        Pass the request's ``db`` once the request has written anything: the
        release then rides that transaction rather than waiting on it. This never
        takes the reservation lock.
        """
        with self._session(db) as session:
            session.execute(
                delete(LoginAttempt).where(LoginAttempt.id == reservation.attempt_id)
            )
        if succeeded:
            self.record_success(ip=reservation.ip, username=reservation.username, db=db)

    def _backoff_delay(
        self, db: Session, username: str, cutoff: datetime.datetime
    ) -> float:
        """Return the delay for the next attempt on ``username`` (see module doc).

        It depends only on the name's recorded history and the global count,
        never on the account table, so it reveals nothing about the name.
        """
        prior = self._recent_username_failures(db, username, cutoff)
        if self._recent_login_failures(db, cutoff) >= self._global_max_failures:
            prior = max(prior, self._backoff_after)
        return backoff_seconds(prior, after=self._backoff_after)

    def record_failure(
        self, *, ip: str, username: str, db: Session | None = None
    ) -> None:
        """Record one failed login attempt against ``ip`` and ``username``.

        The username is counted as submitted -- never resolved against the
        account table here -- so counting itself reveals nothing about whether
        the account exists.
        """
        now = self._now()
        with self._session(db) as session:
            session.add(
                LoginAttempt(
                    ip=ip, username=username, attempted_at=now, succeeded=False
                )
            )
            self._prune(session, now - self._window)

    def record_success(
        self, *, ip: str, username: str, db: Session | None = None
    ) -> None:
        """Record a successful login, clearing the failure budget.

        A genuine login proves the source/account pair is not an attacker, so an
        earlier mistyped password does not keep counting against them: the
        success row marks the point past which earlier failures for this IP and
        username no longer count.
        """
        now = self._now()
        with self._session(db) as session:
            session.add(
                LoginAttempt(ip=ip, username=username, attempted_at=now, succeeded=True)
            )
            self._prune(session, now - self._window)

    def _recent_ip_failures(
        self, db: Session, ip: str, cutoff: datetime.datetime
    ) -> int:
        """Count ``ip``'s live failures, forgiving each account on its own success.

        A failure counts when it is within the window and is **not** superseded by
        a later success for the *same* ``(ip, username)`` pair. Scoping the reset
        to the pair -- rather than to the IP as a whole -- means a genuine login to
        one account forgives that account's earlier typos without wiping the
        budget the IP has accrued against *other* accounts. An attacker who holds
        one valid credential therefore cannot reset an in-progress spray by
        logging into the account they already own; each sprayed username keeps its
        own failures until it, specifically, succeeds.
        """
        # Alias the failure row so the correlated "was this account forgiven by a
        # later success?" subquery can reference each candidate failure's username
        # and timestamp.
        _failure = aliased(LoginAttempt)
        later_success = (
            select(LoginAttempt.id)
            .where(
                LoginAttempt.ip == ip,
                LoginAttempt.username == _failure.username,
                LoginAttempt.succeeded.is_(True),
                LoginAttempt.attempted_at > _failure.attempted_at,
            )
            .correlate(_failure)
            .exists()
        )
        count = db.execute(
            select(func.count())
            .select_from(_failure)
            .where(
                _failure.ip == ip,
                _failure.succeeded.is_(False),
                _failure.attempted_at >= cutoff,
                ~later_success,
            )
        ).scalar_one()
        return int(count)

    def _recent_username_failures(
        self, db: Session, username: str, cutoff: datetime.datetime
    ) -> int:
        """Count ``username``'s live login failures from every source.

        A failure stops counting once the same username later succeeds **from
        the same source** (the pair rule :meth:`_recent_ip_failures` uses). A
        success elsewhere forgives nothing here: otherwise a third party probing
        the name would see its delay drop to zero when the owner signs in, which
        would show that the name is a real, active account. Password-change
        rows are excluded.
        """
        _failure = aliased(LoginAttempt)
        later_success = (
            select(LoginAttempt.id)
            .where(
                LoginAttempt.ip == _failure.ip,
                LoginAttempt.username == _failure.username,
                LoginAttempt.succeeded.is_(True),
                LoginAttempt.attempted_at > _failure.attempted_at,
                ~LoginAttempt.ip.startswith(_PASSWORD_CHANGE_PREFIX, autoescape=True),
            )
            .correlate(_failure)
            .exists()
        )
        count = db.execute(
            select(func.count())
            .select_from(_failure)
            .where(
                _failure.username == username,
                _failure.succeeded.is_(False),
                _failure.attempted_at >= cutoff,
                ~_failure.ip.startswith(_PASSWORD_CHANGE_PREFIX, autoescape=True),
                ~later_success,
            )
        ).scalar_one()
        return int(count)

    def _recent_login_failures(self, db: Session, cutoff: datetime.datetime) -> int:
        """Count live login failures from all sources (password changes excluded)."""
        count = db.execute(
            select(func.count())
            .select_from(LoginAttempt)
            .where(
                LoginAttempt.succeeded.is_(False),
                LoginAttempt.attempted_at >= cutoff,
                ~LoginAttempt.ip.startswith(_PASSWORD_CHANGE_PREFIX, autoescape=True),
            )
        ).scalar_one()
        return int(count)

    def _prune(self, db: Session, cutoff: datetime.datetime) -> None:
        """Delete attempt rows older than the sliding window."""
        db.execute(delete(LoginAttempt).where(LoginAttempt.attempted_at < cutoff))
