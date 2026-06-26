#!/usr/bin/env python3
"""A tiny HTTP snapshot camera stub for local adapter development.

Serves rotating sample JPEG frames so HTTP/JPEG and VAPIX camera adapters can
be developed and tested without physical hardware. Uses only the Python
standard library.

Endpoints:
    GET /snapshot.jpg               -- generic HTTP/JPEG snapshot URL
    GET /axis-cgi/jpg/image.cgi     -- VAPIX-shaped snapshot path (Axis cameras)
    GET /healthz                    -- liveness probe (plain text "ok")

Each snapshot request returns the next frame from ``sample_frames/`` in a
round-robin rotation, so successive captures differ (useful for verifying a
capture loop actually advances).

Run directly::

    python dev/mock_cameras/http_snapshot.py [--host HOST] [--port PORT]

Defaults to 127.0.0.1:8555.
"""

from __future__ import annotations

import argparse
import itertools
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8555

FRAMES_DIR = Path(__file__).resolve().parent / "sample_frames"

# Paths that should return a snapshot JPEG.
SNAPSHOT_PATHS = frozenset(
    {
        "/snapshot.jpg",
        "/axis-cgi/jpg/image.cgi",
    }
)


def _load_frames() -> list[bytes]:
    """Read all ``*.jpg`` frames from the sample directory, sorted by name.

    Raises ``FileNotFoundError`` if no frames are present, so the server fails
    loudly rather than serving empty bodies.
    """
    if not FRAMES_DIR.is_dir():
        raise FileNotFoundError(f"sample frames directory missing: {FRAMES_DIR}")
    frames = [p.read_bytes() for p in sorted(FRAMES_DIR.glob("*.jpg"))]
    if not frames:
        raise FileNotFoundError(f"no *.jpg frames found in {FRAMES_DIR}")
    return frames


class _SnapshotHandler(BaseHTTPRequestHandler):
    """Serves rotating JPEG frames. State is shared via class attributes."""

    server_version = "MockSnapshotCamera/1.0"

    # Populated by ``run`` before the server starts.
    frames: list[bytes] = []
    _cycle: "itertools.cycle[bytes]" = itertools.cycle([b""])
    _lock = threading.Lock()

    def _next_frame(self) -> bytes:
        with self._lock:
            return next(self._cycle)

    def do_GET(self) -> None:  # noqa: N802 (stdlib API name)
        # Strip any query string (VAPIX clients append parameters like
        # ?resolution=640x480); only the path selects the response.
        path = self.path.split("?", 1)[0]

        if path == "/healthz":
            self._send_text(200, "ok")
            return

        if path in SNAPSHOT_PATHS:
            frame = self._next_frame()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(frame)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(frame)
            return

        self._send_text(404, "not found")

    def _send_text(self, status: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: object) -> None:
        # Keep logs terse and on stderr.
        sys.stderr.write(
            "[http_snapshot] %s - %s\n" % (self.address_string(), fmt % args)
        )


def run(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    frames = _load_frames()
    _SnapshotHandler.frames = frames
    _SnapshotHandler._cycle = itertools.cycle(frames)

    server = ThreadingHTTPServer((host, port), _SnapshotHandler)
    print(
        f"Mock HTTP snapshot camera on http://{host}:{port}\n"
        f"  snapshot: http://{host}:{port}/snapshot.jpg\n"
        f"  vapix:    http://{host}:{port}/axis-cgi/jpg/image.cgi\n"
        f"  frames:   {len(frames)} loaded from {FRAMES_DIR}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    run(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
