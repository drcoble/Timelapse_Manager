"""ASGI application factory.

Builds the FastAPI application from resolved settings. The factory itself is a
pure function with no import-time or construction-time side effects -- it only
declares routes and a lifespan. All process wiring (logging, engine, session
factory, local token, shared context) happens inside the lifespan when the
application actually starts serving, so the factory stays cheap to call from
tests and from each control surface.
"""

from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import logging
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio.to_thread
from alembic.migration import MigrationContext
from fastapi import FastAPI, Request, Response
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from . import __version__
from .api import mount_api
from .capture import CaptureSupervisor
from .capture.event_listener import EventListenerFactory
from .config import Settings
from .db.engine import (
    REQUEST_THREADPOOL_TOKENS,
    capture_executor_workers,
    create_db_engine,
)
from .db.session import create_session_factory, session_scope, set_session_factory
from .executors import (
    JOBS_THREAD_NAME_PREFIX,
    CountingThreadPoolExecutor,
    create_jobs_executor,
)
from .ffmpeg_pin import resolve_ffmpeg_binary
from .logging import configure_logging
from .monitoring import NotificationDispatcher, readiness
from .monitoring.channels import StoredChannelsProvider
from .monitoring.diagnostics import (
    LogLevelOverrides,
    LoopDbWatch,
    LoopLagMonitor,
    dump_tasks,
    install_task_dump_signal,
    register_executor,
    remove_task_dump_signal,
    unregister_executor,
)
from .render import RenderQueue, RenderScheduler
from .runtime import AppContext, dispose, get_context, set_context
from .security.crypto import set_key_provider
from .security.key_integrity import ensure_key_integrity
from .security.token import (
    TokenFileError,
    _extract_bearer,
    load_token_caches,
    peer_allowed,
    verify_token,
)
from .storage import DiskSpaceMonitor
from .version import probe_ffmpeg_version
from .web import mount_web

logger = logging.getLogger(__name__)

_UNKNOWN_REVISION = "unknown"

# Thread-name prefix of the capture executor's workers, so a log record's thread
# name, ``GET /api/v1/system/debug/threads`` and the SIGUSR2 dump's ident-to-name
# map show that a hop was capture work. A SIGUSR1 (faulthandler) dump prints only
# thread idents, not these names, so read it next to that map.
CAPTURE_THREAD_NAME_PREFIX = "tlm-capture"

# Peers that get the full ``/healthz`` body without a token: this host only.
# Judged on the raw TCP peer with no proxy trusted, so a request relayed by a
# proxy (it sends forwarded headers) never counts as local (#67 S19).
_LOOPBACK_NETWORKS = ("127.0.0.0/8", "::1/128")


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


def _merge_stored_ssrf_subnets(session_factory: sessionmaker[Session]) -> None:
    """Merge the admin-managed SSRF subnets (stored in the DB) onto the runtime.

    Synchronous: the lifespan runs it in a worker thread (#66 L4), so a read
    that waits out SQLite's busy timeout never blocks the event loop. Raises
    whatever the read raises; the caller decides that boot survives it.
    """
    from .security.ssrf_settings_service import apply_to_runtime

    with session_scope(session_factory) as ssrf_session:
        apply_to_runtime(ssrf_session)


def _normalize_stored_camera_uris(session_factory: sessionmaker[Session]) -> int:
    """Move ``user:pass@`` out of stored camera URIs; return how many were moved.

    Synchronous: the lifespan runs it in a worker thread, before the capture
    supervisor starts, so a camera is never captured with a login still in its
    URI. It runs here, under the real key provider, rather than as an Alembic
    step (the package upgrade scripts swallow migration failures). Idempotent;
    see :func:`~timelapse_manager.services.cameras.normalize_stored_camera_uris`.
    """
    from .services.cameras import normalize_stored_camera_uris

    with session_scope(session_factory) as camera_session:
        return normalize_stored_camera_uris(camera_session)


