"""ASGI application factory.

Builds the FastAPI application from resolved settings. The factory itself is a
pure function with no import-time or construction-time side effects -- it only
declares routes and a lifespan. All process wiring (logging, engine, session
factory, local token, shared context) happens inside the lifespan when the
application actually starts serving, so the factory stays cheap to call from
tests and from each control surface.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from alembic.migration import MigrationContext
from fastapi import FastAPI, Response
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from . import __version__
from .api import mount_api
from .capture import CaptureSupervisor
from .capture.event_listener import EventListenerFactory
from .config import Settings
from .db.engine import create_db_engine
from .db.session import create_session_factory, session_scope, set_session_factory
from .ffmpeg_pin import resolve_ffmpeg_binary
from .logging import configure_logging
from .monitoring import NotificationChannel, NotificationDispatcher
from .monitoring.channels.smtp import SMTPChannel, build_smtp_config
from .monitoring.settings_service import (
    build_webhook_channel,
    load_settings,
    resolve_smtp_password,
)
from .render import RenderQueue, RenderScheduler
from .runtime import AppContext, dispose, get_context, set_context
from .security.crypto import set_key_provider
from .security.keystore import build_key_provider
from .security.token import ensure_local_token
from .storage import DiskSpaceMonitor
from .version import probe_ffmpeg_version
from .web import mount_web

logger = logging.getLogger(__name__)

_UNKNOWN_REVISION = "unknown"


def _current_alembic_revision(engine: Engine) -> str:
    """Return the database's current Alembic head revision, or ``"unknown"``.

    Reads the revision recorded in the database via Alembic's migration context.
    Any failure -- the table missing, the database unreachable -- degrades to
    ``"unknown"`` so liveness reporting never raises.
    """
    try:
        with engine.connect() as connection:
            revision = MigrationContext.configure(connection).get_current_revision()
        return revision or _UNKNOWN_REVISION
    except Exception:
        return _UNKNOWN_REVISION


def _db_status(session_factory: sessionmaker[Session]) -> str:
    """Return ``"ok"`` if a trivial query succeeds, else ``"error"``."""
    try:
        session = session_factory()
        try:
            session.execute(text("SELECT 1"))
        finally:
            session.close()
    except Exception:
        return "error"
    return "ok"


def _build_notification_channels(
    settings: Settings, session_factory: sessionmaker[Session]
) -> list[NotificationChannel]:
    """Construct the notification channels from the stored settings row.

    The channels' transport configuration (SMTP server/credentials, webhook
    URLs) lives in the singleton ``notification_settings`` row, not in
    ``settings.monitoring`` -- only the per-send timeout is taken from there. The
    row may be absent or unreadable on a fresh/unmigrated database, so this is
    defensive: any failure degrades to no channels rather than aborting startup
    (mirroring the dispatcher, which also tolerates a degraded database).

    Channel transport configuration is read once here and is **not** hot-reloaded;
    editing SMTP/webhook settings in the UI takes effect on the next restart.
    Only the routing rules are re-read per poll cycle (by the dispatcher's
    default ``routing_rules_fn``). A channel is included only when its name is in
    the row's ``enabled_channels`` list, so an admin can disable a channel
    without clearing its configuration.
    """
    timeout = settings.monitoring.channel_send_timeout_seconds
    channels: list[NotificationChannel] = []
    try:
        with session_factory() as session:
            view = load_settings(session)
            enabled = set(view.enabled_channels)
            if "email" in enabled:
                smtp_config = build_smtp_config(
                    server=view.smtp_server or None,
                    port=view.smtp_port,
                    security=view.smtp_security,
                    username=view.smtp_username or None,
                    # The view masks the password; decrypt the stored secret at use
                    # for transport. It is never logged or echoed.
                    password=resolve_smtp_password(session),
                    from_address=view.smtp_from_address or None,
                    recipients=view.smtp_recipients,
                )
                if smtp_config is not None:
                    channels.append(
                        SMTPChannel(smtp_config, send_timeout_seconds=timeout)
                    )
            if "webhook" in enabled:
                # Built from decrypted stored URLs at use; each URL is validated
                # by the channel's outbound-URL seam at send time.
                webhook = build_webhook_channel(session, send_timeout_seconds=timeout)
                if webhook is not None:
                    channels.append(webhook)
    except Exception:
        logger.exception("failed to read notification settings; channels disabled")
        return channels
    return channels


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Wire the process on startup and tear it down on shutdown.

    Order matters: configure logging first so the rest of startup is observable,
    then build the engine and session factory and install the factory before any
    request can be served, ensure the local API token exists, probe component
    versions, and publish the shared :class:`AppContext`.
    """
    settings: Settings = app.state.settings
    # Provenance of the settings (which leaves the environment determined),
    # carried alongside settings on app state. Absent when the app was built
    # without provenance (tests, alternative wiring): the read-only settings UI
    # then shows no per-field chips and falls back to its banner.
    env_overrides: frozenset[str] = getattr(app.state, "env_overrides", frozenset())

    configure_logging(settings)

    engine = create_db_engine(settings.database.url)
    session_factory = create_session_factory(engine)
    set_session_factory(session_factory)

    ensure_local_token(settings)

    # Install the at-rest encryption key provider before anything reads or writes
    # a stored secret. Selection (OS keystore vs the restricted key file) happens
    # here so a missing Secret Service degrades to the file fallback rather than
    # failing on first secret access. The key is never logged.
    set_key_provider(build_key_provider(settings.secrets, settings.paths.data_dir))

    # Always construct the supervisor so the shared HTTP client and the manual
    # capture path are available; only start background loops when configured to.
    # The disk-space gate is built from the storage settings and injected so the
    # capture loop pauses (never deletes) when a storage volume runs low.
    disk_monitor = DiskSpaceMonitor(
        low_watermark_bytes=settings.storage.low_watermark_bytes,
        low_watermark_percent=settings.storage.low_watermark_percent,
        resume_watermark_bytes=settings.storage.resume_watermark_bytes,
        resume_watermark_percent=settings.storage.resume_watermark_percent,
        check_interval_seconds=settings.storage.check_interval_seconds,
    )
    supervisor = CaptureSupervisor(settings, session_factory, disk_monitor=disk_monitor)
    # Wire the real event-source factory after the supervisor exists: it reuses
    # the supervisor's shared (camera-allowlisted) HTTP client, resolved ffmpeg
    # binary, and camera/credential loaders, none of which exist until the
    # supervisor is constructed. Per-project event listeners consume it.
    supervisor.set_event_source_factory(
        EventListenerFactory(
            supervisor.http_client,
            supervisor._load_camera,
            ffmpeg_binary=supervisor.ffmpeg_binary,
            default_credentials_loader=supervisor._load_default_credentials,
        )
    )

    # The render worker and scheduler mirror the supervisor: long-lived asyncio
    # tasks with bulletproof, idempotent stop(). The worker is always constructed
    # so the render API (validate, trigger, cancel) has it available; the
    # scheduler enqueues recurring renders into it.
    render_queue = RenderQueue(settings, session_factory)
    render_scheduler = RenderScheduler(settings, session_factory, render_queue)

    # The notification dispatcher mirrors the other background tasks: a long-lived
    # asyncio task with a bulletproof, idempotent stop(). Channels are built once
    # from the stored settings row (transport config is not hot-reloaded); the
    # routing rules are re-read each poll cycle via the dispatcher's default
    # routing_rules_fn. The dispatcher is always constructed so it can be driven
    # in tests, but its poll loop starts only when configured to.
    notification_channels = _build_notification_channels(settings, session_factory)
    notification_dispatcher = NotificationDispatcher(
        session_factory,
        channels=notification_channels,
        settings=settings.monitoring,
    )

    # Resolve the ffmpeg binary once for the process. When frozen with no bundled
    # ffmpeg (and no explicit knob) this raises and aborts startup -- a packaged
    # release that cannot render is a defect that must surface, not be hidden.
    # The version is probed against this resolved binary so the reported version
    # is the build the application will actually encode with.
    ffmpeg_path = resolve_ffmpeg_binary(settings)

    context = AppContext(
        settings=settings,
        env_overrides=env_overrides,
        db_engine=engine,
        session_factory=session_factory,
        logger=logger,
        app_version=__version__,
        ffmpeg_version=probe_ffmpeg_version(ffmpeg_path),
        ffmpeg_path=ffmpeg_path,
        capture_supervisor=supervisor,
        render_queue=render_queue,
        render_scheduler=render_scheduler,
        notification_dispatcher=notification_dispatcher,
        # Capture the config/env SSRF subnet baseline before any admin (DB)
        # subnets are merged in; the merge below rebinds the live list to the
        # union of this and the stored admin list.
        ssrf_config_subnets=tuple(settings.ssrf.allowed_private_subnets),
    )
    set_context(context)

    # Merge any admin-managed SSRF subnets (stored in the DB, editable from the
    # web UI) on top of the config baseline so the camera/scan guard honours them
    # without a restart. Best-effort: a missing or unreadable ssrf_settings row
    # (a fresh or briefly-locked DB) must never crash boot -- the config baseline
    # already stands and the request-time save will re-apply.
    try:
        from .security.ssrf_settings_service import apply_to_runtime

        with session_scope(session_factory) as ssrf_session:
            apply_to_runtime(ssrf_session)
    except Exception:  # noqa: BLE001 -- boot must survive a settings-merge failure.
        logger.warning(
            "Could not merge stored SSRF subnets; using config baseline only",
            exc_info=True,
        )

    try:
        if settings.capture.autostart:
            await supervisor.start()
        if settings.render.autostart:
            await render_queue.start()
            await render_scheduler.start()
        if settings.monitoring.autostart:
            await notification_dispatcher.start()
        yield
    finally:
        # Each stop() is bulletproof and idempotent: it cancels its task(s) and
        # awaits them so nothing leaks onto the closing loop. Order matters: stop
        # the scheduler first so it enqueues no more, then the worker (cancelling
        # an in-flight render kills the ffmpeg child, removes the partial output,
        # and records the job failed via a synchronous write -- which needs the
        # engine), then the capture supervisor, and only then dispose the engine.
        await render_scheduler.stop()
        await render_queue.stop()
        # Stop the dispatcher alongside the other background tasks and before
        # dispose(): its stop() cancels the poll loop and every in-flight send so
        # it never blocks shutdown, and it records delivery-failure events, so it
        # needs the engine still alive.
        await notification_dispatcher.stop()
        await supervisor.stop()
        dispose()


