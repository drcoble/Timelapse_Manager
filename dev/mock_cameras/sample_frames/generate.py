#!/usr/bin/env python3
"""Regenerate the sample JPEG frames used by the mock snapshot camera.

The three committed ``frame-*.jpg`` files in this directory are produced by this
script. They are intentionally tiny, distinct, solid-colour images so the mock
HTTP snapshot server can rotate through visibly different frames.

This shells out to FFmpeg, which is a standard development dependency for this
project (it is also what the encoder pipeline uses). The generated frames are
committed to the repository, so contributors do not need to run this unless they
want to refresh them::

    python dev/mock_cameras/sample_frames/generate.py

If FFmpeg is unavailable, the script reports the failure and exits non-zero.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent

# (filename, FFmpeg lavfi colour) pairs.
FRAMES = [
    ("frame-001.jpg", "red"),
    ("frame-002.jpg", "green"),
    ("frame-003.jpg", "blue"),
]

SIZE = "64x48"


def main() -> int:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        print("ERROR: ffmpeg not found on PATH.", file=sys.stderr)
        return 1

    for name, colour in FRAMES:
        out = OUT_DIR / name
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={colour}:s={SIZE}:d=1",
                "-frames:v",
                "1",
                str(out),
            ],
            check=True,
        )
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
