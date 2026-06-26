#!/usr/bin/env python3
"""Launch the full mock-camera stack for local adapter development.

Starts three processes and supervises them as a group:

1. ``mediamtx`` -- RTSP server (port 8554), using ``mediamtx.conf``.
2. FFmpeg ``testsrc`` -- publishes a synthetic stream to mediamtx.
3. The HTTP snapshot stub (port 8555) -- rotating sample JPEGs.

Resulting endpoints for adapter development::

    rtsp://localhost:8554/testsrc                       (RTSP)
    http://localhost:8555/snapshot.jpg                  (HTTP/JPEG)
    http://localhost:8555/axis-cgi/jpg/image.cgi        (VAPIX-shaped)

Ctrl-C stops everything. If any child exits unexpectedly, the rest are torn
down so the stack never lingers half-up.

mediamtx and FFmpeg must be installed and on PATH. The HTTP snapshot stub is
pure standard library and always available.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path
from types import FrameType

HERE = Path(__file__).resolve().parent
CONF = HERE / "mediamtx.conf"
PYTHON = sys.executable or "python3"


def _require(name: str) -> None:
    import shutil

    if shutil.which(name) is None:
        print(
            f"ERROR: '{name}' not found on PATH. Install it to run the mock "
            f"RTSP camera (the HTTP snapshot stub works without it).",
            file=sys.stderr,
        )
        raise SystemExit(1)


def main() -> int:
    _require("mediamtx")
    _require("ffmpeg")

    procs: list[tuple[str, subprocess.Popen[bytes]]] = []

    def shutdown(*_: object) -> None:
        for name, proc in reversed(procs):
            if proc.poll() is None:
                print(f"stopping {name}...", flush=True)
                proc.terminate()
        # Give children a moment, then hard-kill stragglers.
        deadline = time.time() + 5
        for _name, proc in procs:
            remaining = max(0.0, deadline - time.time())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _on_signal(_signum: int, _frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _on_signal)

    try:
        # 1. RTSP server.
        procs.append(
            ("mediamtx", subprocess.Popen(["mediamtx", str(CONF)]))
        )
        # Give mediamtx a moment to bind before publishing to it.
        time.sleep(1.0)

        # 2. FFmpeg test-pattern publisher.
        procs.append(
            (
                "ffmpeg_testsrc",
                subprocess.Popen(
                    [PYTHON, str(HERE / "ffmpeg_testsrc.py")]
                ),
            )
        )

        # 3. HTTP snapshot stub.
        procs.append(
            (
                "http_snapshot",
                subprocess.Popen(
                    [PYTHON, str(HERE / "http_snapshot.py")]
                ),
            )
        )

        print("Mock cameras running. Press Ctrl-C to stop.", flush=True)

        # Supervise: if any child dies, tear the rest down.
        while True:
            for name, proc in procs:
                code = proc.poll()
                if code is not None:
                    print(
                        f"{name} exited (code {code}); shutting down stack.",
                        file=sys.stderr,
                    )
                    shutdown()
                    return code or 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        shutdown()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
