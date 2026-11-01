"""SIGUSR1 / SIGUSR2 against a real daemon process (issue #7).

Starts ``timelapse-daemon`` in a subprocess, waits until it serves, then sends
``SIGUSR1`` (faulthandler thread dump) and ``SIGUSR2`` (asyncio task dump). Both
dumps must reach stderr and the process must still be alive, with the same PID,
afterwards. POSIX only: Windows has neither signal.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from tests.integration.test_smoke_server import (
    _find_daemon_executable,
    _free_ports,
    _wait_for_server,
)

pytestmark = pytest.mark.skipif(
    not (hasattr(signal, "SIGUSR1") and hasattr(signal, "SIGUSR2")),
    reason="SIGUSR1/SIGUSR2 are POSIX only",
)


def _wait_for_text(path: Path, needle: str, timeout: float = 15.0) -> str:
    """Poll ``path`` until it contains ``needle``; return its final text."""
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        text = path.read_text(encoding="utf-8", errors="replace")
        if needle in text:
            return text
        time.sleep(0.2)
    return text


@pytest.mark.slow
def test_daemon_survives_sigusr1_then_sigusr2_and_dumps_to_stderr() -> None:
    http_port, https_port = _free_ports(2)
    daemon_exe = _find_daemon_executable()

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        data_dir = tmp / "data"
        data_dir.mkdir()
        stderr_path = tmp / "stderr.log"
        env = {
            **os.environ,
            "TLM_SERVER__HTTP_PORT": str(http_port),
            "TLM_SERVER__HTTPS_PORT": str(https_port),
            "TLM_SERVER__BIND_ADDRESS": "127.0.0.1",
            "TLM_DATABASE__URL": f"sqlite:///{tmp / 'signals.db'}",
            "TLM_PATHS__DATA_DIR": str(data_dir),
            "TLM_PATHS__TOKEN_FILE": str(data_dir / ".local-token"),
            # WARNING proves the task dump is visible at a quiet level.
            "TLM_LOGGING__LEVEL": "WARNING",
            "TLM_LOGGING__FORMAT": "text",
        }
        # stderr goes to a file, not a pipe: a large dump must never block the
        # process on a full pipe buffer.
        with stderr_path.open("wb") as stderr_file:
            proc = subprocess.Popen(
                [str(daemon_exe)],
                env=env,
                cwd=str(Path(__file__).parent.parent.parent),
                stdout=subprocess.DEVNULL,
                stderr=stderr_file,
            )
            try:
                healthz = f"http://127.0.0.1:{http_port}/healthz"
                # The handlers are installed during startup; signalling earlier
                # would hit the default action (terminate).
                assert _wait_for_server(healthz, timeout=30.0), stderr_path.read_text()

                proc.send_signal(signal.SIGUSR1)
                text = _wait_for_text(stderr_path, "Thread 0x")
                assert "Thread 0x" in text or "Current thread" in text, text[-2000:]
                assert proc.poll() is None, "SIGUSR1 terminated the daemon"

                proc.send_signal(signal.SIGUSR2)
                text = _wait_for_text(stderr_path, "asyncio task dump end")
                assert "asyncio task dump begin" in text, text[-2000:]
                assert "asyncio task name=loop-lag" in text
                assert "asyncio task name=https-server" in text
                assert "default executor" in text
                assert proc.poll() is None, "SIGUSR2 terminated the daemon"

                # Still serving, same process.
                assert httpx.get(healthz, timeout=5.0).status_code == 200
                assert proc.poll() is None
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=10.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