def create_app(
    settings: Settings, env_overrides: frozenset[str] = frozenset()
) -> FastAPI:
    """Construct and return the FastAPI application for the given settings.

    The returned application carries the settings on its state and defers all
    side-effecting wiring to its lifespan, so constructing it is free of I/O.

    :param env_overrides: dotted leaf paths whose effective value came from the
        environment (see :func:`config.load_settings_with_provenance`). Defaults
        to empty, in which case the read-only settings UI shows no per-field
        environment chips.
    """
    app = FastAPI(title="Timelapse Manager", version=__version__, lifespan=_lifespan)
    app.state.settings = settings
    app.state.env_overrides = env_overrides

    @app.get("/healthz")
    def healthz(response: Response) -> dict[str, Any]:
        """Liveness probe. Unauthenticated; never raises.

        Reports the application and ffmpeg versions, the resolved ffmpeg path, a
        live database status, and the current Alembic revision. Each probe is
        guarded independently so a single failing component degrades its own
        field rather than the route.

        The HTTP status reflects readiness: 200 when the database probe reports
        ``ok``, and 503 otherwise (including when the application context is not
        yet installed). This lets a reverse proxy or load balancer drain or
        route around an unhealthy instance from the status code alone. The JSON
        body is identical in both cases.
        """
        app_version = __version__
        ffmpeg_version = "unavailable"
        ffmpeg_path = "unavailable"
        db_status = "error"
        alembic_revision = _UNKNOWN_REVISION
        try:
            context = get_context()
            app_version = context.app_version
            ffmpeg_version = context.ffmpeg_version
            ffmpeg_path = context.ffmpeg_path
            db_status = _db_status(context.session_factory)
            alembic_revision = _current_alembic_revision(context.db_engine)
        except Exception:
            # Context not yet installed or unexpectedly unavailable; report the
            # safe defaults above rather than failing the liveness check.
            pass
        if db_status != "ok":
            response.status_code = 503
        return {
            "app_version": app_version,
            "ffmpeg_version": ffmpeg_version,
            "ffmpeg_path": ffmpeg_path,
            "db_status": db_status,
            "alembic_revision": alembic_revision,
        }

    mount_api(app)
    mount_web(app)
    return app


def create_app_from_env() -> FastAPI:
    """Zero-argument ASGI factory for ``uvicorn --factory``.

    Resolves :class:`Settings` from the environment (and the optional
    ``TLM_CONFIG`` file), configures logging, and builds the application -- the
    same settings and logging setup the foreground serve path uses, so behaviour
    matches. It does not start a server, generate TLS certificates, or bind any
    port: under ``--factory`` uvicorn owns the server, and this entry point is
    used where TLS is terminated by a proxy in front of a plain-HTTP listener.

    The settings loader is imported function-locally and aliased because this
    module already binds the name ``load_settings`` to the notification-settings
    loader; the alias keeps that binding intact.
    """
    from .config.loader import (
        load_settings_with_provenance as load_env_settings_with_provenance,
    )

    settings, env_overrides = load_env_settings_with_provenance()
    configure_logging(settings)
    return create_app(settings, env_overrides)
