#!/usr/bin/env python3
"""Publish a synthetic RTSP stream to the local mediamtx server.

Launches FFmpeg with its built-in ``testsrc`` test pattern (a moving colour bar
chart with a timestamp) and pushes it to the mediamtx RTSP endpoint, giving
camera adapters a live RTSP source without physical hardware::

    rtsp://localhost:8554/testsrc

mediamtx must already be running with the matching configuration
(``mediamtx.conf``) before this is started; the bundled launcher
(``run.py``) starts both in the correct order.

Run directly::

    python dev/mock_cameras/ffmpeg_testsrc.py [--url URL] [--fps N] [--size WxH]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

DEFAULT_URL = "rtsp://localhost:8554/testsrc"
DEFAULT_FPS = 15
DEFAULT_SIZE = "640x480"


def build_command(url: str, fps: int, size: str) -> list[str]:
    """Build the FFmpeg argument list for publishing the test pattern."""
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        # Generate the test pattern in real time so the stream paces correctly.
        "-re",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size={size}:rate={fps}",
        # A synthetic audio tone, so adapters can exercise A/V demuxing.
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=1000:sample_rate=48000",
        # H.264 video tuned for low-latency live streaming.
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-g",
        str(fps * 2),
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        # Publish to mediamtx over RTSP/TCP.
        "-f",
        "rtsp",
        "-rtsp_transport",
        "tcp",
        url,
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS)
    parser.add_argument("--size", default=DEFAULT_SIZE)
    args = parser.parse_args()

    if shutil.which("ffmpeg") is None:
        print("ERROR: ffmpeg not found on PATH.", file=sys.stderr)
        return 1

    cmd = build_command(args.url, args.fps, args.size)
    print(f"Publishing test pattern to {args.url}", flush=True)
    try:
        return subprocess.run(cmd).returncode
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
