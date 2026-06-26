"""Minimal uvicorn launcher for E2E browser tests.

Invoked as a subprocess by the ``live_server`` fixture in conftest.py:

    python -m tests.e2e._serve --db-url <url> --data-dir <dir> --port <n>

Builds a plain-HTTP (no TLS, no redirect) Settings instance from the
arguments and runs the app with uvicorn.  Exits when the parent process
terminates or when uvicorn receives SIGTERM.

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
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

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
        SecretsSettings,
        ServerSettings,
        Settings,
        TlsSettings,
    )

    data_dir = Path(args.data_dir)
    settings = Settings(
        database=DatabaseSettings(url=args.db_url),
        logging=LoggingSettings(level="WARNING", format="text"),
        paths=PathsSettings(
            data_dir=data_dir,
            frames_root=data_dir / "frames",
            token_file=data_dir / ".local-token",
        ),
        capture=CaptureSettings(autostart=False),
        render=RenderSettings(autostart=False),
        monitoring=MonitoringSettings(autostart=False),
        auth=AuthSettings(
            argon2_memory_kib=256,
            argon2_time_cost=1,
            argon2_parallelism=1,
            password_min_length=12,
        ),
        tls=TlsSettings(auto_generate=False),
        # Plain HTTP for browser tests — no redirect so Playwright does not chase
        # an HTTPS URL on a port with no TLS listener.
        server=ServerSettings(redirect_http_to_https=False),
        secrets=SecretsSettings(use_os_keystore=False),
    )

    app = create_app(settings)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main(sys.argv[1:])
