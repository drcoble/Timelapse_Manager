"""Brute-force login throttling.

Repeated failed logins are slowed to make online password guessing impractical
without turning login into an account-enumeration or denial-of-service oracle.

Design constraints:

* **Per-IP is the primary limit.** A single source that exceeds the failure
  budget within the sliding window is throttled regardless of which usernames
  it targets, which is what actually bounds a guessing attack.
* **The per-username component must not leak account existence.** Failures are
  counted against the *submitted username string* whether or not such an account
  exists, and the throttled outcome is identical for valid and invalid
  usernames. There is deliberately **no** hard per-username lockout: a lockout
  that only triggers for real accounts would confirm which usernames exist and
  would let an attacker lock a victim out at will. The username counter only
  contributes to the same uniform "slow down" decision the IP counter drives.

The throttle records only failures; a success clears the relevant counters so a
legitimate user is not penalised for an earlier typo. All counting is in-memory
and best-effort (a single-process control surface); it never stores or logs the
attempted password.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Callable

from ..config import AuthSettings

MonotonicFn = Callable[[], float]


class BruteForceThrottle:
    """Sliding-window failed-login counter, keyed per IP and per username.

    Thread-safe (the synchronous request handlers run in a threadpool). Counts
    are kept in memory and pruned lazily to the configured window. Construct one
    per process and share it across login requests.
    """

    def __init__(
        self,
        settings: AuthSettings,
        *,
        monotonic: MonotonicFn = time.monotonic,
    ) -> None:
        self._max_failures = settings.throttle_max_failures
        self._window = float(settings.throttle_window_seconds)
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._ip_failures: dict[str, list[float]] = defaultdict(list)
        self._username_failures: dict[str, list[float]] = defaultdict(list)

    def _prune(self, stamps: list[float], cutoff: float) -> list[float]:
        """Return only the timestamps at or after ``cutoff``."""
        return [t for t in stamps if t >= cutoff]

    def is_throttled(self, *, ip: str, username: str) -> bool:
        """Return True if login from ``ip`` for ``username`` should be refused.

        Throttled when either the per-IP or the per-username failure count
        within the window meets the configured ceiling. The username branch is
        evaluated identically for existent and non-existent accounts, so the
        decision is not an enumeration oracle. Callers must surface the same
        generic response whether or not this returns True for an unknown user.
        """
        now = self._monotonic()
        cutoff = now - self._window
        with self._lock:
            ip_hits = self._prune(self._ip_failures.get(ip, []), cutoff)
            self._ip_failures[ip] = ip_hits
            name_hits = self._prune(self._username_failures.get(username, []), cutoff)
            self._username_failures[username] = name_hits
            return (
                len(ip_hits) >= self._max_failures
                or len(name_hits) >= self._max_failures
            )

    def record_failure(self, *, ip: str, username: str) -> None:
        """Record one failed login attempt against ``ip`` and ``username``.

        The username is counted as submitted -- never resolved against the
        account table here -- so counting itself reveals nothing about whether
        the account exists.
        """
        now = self._monotonic()
        cutoff = now - self._window
        with self._lock:
            ip_hits = self._prune(self._ip_failures.get(ip, []), cutoff)
            ip_hits.append(now)
            self._ip_failures[ip] = ip_hits
            name_hits = self._prune(self._username_failures.get(username, []), cutoff)
            name_hits.append(now)
            self._username_failures[username] = name_hits

    def record_success(self, *, ip: str, username: str) -> None:
        """Clear the failure counters for ``ip`` and ``username``.

        A genuine login proves the source/account pair is not an attacker, so
        an earlier mistyped password does not keep counting against them.
        """
        with self._lock:
            self._ip_failures.pop(ip, None)
            self._username_failures.pop(username, None)
