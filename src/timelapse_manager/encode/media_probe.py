"""Probe a finished render with ffprobe for its codec string and MIME type.

The render worker calls :func:`probe_media` once a render is written (and the
startup backfill calls it for older renders), then stores the result on the
job so the page can ask the viewer's browser whether it can play the file. See
:mod:`.codec_string` for the string rules.

Probing is best effort and never raises: a missing ffprobe, a timeout, a
non-zero exit or unparseable output all return ``None`` with a warning, and
the job keeps ``NULL`` (the browser then falls back to a MIME-only check).
ffprobe runs as an argv list, never through a shell, and is killed on timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .codec_string import codec_string, media_type_for_path

logger = logging.getLogger(__name__)

# Per-file ceiling: a probe reads the container header only, so anything
# slower than this means a wedged binary or a broken file.
PROBE_TIMEOUT_SECONDS = 10.0

# The stream fields the codec-string rules read; nothing else is requested.
_STREAM_FIELDS = "codec_name,profile,level,pix_fmt,codec_tag_string"


@dataclass(frozen=True)
class MediaInfo:
    """What a probe learned about a render file."""

    codec_string: str | None
    mime_type: str


def probe_argv(ffprobe_binary: str, path: Path) -> list[str]:
    """Return the ffprobe argv for the first video stream of ``path``.

    ``path`` is resolved to an absolute path, so it never starts with ``-``
    and is never read as an option or a protocol prefix.
    """
    return [
        ffprobe_binary,
        "-v",
        "error",
        # Only local files: a render path can never make ffprobe open a URL or
        # follow a protocol (concat:, http:, ...) to somewhere else.
        "-protocol_whitelist",
        "file",
        "-select_streams",
        "v:0",
        "-show_entries",
        f"stream={_STREAM_FIELDS}",
        "-of",
        "json",
        str(path.resolve()),
    ]


def parse_probe_output(raw: bytes | str) -> str | None:
    """Return the codec string from ffprobe's JSON output, or ``None``."""
    try:
        document = json.loads(raw)
    except (TypeError, ValueError):
        return None
    streams = document.get("streams") if isinstance(document, dict) else None
    if not isinstance(streams, list) or not streams:
        return None
    first = streams[0]
    if not isinstance(first, dict):
        return None
    return codec_string(first)


async def probe_media(
    ffprobe_binary: str,
    path: Path,
    *,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> MediaInfo | None:
    """Probe ``path``; return its codec string and MIME type, or ``None``.

    ``None`` means the probe itself failed (logged as a warning). A probe that
    runs but finds a codec with no rule returns a :class:`MediaInfo` whose
    ``codec_string`` is ``None``.
    """
    argv = probe_argv(ffprobe_binary, path)
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        logger.warning("ffprobe could not start for %s: %s", path, exc)
        return None
    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await process.communicate()
    except TimeoutError:
        await _kill(process)
        logger.warning("ffprobe timed out after %.0fs on %s", timeout, path)
        return None
    except asyncio.CancelledError:
        await _kill(process)
        raise
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip().splitlines()
        logger.warning(
            "ffprobe failed on %s (exit %s): %s",
            path,
            process.returncode,
            detail[-1] if detail else "no output",
        )
        return None
    codec = parse_probe_output(stdout)
    if codec is None:
        logger.warning("ffprobe gave no recognised codec string for %s", path)
    return MediaInfo(codec_string=codec, mime_type=media_type_for_path(path))


async def _kill(process: asyncio.subprocess.Process) -> None:
    """Kill and reap the ffprobe child, tolerating one that already exited."""
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    with contextlib.suppress(Exception):
        await process.wait()
