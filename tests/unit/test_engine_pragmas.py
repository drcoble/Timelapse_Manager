"""Connection-pragma and write-retry behavior for the SQLite engine."""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError

from timelapse_manager.db.engine import (
    SQLITE_BUSY_TIMEOUT_MS,
    create_db_engine,
    run_with_write_retry,
)


def test_busy_timeout_set_on_new_connection(tmp_path) -> None:
    """The connect hook applies the busy timeout (plus WAL and FK) per connection."""
    url = f"sqlite:///{tmp_path / 'pragmas.db'}"
    engine = create_db_engine(url)
    try:
        with engine.connect() as conn:
            assert (
                conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
                == SQLITE_BUSY_TIMEOUT_MS
            )
            assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
    finally:
        engine.dispose()


def test_retry_succeeds_after_transient_lock() -> None:
    """A few 'database is locked' failures are retried until the call succeeds."""
    calls: list[int] = []

    def operation() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    result = run_with_write_retry(operation, base_delay=0.0, max_attempts=5)

    assert result == "ok"
    assert len(calls) == 3


def test_retry_handles_sqlalchemy_operational_error() -> None:
    """A SQLAlchemy-wrapped lock error is recognized and retried too."""
    calls: list[int] = []

    def operation() -> str:
        calls.append(1)
        if len(calls) < 2:
            raise SQLAlchemyOperationalError(
                "UPDATE ...", {}, Exception("database is locked")
            )
        return "ok"

    assert run_with_write_retry(operation, base_delay=0.0) == "ok"
    assert len(calls) == 2


def test_retry_gives_up_after_max_attempts() -> None:
    """Persistent lock contention re-raises the final error once attempts run out."""
    calls: list[int] = []

    def operation() -> str:
        calls.append(1)
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        run_with_write_retry(operation, base_delay=0.0, max_attempts=3)

    assert len(calls) == 3


def test_retry_does_not_swallow_other_operational_errors() -> None:
    """Non-lock operational errors propagate immediately without retrying."""
    calls: list[int] = []

    def operation() -> str:
        calls.append(1)
        raise sqlite3.OperationalError("no such table: widget")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        run_with_write_retry(operation, base_delay=0.0, max_attempts=5)

    assert len(calls) == 1


class TestPoolSizing:
    """The pool fits the threads that can hold a connection at once (#37).

    SQLAlchemy's default (5 + 10, 30 s timeout) was smaller than the request
    threadpool alone, so a dashboard burst waited on the pool.
    """

    def test_file_database_pool_covers_both_thread_pools(self, tmp_path) -> None:
        from concurrent.futures import ThreadPoolExecutor

        from timelapse_manager.db.engine import (
            POOL_SIZE,
            POOL_TIMEOUT_SECONDS,
            REQUEST_THREADPOOL_TOKENS,
            background_executor_workers,
            pool_capacity,
        )

        # The mirror of the stdlib executor default must stay true.
        executor = ThreadPoolExecutor()
        try:
            assert background_executor_workers() == executor._max_workers
        finally:
            executor.shutdown()
        # Plus the jobs executor's default 2 workers and the capture executor,
        # whose default size is the default executor's own (#66 C3).
        assert pool_capacity() == (
            REQUEST_THREADPOOL_TOKENS + 2 * background_executor_workers() + 2
        )
        assert pool_capacity(2, 3) == REQUEST_THREADPOOL_TOKENS + (
            background_executor_workers() + 2 + 3
        )

        engine = create_db_engine(f"sqlite:///{tmp_path / 'pool.db'}")
        try:
            pool = engine.pool
            assert pool.size() == POOL_SIZE  # type: ignore[attr-defined]
            assert pool.timeout() == POOL_TIMEOUT_SECONDS  # type: ignore[attr-defined]
            # No public accessor for the overflow cap.
            assert pool._max_overflow == pool_capacity() - POOL_SIZE  # type: ignore[attr-defined]
        finally:
            engine.dispose()

    def test_every_thread_gets_a_connection_without_waiting(self, tmp_path) -> None:
        """``pool_capacity()`` connections can be held at once, with no wait."""
        import time

        from timelapse_manager.db.engine import pool_capacity

        engine = create_db_engine(f"sqlite:///{tmp_path / 'pool.db'}")
        held = []
        try:
            start = time.monotonic()
            for _ in range(pool_capacity()):
                held.append(engine.connect())
            assert time.monotonic() - start < 5.0
        finally:
            for conn in held:
                conn.close()
            engine.dispose()

    @pytest.mark.parametrize(
        "url",
        ["sqlite://", "sqlite:///:memory:", "sqlite:///file:x?mode=memory&uri=true"],
    )
    def test_in_memory_database_keeps_its_singleton_pool(self, url: str) -> None:
        """An in-memory URL takes no pool sizing (it would raise)."""
        engine = create_db_engine(url)
        try:
            with engine.connect() as conn:
                assert conn.exec_driver_sql("SELECT 1").scalar() == 1
        finally:
            engine.dispose()
