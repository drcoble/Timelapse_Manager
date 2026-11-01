"""A deliberately small multipart/form-data reader.

The web layer parses ``application/x-www-form-urlencoded`` bodies by hand
(:func:`.dependencies.parsed_form`) because FastAPI's ``Form()``/``File()``
need an optional dependency the project does not install. The two upload
routes -- the frame import and the problem-report form -- and the CSRF
middleware's no-JS path share this reader instead.

It is not a general parser: it reads each part's ``Content-Disposition`` and
``Content-Type`` and returns the text fields and the file parts. Two readers
share the part-header helpers:

* :func:`parse_multipart` splits a body already held in memory. It serves the
  small, capped forms (the problem report).
* :class:`MultipartParser` and :func:`stream_multipart` read the body chunk by
  chunk. The frame import uses them: file parts go to disk as they arrive, so
  the body is never buffered, and the size and file-count caps are enforced on
  the bytes received (#67 P1). The CSRF middleware reads the leading
  ``csrf_token`` part of such a body with the *same* :class:`MultipartParser`,
  so the token check and the upload cannot disagree about where a part starts.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, TYPE_CHECKING

import anyio
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

if TYPE_CHECKING:
    from ..config import Settings


@dataclass(frozen=True)
class FilePart:
    """One uploaded file part: its field name, client filename, type, bytes."""

    name: str
    filename: str
    content_type: str
    data: bytes = b""
    # Set for a part :func:`stream_multipart` spooled to disk (``data`` is then
    # empty): the file holding the part's bytes, and their count.
    path: Path | None = None
    size: int = 0


@dataclass
class MultipartForm:
    """The parsed body: text fields (last value wins) and file parts in order."""

    fields: dict[str, str] = field(default_factory=dict)
    files: list[FilePart] = field(default_factory=list)


def multipart_boundary(content_type: str) -> bytes | None:
    """Return the boundary token from a multipart Content-Type, or ``None``."""
    if not content_type.startswith("multipart/form-data"):
        return None
    for part in content_type.split(";"):
        key, _, value = part.strip().partition("=")
        if key.strip().lower() == "boundary":
            token = value.strip().strip('"')
            return token.encode("latin-1") if token else None
    return None


def _part_headers(header_blob: bytes) -> dict[str, str]:
    """Parse one part's header block into a lowercased-key dict."""
    headers: dict[str, str] = {}
    for line in header_blob.split(b"\r\n"):
        if not line:
            continue
        key, _, value = line.partition(b":")
        headers[key.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()
    return headers


def disposition_param(disposition: str, param: str) -> str | None:
    """Return a ``Content-Disposition`` parameter value (e.g. ``name``), or None."""
    for piece in disposition.split(";"):
        key, _, value = piece.strip().partition("=")
        if key.strip().lower() == param:
            return value.strip().strip('"')
    return None


def parse_multipart(body: bytes, content_type: str) -> MultipartForm:
    """Split a multipart/form-data body into text fields and file parts.

    A part with a ``filename`` parameter is a file; one whose filename is empty
    or whose content is empty (an empty file input) is skipped. Every other
    part is a text field, decoded as UTF-8 (undecodable bytes are replaced).
    Returns an empty form when the boundary is missing.
    """
    form = MultipartForm()
    boundary = multipart_boundary(content_type)
    if boundary is None:
        return form
    delimiter = b"--" + boundary
    # The first chunk is the preamble and the last the closing "--" epilogue;
    # both fail the header/blank-line split below and are dropped.
    for segment in body.split(delimiter):
        if not segment or segment in (b"--\r\n", b"--", b"\r\n"):
            continue
        segment = segment[2:] if segment.startswith(b"\r\n") else segment
        header_blob, sep, content = segment.partition(b"\r\n\r\n")
        if not sep:
            continue
        headers = _part_headers(header_blob)
        disposition = headers.get("content-disposition", "")
        name = disposition_param(disposition, "name")
        if not name:
            continue
        # Trim the CRLF the multipart framing appends after the content.
        if content.endswith(b"\r\n"):
            content = content[:-2]
        filename = disposition_param(disposition, "filename")
        if filename is None:
            form.fields[name] = content.decode("utf-8", errors="replace")
            continue
        if not filename or not content:
            continue
        form.files.append(
            FilePart(
                name=name,
                filename=filename,
                content_type=headers.get("content-type", "application/octet-stream"),
                data=content,
            )
        )
    return form


class BodyTooLargeError(Exception):
    """The request body passed the caller's byte limit while streaming in."""


async def read_body_limited(request: Request, limit: int) -> bytes:
    """Read the request body, refusing it as soon as it passes ``limit`` bytes.

    The limit is enforced on the bytes actually received, so a chunked body
    with no ``Content-Length``, or one whose ``Content-Length`` understates it,
    is cut off rather than buffered. The bytes are cached on ``request.state``
    (shared by the middleware and the route), so whichever layer reads first
    does the bounded read and the other reuses it.

    :raises BodyTooLargeError: once more than ``limit`` bytes have arrived.
    """
    cached = getattr(request.state, "limited_body", None)
    if isinstance(cached, bytes):
        return cached
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise BodyTooLargeError
        chunks.append(chunk)
    data = b"".join(chunks)
    request.state.limited_body = data
    # This read drained ``request.stream()``. Seed Starlette's own body cache
    # (``Request.body()``/``stream()``/``form()`` serve ``_body`` once it is
    # set) so a later ``request.body()`` on the same request -- e.g.
    # ``_form_getlist`` after ``parsed_form`` -- reads these bytes instead of
    # raising "Stream consumed".
    request._body = data
    return data


# The problem-report form's path, and the most its multipart body may carry:
# the upload cap plus headroom for the text fields and the framing.
REPORT_FORM_PATH = "/report"
_REPORT_FORM_OVERHEAD_BYTES = 256 * 1024


def report_body_limit(settings: Settings | None = None) -> int:
    """The byte limit for a ``POST /report`` body.

    Reads ``settings`` when given (the body-size middleware passes the ones it
    already holds), else the running process settings.
    """
    if settings is None:
        from ..runtime import get_context

        settings = get_context().settings
    return settings.reporting.max_upload_bytes + _REPORT_FORM_OVERHEAD_BYTES


# --- streaming reader --------------------------------------------------------

# The frame import, on the web UI and the API: the routes whose multipart body is
# read as a stream. ``web.body_limit`` classifies the same paths for its cap; a
# test pins the two patterns together.
FRAME_IMPORT_PATH = re.compile(r"(?:/api/v1)?/projects/[^/]+/frames/import")
# The raw-body single-frame upload (API only); never a form.
FRAME_UPLOAD_PATH = re.compile(r"/api/v1/projects/[^/]+/frames/upload")

# Largest text field, part-header block and preamble the parser accepts. A real
# form's fields are a few bytes; nothing legitimate comes near these.
MAX_FIELD_BYTES = 64 * 1024
MAX_PART_HEADER_BYTES = 16 * 1024
MAX_PREAMBLE_BYTES = 64 * 1024
# Most non-spooled parts (text fields, plus file parts that are dropped) one body
# may carry, and the most text the wanted fields may retain in total. Nothing
# legitimate sends more than a CSRF token and a handful of small fields.
MAX_EXTRA_PARTS = 32
MAX_TEXT_TOTAL_BYTES = 64 * 1024
# Spool writes are batched to at least this many bytes (one thread hop each).
SPOOL_WRITE_BYTES = 1024 * 1024

# Leftover spool directories (a crash or a shut-down executor) older than this
# are removed the next time an upload starts.
_STALE_SPOOL_SECONDS = 24 * 60 * 60
_SPOOL_DIR_PATTERN = re.compile(r"upload-[0-9a-f]{32}")


class MultipartError(ValueError):
    """The body is not well-formed multipart/form-data (a 400-class failure)."""


class TooManyFilesError(Exception):
    """The body carries more file parts than the caller allows."""


class SpoolReleasedError(RuntimeError):
    """A worker started after its request had already left and removed the spool."""


class InsufficientSpoolSpaceError(Exception):
    """Not enough free disk to spool the upload (a 507-class failure)."""


@dataclass(frozen=True)
class PartStart:
    """A part's headers are complete. ``filename`` is ``None`` for a text field."""

    name: str
    filename: str | None
    content_type: str


@dataclass(frozen=True)
class PartData:
    """Some of the current part's content (never empty)."""

    data: bytes


@dataclass(frozen=True)
class PartEnd:
    """The current part is complete."""


@dataclass(frozen=True)
class FormEnd:
    """The closing delimiter was seen; anything after it is epilogue."""


MultipartEvent = PartStart | PartData | PartEnd | FormEnd

_PREAMBLE, _AFTER_DELIMITER, _HEADERS, _CONTENT, _EPILOGUE = range(5)


class MultipartParser:
    """An incremental multipart/form-data reader: feed chunks, get events.

    Memory is bounded whatever the body: the buffer holds at most one chunk
    plus the tail that could still be the start of a delimiter. Chunk
    boundaries are invisible -- the events for a body are the same for every way
    of splitting it (tests fuzz this).

    The wire form is ``--B CRLF headers CRLF CRLF content CRLF --B ... CRLF
    --B-- [epilogue]``. A CRLF is prepended to the stream so every delimiter,
    the first included, is ``CRLF --B``. Anything before the first delimiter
    (the preamble) is dropped, up to :data:`MAX_PREAMBLE_BYTES`.

    :raises MultipartError: on a malformed body: an oversized header block, text
        field or preamble, a delimiter not followed by CRLF or ``--``, or (from
        :meth:`finish`) a body that ends before its closing delimiter.
    """

    def __init__(self, boundary: bytes) -> None:
        self._delimiter = b"\r\n--" + boundary
        self._buf = bytearray(b"\r\n")
        self._state = _PREAMBLE
        self._discarded = 0
        self._is_file = False
        self._part_bytes = 0

    @property
    def done(self) -> bool:
        """Whether the closing delimiter has been seen."""
        return self._state == _EPILOGUE

    def feed(self, chunk: bytes) -> list[MultipartEvent]:
        """Consume ``chunk`` and return the events it completes."""
        events: list[MultipartEvent] = []
        if self._state == _EPILOGUE:
            return events
        self._buf += chunk
        while self._step(events):
            pass
        return events

    def finish(self) -> None:
        """Check the body ended cleanly.

        :raises MultipartError: when it ended before the closing delimiter.
        """
        if self._state != _EPILOGUE:
            raise MultipartError("the multipart body ended before its closing boundary")

    def _step(self, events: list[MultipartEvent]) -> bool:
        """Advance the state machine once; return whether it made progress."""
        buf = self._buf
        if self._state == _PREAMBLE:
            return self._preamble(buf)
        if self._state == _AFTER_DELIMITER:
            return self._after_delimiter(buf, events)
        if self._state == _HEADERS:
            return self._headers(buf, events)
        return self._content(buf, events)

    def _preamble(self, buf: bytearray) -> bool:
        index = buf.find(self._delimiter)
        if index >= 0:
            del buf[: index + len(self._delimiter)]
            self._state = _AFTER_DELIMITER
            return True
        keep = len(self._delimiter) - 1
        if len(buf) > keep:
            self._discarded += len(buf) - keep
            del buf[: len(buf) - keep]
            if self._discarded > MAX_PREAMBLE_BYTES:
                raise MultipartError("the multipart body has no first boundary")
        return False

    def _after_delimiter(self, buf: bytearray, events: list[MultipartEvent]) -> bool:
        if buf[:2] == b"--":
            self._state = _EPILOGUE
            buf.clear()
            events.append(FormEnd())
            return False
        end = buf.find(b"\r\n")
        if end < 0:
            # Not a full line yet. Only whitespace, a lone "-" (maybe the start
            # of "--") or a trailing CR may be waiting here.
            pending = bytes(buf).rstrip(b"\r")
            if len(buf) > 1024 or (pending != b"-" and pending.strip(b" \t")):
                raise MultipartError("a boundary is not followed by a line break")
            return False
        if buf[:end].strip(b" \t"):
            # RFC 2046 allows trailing whitespace after a boundary, nothing else.
            raise MultipartError("a boundary is not followed by a line break")
        del buf[: end + 2]
        self._state = _HEADERS
        return True

    def _headers(self, buf: bytearray, events: list[MultipartEvent]) -> bool:
        if buf.startswith(b"\r\n"):
            blob = b""
            consumed = 2
        else:
            end = buf.find(b"\r\n\r\n")
            if end < 0:
                if len(buf) > MAX_PART_HEADER_BYTES:
                    raise MultipartError("a multipart part header is too large")
                return False
            if end > MAX_PART_HEADER_BYTES:
                raise MultipartError("a multipart part header is too large")
            blob = bytes(buf[:end])
            consumed = end + 4
        del buf[:consumed]
        headers = _part_headers(blob)
        disposition = headers.get("content-disposition", "")
        name = disposition_param(disposition, "name") or ""
        filename = disposition_param(disposition, "filename")
        self._is_file = filename is not None
        self._part_bytes = 0
        self._state = _CONTENT
        events.append(
            PartStart(
                name=name,
                filename=filename,
                content_type=headers.get("content-type", "application/octet-stream"),
            )
        )
        return True

    def _content(self, buf: bytearray, events: list[MultipartEvent]) -> bool:
        index = buf.find(self._delimiter)
        if index >= 0:
            self._emit(bytes(buf[:index]), events)
            del buf[: index + len(self._delimiter)]
            events.append(PartEnd())
            self._state = _AFTER_DELIMITER
            return True
        # The tail could still be the start of a delimiter: hold it back.
        safe = len(buf) - (len(self._delimiter) - 1)
        if safe > 0:
            self._emit(bytes(buf[:safe]), events)
            del buf[:safe]
        return False

    def _emit(self, data: bytes, events: list[MultipartEvent]) -> None:
        if not data:
            return
        self._part_bytes += len(data)
        if not self._is_file and self._part_bytes > MAX_FIELD_BYTES:
            raise MultipartError("a multipart text field is too large")
        events.append(PartData(data))


class _SpoolFile:
    """One file being written to the spool, with batched threadpool writes."""

    def __init__(self, path: Path, limit: int, free_floor: int = 0) -> None:
        self.path = path
        self.size = 0
        self._limit = limit
        self._free_floor = free_floor
        self._handle: IO[bytes] | None = None
        self._pending: list[bytes] = []
        self._pending_bytes = 0

    async def open(self) -> None:
        # Exclusive create: a collision (impossible with a uuid) never overwrites.
        self._handle = await run_in_threadpool(self.path.open, "xb")

    async def write(self, data: bytes) -> None:
        if self.size + len(data) > self._limit:
            raise BodyTooLargeError
        if self._handle is None:
            # Opened on the first byte, so an empty part never touches the disk.
            await self.open()
        self.size += len(data)
        self._pending.append(data)
        self._pending_bytes += len(data)
        if self._pending_bytes >= SPOOL_WRITE_BYTES:
            await self._flush()

    async def _flush(self) -> None:
        if self._handle is None or not self._pending:
            return
        blob = b"".join(self._pending)
        self._pending = []
        self._pending_bytes = 0
        await run_in_threadpool(self._write_checked, self._handle, blob)

    def _write_checked(self, handle: IO[bytes], blob: bytes) -> None:
        """Write one batch, refusing it if the disk would fall under the floor.

        Checked once per batch (about every MiB), so a chunked body, or several
        uploads at once, cannot push free space below the watermark the capture
        loop pauses at (#219).
        """
        if self._free_floor:
            free = shutil.disk_usage(self.path.parent).free
            if free - len(blob) < self._free_floor:
                raise InsufficientSpoolSpaceError(
                    "not enough free disk space to accept this upload"
                )
        handle.write(blob)

    async def close(self) -> None:
        """Flush and close; the file is complete afterwards."""
        try:
            await self._flush()
        finally:
            await self.abort()

    async def abort(self) -> None:
        """Close the handle without flushing (error path). Safe to repeat."""
        handle, self._handle = self._handle, None
        if handle is not None:
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(handle.close)


async def stream_multipart(
    request: Request,
    *,
    spool_dir: Path,
    file_field: str,
    text_fields: frozenset[str] = frozenset(),
    max_file_bytes: int,
    max_total_bytes: int,
    max_files: int,
    low_watermark_bytes: int = 0,
) -> MultipartForm:
    """Read a multipart body as a stream, spooling file parts to ``spool_dir``.

    Only the text fields named in ``text_fields`` are kept (in memory, each at
    most :data:`MAX_FIELD_BYTES`, :data:`MAX_TEXT_TOTAL_BYTES` together); every
    other text part is read and dropped without being stored. A body with more
    than :data:`MAX_EXTRA_PARTS` such dropped or text parts is refused. A file
    part named ``file_field`` with a filename is written to
    ``spool_dir/<uuid>`` as it arrives (the client's filename is only a label,
    never a path component); file parts under any other name, or with an empty
    filename, are read and dropped. An empty file is dropped too, like the
    in-memory reader. All the limits are enforced on bytes as they arrive, so
    nothing beyond them is ever buffered or written. File I/O runs in the
    threadpool.

    The caller owns ``spool_dir`` and removes it (see :func:`upload_spool`).

    :param max_file_bytes: most bytes one spooled file may hold.
    :param max_total_bytes: most bytes the whole body may carry.
    :param max_files: most ``file_field`` file parts accepted (empty ones too).
    :param low_watermark_bytes: free space that must remain while spooling.
    :raises InsufficientSpoolSpaceError: when spooling would pass that floor.
    :raises BodyTooLargeError: past ``max_file_bytes`` or ``max_total_bytes``.
    :raises TooManyFilesError: when a further file part would pass ``max_files``.
    :raises MultipartError: on a malformed body, a missing boundary, an
        oversized text part, or too many non-file parts.
    """
    boundary = multipart_boundary(request.headers.get("content-type", ""))
    if boundary is None:
        raise MultipartError("the request is not multipart/form-data with a boundary")
    parser = MultipartParser(boundary)
    form = MultipartForm()
    total = 0
    name = ""
    filename: str | None = None
    content_type = ""
    text = bytearray()
    keep_text = False
    extra_parts = 0
    file_parts = 0
    text_total = 0
    spool: _SpoolFile | None = None
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > max_total_bytes:
                raise BodyTooLargeError
            for event in parser.feed(chunk):
                if isinstance(event, PartStart):
                    name, filename, content_type = (
                        event.name,
                        event.filename,
                        event.content_type,
                    )
                    text.clear()
                    spool = None
                    keep_text = filename is None and name in text_fields
                    if not (filename and name == file_field):
                        extra_parts += 1
                        if extra_parts > MAX_EXTRA_PARTS:
                            raise MultipartError(
                                "the multipart body has too many parts"
                            )
                    if filename and name == file_field:
                        # Every such part counts, empty ones too (#220); the
                        # file itself is created on its first byte.
                        file_parts += 1
                        if file_parts > max_files:
                            raise TooManyFilesError
                        spool = _SpoolFile(
                            spool_dir / uuid.uuid4().hex,
                            max_file_bytes,
                            low_watermark_bytes,
                        )
                elif isinstance(event, PartData):
                    if spool is not None:
                        await spool.write(event.data)
                    elif keep_text:
                        text_total += len(event.data)
                        if text_total > MAX_TEXT_TOTAL_BYTES:
                            raise MultipartError(
                                "the multipart text fields are too large"
                            )
                        text += event.data
                elif isinstance(event, PartEnd):
                    if spool is not None:
                        await spool.close()
                        if spool.size:
                            form.files.append(
                                FilePart(
                                    name=name,
                                    filename=filename or "",
                                    content_type=content_type,
                                    path=spool.path,
                                    size=spool.size,
                                )
                            )
                        spool = None
                    elif keep_text:
                        form.fields[name] = bytes(text).decode(
                            "utf-8", errors="replace"
                        )
                    name, filename = "", None
        parser.finish()
    except BaseException:
        if spool is not None:
            await spool.abort()
        raise
    return form


async def stream_body_to_file(
    request: Request, path: Path, *, max_bytes: int, low_watermark_bytes: int = 0
) -> int:
    """Write the raw request body to ``path``, refusing more than ``max_bytes``.

    For the single-image upload: the body is spooled rather than held in memory
    on the event loop. Returns the byte count.

    :raises BodyTooLargeError: once more than ``max_bytes`` have arrived.
    """
    spool = _SpoolFile(path, max_bytes, low_watermark_bytes)
    try:
        async for chunk in request.stream():
            await spool.write(chunk)
        await spool.close()
    except BaseException:
        await spool.abort()
        raise
    return spool.size


# --- spool directory ---------------------------------------------------------


class UploadSpool:
    """A per-request spool directory with exactly one owner at a time.

    The request owns it until a worker :meth:`claim` it by starting; from then
    the worker removes it (:meth:`finish`) and the import runs to completion even
    if the client goes away. If the request ends first -- cancelled while the job
    is still queued, or the executor shut down and cancelled the job -- its
    :meth:`release` removes the directory and a worker that starts later finds
    :meth:`claim` refused and does nothing. The lock makes the hand-over atomic,
    so the directory is removed exactly once and never under a running worker.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._state = "request"  # request | worker | gone

    def claim(self) -> bool:
        """Take ownership for the worker; ``False`` if the request already left."""
        with self._lock:
            if self._state != "request":
                return False
            self._state = "worker"
            return True

    def release(self) -> None:
        """Request exit: remove the directory unless a worker owns it."""
        with self._lock:
            if self._state != "request":
                return
            self._state = "gone"
        remove_spool(self.path)

    def claim_or_raise(self) -> None:
        """:meth:`claim`, raising :class:`SpoolReleasedError` when refused."""
        if not self.claim():
            raise SpoolReleasedError("the request ended before the job started")

    def finish(self) -> None:
        """Worker exit: remove the directory."""
        with self._lock:
            self._state = "gone"
        remove_spool(self.path)


def remove_spool(path: Path) -> None:
    """Remove a spool directory and everything in it (idempotent, never raises)."""
    shutil.rmtree(path, ignore_errors=True)


def spool_root(data_dir: Path) -> Path:
    """The directory every upload spool lives under (``<data_dir>/tmp``)."""
    return data_dir / "tmp"


def _create_spool(
    root: Path, *, expected_bytes: int, low_watermark_bytes: int
) -> UploadSpool:
    """Create a private spool directory after the stale sweep and a space check."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _sweep_stale_spools(root)
    free = shutil.disk_usage(root).free
    if free - max(expected_bytes, 0) < low_watermark_bytes:
        raise InsufficientSpoolSpaceError(
            "not enough free disk space to accept this upload"
        )
    path = root / f"upload-{uuid.uuid4().hex}"
    path.mkdir(mode=0o700)
    return UploadSpool(path)


def _sweep_stale_spools(root: Path) -> None:
    """Remove ``upload-*`` directories left behind by a crash, once they are old."""
    cutoff = time.time() - _STALE_SPOOL_SECONDS
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    for entry in entries:
        try:
            if (
                _SPOOL_DIR_PATTERN.fullmatch(entry.name)
                and entry.is_dir(follow_symlinks=False)
                and entry.stat(follow_symlinks=False).st_mtime < cutoff
            ):
                shutil.rmtree(entry.path, ignore_errors=True)
        except OSError:
            continue


@asynccontextmanager
async def upload_spool(
    data_dir: Path, *, expected_bytes: int, low_watermark_bytes: int
) -> AsyncIterator[UploadSpool]:
    """Create a spool directory for one upload and remove it on every exit.

    :param expected_bytes: the declared body size (``Content-Length``), or 0.
    :param low_watermark_bytes: the free space that must remain afterwards
        (``storage.low_watermark_bytes``), so an upload cannot fill the disk the
        capture loop shares.
    :raises InsufficientSpoolSpaceError: when the free space is below that.

    The directory is removed in a shielded block, so a cancelled request (the
    client went away) still cleans up, unless a worker has claimed it
    (:meth:`UploadSpool.claim`).
    """
    spool = await run_in_threadpool(
        _create_spool,
        spool_root(data_dir),
        expected_bytes=expected_bytes,
        low_watermark_bytes=low_watermark_bytes,
    )
    try:
        yield spool
    finally:
        with anyio.CancelScope(shield=True):
            await run_in_threadpool(spool.release)
