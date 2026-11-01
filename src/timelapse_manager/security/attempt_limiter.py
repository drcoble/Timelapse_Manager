"""In-memory sliding-window attempt limiter with reserve-then-release.

Used by the one-time camera admin read (#68). It is deliberately separate from
the login throttle (:mod:`.throttle`): that one is keyed on a free-text username
any unauthenticated client can submit, so sharing its table would let anyone
fill a camera's budget from ``POST /login``, and each feature would pollute the
other's counts.

How it works:

* An attempt is **reserved** against several keys at once (for example the
  target device, the camera row and the acting user) *before* the guarded work
  runs. The reservation counts as a failure from that moment.
* If any key already holds ``max_attempts`` live reservations, the attempt is
  refused and nothing is recorded.
* When the guarded work proves the attempt legitimate, the caller **releases**
  the reservation, removing it from every key. Otherwise it stays and ages out
  after ``window_seconds``.

The check and the reservation run in one synchronous call with no ``await`` in
between, so on the event loop they are atomic: concurrent requests cannot all
pass the check before any of them is counted. Not thread-safe; call it from the
event loop only. State is per process and is lost on restart, which is
acceptable for a short window guarding an operator-only action.
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass

MonotonicFn = Callable[[], float]


@dataclass(frozen=True)
class Reservation:
    """One reserved attempt, as handed back by :meth:`AttemptLimiter.try_reserve`.

    :param keys: the keys the attempt was counted against.
    :param token: an opaque id that tells this attempt apart from others.
    """

    keys: tuple[Hashable, ...]
    token: int


class AttemptLimiter:
    """Cap attempts per key within a sliding window, counting before the work.

    :param max_attempts: live reservations a key may hold before it refuses.
    :param window_seconds: how long an unreleased reservation counts.
    :param now: monotonic clock in seconds; injectable for tests.
    """

    def __init__(
        self,
        *,
        max_attempts: int,
        window_seconds: float,
        now: MonotonicFn = time.monotonic,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._max_attempts = max_attempts
        self._window = window_seconds
        self._now = now
        self._tokens = itertools.count(1)
        # key -> [(reserved_at, token), ...] in reservation order.
        self._hits: dict[Hashable, list[tuple[float, int]]] = {}

    def try_reserve(self, keys: Iterable[Hashable]) -> Reservation | None:
        """Reserve one attempt against every key, or return None if any is full."""
        unique = tuple(dict.fromkeys(keys))
        if not unique:
            raise ValueError("at least one key is required")
        now = self._now()
        # Sweep every key, not only these, so keys that are never touched again
        # cannot accumulate (the key set is small: devices, cameras, users).
        for key in list(self._hits):
            self._prune(key, now)
        if any(len(self._hits.get(key, ())) >= self._max_attempts for key in unique):
            return None
        token = next(self._tokens)
        for key in unique:
            self._hits.setdefault(key, []).append((now, token))
        return Reservation(keys=unique, token=token)

    def release(self, reservation: Reservation) -> None:
        """Forget a reservation: the attempt proved legitimate, so it is not counted."""
        for key in reservation.keys:
            hits = self._hits.get(key)
            if hits is None:
                continue
            remaining = [hit for hit in hits if hit[1] != reservation.token]
            if remaining:
                self._hits[key] = remaining
            else:
                del self._hits[key]

    def count(self, key: Hashable) -> int:
        """Return the live reservations held against ``key``."""
        self._prune(key, self._now())
        return len(self._hits.get(key, ()))

    def clear(self) -> None:
        """Forget every reservation (test isolation, or an operator reset)."""
        self._hits.clear()

    def _prune(self, key: Hashable, now: float) -> None:
        """Drop ``key``'s reservations older than the window (and the key if empty)."""
        hits = self._hits.get(key)
        if hits is None:
            return
        cutoff = now - self._window
        live = [hit for hit in hits if hit[0] > cutoff]
        if live:
            self._hits[key] = live
        else:
            del self._hits[key]
