"""SIGUSR1 dumps thread stacks instead of terminating the process (issue #6)."""

from __future__ import annotations

import signal
import subprocess
import sys

import pytest

_SCRIPT = """
import os, signal
from timelapse_manager.app import _install_thread_dump_signal
_install_thread_dump_signal()
os.kill(os.getpid(), signal.SIGUSR1)
print("alive")
"""


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="POSIX only")
def test_sigusr1_dumps_stacks_and_the_process_survives() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "alive"
    assert "Current thread" in proc.stderr or "Thread 0x" in proc.stderr