def _install_thread_dump_signal() -> None:
    """Make ``SIGUSR1`` dump every thread's Python stack to stderr (the journal).

    A diagnostic for stalls like issue #6: ``kill -USR1 <pid>`` shows where each
    thread is without restarting the service. Before 0.1.1 the signal had no
    handler and terminated the process. Not available on Windows; best effort
    where stderr has no file descriptor (e.g. under a test runner's capture).
    """
    # Also print every thread's traceback on a fatal signal (SIGSEGV, SIGABRT,
    # ...) -- the PyInstaller bootloader ignores PYTHONFAULTHANDLER (#7).
    with contextlib.suppress(Exception):
        faulthandler.enable(all_threads=True)
    sig = getattr(signal, "SIGUSR1", None)
    if sig is None:
        return
    try:
        faulthandler.register(sig, all_threads=True)
    except Exception:  # noqa: BLE001 - diagnostics must never block startup
        logger.warning("could not install the SIGUSR1 thread-dump handler")


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
    _install_thread_dump_signal()

    # The connection pool is sized to this threadpool (see db.engine), so pin it
    # rather than depend on anyio's default staying what the pool assumes.
    anyio.to_thread.current_default_thread_limiter().total_tokens = (
        REQUEST_THREADPOOL_TOKENS
    )
    # Resolve the capture executor's size once: the pool below and the executor
    # built before the supervisor must agree on it.
    capture_workers = capture_executor_workers(settings.capture.executor_workers)
    engine = create_db_engine(
        settings.database.url,
        jobs_executor_workers=settings.render.jobs_executor_workers,
        capture_workers=capture_workers,
    )
    session_factory = create_session_factory(engine)
    set_session_factory(session_factory)
    # Debug builds report any request that queries on the event loop (#46-#50).
    loop_db_watch: LoopDbWatch | None = None
    if settings.observability.debug_endpoints_enabled:
        loop_db_watch = LoopDbWatch(engine)
        loop_db_watch.install()

    # Create (O_EXCL, 0600) or validate the API token files before any listener
    # serves, and keep them cached for the bearer dependencies (#67 S2).
    local_token, metrics_token = load_token_caches(settings)

    # Install the at-rest encryption key provider before anything reads or writes
    # a stored secret. Selection (OS keystore vs the restricted key file) happens
    # here so a missing Secret Service degrades to the file fallback rather than
    # failing on first secret access. The key is checked against the fingerprint
    # recorded in ``secrets_meta`` first: a missing or different key refuses to
    # start rather than minting a new key over data it cannot read (#67 S8). The
    # key is never logged.
    set_key_provider(await ensure_key_integrity(settings, session_factory))

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
    # Every blocking capture hop (database reads and writes, frame writes, the
    # event listeners' camera loads) runs on a dedicated executor (#66 C3), so a
    # capture -- the daily anchor above all -- never queues behind the
    # scheduler, the dispatcher or the render queue's short hops on the loop's
    # default executor. It starts no thread until work is submitted; the
    # teardown below shuts it down after the supervisor stops.
    capture_executor = CountingThreadPoolExecutor(
        max_workers=capture_workers, thread_name_prefix=CAPTURE_THREAD_NAME_PREFIX
    )
    supervisor = CaptureSupervisor(
        settings,
        session_factory,
        disk_monitor=disk_monitor,
        executor=capture_executor,
    )
    # Wire the real event-source factory after the supervisor exists: it reuses
    # the supervisor's shared (camera-allowlisted) HTTP client, resolved ffmpeg
    # binary, camera-session registry and executor, none of which exist until
    # the supervisor is constructed. Through the registry a listener shares the
    # captures' cached, once-decrypted credentials, and an undecryptable one
    # fails with the named local error (#153). Per-project listeners consume it.
    supervisor.set_event_source_factory(
        EventListenerFactory(
            supervisor.client_for("events"),
            supervisor._load_camera,
            ffmpeg_binary=supervisor.ffmpeg_binary,
            default_credentials_loader=supervisor._load_default_credentials,
            executor=capture_executor,
            camera_sessions=supervisor.camera_sessions,
            pinned_client_for=lambda pem: supervisor.client_for("events", pem),
        )
    )

    # The render worker and scheduler mirror the supervisor: long-lived asyncio
    # tasks with bulletproof, idempotent stop(). The worker is always constructed
    # so the render API (validate, trigger, cancel) has it available; the
    # scheduler enqueues recurring renders into it.
    #
    # Long render-side jobs (preparation, export zips, post-render copies and
    # prunes, the codec backfill scan) run on their own small executor (#66 C3),
    # so they neither starve capture on the loop's default executor nor hold
    # request threads. It needs no running loop and starts no thread until work
    # is submitted; the teardown below shuts it down.
    jobs_executor = create_jobs_executor(settings.render.jobs_executor_workers)
    render_queue = RenderQueue(settings, session_factory, jobs_executor=jobs_executor)
    render_scheduler = RenderScheduler(settings, session_factory, render_queue)

    # The notification dispatcher mirrors the other background tasks: a long-lived
    # asyncio task with a bulletproof, idempotent stop(). Its channels come from
    # the stored settings row through the provider, built in a worker thread on
    # the first poll and rebuilt whenever that row's transport settings change,
    # so edits apply without a restart (#67 R8). The routing rules are re-read
    # each poll cycle via the dispatcher's default routing_rules_fn. The
    # dispatcher is always constructed so it can be driven in tests, but its
    # poll loop starts only when configured to.
    notification_dispatcher = NotificationDispatcher(
        session_factory,
        channels=(),
        settings=settings.monitoring,
        channels_provider=StoredChannelsProvider(
            send_timeout_seconds=settings.monitoring.channel_send_timeout_seconds
        ),
    )

    # Resolve the ffmpeg binary once for the process. When frozen with no bundled
    # ffmpeg (and no explicit knob) this raises and aborts startup -- a packaged
    # release that cannot render is a defect that must surface, not be hidden.
    # The version is probed against this resolved binary so the reported version
    # is the build the application will actually encode with.
    ffmpeg_path = resolve_ffmpeg_binary(settings)
    # ``ffmpeg -version`` is a subprocess: run it off the loop like any other
    # blocking startup work (#66 L4).
    ffmpeg_version = await asyncio.to_thread(probe_ffmpeg_version, ffmpeg_path)

    # In-process diagnostics (#7). The lag ticker and the log-level override
    # timer live on this (the server's) loop.
    loop = asyncio.get_running_loop()
    loop_lag_monitor = LoopLagMonitor()
    log_level_overrides = LogLevelOverrides(loop)

    context = AppContext(
        settings=settings,
        env_overrides=env_overrides,
        db_engine=engine,
        session_factory=session_factory,
        logger=logger,
        app_version=__version__,
        ffmpeg_version=ffmpeg_version,
        ffmpeg_path=ffmpeg_path,
        capture_supervisor=supervisor,
        render_queue=render_queue,
        render_scheduler=render_scheduler,
        notification_dispatcher=notification_dispatcher,
        # Capture the config/env SSRF subnet baseline before any admin (DB)
        # subnets are merged in; the merge below rebinds the live list to the
        # union of this and the stored admin list.
        ssrf_config_subnets=tuple(settings.ssrf.allowed_private_subnets),
        loop_lag_monitor=loop_lag_monitor,
        log_level_overrides=log_level_overrides,
        local_token=local_token,
        metrics_token=metrics_token,
        jobs_executor=jobs_executor,
    )
    set_context(context)

    # Merge any admin-managed SSRF subnets (stored in the DB, editable from the
    # web UI) on top of the config baseline so the camera/scan guard honours them
    # without a restart. Best-effort: a missing or unreadable ssrf_settings row
    # (a fresh or briefly-locked DB) must never crash boot -- the config baseline
    # already stands and the request-time save will re-apply. The read runs in a
    # worker thread so a locked database never stalls the loop (#66 L4).
    try:
        await asyncio.to_thread(_merge_stored_ssrf_subnets, session_factory)
    except Exception:  # noqa: BLE001 -- boot must survive a settings-merge failure.
        logger.warning(
            "Could not merge stored SSRF subnets; using config baseline only",
            exc_info=True,
        )

    # Move any login stored inside a camera address or URI into the encrypted
    # credential document (#69 CAM-S2). Best-effort and idempotent: a failure is
    # logged and retried at the next start, and never blocks boot. Only a count
    # is logged. Before the supervisor starts.
    try:
        moved = await asyncio.to_thread(_normalize_stored_camera_uris, session_factory)
        logger.info("camera URI userinfo normalised for %d camera(s)", moved)
    except Exception as exc:  # noqa: BLE001 -- boot must survive this pass.
        # The class name only: a database error can echo the row's values.
        logger.warning(
            "Could not normalise stored camera URIs (%s); will retry at the next start",
            type(exc).__name__,
        )

    # SIGUSR2 logs every asyncio task (#7). Installed on the running loop, so
    # it only works from the main thread; skipped (not raised) elsewhere.
    task_dump_installed = install_task_dump_signal(
        loop, lambda: dump_tasks(loop, supervisor)
    )
    loop_lag_monitor.start()
    # The debug endpoint and the SIGUSR2 dump report every named pool next to
    # the default executor (#161); the teardown below unregisters them.
    register_executor(CAPTURE_THREAD_NAME_PREFIX, capture_executor)
    register_executor(JOBS_THREAD_NAME_PREFIX, jobs_executor)

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
        # and records the job failed in a worker thread that stop() awaits --
        # which needs the engine), then the capture supervisor, and only then
        # dispose the engine.
        await render_scheduler.stop()
        await render_queue.stop()
        # The worker has stopped, so nothing submits long jobs any more. Drop
        # queued ones and do not wait: a running thread (an export zip cannot be
        # interrupted) finishes on its own, as it would on the default executor.
        jobs_executor.shutdown(wait=False, cancel_futures=True)
        # Stop the dispatcher alongside the other background tasks and before
        # dispose(): its stop() cancels the poll loop and every in-flight send so
        # it never blocks shutdown, and it records delivery-failure events, so it
        # needs the engine still alive.
        await notification_dispatcher.stop()
        await supervisor.stop()
        # The supervisor has stopped, so nothing submits capture hops any more.
        # Drop queued ones and do not wait, like the jobs executor: a hop still
        # running (a frame write, a write waiting out SQLite's busy timeout)
        # finishes on its own, and a wedged one cannot hold up shutdown.
        capture_executor.shutdown(wait=False, cancel_futures=True)
        unregister_executor(CAPTURE_THREAD_NAME_PREFIX, capture_executor)
        unregister_executor(JOBS_THREAD_NAME_PREFIX, jobs_executor)
        await loop_lag_monitor.stop()
        await log_level_overrides.stop()
        if task_dump_installed:
            remove_task_dump_signal(loop)
        if loop_db_watch is not None:
            loop_db_watch.remove()
        dispose()


