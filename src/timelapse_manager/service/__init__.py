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

Uvicorn installs its own signal handlers, so ``SIGINT``/``SIGTERM`` trigger a
graceful shutdown that runs the application's lifespan teardown. The single
shared application means the lifespan (engine, context, background loops) runs
once regardless of how many listeners front it.
"""

from __future__ import annotations

import argparse
import asyncio

import uvicorn

from ..app import create_app
from ..config import Settings, load_settings_with_provenance
from .tls import ensure_tls_cert

# Loopback address the HTTP listener binds to when redirection is disabled, so a
# plaintext port is never exposed beyond the host.
_LOOPBACK = "127.0.0.1"


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
    loopback. Uvicorn's signal handling drives graceful shutdown; the lifespan
    disposes the engine on the way down.
    """
    args = _parse_args(argv)
    settings, env_overrides = load_settings_with_provenance(args.config)
    serve(settings, env_overrides)


def serve(settings: Settings, env_overrides: frozenset[str] = frozenset()) -> None:
    """Run the service in the foreground until interrupted.

    The single public entry into the serve loop, shared by the daemon entry
    point and the frozen ``timelapse-manager run`` subcommand so the foreground
    serve logic lives in exactly one place. Blocks on :func:`asyncio.run` until
    a signal triggers Uvicorn's graceful shutdown.

    :param env_overrides: dotted settings leaf paths the environment determined
        (see :func:`config.load_settings_with_provenance`), forwarded to the app
        so the read-only settings UI can mark environment-controlled values.
        Defaults to empty when a caller has no provenance to forward.

    The schema is brought to head before the listeners start: a packaged
    deployment has no separate "migrate first" step, so the service must
    initialize its own database on startup. This is idempotent on an
    already-migrated database.
    """
    from ..db.migrate import apply_migrations

    apply_migrations(settings)
    asyncio.run(_serve(settings, env_overrides))


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
    """
    app = create_app(settings, env_overrides)
    cert_path, key_path = ensure_tls_cert(settings)

    https_config = uvicorn.Config(
        app,
        host=settings.server.bind_address,
        port=settings.server.https_port,
        ssl_certfile=str(cert_path),
        ssl_keyfile=str(key_path),
        log_config=None,
    )
    # When redirection is disabled the plaintext port must not be reachable off
    # the host, so it is confined to loopback rather than the public bind address.
    http_host = (
        settings.server.bind_address
        if settings.server.redirect_http_to_https
        else _LOOPBACK
    )
    http_config = uvicorn.Config(
        app,
        host=http_host,
        port=settings.server.http_port,
        # The shared application's lifespan runs only under the HTTPS server so the
        # engine, context, and background loops are not started twice.
        lifespan="off",
        log_config=None,
    )

    https_server = uvicorn.Server(https_config)
    http_server = uvicorn.Server(http_config)

    https_task = asyncio.create_task(https_server.serve())
    # Let HTTPS finish startup (installing the shared context) before HTTP serves.
    while not https_server.started and not https_task.done():
        await asyncio.sleep(0.05)
    http_task = asyncio.create_task(http_server.serve())
    await asyncio.gather(https_task, http_task)
