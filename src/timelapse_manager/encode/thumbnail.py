"""Thumbnail generation via the bundled ffmpeg.

A small, self-contained ffmpeg spawn site that downscales a single image into a
JPEG thumbnail. Kept out of the web layer so the request handlers never spawn a
process directly; this module is registered in the subprocess-execution audit
(``tests/abuse/test_no_arbitrary_execution.py``) and follows its rules: an argv
list (never a shell string) whose ``argv[0]`` is the admin-configured ffmpeg
binary, never a value derived from user input.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def generate_thumbnail(
    ffmpeg_binary: str,
    source_path: str,
    dest_path: Path,
    *,
    width: int,
    timeout: float = 20.0,
) -> None:
    """Write a ``width``-px-wide JPEG thumbnail of ``source_path`` to ``dest_path``.

    Spawns the configured ffmpeg with an argv list (``shell=False`` semantics).
    ``argv[0]`` is the admin-configured binary; ``source_path`` and ``dest_path``
    are local filesystem paths the caller has already resolved and
    containment-checked. The width is forced and the height is auto (kept even) so
    the aspect ratio is preserved. Raises :class:`subprocess.SubprocessError` (on
    a non-zero exit or timeout) or :class:`OSError` (if ffmpeg is missing), which
    the caller handles by falling back to the full-size image.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg_binary,
            "-y",
            "-loglevel",
            "error",
            "-i",
            source_path,
            "-vf",
            f"scale={width}:-1",
            "-frames:v",
            "1",
            str(dest_path),
        ],
        check=True,
        capture_output=True,
        timeout=timeout,
    )
