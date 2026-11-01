"""Foreground service runner.

Loads settings, builds the application, and serves it with Uvicorn over both
HTTPS and HTTP from a single process:

* **HTTPS** binds on ``server.https_port`` using a certificate ensured by
  :func:`timelapse_manager.service.tls.ensure_tls_cert` (an explicit pair, or a
  self-signed one generated into the data directory). This is the only place
  socket-level TLS lives; the scheme/redirect *behaviour* is in-app middleware.
* **HTTP** binds on ``server.http_port`` serving the *same* application. When
  HTTP-to-HTTPS redirection is enabled the in-app middleware turns those requests
  into ``308`` redirects to HTTPS, so the port exists only to bounce clients. When
  redirection is disabled the HTTP listener is bound to loopback only, so no
  plaintext port is exposed to the network.

One shutdown coordinator owns ``SIGINT``/``SIGTERM`` for both listeners (#67
R4). The first signal asks both servers to exit at once, so they drain their
connections concurrently, each bounded by ``server.shutdown_grace_seconds``; a
second signal forces the exit. The HTTPS server owns the lifespan, and its
teardown waits for the HTTP listener to finish draining, so no request is still
running when the engine and the background loops go away. The single shared
application means the lifespan (engine, context, background loops) runs once
regardless of how many listeners front it.

When hardware encoding is configured, the service checks at startup that it can
open the GPU device node and logs an error naming the systemd drop-in when it
cannot (#67 R15). That is a diagnostic only: render behaviour is unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import socket
import ssl
from collections.abc import Awaitable, Callable, Generator, Sequence
from pathlib import Path
from typing import TypedDict

import uvicorn
from fastapi import FastAPI

from ..app import create_app
from ..config import Settings, load_settings_with_provenance
from ..config.settings import RenderSettings
from ..encode.hwaccel import HWACCEL_APIS
from ..security.key_integrity import EXIT_KEY_INTEGRITY, key_integrity_refused
from .tls import ensure_tls_cert

# Loopback address the HTTP listener binds to when redirection is disabled, so a
# plaintext port is never exposed beyond the host.
_LOOPBACK = "127.0.0.1"

# The signals the shutdown coordinator handles: systemd stops the unit with
# SIGTERM (``KillSignal``), and Ctrl-C in a foreground run sends SIGINT.
_SHUTDOWN_SIGNALS: tuple[signal.Signals, ...] = (signal.SIGTERM, signal.SIGINT)

# Extra time the lifespan teardown gives the HTTP listener beyond its own grace
# before it goes ahead regardless. The HTTP drain is already bounded by the
# grace; this only guards against a listener that never reports finishing.
_PEER_DRAIN_MARGIN_SECONDS = 5.0

# Exit status when the application fails to start (the value Uvicorn's own
# ``run`` uses), so a service manager restarts it (#177).
_EXIT_STARTUP_FAILURE = 3

# How long the lifespan teardown waits for request tasks a listener cancelled at
# the end of its grace to finish unwinding, so a cancelled request is not still
# running (in its worker thread) when the engine is disposed.
_CANCELLED_REQUEST_SETTLE_SECONDS = 2.0

# The render node VA-API and Quick Sync use when ``render.hwaccel_device`` names
# none, and the NVIDIA control node every NVENC encode opens.
_DEFAULT_RENDER_NODE = "/dev/dri/renderD128"
_NVIDIA_CONTROL_NODE = "/dev/nvidiactl"

# Where the opt-in systemd drop-in for GPU access goes (#67 R15). install.sh
# re-renders the unit on upgrade but leaves the ``.d`` directory alone.
_HWACCEL_DROP_IN = "/etc/systemd/system/timelapse-manager.service.d/hwaccel.conf"

logger = logging.getLogger(__name__)


def _floor_tls_minimum_version(
    _config: uvicorn.Config,
    default_factory: Callable[[], ssl.SSLContext],
) -> ssl.SSLContext:
    """Build the HTTPS listener's SSL context with a TLS 1.2 floor.

    Uvicorn invokes this while loading the HTTPS listener. We take the context it
    would otherwise have built and raise its minimum protocol version so that
    handshakes offering only the obsolete TLS 1.0/1.1 are refused outright.
    """
    context = default_factory()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class _ConnectionLimits(TypedDict):
    """Uvicorn keyword arguments that bound per-listener connection resources."""

    limit_concurrency: int | None
    timeout_keep_alive: int
    limit_max_requests: int | None
    h11_max_incomplete_event_size: int | None
    timeout_graceful_shutdown: int | None


def _apply_connection_limits(settings: Settings) -> _ConnectionLimits:
    """Return the Uvicorn keyword arguments that bound connection resources.

    These caps blunt slowloris and connection-exhaustion attacks: a ceiling on
    concurrent connections, a short keep-alive timeout that reclaims idle
    sockets, an optional worker request-recycling limit, and a tight bound on the
    size of a not-yet-complete request event. They apply to both listeners.

    ``timeout_graceful_shutdown`` bounds how long a stopping listener waits for
    open connections (a long download, a video stream) before it cancels them,
    so a stop always finishes inside the unit's ``TimeoutStopSec`` (#67 R4).
    """
    return {
        "limit_concurrency": settings.server.limit_concurrency,
        "timeout_keep_alive": settings.server.timeout_keep_alive_seconds,
        "limit_max_requests": settings.server.limit_max_requests,
        "h11_max_incomplete_event_size": (
            settings.server.h11_max_incomplete_event_size
        ),
        "timeout_graceful_shutdown": settings.server.shutdown_grace_seconds,
    }


def _build_https_config(
    app: FastAPI, settings: Settings, cert_path: Path, key_path: Path
) -> uvicorn.Config:
    """Build the Uvicorn config for the TLS listener with hardened limits.

    The SSL context is materialized through :func:`_floor_tls_minimum_version` so
    the listener refuses pre-TLS-1.2 handshakes.

    ``proxy_headers=False`` (both listeners): Uvicorn would otherwise rewrite the
    client address and scheme from ``X-Forwarded-*`` for a loopback peer, so any
    local process could pose as another client to the login throttle and the
    local-token peer policy (#67 S1). Forwarded headers are honoured only by
    ``EffectiveSchemeMiddleware``, and only from ``server.trusted_proxies``; a
    same-host reverse proxy must therefore be listed there.
    """
    return uvicorn.Config(
        app,
        host=settings.server.bind_address,
        port=settings.server.https_port,
        ssl_certfile=str(cert_path),
        ssl_keyfile=str(key_path),
        ssl_context_factory=_floor_tls_minimum_version,
        log_config=None,
        proxy_headers=False,
        **_apply_connection_limits(settings),
    )


def _build_http_config(app: FastAPI, settings: Settings) -> uvicorn.Config:
    """Build the Uvicorn config for the plaintext listener with hardened limits.

    When redirection is disabled the plaintext port must not be reachable off the
    host, so it is confined to loopback rather than the public bind address. The
    shared application's lifespan runs only under the HTTPS server, so this
    listener runs with ``lifespan="off"`` to avoid starting the engine, context,
    and background loops twice.
    """
    http_host = (
        settings.server.bind_address
        if settings.server.redirect_http_to_https
        else _LOOPBACK
    )
    return uvicorn.Config(
        app,
        host=http_host,
        port=settings.server.http_port,
        lifespan="off",
        log_config=None,
        proxy_headers=False,
        **_apply_connection_limits(settings),
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse service runner arguments."""
    parser = argparse.ArgumentParser(
        prog="timelapse-daemon",
        description="Run the Timelapse Manager service in the foreground.",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="Path to a configuration file (YAML or JSON).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Service entry point: load settings, build the app, and serve it.

    Serves HTTPS on ``server.https_port`` (with the ensured certificate) and HTTP
    on ``server.http_port`` from the same application. With redirection enabled
    the HTTP listener binds the configured address and the in-app middleware
    redirects to HTTPS; with redirection disabled the HTTP listener is confined to
    loopback. ``SIGTERM``/``SIGINT`` stop both listeners together (see
    :func:`_serve`); the lifespan disposes the engine on the way down.
    """
    args = _parse_args(argv)
    settings, env_overrides = load_settings_with_provenance(args.config)
    serve(settings, env_overrides)


def serve(settings: Settings, env_overrides: frozenset[str] = frozenset()) -> None:
    """Run the service in the foreground until interrupted.

    The single public entry into the serve loop, shared by the daemon entry
    point and the frozen ``timelapse-manager run`` subcommand so the foreground
    serve logic lives in exactly one place. Blocks on :func:`asyncio.run` until
    a stop signal has drained both listeners and run the lifespan teardown.

    :param env_overrides: dotted settings leaf paths the environment determined
        (see :func:`config.load_settings_with_provenance`), forwarded to the app
        so the read-only settings UI can mark environment-controlled values.
        Defaults to empty when a caller has no provenance to forward.

    The schema is brought to head before the listeners start: a packaged
    deployment has no separate "migrate first" step, so the service must
    initialize its own database on startup. This is idempotent on an
    already-migrated database.

    Once migrated, every project's stored ``storage_path`` is checked against
    the storage policy (:func:`check_storage_paths`). That only logs; it never
    changes a row or stops startup.
    """
    from ..db.migrate import apply_migrations

    apply_migrations(settings)
    check_storage_paths(settings)
    asyncio.run(_serve(settings, env_overrides))


def check_storage_paths(settings: Settings) -> None:
    """Warn about projects whose stored ``storage_path`` breaks the policy (#67 S5).

    Synchronous and run before the event loop starts, on its own short-lived
    engine. Rows are never changed: a flagged project keeps capturing into, and
    rendering from, its current directory. Any failure of the check itself is
    logged and swallowed, because a warning-only scan must never keep the
    service from starting.
    """
    from ..db.engine import create_db_engine
    from ..db.session import create_session_factory, session_scope
    from ..services.projects import warn_on_storage_path_policy

    try:
        engine = create_db_engine(settings.database.url)
        try:
            with session_scope(create_session_factory(engine)) as session:
                warn_on_storage_path_policy(session, settings)
        finally:
            engine.dispose()
    except Exception as exc:  # noqa: BLE001 - a warning-only check never blocks startup
        logger.warning(
            "storage_path policy: startup check skipped (%s)", type(exc).__name__
        )


def hwaccel_device_nodes(render: RenderSettings) -> list[str]:
    """Return the device nodes a hardware encode with ``render`` will open.

    Empty when hardware encoding is off or ``hwaccel_api`` is not a recognised
    API, because then every render is software and needs no device:

    * ``vaapi``: ``hwaccel_device`` (a render-node path), else
      ``/dev/dri/renderD128``;
    * ``qsv``: ``hwaccel_device`` when it is a path, else
      ``/dev/dri/renderD128``;
    * ``nvenc``: ``/dev/nvidiactl`` and ``/dev/nvidia<N>``, where ``N`` is the
      GPU index in ``hwaccel_device`` (``0`` when unset).
    """
    if not render.hwaccel_enabled or render.hwaccel_api not in HWACCEL_APIS:
        return []
    device = (render.hwaccel_device or "").strip()
    if render.hwaccel_api == "nvenc":
        index = device if device.isdigit() else "0"
        return [_NVIDIA_CONTROL_NODE, f"/dev/nvidia{index}"]
    if device.startswith("/"):
        return [device]
    return [_DEFAULT_RENDER_NODE]


def check_hwaccel_device_access(settings: Settings) -> list[str]:
    """Log an error for each GPU device node the service cannot open (#67 R15).

    The packaged unit runs with ``PrivateDevices=true``, which hides ``/dev/dri``
    and ``/dev/nvidia*``, and the service account is not in the ``render``
    group. A configured hardware encoder then fails at render time with an
    ffmpeg device error that does not say why, so this check names the cause and
    the opt-in drop-in that grants access. It only logs: no setting changes and
    no software fallback is forced, so render behaviour is unchanged.

    Never raises, like :func:`check_storage_paths`. Returns the nodes that
    failed the check, for tests.
    """
    try:
        nodes = hwaccel_device_nodes(settings.render)
        denied = [node for node in nodes if not os.access(node, os.R_OK | os.W_OK)]
    except Exception as exc:  # noqa: BLE001 - a diagnostic never blocks startup
        logger.warning("hwaccel: device access check skipped (%s)", type(exc).__name__)
        return []
    for node in denied:
        logger.error(
            "hwaccel: %s is missing or not readable and writable by this service"
            " (render.hwaccel_api=%s), so hardware encodes will fail. Under systemd"
            " the unit's PrivateDevices=true hides GPU devices: grant access with"
            " the opt-in drop-in %s (see docs/installation.md), or turn"
            " render.hwaccel_enabled off",
            node,
            settings.render.hwaccel_api,
            _HWACCEL_DROP_IN,
        )
    return denied


class _CoordinatedServer(uvicorn.Server):
    """A Uvicorn server whose stop signals the :class:`_ShutdownCoordinator` owns.

    Uvicorn's own ``capture_signals`` installs process-wide handlers per server,
    so with two servers the last one to start wins every signal and the other
    only stops after it, one drain after the other (#67 R4). Once the
    coordinator has installed its loop handlers, :attr:`signals_coordinated` is
    set and this server installs none; until then (or where the loop cannot take
    signal handlers, such as Windows) it falls back to Uvicorn's behaviour.

    ``before_lifespan_shutdown`` runs after this server has drained and before
    its lifespan teardown. The HTTPS server uses it to wait for the HTTP
    listener, which shares the application, to finish draining first.
    """

    def __init__(
        self,
        config: uvicorn.Config,
        *,
        before_lifespan_shutdown: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(config)
        self.signals_coordinated = False
        self._before_lifespan_shutdown = before_lifespan_shutdown

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        """Install no handlers when coordinated; otherwise Uvicorn's own."""
        if self.signals_coordinated:
            yield
            return
        with super().capture_signals():
            yield

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        """Drain as Uvicorn does, running the hook before the lifespan teardown."""
        hook = self._before_lifespan_shutdown
        if hook is not None:
            lifespan = self.lifespan
            run_lifespan_shutdown = lifespan.shutdown

            async def _shutdown_after_hook() -> None:
                await hook()
                await run_lifespan_shutdown()

            # Uvicorn calls ``self.lifespan.shutdown()`` at the end of its own
            # shutdown(), after the drain; wrapping it is the only seam there.
            lifespan.shutdown = _shutdown_after_hook
        await super().shutdown(sockets)


class _ShutdownCancelFilter(logging.Filter):
    """Drop Uvicorn's traceback for a request cancelled by the shutdown grace (#179).

    When ``server.shutdown_grace_seconds`` runs out, Uvicorn cancels each request
    still open, and logs ``Exception in ASGI application`` with a
    ``CancelledError`` traceback for every one. That is the designed outcome of a
    stop, not a fault, so while ``is_stopping`` is true those records are
    dropped and counted; :attr:`cancelled` feeds one summary line. A
    ``CancelledError`` outside a shutdown, and every other exception, is logged
    as before.
    """

    def __init__(self, is_stopping: Callable[[], bool]) -> None:
        super().__init__()
        self._is_stopping = is_stopping
        self.cancelled = 0

    def filter(self, record: logging.LogRecord) -> bool:
        """Return False for a shutdown-time ``CancelledError`` record."""
        exc_info = record.exc_info
        if (
            exc_info is not None
            and isinstance(exc_info[1], asyncio.CancelledError)
            and self._is_stopping()
        ):
            self.cancelled += 1
            return False
        return True


class _ShutdownCoordinator:
    """One owner for ``SIGTERM``/``SIGINT`` across every listener (#67 R4).

    The first signal sets ``should_exit`` on every server, so they all stop
    accepting and drain at the same time; a second sets ``force_exit`` on every
    server, which skips the remaining drain. Handlers go on the running loop
    (``loop.add_signal_handler``), so they run as ordinary callbacks, not inside
    an interrupted frame.
    """

    def __init__(self, servers: Sequence[uvicorn.Server]) -> None:
        self._servers = tuple(servers)
        self._installed: list[signal.Signals] = []
        self.exit_requested = False

    def install(self, loop: asyncio.AbstractEventLoop) -> bool:
        """Install the loop's signal handlers; return False where unsupported.

        Fails (and installs nothing) off the main thread or on a loop without
        signal support (Windows' proactor loop), in which case the caller keeps
        Uvicorn's per-server handling.
        """
        try:
            for sig in _SHUTDOWN_SIGNALS:
                loop.add_signal_handler(sig, self.handle_signal, sig)
                self._installed.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            self.remove(loop)
            return False
        return True

    def remove(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remove every handler :meth:`install` added. Idempotent."""
        while self._installed:
            loop.remove_signal_handler(self._installed.pop())

    def handle_signal(self, sig: int) -> None:
        """First signal: graceful exit for every server. Again: force it."""
        name = signal.Signals(sig).name
        if not self.exit_requested:
            self.exit_requested = True
            logger.info("%s received: stopping both listeners", name)
            for server in self._servers:
                server.should_exit = True
            return
        logger.warning("%s received again: forcing exit without draining", name)
        for server in self._servers:
            server.should_exit = True
            server.force_exit = True


async def _serve(
    settings: Settings, env_overrides: frozenset[str] = frozenset()
) -> None:
    """Run the HTTPS and HTTP listeners concurrently against one application.

    The application is constructed once and shared. The lifespan (engine, shared
    context, and the background capture/render loops) must run exactly once, so
    only the HTTPS server drives it; the HTTP server runs with ``lifespan="off"``
    and relies on the process-global context the HTTPS startup installed. The
    HTTPS listener is started first and allowed to finish startup before the HTTP
    listener accepts traffic, so an early HTTP request never races the context
    installation.

    Shutdown (#67 R4): a :class:`_ShutdownCoordinator` is installed before
    either server starts, so a stop signal during startup is not lost. A stop
    asks both servers to exit together. The HTTPS lifespan teardown first tells
    the HTTP listener to exit (whatever stopped HTTPS) and waits for it to
    drain, so the two always stop together and teardown never runs under a live
    request. A stop that arrives before HTTPS finished starting never starts the
    HTTP listener.
    """
    app = create_app(settings, env_overrides)
    cert_path, key_path = ensure_tls_cert(settings)

    https_config = _build_https_config(app, settings, cert_path, key_path)
    http_config = _build_http_config(app, settings)

    http_task: asyncio.Task[None] | None = None
    http_drain_timeout = settings.server.shutdown_grace_seconds + (
        _PEER_DRAIN_MARGIN_SECONDS
    )

    async def _stop_http_listener() -> None:
        http_server.should_exit = True
        if http_task is not None:
            await asyncio.wait({http_task}, timeout=http_drain_timeout)
        # Uvicorn cancels requests still open after the grace without awaiting
        # them; let them unwind before the teardown disposes what they use.
        cancelled = {
            task
            for server in (https_server, http_server)
            for task in server.server_state.tasks
        }
        if cancelled:
            await asyncio.wait(cancelled, timeout=_CANCELLED_REQUEST_SETTLE_SECONDS)

    https_server = _CoordinatedServer(
        https_config, before_lifespan_shutdown=_stop_http_listener
    )
    http_server = _CoordinatedServer(http_config)

    cancel_filter = _ShutdownCancelFilter(
        lambda: https_server.should_exit or http_server.should_exit
    )
    server_error_logger = logging.getLogger("uvicorn.error")
    server_error_logger.addFilter(cancel_filter)

    loop = asyncio.get_running_loop()
    coordinator = _ShutdownCoordinator((https_server, http_server))
    if coordinator.install(loop):
        https_server.signals_coordinated = True
        http_server.signals_coordinated = True
    try:
        # Named so the SIGUSR2 task dump and the debug endpoint are readable (#7).
        https_task = asyncio.create_task(https_server.serve(), name="https-server")
        # Let HTTPS finish startup (installing the shared context) before HTTP
        # serves.
        while not https_server.started and not https_task.done():
            await asyncio.sleep(0.05)
        if https_server.started:
            # Logging is configured by now (the lifespan did it first thing).
            check_hwaccel_device_access(settings)
        tasks = [https_task]
        if https_server.started and not https_server.should_exit:
            http_task = asyncio.create_task(http_server.serve(), name="http-server")
            tasks.append(http_task)
        await asyncio.gather(*tasks)
    finally:
        coordinator.remove(loop)
        server_error_logger.removeFilter(cancel_filter)
    if cancel_filter.cancelled:
        logger.warning(
            "%d request(s) still open when the %d s shutdown grace ran out were "
            "cancelled",
            cancel_filter.cancelled,
            settings.server.shutdown_grace_seconds,
        )
    if https_server.started:
        logger.info("service stopped: both listeners closed, lifespan shut down")
    elif not coordinator.exit_requested:
        # The application never finished starting (a lifespan startup failure,
        # for instance) and nobody asked it to stop. Exit non-zero so the
        # service manager sees a failure: ``Restart=on-failure`` ignores a clean
        # exit and would leave the service down (#177).
        if key_integrity_refused():
            # The encryption key does not match the data (#67 S8). A restart
            # cannot fix that, so use the status the unit's
            # RestartPreventExitStatus holds still on; the cause is already in
            # the journal.
            logger.error(
                "service refused to start over the encryption key; exiting with "
                "status %d (no automatic restart)",
                EXIT_KEY_INTEGRITY,
            )
            raise SystemExit(EXIT_KEY_INTEGRITY)
        logger.error(
            "service failed to start; exiting with status %d", _EXIT_STARTUP_FAILURE
        )
        raise SystemExit(_EXIT_STARTUP_FAILURE)
