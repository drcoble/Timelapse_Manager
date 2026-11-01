"""Thumbnail generation via the bundled ffmpeg.

A small, self-contained ffmpeg spawn site that downscales a single image into a
JPEG thumbnail. Kept out of the web layer so the request handlers never spawn a
process directly; this module is registered in the subprocess-execution audit
(``tests/abuse/test_no_arbitrary_execution.py``) and follows its rules: an argv
list (never a shell string) whose ``argv[0]`` is the admin-configured ffmpeg
binary, never a value derived from user input.

Generation is bounded (#67 P3): :func:`generation_slot` hands out at most
``render.thumbnail_concurrency`` concurrent runs process-wide, and
:func:`generate_thumbnail_atomic` writes through a temporary file that is
renamed into place, so a reader never sees a partial thumbnail.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

from ..cameras._imageinfo import read_dimensions_bounded
from ..storage.paths import THUMBNAIL_TEMP_MARKER

# How long a request waits for a generation slot before the caller gives up
# and serves something else. Read at call time, so tests can shorten it.
SLOT_TIMEOUT_SECONDS = 2.0

_slots_lock = threading.Lock()
_slots: threading.BoundedSemaphore | None = None
_slots_limit = 0


class ThumbnailOutputError(subprocess.SubprocessError):
    """ffmpeg exited cleanly but wrote no usable thumbnail."""


def _slots_for(limit: int) -> threading.BoundedSemaphore:
    """Return the process-wide generation semaphore sized for ``limit``.

    Created on first use, because the limit is a setting and settings are not
    known at import time. A different ``limit`` (a test, or a reconfigured
    context) gets a fresh semaphore; holders of the old one release into it.
    """
    global _slots, _slots_limit
    if limit < 1:
        raise ValueError("thumbnail concurrency must be at least 1")
    with _slots_lock:
        if _slots is None or _slots_limit != limit:
            _slots = threading.BoundedSemaphore(limit)
            _slots_limit = limit
        return _slots


@contextlib.contextmanager
def generation_slot(limit: int, *, timeout: float | None = None) -> Iterator[bool]:
    """Hold one of ``limit`` process-wide thumbnail-generation slots.

    Yields ``True`` once a slot is held, or ``False`` when none came free within
    ``timeout`` seconds (default :data:`SLOT_TIMEOUT_SECONDS`); the caller then
    serves a fallback instead of spawning ffmpeg. A held slot is released on
    exit, including on an exception.
    """
    slots = _slots_for(limit)
    wait = SLOT_TIMEOUT_SECONDS if timeout is None else timeout
    acquired = slots.acquire(timeout=wait)
    try:
        yield acquired
    finally:
        if acquired:
            slots.release()


def generate_thumbnail(
    ffmpeg_binary: str,
    source_path: str,
    dest_path: Path,
    *,
    width: int,
    timeout: float = 20.0,
    max_dimension_px: int | None = None,
) -> None:
    """Write a ``width``-px-wide JPEG thumbnail of ``source_path`` to ``dest_path``.

    Spawns the configured ffmpeg with an argv list (``shell=False`` semantics).
    ``argv[0]`` is the admin-configured binary; ``source_path`` and ``dest_path``
    are local filesystem paths the caller has already resolved and
    containment-checked. The width is forced and the height is auto (kept even) so
    the aspect ratio is preserved. Raises :class:`subprocess.SubprocessError` (on
    a non-zero exit or timeout) or :class:`OSError` (if ffmpeg is missing), which
    the caller handles by falling back to the full-size image.

    Before ffmpeg is spawned the source image header is read and its declared
    dimensions are checked against ``max_dimension_px`` (defaulting to the
    application setting when not supplied). A frame whose header declares an
    over-ceiling width or height is refused with :class:`ImageTooLargeError`
    *before* ffmpeg decodes it into a pixel buffer, so a dimension bomb cannot
    drive a multi-gigabyte allocation. Reading the header reads only the raw file
    bytes (already bounded at capture time), never a decoded image.
    """
    if max_dimension_px is None:
        from ..runtime import get_context

        max_dimension_px = get_context().settings.capture.max_image_dimension_px

    # Refuse a dimension bomb on the cheap header bytes before ffmpeg allocates a
    # full-resolution buffer. ImageTooLargeError propagates to the caller.
    read_dimensions_bounded(Path(source_path).read_bytes(), max_dimension_px)

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


def generate_thumbnail_atomic(
    ffmpeg_binary: str,
    source_path: str,
    dest_path: Path,
    *,
    width: int,
    timeout: float = 20.0,
    max_dimension_px: int | None = None,
) -> None:
    """Generate a thumbnail like :func:`generate_thumbnail`, atomically.

    ffmpeg writes ``<dest stem>.tmp-<pid>-<random>.jpg`` beside ``dest_path``
    (the ``.jpg`` suffix picks ffmpeg's output format), which is then renamed
    over ``dest_path`` with :func:`os.replace`. A failed run, or a run that
    wrote nothing, leaves no file behind: the temporary file is always removed,
    and ``dest_path`` is only ever a complete thumbnail. Concurrent runs for one
    frame use distinct temporary names and the last rename wins.

    Raises what :func:`generate_thumbnail` raises, plus
    :class:`ThumbnailOutputError` when ffmpeg produced an empty file.
    """
    temp_path = dest_path.with_name(
        f"{dest_path.stem}{THUMBNAIL_TEMP_MARKER}"
        f"{os.getpid()}-{secrets.token_hex(4)}.jpg"
    )
    try:
        generate_thumbnail(
            ffmpeg_binary,
            source_path,
            temp_path,
            width=width,
            timeout=timeout,
            max_dimension_px=max_dimension_px,
        )
        if temp_path.stat().st_size == 0:
            raise ThumbnailOutputError("ffmpeg wrote an empty thumbnail")
        os.replace(temp_path, dest_path)
    finally:
        # After a successful rename the temporary name is already gone.
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()
