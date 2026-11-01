"""SQLite engine construction with write-ahead logging enabled per connection.

This module owns how the application opens its SQLite database. WAL is enabled
via a ``connect`` event so it applies to every pooled connection, whether the
engine is created by the running application or reused by database migrations.

No ORM models are defined here yet; this is the connection substrate only.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable
from typing import Any, TypeVar

from sqlalchemy import Engine, create_engine, event, make_url
from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError

from ..executors import DEFAULT_JOBS_EXECUTOR_WORKERS
from ..paths import default_database_url

# How long SQLite waits for a competing writer before returning "database is
# locked". WAL allows one writer at a time; under brief contention (e.g. the
# capture loop and a web request writing at once) a non-zero busy timeout lets
# the loser block briefly and succeed instead of failing immediately.
SQLITE_BUSY_TIMEOUT_MS = 15000

# Ceiling, in bytes, that SQLite truncates the WAL file back to after a
# checkpoint resets it (issue #66, C9). Without it the ``-wal`` file keeps its
# high-water size forever after one burst of writes. Durability is unchanged:
# this only bounds the file left on disk, not when data is synced.
SQLITE_JOURNAL_SIZE_LIMIT_BYTES = 64 * 1024 * 1024

_T = TypeVar("_T")

# Defaults for :func:`run_with_write_retry`. Short, bounded backoff: enough to
# ride out a transient lock that outlives the busy timeout without stalling a
# request for long or masking a genuine deadlock.
_WRITE_RETRY_MAX_ATTEMPTS = 5
_WRITE_RETRY_BASE_DELAY = 0.05
_WRITE_RETRY_MAX_DELAY = 1.0

# Connection-pool sizing (issue #37). A pool wait must never be what a request
# blocks on, so the pool is sized from the thread budget that can hold a
# connection at once rather than left at SQLAlchemy's 5 + 10 default:
#
# * Request work (sync endpoints and sync dependencies) runs in anyio's worker
#   threadpool. The application pins that limiter to REQUEST_THREADPOOL_TOKENS at
#   startup (anyio's own default), so it is a known number, not an implicit one.
#   ``async def`` routes hop their database work onto the same threadpool with
#   ``run_in_threadpool`` (#66 C3), not onto the loop's default executor.
# * The background loops (render, scheduler, dispatcher) reach the database
#   through ``asyncio.to_thread``, i.e. the event loop's default executor, whose
#   size is ``min(32, cpu_count + 4)`` (8 on a 4-core Pi).
# * Capture (the supervisor, one-shot captures, the event listeners) runs every
#   blocking hop on its own executor (``capture.executor_workers``, by default
#   the same ``min(32, cpu_count + 4)``; thread names ``tlm-capture``), so a
#   capture never queues behind the default executor (#66 C3).
# * Long render-side jobs (render preparation, export zips, post-render prunes,
#   the codec backfill scan) run on the dedicated jobs executor
#   (``executors.py``, ``render.jobs_executor_workers``, default 2), and each
#   of its workers can hold a connection too.
#
# A request session keeps its connection from its first query to its commit, so
# what the pool really has to cover is in-flight requests plus background work.
# Sizing it to all four thread pools covers any burst up to the request
# threadpool: a dashboard load is ~24 requests (23 connections at peak,
# measured), against a cap of 58 on a 4-core Pi. Beyond that, a request can wait
# up to the timeout below, and that wait is logged. Only POOL_SIZE connections
# are kept open between bursts; the overflow ones are closed as soon as they are
# returned, so an idle Pi holds 10 SQLite handles (an fd and a lazily filled page
# cache each), and a dashboard burst briefly opens a few more instead of
# queueing behind the pool.
REQUEST_THREADPOOL_TOKENS = 40
POOL_SIZE = 10
# Inside that budget, waiting for a connection means one leaked (held across an
# await, or never returned) or a load well past what the app is built for. Fail
# that checkout quickly with a logged error rather than hanging the request for
# SQLAlchemy's 30 s default.
POOL_TIMEOUT_SECONDS = 10.0

# Default database location. Resolved once, from the single authoritative helper
# in :mod:`timelapse_manager.paths`, so this module and the settings model share
# one value and cannot drift apart. It anchors under an OS-standard, writable
# state directory (never the working directory), so migrations run from a frozen
# or service-managed process with no useful CWD still target the right database.
# Resolving the value is side-effect free; no directory is created here.
DEFAULT_DATABASE_URL = default_database_url()


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
    """Apply WAL, FK enforcement, busy timeout and WAL size cap per connection.

    Registered against the generic SQLAlchemy ``Engine`` class so the pragmas
    are applied uniformly to any SQLite connection opened in this process,
    including connections opened by Alembic migrations. SQLite disables foreign
    keys by default and the setting is per-connection, so it must be enabled on
    every connection for the schema's ``ON DELETE`` rules to take effect. The
    busy timeout is likewise per-connection and lets a connection wait briefly
    for a competing writer rather than failing instantly under contention. The
    journal size limit caps the ``-wal`` file kept after a checkpoint; it does
    not change durability. Non-SQLite connections are ignored.
    """
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute(f"PRAGMA journal_size_limit={SQLITE_JOURNAL_SIZE_LIMIT_BYTES}")
    finally:
        cursor.close()


def checkpoint_wal_passive(engine: Engine) -> tuple[int, int, int] | None:
    """Run ``PRAGMA wal_checkpoint(PASSIVE)`` and return SQLite's result row.

    A passive checkpoint copies as much of the WAL into the database as it can
    without waiting on any reader or writer, so it never blocks the app; with
    :data:`SQLITE_JOURNAL_SIZE_LIMIT_BYTES` it lets the ``-wal`` file shrink
    back after a write burst (issue #66, C9). Synchronous: call it off the event
    loop. Returns ``(busy, wal_frames, checkpointed_frames)``, or ``None`` for a
    non-SQLite engine.
    """
    if engine.dialect.name != "sqlite":
        return None
    with engine.connect() as conn:
        row = conn.exec_driver_sql("PRAGMA wal_checkpoint(PASSIVE)").one()
    return int(row[0]), int(row[1]), int(row[2])


def _is_database_locked(exc: BaseException) -> bool:
    """Return ``True`` when ``exc`` reports SQLite write contention.

    Matches the ``database is locked`` message raised (as ``SQLITE_BUSY``) when
    a writer cannot acquire the lock within the busy timeout. The message is
    surfaced by the stdlib ``sqlite3`` driver and preserved in SQLAlchemy's
    wrapping ``OperationalError``, so checking the stringified exception covers
    both callers.
    """
    return "database is locked" in str(exc).lower()


def run_with_write_retry(
    operation: Callable[[], _T],
    *,
    max_attempts: int = _WRITE_RETRY_MAX_ATTEMPTS,
    base_delay: float = _WRITE_RETRY_BASE_DELAY,
    max_delay: float = _WRITE_RETRY_MAX_DELAY,
) -> _T:
    """Run ``operation`` and retry it while SQLite reports the database locked.

    Intended to wrap a short, self-contained write transaction so transient
    lock contention that outlives the connection's busy timeout does not bubble
    up as an error. ``operation`` is a zero-argument callable that performs the
    write (and any commit) and returns its result; it must be idempotent or
    safe to re-run, because it is invoked again on each retry.

    Only ``OperationalError`` whose message indicates ``database is locked`` is
    retried; every other error (including other operational errors such as a
    missing table) propagates immediately. Between attempts the call sleeps with
    bounded exponential backoff, and the final ``database is locked`` failure is
    re-raised once ``max_attempts`` is exhausted.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            return operation()
        except (sqlite3.OperationalError, SQLAlchemyOperationalError) as exc:
            if attempt >= max_attempts or not _is_database_locked(exc):
                raise
            delay = min(base_delay * 2 ** (attempt - 1), max_delay)
            time.sleep(delay)
    # Unreachable: the loop either returns or raises on every iteration. Present
    # so the function is statically exhaustive for the type checker.
    raise AssertionError("run_with_write_retry exhausted its loop unexpectedly")


def background_executor_workers() -> int:
    """Return the size of the event loop's default executor.

    This is the pool ``asyncio.to_thread`` runs on (the background loops' route
    to the database). It mirrors :class:`concurrent.futures.ThreadPoolExecutor`'s
    default ``max_workers`` of ``min(32, cpu_count + 4)``.
    """
    return min(32, (os.cpu_count() or 1) + 4)


def capture_executor_workers(configured: int | None = None) -> int:
    """Return the size of the capture executor.

    :param configured: ``capture.executor_workers``; ``None`` (the default)
        means the default executor's own size, :func:`background_executor_workers`,
        so capture keeps the capacity it had on the shared pool, now unshared.
    :raises ValueError: when ``configured`` is below 1.
    """
    if configured is None:
        return background_executor_workers()
    if configured < 1:
        raise ValueError(
            f"the capture executor needs at least 1 worker, got {configured}"
        )
    return configured


def pool_capacity(
    jobs_executor_workers: int = DEFAULT_JOBS_EXECUTOR_WORKERS,
    capture_workers: int | None = None,
) -> int:
    """Return how many connections the pool may hand out at once.

    One per thread that can hold a connection concurrently: the request
    threadpool, the background executor, the jobs executor and the capture
    executor (see the module constants).

    :param jobs_executor_workers: the jobs executor's size
        (``render.jobs_executor_workers``).
    :param capture_workers: the capture executor's size, already resolved;
        ``None`` means its default (:func:`capture_executor_workers`).
    """
    if capture_workers is None:
        capture_workers = capture_executor_workers()
    return (
        REQUEST_THREADPOOL_TOKENS
        + background_executor_workers()
        + jobs_executor_workers
        + capture_workers
    )


def _uses_queue_pool(database_url: str) -> bool:
    """Return whether SQLAlchemy builds a ``QueuePool`` for ``database_url``.

    An in-memory SQLite database gets a ``SingletonThreadPool`` (each connection
    would otherwise be a separate, empty database), which takes no overflow or
    timeout arguments; every file database and non-SQLite URL gets a
    ``QueuePool``.
    """
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        return True
    in_memory = url.database in (None, "", ":memory:") or (
        url.query.get("mode") == "memory"
    )
    return not in_memory


def create_db_engine(
    database_url: str = DEFAULT_DATABASE_URL,
    *,
    jobs_executor_workers: int = DEFAULT_JOBS_EXECUTOR_WORKERS,
    capture_workers: int | None = None,
) -> Engine:
    """Create a SQLAlchemy engine for the given SQLite database URL.

    The WAL pragma is applied automatically to each connection via the
    module-level ``connect`` event listener. A pooled (file) database gets a pool
    sized to the thread budget (see :func:`pool_capacity`), so a burst of
    concurrent requests never waits for a connection.

    :param jobs_executor_workers: the jobs executor's size, counted in the pool.
    :param capture_workers: the capture executor's size, counted in the pool;
        ``None`` means its default (:func:`capture_executor_workers`).
    """
    if not _uses_queue_pool(database_url):
        return create_engine(database_url, future=True)
    return create_engine(
        database_url,
        future=True,
        pool_size=POOL_SIZE,
        max_overflow=pool_capacity(jobs_executor_workers, capture_workers) - POOL_SIZE,
        pool_timeout=POOL_TIMEOUT_SECONDS,
    )