def _healthz_full_view(request: Request) -> bool:
    """Whether this ``/healthz`` caller may see the full body (#67 S19).

    True for a loopback TCP peer that sends no forwarded headers (the upgrade
    script, the CLI, a probe on the host), and, while the metrics endpoint is
    enabled, for a valid metrics token from a peer in
    ``observability.metrics_allowed_clients``. The token is compared only for
    such a peer, so the response shape is no token oracle for anyone else, and
    the token file is never created from here. Never raises and never logs a
    refusal: the OpenNMS poller hits this every few minutes and is entitled to
    the short body.
    """
    try:
        if peer_allowed(
            request, _LOOPBACK_NETWORKS, trusted_proxies=(), require_https=False
        ):
            return True
        received = _extract_bearer(request)
        # ``compare_digest`` raises on non-ASCII text; such a token is wrong anyway.
        if not received or not received.isascii():
            return False
        context = get_context()
        settings = context.settings
        if not settings.observability.metrics_enabled or context.metrics_token is None:
            return False
        if not peer_allowed(
            request,
            settings.observability.metrics_allowed_clients,
            trusted_proxies=settings.server.trusted_proxies,
            require_https=False,
        ):
            return False
        return verify_token(received, context.metrics_token.get())
    except TokenFileError:
        # The metrics token file is unusable; startup already logged why.
        return False
    except Exception:
        logger.exception("healthz: could not decide the caller's view")
        return False


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
    def healthz(request: Request, response: Response) -> dict[str, Any]:
        """Readiness probe. Unauthenticated; never raises (#67 S19, R5).

        ``status`` is ``ok``, ``degraded`` or ``failed`` (see
        :func:`monitoring.readiness.evaluate_readiness`). The HTTP status is 503
        for ``failed`` (the database probe failed, a started background loop
        died, or the application context is not installed yet) and 200
        otherwise, so a status-code poller sees no change for ``degraded``
        (owner decision D4).

        Who sees what:

        * **Anyone** gets ``{"status": ..., "db_status": "ok"|"error"}``. It
          keeps the compact ``"db_status":"ok"`` the OpenNMS pollers match, and
          discloses no version, path or component map.
        * **This host** (a loopback TCP peer that relays for nobody) and a
          caller presenting the **metrics token** from a peer in
          ``observability.metrics_allowed_clients`` also get ``app_version``,
          ``ffmpeg_version``, ``ffmpeg_path``, ``alembic_revision``,
          ``components`` and ``checks``. ``tlm-upgrade.sh`` polls over loopback
          and keeps matching ``"app_version":"<ver>"``.

        A wrong, missing or unusable token is never an error here: the caller
        simply gets the short body.
        """
        app_version = __version__
        ffmpeg_version = "unavailable"
        ffmpeg_path = "unavailable"
        db_status = "error"
        alembic_revision = _UNKNOWN_REVISION
        components: dict[str, str] = {}
        result: readiness.Readiness | None = None
        try:
            context = get_context()
            app_version = context.app_version
            ffmpeg_version = context.ffmpeg_version
            ffmpeg_path = context.ffmpeg_path
            db_status = readiness.db_status(context.session_factory)
            alembic_revision = _current_alembic_revision(context.db_engine)
            components = readiness.component_statuses(context)
            result = readiness.evaluate_readiness(
                context, database=db_status, components=components
            )
        except Exception:
            # Context not yet installed or unexpectedly unavailable; report the
            # safe defaults above rather than failing the probe.
            pass
        status = result.status if result is not None else "failed"
        response.status_code = result.http_status if result is not None else 503
        if not _healthz_full_view(request):
            return {"status": status, "db_status": db_status}
        checks = (
            {name: check.as_dict() for name, check in result.checks.items()}
            if result is not None
            else {}
        )
        return {
            "status": status,
            "app_version": app_version,
            "ffmpeg_version": ffmpeg_version,
            "ffmpeg_path": ffmpeg_path,
            "db_status": db_status,
            "alembic_revision": alembic_revision,
            "components": components,
            "checks": checks,
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

    The settings loader is imported function-locally and aliased so it is not
    confused with the notification-settings ``load_settings``.
    """
    from .config.loader import (
        load_settings_with_provenance as load_env_settings_with_provenance,
    )

    settings, env_overrides = load_env_settings_with_provenance()
    configure_logging(settings)
    return create_app(settings, env_overrides)
