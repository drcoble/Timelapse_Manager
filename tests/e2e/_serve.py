"""Minimal uvicorn launcher for E2E browser tests.

Invoked as a subprocess by the ``live_server`` fixture in conftest.py:

    python -m tests.e2e._serve --db-url <url> --data-dir <dir> --port <n>

Builds a plain-HTTP (no TLS, no redirect) Settings instance from the
arguments and runs the app with uvicorn.  Exits when the parent process
terminates or when uvicorn receives SIGTERM.

With ``--https-port`` it runs the real service runner instead
(:func:`timelapse_manager.service.serve`): migrations, the HTTPS listener on
that port with a self-signed certificate generated into the data directory, the
HTTP listener on ``--port`` (loopback, no redirect), and the service's own
shutdown coordinator (#67 R4, ``tests/e2e/test_service_shutdown.py``).

Do NOT import this module directly in tests — it is a subprocess entry point
only.  All Playwright / playwright-import logic lives in conftest.py.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="E2E test app server")
    p.add_argument("--db-url", required=True)
    p.add_argument("--data-dir", required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument(
        "--loop-guard-log",
        type=Path,
        default=None,
        help="record database work on the event loop (in a request or background"
        " work) to this JSON-lines file (tests/loop_guard_support.py)",
    )
    p.add_argument(
        "--render-autostart",
        action="store_true",
        help="start the render worker and scheduler (off by default; the #62"
        " regression drives a running render through _serve_render_worker.py)",
    )
    p.add_argument(
        "--idle-timeout-seconds",
        type=int,
        default=None,
        help="test only: override session.idle_timeout_seconds (the touch"
        " interval follows it: a tenth, at least 1 s) so an idle sign-out can be"
        " driven in seconds (tests/e2e/test_polling_policy.py)",
    )
    p.add_argument(
        "--https-port",
        type=int,
        default=None,
        help="run the real dual-listener service runner, HTTPS on this port",
    )
    p.add_argument(
        "--shutdown-grace-seconds",
        type=int,
        default=None,
        help="override server.shutdown_grace_seconds (with --https-port)",
    )
    p.add_argument(
        "--ffmpeg-binary",
        default=None,
        help="override render.ffmpeg_binary (a fake ffmpeg script in tests)",
    )
    p.add_argument(
        "--log-level",
        default="WARNING",
        help="logging.level for the app (INFO to see lifecycle log lines)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    if args.loop_guard_log is not None:
        # Run as a script, so the repository root (holding the ``tests``
        # package) is not on sys.path by default.
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from tests import loop_guard_support

        loop_guard_support.install(file_sink=args.loop_guard_log)

    import uvicorn

    from timelapse_manager.app import create_app
    from timelapse_manager.config.settings import (
        AuthSettings,
        CaptureSettings,
        DatabaseSettings,
        LoggingSettings,
        MonitoringSettings,
        PathsSettings,
        RenderSettings,
        ReportingGithubSettings,
        ReportingSettings,
        SecretsSettings,
        ServerSettings,
        SessionSettings,
        Settings,
        TlsSettings,
    )

    data_dir = Path(args.data_dir)
    dual_listener = args.https_port is not None
    server_settings = ServerSettings(redirect_http_to_https=False)
    if dual_listener:
        server_settings = ServerSettings(
            redirect_http_to_https=False,
            bind_address="127.0.0.1",
            http_port=args.port,
            https_port=args.https_port,
            **(
                {"shutdown_grace_seconds": args.shutdown_grace_seconds}
                if args.shutdown_grace_seconds is not None
                else {}
            ),
        )
    settings = Settings(
        database=DatabaseSettings(url=args.db_url),
        logging=LoggingSettings(level=args.log_level, format="text"),
        paths=PathsSettings(
            data_dir=data_dir,
            frames_root=data_dir / "frames",
            token_file=data_dir / ".local-token",
        ),
        capture=CaptureSettings(autostart=False),
        render=RenderSettings(
            autostart=args.render_autostart, ffmpeg_binary=args.ffmpeg_binary
        ),
        monitoring=MonitoringSettings(autostart=False),
        auth=AuthSettings(
            argon2_memory_kib=256,
            argon2_time_cost=1,
            argon2_parallelism=1,
            password_min_length=12,
        ),
        # The dual-listener runner needs a certificate; it is generated into
        # the data directory.
        tls=TlsSettings(auto_generate=dual_listener),
        # Plain HTTP for browser tests — no redirect so Playwright does not chase
        # an HTTPS URL on a port with no TLS listener.
        server=server_settings,
        secrets=SecretsSettings(use_os_keystore=False),
        # #8's plain report flow (screenshot field shown); the GitHub hand-off
        # (#21) is covered by tests/web/test_report_github.py.
        reporting=ReportingSettings(github=ReportingGithubSettings(enabled=False)),
    )
    if args.idle_timeout_seconds is not None:
        settings = settings.model_copy(
            update={
                "session": SessionSettings(
                    idle_timeout_seconds=args.idle_timeout_seconds
                )
            }
        )

    if dual_listener:
        from timelapse_manager.service import serve

        serve(settings)
        return

    app = create_app(settings)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main(sys.argv[1:])
