"""Process-wide application context.

Holds the live, per-process objects that request handlers and CLI commands need
but that cannot be threaded through every call site: resolved settings, the
database engine, the session factory, a logger, and probed component versions.

The context is installed once during startup (see the application lifespan and
the service runner) via :func:`set_context` and read back at request time via
:func:`get_context`. Module-level, stable functions such as the local-token
dependency and the system endpoint deliberately read from this context rather
than closing over a particular ``Settings`` instance, so they keep stable import
names while still observing the running process's configuration.

Named ``runtime`` (not ``context`` or ``engine``) to avoid confusion with the
database engine module and SQLAlchemy's own connection context.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings

if TYPE_CHECKING:
    from .capture import CaptureSupervisor
    from .monitoring import NotificationDispatcher
    from .render import RenderQueue, RenderScheduler


@dataclass
class AppContext:
    """Container for the live objects shared across a running process.

    :param settings: the resolved application settings in effect.
    :param env_overrides: dotted leaf paths (e.g. ``server.http_port``) whose
        effective value came from an environment variable, computed once at load.
        Empty when settings were built directly (tests, alternative wiring) rather
        than loaded with provenance. Read-only display surfaces use it to mark
        which values the environment, rather than the editable config, controls.
    :param ssrf_config_subnets: the SSRF private-subnet allow-list as supplied by
        config/env, captured once at startup before any admin-added (DB) subnets
        are merged in. It is the immutable baseline: at runtime
        ``settings.ssrf.allowed_private_subnets`` holds the *union* of this and the
        admin-managed list, so a future reader is not surprised that the live
        settings value can diverge from the loaded configuration. Empty by default
        so direct constructors (tests) need not supply it.
    :param db_engine: the SQLAlchemy engine backing the application.
    :param session_factory: factory producing synchronous ORM sessions.
    :param logger: the application logger.
    :param app_version: the application version string.
    :param ffmpeg_version: the probed ffmpeg version string.
    :param ffmpeg_path: the resolved path to the ffmpeg binary the application
        encodes and captures with (bundled when frozen, an explicit knob when
        set, else ``ffmpeg`` on ``PATH``). Resolved once at startup and surfaced
        on liveness/system endpoints so the shipped encoder is identifiable.
    :param capture_supervisor: the running capture supervisor, or None before it
        is constructed (it is installed during startup wiring).
    :param render_queue: the bounded render worker, or None before startup wiring
        constructs it. The render API enqueues jobs and cancels renders through it.
    :param render_scheduler: the periodic render/archive scheduler, or None before
        startup wiring constructs it.
    :param notification_dispatcher: the poll-based notification dispatcher, or
        None before startup wiring constructs it. Startup installs the channels
        and starts/stops its poll loop.
    """

    settings: Settings
    db_engine: Engine
    session_factory: sessionmaker[Session]
    logger: logging.Logger
    app_version: str
    ffmpeg_version: str
    # Defaulted so direct constructors (tests, alternative wiring) need not supply
    # it; startup always sets it explicitly to the resolved binary.
    ffmpeg_path: str = "ffmpeg"
    capture_supervisor: CaptureSupervisor | None = None
    render_queue: RenderQueue | None = None
    render_scheduler: RenderScheduler | None = None
    notification_dispatcher: NotificationDispatcher | None = None
    # Dotted leaf paths whose effective value came from the environment. A
    # frozenset is immutable, so a shared default is safe. Empty by default so
    # direct constructors (tests, alternative wiring) need not supply it. Placed
    # last so positional construction of the optional tail fields is unaffected.
    env_overrides: frozenset[str] = frozenset()
    # Config/env SSRF subnet baseline, captured before admin (DB) subnets merge in.
    # A tuple is immutable, so a shared default is safe. Appended after
    # env_overrides so existing positional construction is unaffected.
    ssrf_config_subnets: tuple[str, ...] = ()


# Installed once at startup via set_context(); read back via get_context().
_context: AppContext | None = None


def set_context(context: AppContext) -> None:
    """Install the process-wide application context.

    Called once during startup before any request is served. Replacing an
    existing context is permitted (for example, when a test rebuilds the
    application), with the caller responsible for disposing the prior one.
    """
    global _context
    _context = context


def get_context() -> AppContext:
    """Return the installed application context.

    :raises RuntimeError: if no context has been installed yet. This signals a
        programming error -- a request or command reached context-dependent code
        before startup wiring ran.
    """
    if _context is None:
        raise RuntimeError(
            "Application context is not configured; it is installed during "
            "startup before requests are served."
        )
    return _context


def dispose() -> None:
    """Tear down and clear the installed context, if any.

    Disposes the database engine's connection pool and drops the reference so a
    fresh context can be installed. Also clears the process-wide encryption key
    provider (installed during startup) so a subsequent boot re-selects it
    cleanly and no provider leaks across application lifecycles. Safe to call when
    no context is set.
    """
    global _context
    if _context is not None:
        _context.db_engine.dispose()
        _context = None
    # Clear the at-rest key provider installed at startup. Imported locally to
    # avoid a module-level import cycle (security.crypto has no runtime import of
    # this module). Binds the key-provider lifecycle to the application lifecycle.
    from .security.crypto import set_key_provider

    set_key_provider(None)
