"""Replay-safe JSON mutations through an ``Idempotency-Key`` header (#70 F-R2).

A double click, a retried request after a dropped connection, or a second tab
can send the same mutation twice. ``POST /frames/offset`` is not idempotent (a
second shift moves the frames again), and ``POST /frames/export`` enqueues a
second job. The browser mints one key per user action and sends it on the
mutating request; :func:`run_idempotent` then makes the route act at most once
per key:

* the **first** request with a key reserves it and runs the handler;
* a **concurrent duplicate** (the first is still running) gets ``409`` with
  ``{"detail": "duplicate request in progress"}`` and changes nothing;
* a **completed duplicate** replays the stored response byte for byte, with an
  ``Idempotent-Replayed: true`` header;
* the **same key with a different body** gets ``422``: a key names one request,
  so reusing it for another is a client bug, not a retry;
* a request **without** the header behaves exactly as before (the CLI and the
  JSON API do not send one).

Only a 2xx response is stored. A non-2xx response or an exception releases the
reservation, so the user can retry the same action after a failure.

Entries are scoped by ``(user_id, route, key)``: one user can never replay, or
block, another user's request by guessing a key. The cache is in memory, per
process and per app (``app.state``), with a TTL of :data:`DEFAULT_TTL_SECONDS`
and at most :data:`DEFAULT_MAX_ENTRIES` entries (least recently used evicted
first). That is sufficient because the service runs a single uvicorn worker. A
multi-worker deployment would need a shared store, and a restart forgets every
key, which only re-opens the double-submit window for requests in flight at the
restart.

The routes that use this are sync ``def`` routes running in the threadpool, so
the cache is guarded by a :class:`threading.Lock`, never an asyncio primitive.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum

from fastapi import Request, status
from fastapi.responses import JSONResponse, Response

#: Request header carrying the client-minted key.
IDEMPOTENCY_HEADER = "Idempotency-Key"
#: Response header set on a replayed response.
REPLAYED_HEADER = "Idempotent-Replayed"
DEFAULT_TTL_SECONDS = 600.0
DEFAULT_MAX_ENTRIES = 1000

# A UUID (36 chars) or a hex fallback fits; anything else is refused before it
# reaches the cache, so arbitrary header text never becomes a dict key.
_KEY_RE = re.compile(r"\A[A-Za-z0-9_-]{8,128}\Z")
# Form fields that never identify the request (the CSRF token rotates per page).
_FINGERPRINT_EXCLUDED = frozenset({"csrf_token"})

_APP_STATE_ATTR = "idempotency_cache"
_app_state_lock = threading.Lock()

CacheKey = tuple[int, str, str]


@dataclass(frozen=True)
class StoredResponse:
    """The parts of a 2xx response needed to replay it."""

    status_code: int
    media_type: str | None
    body: bytes


class Outcome(Enum):
    """Result of :meth:`IdempotencyCache.reserve`."""

    RESERVED = "reserved"
    IN_PROGRESS = "in_progress"
    REPLAY = "replay"
    MISMATCH = "mismatch"


@dataclass
class _Entry:
    fingerprint: str
    expires_at: float
    response: StoredResponse | None = None


class IdempotencyCache:
    """Thread-safe, TTL- and size-bounded map of idempotency keys to responses."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[CacheKey, _Entry] = OrderedDict()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def reserve(
        self, key: CacheKey, fingerprint: str
    ) -> tuple[Outcome, StoredResponse | None]:
        """Atomically look up ``key`` and reserve it when it is free.

        Returns ``(RESERVED, None)`` when the caller now owns the key and must
        later call :meth:`complete` or :meth:`release`; ``(IN_PROGRESS, None)``
        when another request holds it; ``(REPLAY, stored)`` when it completed;
        ``(MISMATCH, None)`` when it was used for a different request body.
        """
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                if entry.fingerprint != fingerprint:
                    return Outcome.MISMATCH, None
                if entry.response is None:
                    return Outcome.IN_PROGRESS, None
                return Outcome.REPLAY, entry.response
            self._entries[key] = _Entry(
                fingerprint=fingerprint, expires_at=now + self._ttl
            )
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)
            return Outcome.RESERVED, None

    def complete(self, key: CacheKey, response: StoredResponse) -> None:
        """Store the response for a key this caller reserved."""
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                # Evicted while the handler ran (a burst of >max other keys).
                # Nothing to replay later; the mutation itself already happened.
                return
            entry.response = response
            entry.expires_at = now + self._ttl
            self._entries.move_to_end(key)

    def release(self, key: CacheKey) -> None:
        """Drop a reservation whose request failed, so it can be retried."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.response is None:
                del self._entries[key]

    def _purge_expired(self, now: float) -> None:
        expired = [k for k, e in self._entries.items() if e.expires_at <= now]
        for k in expired:
            del self._entries[k]


def cache_for(request: Request) -> IdempotencyCache:
    """Return the app's cache, creating it on first use."""
    state = request.app.state
    cache = getattr(state, _APP_STATE_ATTR, None)
    if cache is None:
        with _app_state_lock:
            cache = getattr(state, _APP_STATE_ATTR, None)
            if cache is None:
                cache = IdempotencyCache()
                setattr(state, _APP_STATE_ATTR, cache)
    return cache


def fingerprint_form(form: Mapping[str, str]) -> str:
    """Hash the identifying form fields, order-independently."""
    digest = hashlib.sha256()
    for name in sorted(form):
        if name in _FINGERPRINT_EXCLUDED:
            continue
        digest.update(name.encode("utf-8", "surrogatepass"))
        digest.update(b"\x00")
        digest.update(str(form[name]).encode("utf-8", "surrogatepass"))
        digest.update(b"\x00")
    return digest.hexdigest()


def run_idempotent(
    request: Request,
    *,
    user_id: int,
    route: str,
    form: Mapping[str, str],
    handler: Callable[[], Response],
) -> Response:
    """Run ``handler`` at most once per ``Idempotency-Key`` (see module docs).

    ``handler`` must commit its own transaction before returning a 2xx response:
    the response is stored as soon as it returns, and a replay must never report
    a change that a later commit failure rolled back.
    """
    raw_key = request.headers.get(IDEMPOTENCY_HEADER)
    if raw_key is None:
        return handler()
    if not _KEY_RE.match(raw_key):
        return JSONResponse(
            {"detail": f"Invalid {IDEMPOTENCY_HEADER} header."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    cache = cache_for(request)
    key: CacheKey = (user_id, route, raw_key)
    outcome, stored = cache.reserve(key, fingerprint_form(form))
    if outcome is Outcome.REPLAY:
        assert stored is not None
        return Response(
            content=stored.body,
            status_code=stored.status_code,
            media_type=stored.media_type,
            headers={REPLAYED_HEADER: "true"},
        )
    if outcome is Outcome.IN_PROGRESS:
        return JSONResponse(
            {"detail": "duplicate request in progress"},
            status_code=status.HTTP_409_CONFLICT,
        )
    if outcome is Outcome.MISMATCH:
        return JSONResponse(
            {"detail": f"{IDEMPOTENCY_HEADER} was already used for another request."},
            # HTTP_422_UNPROCESSABLE_CONTENT; that constant was renamed in Starlette.
            status_code=422,
        )

    try:
        response = handler()
    except BaseException:
        cache.release(key)
        raise
    if 200 <= response.status_code < 300:
        cache.complete(
            key,
            StoredResponse(
                status_code=response.status_code,
                media_type=response.media_type,
                body=bytes(response.body),
            ),
        )
    else:
        cache.release(key)
    return response
