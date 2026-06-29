"""Synchronous session management.

The application uses a synchronous SQLAlchemy engine, so all session handling
here is synchronous. FastAPI runs synchronous dependencies in a worker thread,
so :func:`get_session` integrates cleanly with the async server without an async
database driver.

Wiring for the request dependency
---------------------------------
:func:`get_session` reads a module-level session factory that must be installed
once at application startup via :func:`set_session_factory`. The application's
startup code is expected to build the engine, create a factory with
:func:`create_session_factory`, and call :func:`set_session_factory` before any
request is served. Requests then depend on :func:`get_session` directly.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

# Installed at startup via set_session_factory(); read by get_session().
_session_factory: sessionmaker[Session] | None = None


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Build a synchronous session factory bound to ``engine``.

    ``expire_on_commit`` is disabled so attributes remain accessible after the
    surrounding scope commits, which is the more ergonomic default for the
    request and CLI flows that consume these sessions.
    """
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def set_session_factory(session_factory: sessionmaker[Session]) -> None:
    """Install the process-wide session factory used by :func:`get_session`."""
    global _session_factory
    _session_factory = session_factory


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Provide a transactional session scope.

    Commits on successful exit, rolls back on any exception, and always closes
    the session.
    """
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session.

    Synchronous by design; FastAPI executes it in a threadpool. Commits when the
    request handler returns normally and rolls back if it raises, then always
    closes the session. Requires :func:`set_session_factory` to have been called
    at startup; otherwise a clear ``RuntimeError`` is raised.
    """
    if _session_factory is None:
        raise RuntimeError(
            "Session factory is not configured; call set_session_factory() "
            "during application startup before serving requests."
        )
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
