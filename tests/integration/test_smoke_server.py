"""Smoke test: start the daemon in a subprocess and verify live endpoints.

Marked slow; spawns a real uvicorn process on an ephemeral loopback port,
polls until the server is up, then asserts the core API contract, and
terminates. A generous startup budget (~30 s) covers cold-start on slower
machines. Only one subprocess test exists to keep the suite fast.
"""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
import pytest


def _free_ports(count: int) -> list[int]:
    """Return ``count`` distinct available TCP ports on 127.0.0.1.

    Every socket is held open until all ports are chosen, so the kernel cannot
    hand the same ephemeral port out twice within one call. The sockets close on
    return; the brief window before the daemon rebinds them is the same race any
    port-zero scheme accepts.
    """
    socks = []
    try:
        for _ in range(count):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", 0))
            socks.append(s)
        return [s.getsockname()[1] for s in socks]
    finally:
        for s in socks:
            s.close()


def _wait_for_server(url: str, timeout: float = 30.0, interval: float = 0.5) -> bool:
    """Poll GET url until a 200 is returned or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=2.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _find_daemon_executable() -> Path:
    """Return the path to the timelapse-daemon script installed in the venv."""
    code_root = Path(__file__).parent.parent.parent
    # uv installs scripts into .venv/bin (POSIX) or .venv/Scripts (Windows).
    for candidate in (
        code_root / ".venv" / "bin" / "timelapse-daemon",
        code_root / ".venv" / "Scripts" / "timelapse-daemon.exe",
    ):
        if candidate.exists():
            return candidate
    pytest.skip("timelapse-daemon script not found in .venv; run 'uv sync' first")


@pytest.mark.slow
def test_daemon_starts_and_serves_healthz_and_api() -> None:
    """Start timelapse-daemon in a subprocess and verify liveness + auth.

    Both the HTTP and HTTPS listeners are pinned to ephemeral ports. The daemon
    binds both from one process, so leaving HTTPS on its 8443 default would make
    the smoke fail on any host already running the service -- exactly what this
    test must not depend on.
    """
    port, https_port = _free_ports(2)
    daemon_exe = _find_daemon_executable()

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        data_dir = tmp / "data"
        data_dir.mkdir()
        db_path = tmp / "smoke.db"
        token_file = data_dir / ".local-token"

        env = {
            **os.environ,
            "TLM_SERVER__HTTP_PORT": str(port),
            "TLM_SERVER__HTTPS_PORT": str(https_port),
            "TLM_SERVER__BIND_ADDRESS": "127.0.0.1",
            "TLM_DATABASE__URL": f"sqlite:///{db_path}",
            "TLM_PATHS__DATA_DIR": str(data_dir),
            "TLM_PATHS__TOKEN_FILE": str(token_file),
            "TLM_LOGGING__LEVEL": "WARNING",
            "TLM_LOGGING__FORMAT": "text",
        }

        code_root = Path(__file__).parent.parent.parent
        proc = subprocess.Popen(
            [str(daemon_exe)],
            env=env,
            cwd=str(code_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        healthz_url = f"http://127.0.0.1:{port}/healthz"
        system_url = f"http://127.0.0.1:{port}/api/v1/system"

        try:
            # Wait up to 30 s for the server to be ready.
            assert _wait_for_server(healthz_url, timeout=30.0), (
                f"Server at {healthz_url} did not become ready within 30 s"
            )

            # --- /healthz must return 5-field body ---
            r = httpx.get(healthz_url, timeout=5.0)
            assert r.status_code == 200
            body = r.json()
            assert set(body.keys()) == {
                "app_version",
                "ffmpeg_version",
                "ffmpeg_path",
                "db_status",
                "alembic_revision",
            }
            assert isinstance(body["app_version"], str) and body["app_version"]
            assert isinstance(body["ffmpeg_version"], str) and body["ffmpeg_version"]
            assert isinstance(body["ffmpeg_path"], str) and body["ffmpeg_path"]

            # --- /api/v1/system: 401 without token ---
            r_noauth = httpx.get(system_url, timeout=5.0)
            assert r_noauth.status_code == 401

            # --- /api/v1/system: 200 with correct token ---
            assert token_file.exists(), "Server must have written the token file"
            token = token_file.read_text(encoding="utf-8").strip()
            r_auth = httpx.get(
                system_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=5.0,
            )
            assert r_auth.status_code == 200
            system_body = r_auth.json()
            assert "app_version" in system_body
            assert "config" in system_body

        finally:
            # Graceful shutdown: SIGTERM, then force-kill after 5 s.
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
