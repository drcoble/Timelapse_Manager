"""FFmpeg-backed :class:`~.encoder.Encoder` implementation.

This is the only place that assembles and runs an ffmpeg command line. Several
invariants are enforced here rather than trusted from the caller:

* **Argv, never a shell.** ffmpeg is launched with
  :func:`asyncio.create_subprocess_exec` and an argument *list*; no value is ever
  interpreted by a shell. A hostile frame path or overlay string cannot inject a
  command.
* **Allowlist before spawn.** Codec, container, every numeric parameter, and
  every filter name are validated against :mod:`.allowlist` before a process
  starts. An unsupported request raises and nothing is spawned.
* **Path confinement.** The output path -- and any overlay image -- must resolve
  to a location inside the project's render root, checked with
  :meth:`pathlib.Path.is_relative_to`, not a string prefix.
* **Untrusted concat list.** Frame paths come from the database, so each is
  written into the concat list with ffmpeg's single-quote escaping (``'`` ->
  ``'\\''``); a bare ``file <path>`` line is never written. A path containing a
  line break or NUL, which would end the ``file`` line early, is refused, and
  the concat input may open local files only (``-protocol_whitelist file``).
* **The final filtergraph is checked.** The finished graph string is parsed the
  way ffmpeg parses it; its filter names must equal, in order, the filters the
  builder meant to emit, each on the allowlist, and each ``drawtext`` must carry
  exactly the options the builder wrote.
* **One timestamp drawtext, ever.** The capture-time overlay is a single
  ``drawtext`` reading each frame's presentation timestamp, so a months-long
  project does not explode into tens of thousands of filters.
* **Source frames are never modified.** Overlays are burned into the output at
  encode time only; the input files are read-only.
* **Cancellation kills the child.** Cancelling the render terminates ffmpeg and
  removes any partial output before propagating.
* **A stuck encode cannot hold the render slot.** ffmpeg reports progress on
  stdout (``-progress pipe:1``); a watchdog kills it, removes the partial output
  and raises :class:`EncoderTimeoutError` when no progress counter has advanced
  for the stall timeout, or when the render outlives its wall-clock ceiling.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shlex
import tempfile
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import allowlist, hwaccel, overlay
from .browser_streamable import is_browser_streamable
from .encoder import (
    Chapter,
    Encoder,
    EncoderCapabilityError,
    EncoderError,
    FrameRef,
    OutputSettings,
    RenderResult,
    RenderSpec,
)

logger = logging.getLogger(__name__)

FFMPEG_BINARY = "ffmpeg"

# Containers that can carry chapter markers. WebM (Matroska's subset as muxed by
# ffmpeg's webm muxer) does not, so chapters in WebM are rejected at validation.
_CHAPTER_CAPABLE_CONTAINERS: frozenset[str] = frozenset({"mp4", "mkv"})

# Default font locations probed when no font is supplied. drawtext requires a
# fontfile; a bundled deployment should pass one explicitly, but these cover the
# common development platforms so the overlay works out of the box.
_DEFAULT_FONT_CANDIDATES: tuple[str, ...] = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
)

# Muxers whose index (``moov``) ffmpeg writes at the end unless told otherwise.
# ``+faststart`` moves it to the front so a browser can start playback (and
# seek) without first fetching the tail of the file. MP4 is the only such
# container on the allowlist (``allowlist.CONTAINER_MUXERS``).
_FASTSTART_MUXERS: frozenset[str] = frozenset({"mp4"})

# Every encoder that produces H.265: the software one plus the hardware ones,
# derived from the allowlist so a new hardware HEVC encoder is covered too.
_HEVC_ENCODERS: frozenset[str] = frozenset({"libx265"}) | frozenset(
    name for name, codec in allowlist.HW_ENCODER_CODECS.items() if codec == "h265"
)

_DEFAULT_FONT_COLOR = "white"
_DEFAULT_FONT_SIZE = 24

# Render watchdog (#67 R2). The defaults mirror ``RenderSettings``.
DEFAULT_STALL_TIMEOUT_SECONDS = 300.0
# ``0`` means "derived from the frame count": see :func:`render_time_ceiling`.
DEFAULT_MAX_JOB_SECONDS = 0.0
# The derived ceiling: never below an hour, and two seconds per frame above that.
_MIN_DERIVED_CEILING_SECONDS = 3600.0
_DERIVED_SECONDS_PER_FRAME = 2.0
# How often the watchdog checks the limits. Shortened automatically for small
# limits so a low stall timeout is still enforced promptly.
_WATCHDOG_TICK_SECONDS = 5.0
# The ``-progress`` keys whose increase counts as progress. Both are integers;
# ``N/A`` (ffmpeg's value before the first output frame) is ignored.
_PROGRESS_KEYS: frozenset[bytes] = frozenset({b"frame", b"out_time_us"})
# Pipe reading: fixed-size chunks, with any single line capped so a flood of
# output without newlines cannot grow memory without bound.
_PIPE_READ_CHUNK = 64 * 1024
_MAX_LINE_BYTES = 4096
# Lines of stderr kept for the failure message (the last non-empty one is used).
_STDERR_TAIL_LINES = 50
# After ffmpeg exits (or is killed), how long to wait for its pipes to reach EOF.
_READER_GRACE_SECONDS = 5.0


class EncoderTimeoutError(EncoderError):
    """ffmpeg was killed by the render watchdog.

    The message is the reason the render failed, for example
    ``ffmpeg stalled: no progress for 300s`` or
    ``ffmpeg exceeded max_job_seconds (3600s)``; the render queue records it as
    the job's failure reason.
    """


def render_time_ceiling(max_job_seconds: float, frame_count: int) -> float:
    """Return the wall-clock ceiling, in seconds, for one render.

    A positive ``max_job_seconds`` is used as is. ``0`` derives the ceiling from
    the frame count: ``max(3600, 2 * frame_count)``.
    """
    if max_job_seconds > 0:
        return max_job_seconds
    return max(_MIN_DERIVED_CEILING_SECONDS, _DERIVED_SECONDS_PER_FRAME * frame_count)


def _format_seconds(seconds: float) -> str:
    """Format a limit for a failure reason: ``300s``, ``0.5s``."""
    if seconds == int(seconds):
        return f"{int(seconds)}s"
    return f"{seconds:.2f}".rstrip("0").rstrip(".") + "s"


class _ProgressTracker:
    """Follow ffmpeg's ``-progress`` output and report real advances.

    ffmpeg writes a block of ``key=value`` lines to the progress URL about twice
    a second, ending each block with ``progress=continue`` (``progress=end`` for
    the last). A block arriving is not progress: a wedged encode can repeat the
    same values. Only a strict increase of ``frame`` or ``out_time_us`` counts.
    """

    def __init__(self) -> None:
        """Start with no value seen for any key."""
        self._highest: dict[bytes, int] = {}

    def feed(self, line: bytes) -> bool:
        """Consume one output line; return ``True`` if a counter advanced."""
        key, sep, value = line.strip().partition(b"=")
        if not sep or key not in _PROGRESS_KEYS:
            return False
        try:
            number = int(value)
        except ValueError:
            return False
        previous = self._highest.get(key)
        if previous is not None and number <= previous:
            return False
        self._highest[key] = number
        return True


async def _read_lines(
    stream: asyncio.StreamReader, on_line: Callable[[bytes], None]
) -> None:
    """Read ``stream`` to EOF, calling ``on_line`` for each line.

    Reads fixed-size chunks instead of :meth:`asyncio.StreamReader.readline`,
    which raises on a line longer than its 64 KiB limit. A line longer than
    ``_MAX_LINE_BYTES`` is truncated, so memory stays bounded however the child
    writes. A final line without a trailing newline is still delivered.
    """
    pending = bytearray()
    while True:
        chunk = await stream.read(_PIPE_READ_CHUNK)
        if not chunk:
            break
        pending += chunk
        *complete, rest = pending.split(b"\n")
        for line in complete:
            on_line(bytes(line[:_MAX_LINE_BYTES]))
        pending = rest[:_MAX_LINE_BYTES]
    if pending:
        on_line(bytes(pending))


def _last_nonempty_line(lines: deque[bytes]) -> str | None:
    """Return the last non-blank line of ``lines``, decoded, or ``None``."""
    for line in reversed(lines):
        text = line.decode("utf-8", errors="replace").strip()
        if text:
            return text
    return None


# Characters that end a line for the concat demuxer's and ffmetadata's line
# readers, so cannot appear inside one entry.
_LINE_BREAK_CHARS: frozenset[str] = frozenset("\r\n\0")

# The options every overlay ``drawtext`` carries, in the order they are written.
# The final-graph check requires exactly this sequence, so a value that escaped
# its quoting and added an option (``textfile=``, say) fails the render.
_DRAWTEXT_OPTION_KEYS: tuple[str, ...] = (
    "fontfile",
    "text",
    "x",
    "y",
    "fontcolor",
    "fontsize",
    "box",
    "boxcolor",
)


def _drawtext_filter(font: str, text: str, x: str, y: str) -> str:
    """Return one overlay ``drawtext`` filter with :data:`_DRAWTEXT_OPTION_KEYS`.

    ``font`` and ``text`` must already be escaped for their single-quoted
    position (:func:`.overlay.escape_path_for_filter`,
    :func:`.overlay.escape_drawtext`, or the timestamp expansion).
    """
    return (
        f"drawtext=fontfile='{font}':text='{text}'"
        f":x={x}:y={y}"
        f":fontcolor={_DEFAULT_FONT_COLOR}:fontsize={_DEFAULT_FONT_SIZE}"
        f":box=1:boxcolor=black@0.4"
    )


def _verify_filtergraph(graph: str, expected: list[str], *, allow_hw: bool) -> None:
    """Check the finished filtergraph against what the builder meant to emit.

    The graph is parsed as ffmpeg parses it (:func:`.allowlist.parse_filtergraph`).
    The parsed filter names must equal ``expected`` exactly, in order, and are
    then checked against the allowlist; each ``drawtext`` must carry exactly
    :data:`_DRAWTEXT_OPTION_KEYS`. Comparing the ordered list (not a set) is what
    catches an injected *allowlisted* filter, such as a second ``drawtext``.

    :raises EncoderError: ``filtergraph mismatch`` when the parsed graph differs.
    :raises EncoderCapabilityError: when a parsed filter is not on the allowlist.
    """
    parsed = allowlist.parse_filtergraph(graph)
    names = [f.name for f in parsed]
    if names != expected:
        raise EncoderError(
            f"filtergraph mismatch: expected filters {expected!r}, parsed {names!r}"
        )
    allowlist.ensure_filters_allowed(names, allow_hw=allow_hw)
    for parsed_filter in parsed:
        if parsed_filter.name != "drawtext":
            continue
        options = allowlist.parse_filter_options(parsed_filter.args or "")
        keys = tuple(key for key, _ in options)
        if keys != _DRAWTEXT_OPTION_KEYS:
            raise EncoderError(
                f"filtergraph mismatch: drawtext options {list(keys)!r}, "
                f"expected {list(_DRAWTEXT_OPTION_KEYS)!r}"
            )


def _escape_ffmetadata_value(value: str) -> str:
    r"""Escape a value for one ``key=value`` line of an ffmetadata file.

    ffmetadata's reader (libavformat ``ffmetadec.c``) ends a line at ``\r``,
    ``\n`` or NUL unless the previous byte is a backslash, and its unescape step
    takes the character after any backslash literally. So:

    * line breaks and tabs become spaces and other control characters are
      dropped;
    * ``\``, ``=``, ``;`` and ``#`` are backslash-escaped;
    * trailing backslashes are dropped. The reader remembers only the previous
      raw byte, so an escaped backslash (``\\``) at the end of the line still
      escapes the line break: a title ending in ``\`` swallowed the next line,
      the following chapter's ``[CHAPTER]`` header.
    """
    cleaned = value.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    cleaned = cleaned.replace("\t", " ")
    cleaned = "".join(ch for ch in cleaned if ch >= " " and ch != "\x7f")
    cleaned = cleaned.rstrip("\\")
    return (
        cleaned.replace("\\", "\\\\")
        .replace("=", "\\=")
        .replace(";", "\\;")
        .replace("#", "\\#")
    )


def _escape_concat_path(path: str) -> str:
    r"""Escape a path for an ffmpeg concat-demuxer ``file`` line.

    The concat demuxer treats a single-quoted argument literally except for the
    quote character itself, which is escaped by closing the quote, inserting an
    escaped quote, and reopening (``'`` -> ``'\''``). The returned value is the
    inner content; the caller wraps it in ``file '<...>'``.

    Quoting cannot protect a line break: the demuxer splits the list into lines
    before it tokenises, so ``\r``, ``\n`` or NUL would end the entry and start
    a new directive (#67 S15).

    :raises EncoderError: if ``path`` contains ``\r``, ``\n`` or NUL.
    """
    if not _LINE_BREAK_CHARS.isdisjoint(path):
        raise EncoderError(
            "frame path contains a line break or NUL; refusing to write it to "
            "the concat list"
        )
    return path.replace("'", "'\\''")


def _find_default_font() -> str | None:
    """Return the first existing default font path, or ``None`` if none exist."""
    for candidate in _DEFAULT_FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def _timezone_offset_seconds(tz_name: str, at: datetime) -> int:
    """Return the UTC offset in seconds for ``tz_name`` at instant ``at``.

    The offset is computed once, at the sequence's first capture time, and used
    for the whole render. A render that spans a DST transition therefore shows
    the offset in effect at its start throughout; this is a deliberate, noted
    simplification (a single drawtext cannot switch named-zone rules mid-stream).

    :raises EncoderError: if ``tz_name`` is not a known IANA zone.
    """
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise EncoderError(f"unknown timestamp timezone: {tz_name!r}") from exc
    offset = tz.utcoffset(at.replace(tzinfo=None))
    return int(offset.total_seconds()) if offset is not None else 0


def _quality_args(output: OutputSettings, encoder_name: str) -> list[str]:
    """Return the ``-crf``/``-b:v`` quality arguments for a software encode.

    Prefers an explicit CRF, then an explicit bitrate; with neither set the
    encoder's own default applies and no quality argument is emitted. VP9 needs
    ``-b:v 0`` alongside ``-crf`` to run in constant-quality mode.
    """
    args: list[str] = []
    if output.crf is not None:
        args += ["-crf", str(output.crf)]
        if encoder_name == "libvpx-vp9":
            args += ["-b:v", "0"]
    elif output.bitrate_kbps is not None:
        args += ["-b:v", f"{output.bitrate_kbps}k"]
    return args


# Per-API constant-quality flag: each hardware encoder spells its CRF-equivalent
# rate-control knob differently. The configured ``crf`` (0..63, same scale the
# software path validates) is passed through to the API's nearest equivalent.
_HW_QUALITY_FLAG: dict[str, str] = {
    "nvenc": "-cq",
    "qsv": "-global_quality",
    "vaapi": "-qp",
}


def _playback_args(encoder_name: str, muxer: str) -> list[str]:
    """Return the muxer flags that make an output play well in a browser.

    * ``-movflags +faststart`` for MP4, so the index precedes the media.
    * ``-tag:v hvc1`` for H.265 in MP4: ffmpeg defaults to ``hev1``, which
      Safari refuses. Applies to every HEVC encoder, software or hardware.
    """
    if muxer not in _FASTSTART_MUXERS:
        return []
    args = ["-movflags", "+faststart"]
    if encoder_name in _HEVC_ENCODERS:
        args += ["-tag:v", "hvc1"]
    return args


def _hw_quality_args(output: OutputSettings, api: str) -> list[str]:
    """Return the quality arguments for a hardware encode on ``api``.

    Mirrors :func:`_quality_args` but maps the CRF-style knob onto each API's own
    rate control: ``-cq`` (NVENC), ``-global_quality`` (QSV), ``-qp`` (VA-API).
    An explicit bitrate takes the same ``-b:v`` form on every API. With neither
    set the encoder's default applies.
    """
    args: list[str] = []
    if output.crf is not None:
        args += [_HW_QUALITY_FLAG[api], str(output.crf)]
    elif output.bitrate_kbps is not None:
        args += ["-b:v", f"{output.bitrate_kbps}k"]
    return args


# The hardware APIs whose encoders consume frames from GPU memory, so the
# filtergraph must end in ``hwupload``. NVENC is absent: it accepts
# system-memory frames, so its tail is a plain pixel-format conversion.
_HWUPLOAD_APIS: frozenset[str] = frozenset({"vaapi", "qsv"})


def _format_tail_filters(choice: hwaccel.EncoderChoice) -> list[str]:
    """Return the trailing pixel-format/upload filters for ``choice``.

    * Software and NVENC: ``["format=yuv420p"]`` -- frames stay in system memory
      (NVENC reads them directly), exactly as the software path always has.
    * VA-API and QSV: ``["format=nv12", "hwupload"]`` -- convert to the layout
      the GPU encoder wants, then upload into device memory.
    """
    if choice.hwaccel_api in _HWUPLOAD_APIS:
        return ["format=nv12", "hwupload"]
    return ["format=yuv420p"]


def _hw_init_args(choice: hwaccel.EncoderChoice, device: str | None) -> list[str]:
    """Return the input-side init flags for a hardware encode.

    Emitted before the input so ffmpeg sets up the device context up front:

    * VA-API: ``-vaapi_device <dev>`` only. Frames are deliberately kept in
      system memory so the software filtergraph (fps/scale/drawtext/overlay) can
      process them; the trailing ``format=nv12,hwupload`` then promotes them onto
      the VA-API device for the encoder. Emitting ``-hwaccel_output_format vaapi``
      here would deliver GPU surfaces that the software filters cannot touch, so
      it is intentionally omitted. A render-node path such as
      ``/dev/dri/renderD128`` must be configured for ``hwupload`` to find a device.
    * QSV: ``-init_hw_device qsv[:<dev>]`` so the encoder has a QSV device.
    * NVENC: optionally ``-hwaccel cuda``; NVENC otherwise needs no device init
      because it reads system-memory frames. A GPU index is passed to the
      encoder via ``-gpu`` (see :func:`_hw_encoder_args`), not here.

    Returns an empty list on the software path.
    """
    api = choice.hwaccel_api
    if api == "vaapi":
        return ["-vaapi_device", device] if device else []
    if api == "qsv":
        spec = f"qsv:{device}" if device else "qsv"
        return ["-init_hw_device", spec]
    if api == "nvenc":
        return ["-hwaccel", "cuda"]
    return []


def _hw_encoder_args(
    choice: hwaccel.EncoderChoice,
    output: OutputSettings,
    device: str | None,
) -> list[str]:
    """Return the encoder-side flags (``-c:v``, quality, device) for ``choice``.

    Includes the hardware ``-c:v``, the API-appropriate rate-control mapped from
    the requested quality, and -- for NVENC -- an optional ``-gpu`` index. Unlike
    the software path, no trailing ``-pix_fmt`` is added for VA-API/QSV (the
    uploaded surface format governs); NVENC keeps ``-pix_fmt yuv420p`` since it
    encodes from system memory.
    """
    api = choice.hwaccel_api
    assert api is not None  # callers gate on choice.is_hardware
    args = ["-c:v", choice.encoder_name]
    args += _hw_quality_args(output, api)
    if api == "nvenc":
        if device:
            args += ["-gpu", device]
        args += ["-pix_fmt", "yuv420p"]
    return args


class FfmpegEncoder(Encoder):
    """Encode a project's frames to video by driving ffmpeg as a subprocess."""

    def __init__(
        self,
        *,
        ffmpeg_binary: str = FFMPEG_BINARY,
        font_path: str | None = None,
        hwaccel_enabled: bool = False,
        hwaccel_api: str | None = None,
        hwaccel_device: str | None = None,
        available_hw_encoders: frozenset[str] | None = None,
        stall_timeout_seconds: float = DEFAULT_STALL_TIMEOUT_SECONDS,
        max_job_seconds: float = DEFAULT_MAX_JOB_SECONDS,
        watchdog_clock: Callable[[], float] | None = None,
    ) -> None:
        """Create an encoder.

        :param ffmpeg_binary: the ffmpeg executable to invoke (on ``PATH`` by
            default).
        :param font_path: TrueType font for text/timestamp overlays. When unset,
            a platform default is probed lazily at render time; a render that
            needs an overlay with no available font raises a clear error.
        :param hwaccel_enabled: enable GPU-accelerated encoding. Off by default,
            which keeps every render on the software path byte-for-byte. When on,
            each render uses a hardware encoder if one is available for its codec
            and falls back to software otherwise -- a hardware miss never fails a
            render.
        :param hwaccel_api: the hardware encode API to use when enabled (one of
            ``"nvenc"``, ``"qsv"``, ``"vaapi"``), or ``None`` for software-only.
        :param hwaccel_device: optional device selector for the chosen API (a
            VA-API render-node path or an NVENC GPU index); the API default is
            used when unset.
        :param available_hw_encoders: the set of hardware encoder names this
            ffmpeg build provides. Injected for testing; when ``None`` (and
            hardware is enabled) it is probed lazily and cached on first render.
        :param stall_timeout_seconds: kill ffmpeg when neither its frame count
            nor its output time has advanced for this long. Must be positive.
        :param max_job_seconds: kill ffmpeg when one render runs longer than
            this. ``0`` derives the ceiling from the frame count (see
            :func:`render_time_ceiling`). Must not be negative.
        :param watchdog_clock: the monotonic clock, in seconds, the stall timeout
            and ceiling are measured on. Defaults to the event loop's clock. A
            test seam: a test that starts a real child process can hold the
            clock until the child is ready, so a slow interpreter start under
            load is not counted as a stall.
        :raises ValueError: if either watchdog limit is out of range.
        """
        if stall_timeout_seconds <= 0:
            raise ValueError("stall_timeout_seconds must be positive")
        if max_job_seconds < 0:
            raise ValueError("max_job_seconds must not be negative")
        self._ffmpeg_binary = ffmpeg_binary
        self._font_path = font_path
        self._hwaccel_enabled = hwaccel_enabled
        self._hwaccel_api = hwaccel_api
        self._hwaccel_device = hwaccel_device
        self._available_hw_encoders = available_hw_encoders
        self._stall_timeout_seconds = stall_timeout_seconds
        self._max_job_seconds = max_job_seconds
        self._watchdog_clock = watchdog_clock

    def _hw_capabilities(self) -> frozenset[str]:
        """Return the available hardware encoders, probing lazily if needed.

        When hardware acceleration is off the probe is skipped entirely (it
        cannot affect the software path). When on, an injected capability set is
        returned as-is; otherwise the local ffmpeg is probed once and cached.
        """
        if not self._hwaccel_enabled:
            return frozenset()
        if self._available_hw_encoders is not None:
            return self._available_hw_encoders
        return hwaccel.probe_hw_encoders(self._ffmpeg_binary)

    def _resolve_encoder(self, output: OutputSettings) -> hwaccel.EncoderChoice:
        """Select the concrete encoder for ``output``, logging any fallback.

        Delegates the decision to the pure :func:`hwaccel.resolve_encoder`. When
        hardware was requested but software was chosen (codec unsupported by the
        API or the hardware encoder absent from this ffmpeg build), a single
        clear warning is logged; the render proceeds on software regardless.
        """
        choice = hwaccel.resolve_encoder(
            output.codec,
            hwaccel_enabled=self._hwaccel_enabled,
            hwaccel_api=self._hwaccel_api,
            available=self._hw_capabilities(),
        )
        if choice.fallback_reason is not None:
            logger.warning(
                "hardware encoding unavailable for codec %r (%s); "
                "falling back to software encoder %s",
                output.codec,
                choice.fallback_reason,
                choice.encoder_name,
            )
        return choice

    async def validate(self, output: OutputSettings, *, has_chapters: bool) -> None:
        """Validate a target up front; see :meth:`Encoder.validate`."""
        # Each resolver raises EncoderCapabilityError with the offending option.
        encoder_name = allowlist.resolve_codec(output.codec)
        allowlist.resolve_container(output.container)
        allowlist.validate_codec_container(encoder_name, output.container)
        allowlist.validate_fps(output.fps)
        if output.width is not None and output.height is not None:
            allowlist.validate_dimensions(output.width, output.height)
        if output.crf is not None:
            allowlist.validate_crf(output.crf)
        if output.bitrate_kbps is not None:
            allowlist.validate_bitrate_kbps(output.bitrate_kbps)
        if has_chapters and output.container.lower() not in _CHAPTER_CAPABLE_CONTAINERS:
            raise EncoderCapabilityError(
                f"container {output.container!r} cannot carry chapters",
                option="container",
            )

    def _confine_output(self, spec: RenderSpec) -> Path:
        """Resolve and confine the output path to the project render root.

        :raises EncoderError: if the output resolves outside the render root.
        """
        root = spec.project_render_root.resolve()
        resolved = spec.output_path.resolve()
        if not resolved.is_relative_to(root):
            raise EncoderError(
                f"output path is outside the project render root: {spec.output_path!r}"
            )
        return resolved

    def _resolve_font(self) -> str:
        """Return the font path for overlays, probing defaults if needed.

        :raises EncoderError: if no font is configured and none is found.
        """
        if self._font_path is not None:
            if not Path(self._font_path).is_file():
                raise EncoderError(
                    f"configured overlay font not found: {self._font_path!r}"
                )
            return self._font_path
        found = _find_default_font()
        if found is None:
            raise EncoderError(
                "no overlay font available; pass font_path to enable text or "
                "timestamp overlays"
            )
        return found

    def _write_concat_list(self, frames: list[FrameRef], directory: Path) -> Path:
        """Write the concat-demuxer list with per-frame real-time durations.

        Each frame's ``duration`` is the wall-clock gap to the next capture, so
        the presentation timestamps reflect true (possibly non-uniform) capture
        spacing for the timestamp overlay. The last entry needs no duration: the
        second ``setpts`` renumbers every input frame to one output ordinal, so
        each ``file`` line maps to exactly one output frame -- a 1:1 frame count
        with no duplicated trailing frame. Paths are single-quote escaped because
        they originate from the (untrusted) database, and a path with a line
        break or NUL is refused (:func:`_escape_concat_path`).

        :raises EncoderError: if a frame path contains a line break or NUL;
            nothing is written.
        """
        lines: list[str] = []
        count = len(frames)
        for i, frame in enumerate(frames):
            escaped = _escape_concat_path(str(frame.absolute_path))
            lines.append(f"file '{escaped}'")
            if i < count - 1:
                gap = (
                    frames[i + 1].capture_timestamp - frame.capture_timestamp
                ).total_seconds()
                # Guard against equal/out-of-order timestamps: a non-positive gap
                # would collapse two frames onto one PTS. Use a tiny positive gap.
                duration = gap if gap > 0 else 0.001
                lines.append(f"duration {duration:.6f}")

        list_path = directory / "concat.txt"
        list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return list_path

    def _build_overlay_filters(
        self,
        spec: RenderSpec,
        confined_root: Path,
    ) -> tuple[list[str], list[str], list[str]]:
        """Build the overlay filter fragments and their input/output edges.

        Returns ``(pre_filters, image_inputs, image_filter)`` where:

        * ``pre_filters`` are the drawtext filters (timestamp and/or text) that
          run on the main video chain, between the two ``setpts`` stages;
        * ``image_inputs`` are extra ``-i`` argv fragments for a watermark image;
        * ``image_filter`` is the trailing ``overlay`` filter (empty if no image).

        At most one timestamp ``drawtext`` is ever produced, regardless of frame
        count.
        """
        cfg = spec.overlay
        pre: list[str] = []
        image_inputs: list[str] = []
        image_filter: list[str] = []
        x_text, y_text = overlay.text_position(cfg.placement)

        if cfg.timestamp_enabled:
            font = overlay.escape_path_for_filter(self._resolve_font())
            fmt = overlay.escape_timestamp_format(cfg.timestamp_format)
            # The drawtext sits after setpts has shifted PTS to the absolute
            # capture epoch (plus the tz offset), so gmtime with base 0 reads the
            # true local wall-clock. One filter, independent of frame count.
            text_expr = f"%{{pts\\:gmtime\\:0\\:{fmt}}}"
            pre.append(_drawtext_filter(font, text_expr, x_text, y_text))

        if cfg.text_enabled and cfg.text_content:
            font = overlay.escape_path_for_filter(self._resolve_font())
            content = overlay.escape_drawtext(cfg.text_content)
            # Offset the caption off the timestamp so they don't overlap. Stack
            # downward for top placements and upward for bottom placements, so the
            # second line always grows away from its edge, not past it.
            if not cfg.timestamp_enabled:
                y_caption = y_text
            elif cfg.placement.startswith("bottom"):
                y_caption = f"({y_text})-30"
            else:
                y_caption = f"({y_text})+30"
            pre.append(_drawtext_filter(font, content, x_text, y_caption))

        if cfg.image_enabled and cfg.image_path:
            resolved = overlay.resolve_overlay_image(cfg.image_path, confined_root)
            image_inputs += ["-i", str(resolved)]
            x_img, y_img = overlay.image_position(cfg.placement)
            image_filter.append(f"overlay={x_img}:{y_img}")

        return pre, image_inputs, image_filter

    def _build_filtergraph(
        self,
        spec: RenderSpec,
        base_epoch: int,
        offset_seconds: int,
        pre_filters: list[str],
        image_filter: list[str],
        choice: hwaccel.EncoderChoice,
    ) -> tuple[str, list[str]]:
        """Assemble the full ``-vf``/``-filter_complex`` value and check filters.

        Two ``setpts`` stages decouple the overlay clock from playback timing:
        the first shifts each frame's real-elapsed PTS onto the absolute capture
        epoch (so the single timestamp drawtext reads true wall-clock), the
        second renumbers frames by output ordinal so playback is a compact
        timelapse at the target fps regardless of real capture spacing.

        All overlay, scale, and timing work runs in system memory so the existing
        software filters are reused unchanged. The pixel-format tail is the only
        encoder-specific part: software and NVENC keep ``format=yuv420p`` (NVENC
        accepts system-memory frames); VA-API and QSV convert to ``nv12`` and
        ``hwupload`` the result into GPU memory just before the encoder.

        Returns ``(filter_string, used_filter_names)``. The finished string is
        parsed back (:func:`_verify_filtergraph`): its filters must equal
        ``used`` in order and be on the allowlist (with the hardware filters
        permitted only on a hardware path), so nothing unexpected is emitted even
        if an overlay value escaped its quoting.

        :raises EncoderError: ``filtergraph mismatch`` if the parsed graph is not
            the one the builder assembled.
        """
        output = spec.output_settings
        shift = base_epoch + offset_seconds
        chain: list[str] = []
        used: list[str] = []

        if spec.deflicker:
            chain.append("deflicker")
            used.append("deflicker")

        # Stage 1: real-elapsed PTS -> absolute (offset-adjusted) capture epoch.
        chain.append(f"setpts=PTS+{shift}/TB")
        used.append("setpts")

        for f in pre_filters:
            chain.append(f)
            used.append(f.split("=", 1)[0])

        # Stage 2: renumber by output ordinal -> timelapse playback timing.
        chain.append(f"setpts=N/{output.fps}/TB")
        used.append("setpts")

        chain.append(f"fps={output.fps}")
        used.append("fps")
        # Omit the scale filter for a "source" render (no explicit dimensions):
        # the captured frames keep their native size.
        if output.width is not None and output.height is not None:
            chain.append(f"scale={output.width}:{output.height}")
            used.append("scale")

        # The pixel-format/upload tail differs by encoder (see
        # :func:`_format_tail_filters`). It must run *after* the software image
        # overlay when frames are uploaded to GPU memory -- the software
        # ``overlay`` filter cannot composite onto device surfaces. To keep the
        # software path byte-for-byte unchanged, only the hwupload APIs move the
        # tail past the overlay; software and NVENC keep it in the main chain
        # exactly where it has always been.
        tail = _format_tail_filters(choice)
        tail_uploads = choice.hwaccel_api in _HWUPLOAD_APIS
        defer_tail = tail_uploads and bool(image_filter)
        if not defer_tail:
            for f in tail:
                chain.append(f)
                used.append(f.split("=", 1)[0])

        if image_filter:
            # The image overlay needs a second input, so the whole chain becomes
            # a filter_complex: main chain into [v], then overlay the image.
            overlay_chain = list(image_filter)
            used += [f.split("=", 1)[0] for f in image_filter]
            if defer_tail:
                # Composite in system memory, then convert + upload to the GPU.
                overlay_chain += tail
                used += [f.split("=", 1)[0] for f in tail]
            main = ",".join(chain)
            overlay_expr = ",".join(overlay_chain)
            graph = f"[0:v]{main}[v];[v][1:v]{overlay_expr}[out]"
        else:
            graph = ",".join(chain)
        _verify_filtergraph(graph, used, allow_hw=choice.is_hardware)
        return graph, used

    def _write_chapters_metadata(
        self, chapters: list[Chapter], directory: Path
    ) -> Path:
        """Write an ffmetadata file with one ``[CHAPTER]`` block per chapter.

        Timecodes are in seconds on a 1/1000 timebase. Each chapter ends where
        the next begins; the last extends a nominal second past its start so it
        is non-empty. Each title is escaped for its ffmetadata line
        (:func:`_escape_ffmetadata_value`); no filtergraph escaping applies.
        """
        ms = 1000
        lines = [";FFMETADATA1"]
        ordered = sorted(chapters, key=lambda c: c.timecode_seconds)
        for i, chapter in enumerate(ordered):
            start = int(round(chapter.timecode_seconds * ms))
            if i + 1 < len(ordered):
                end = int(round(ordered[i + 1].timecode_seconds * ms))
            else:
                end = start + ms
            if end <= start:
                end = start + 1
            title = _escape_ffmetadata_value(chapter.label)
            lines += [
                "[CHAPTER]",
                "TIMEBASE=1/1000",
                f"START={start}",
                f"END={end}",
                f"title={title}",
            ]
        meta_path = directory / "chapters.ffmetadata"
        meta_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return meta_path

    def _build_argv(
        self,
        spec: RenderSpec,
        confined_output: Path,
        confined_root: Path,
        work_dir: Path,
    ) -> list[str]:
        """Assemble the full ffmpeg argv list for a render.

        Validates the target, writes the concat list (and ffmetadata for
        chapters), builds the filtergraph, and returns the argv. Raises before
        any of this work commits if the target is unsupported.
        """
        output = spec.output_settings
        # Validate (also covers chapters-in-container); raises before any spawn.
        # We re-run here so a direct render() (without a prior validate()) is safe.
        self_validate_chapters = bool(spec.chapters)
        encoder_name = allowlist.resolve_codec(output.codec)
        muxer = allowlist.resolve_container(output.container)
        allowlist.validate_codec_container(encoder_name, output.container)
        allowlist.validate_fps(output.fps)
        if output.width is not None and output.height is not None:
            allowlist.validate_dimensions(output.width, output.height)
        if output.crf is not None:
            allowlist.validate_crf(output.crf)
        if output.bitrate_kbps is not None:
            allowlist.validate_bitrate_kbps(output.bitrate_kbps)
        if (
            self_validate_chapters
            and output.container.lower() not in _CHAPTER_CAPABLE_CONTAINERS
        ):
            raise EncoderCapabilityError(
                f"container {output.container!r} cannot carry chapters",
                option="container",
            )

        frames = spec.frames.frames
        if not frames:
            raise EncoderError("cannot render a project with no active frames")

        # Base epoch = first frame's true capture instant; PTS is shifted onto it
        # so the overlay shows real wall-clock. The tz offset is folded in so the
        # gmtime-based drawtext displays the requested local time deterministically.
        first_capture = frames[0].capture_timestamp.astimezone(UTC)
        base_epoch = int(first_capture.timestamp())
        offset_seconds = (
            _timezone_offset_seconds(spec.overlay.timestamp_timezone, first_capture)
            if spec.overlay.timestamp_enabled
            else 0
        )

        # Select the concrete encoder (hardware if enabled, available, and
        # supported for this codec; software otherwise). On the software path the
        # rest of this method is identical to before; the hardware path swaps in
        # the encoder name, init flags, rate control, and pixel-format tail.
        choice = self._resolve_encoder(output)

        concat_list = self._write_concat_list(frames, work_dir)

        pre_filters, image_inputs, image_filter = self._build_overlay_filters(
            spec, confined_root
        )
        filtergraph, _ = self._build_filtergraph(
            spec, base_epoch, offset_seconds, pre_filters, image_filter, choice
        )

        # ``-progress pipe:1`` streams key=value progress blocks to stdout for the
        # render watchdog; ``-nostats`` drops the human-readable stats line that
        # would otherwise go to stderr. Both are global options.
        argv: list[str] = [
            self._ffmpeg_binary,
            "-hide_banner",
            "-progress",
            "pipe:1",
            "-nostats",
        ]
        # Hardware device/decode init goes before the input so ffmpeg builds the
        # device context up front. Empty on the software path -> argv unchanged.
        if choice.is_hardware:
            argv += _hw_init_args(choice, self._hwaccel_device)
        # The concat input may open local files only (#67 S15): with ``-safe 0``
        # an entry could otherwise name any protocol ffmpeg was built with. The
        # demuxer passes the whitelist on to every file it opens.
        argv += [
            "-loglevel",
            "error",
            "-y",
            "-protocol_whitelist",
            "file",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
        ]
        argv += image_inputs

        chapters_meta: Path | None = None
        if spec.chapters:
            chapters_meta = self._write_chapters_metadata(spec.chapters, work_dir)
            argv += ["-i", str(chapters_meta)]

        # Filtergraph: filter_complex when an image overlay adds a second input,
        # otherwise a simple -vf chain.
        if image_filter:
            argv += ["-filter_complex", filtergraph, "-map", "[out]"]
        else:
            argv += ["-vf", filtergraph]

        if chapters_meta is not None:
            # The metadata input index is the concat (0) plus any image input.
            meta_index = 1 + (1 if image_inputs else 0)
            argv += ["-map_metadata", str(meta_index)]

        argv += ["-r", str(output.fps)]
        if choice.is_hardware:
            # Hardware: -c:v + API rate-control; VA-API/QSV take their pixel
            # format from the uploaded surface (no trailing -pix_fmt), NVENC
            # keeps -pix_fmt yuv420p (set inside _hw_encoder_args).
            argv += _hw_encoder_args(choice, output, self._hwaccel_device)
        else:
            # Software: unchanged from before hardware support existed.
            argv += ["-c:v", encoder_name]
            argv += _quality_args(output, encoder_name)
            argv += ["-pix_fmt", "yuv420p"]
        argv += _playback_args(choice.encoder_name, muxer)
        argv += ["-f", muxer, str(confined_output)]
        return argv

    async def render(self, spec: RenderSpec) -> RenderResult:
        """Encode ``spec`` into a video; see :meth:`Encoder.render`."""
        output = spec.output_settings
        browser_ok = is_browser_streamable(output.codec, output.container)
        confined_output = self._confine_output(spec)
        confined_root = spec.project_render_root.resolve()
        confined_output.parent.mkdir(parents=True, exist_ok=True)

        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="tlm-encode-") as tmp:
            work_dir = Path(tmp)
            # Build (and validate) before spawning; raises propagate cleanly.
            argv = self._build_argv(spec, confined_output, confined_root, work_dir)
            logger.info(
                "encoding project=%s frames=%s -> %s",
                spec.project_id,
                len(spec.frames),
                confined_output,
            )
            # Log the exact command for diagnosis. The argv is local-file paths and
            # ffmpeg flags only -- no credentials or remote URLs -- so it is safe to
            # log verbatim; shlex.join makes it copy-pasteable for reproduction.
            logger.info("ffmpeg command: %s", shlex.join(argv))
            ceiling = render_time_ceiling(self._max_job_seconds, len(spec.frames))
            return await self._run(
                argv, confined_output, output, browser_ok, started, ceiling
            )

    async def _run(
        self,
        argv: list[str],
        output_path: Path,
        output: OutputSettings,
        browser_ok: bool,
        started: float,
        ceiling_seconds: float,
    ) -> RenderResult:
        """Spawn ffmpeg, watch it, and translate the outcome into a result.

        Three tasks run while ffmpeg does: one reads the ``-progress`` blocks on
        stdout, one drains stderr into a bounded tail (so the pipe never fills
        and blocks the child), and one waits for exit. This coroutine is the
        watchdog: every tick it checks the stall timeout and ``ceiling_seconds``
        (both measured from spawn) and, when one is exceeded, kills ffmpeg,
        removes the partial output and raises :class:`EncoderTimeoutError`.

        On cancellation the child is killed and the partial output removed before
        the :class:`asyncio.CancelledError` is re-raised, so no orphan process or
        half-written file survives. The argv is passed as a list with
        ``shell=False`` semantics; nothing is interpreted by a shell.
        """
        clock = self._watchdog_clock or asyncio.get_running_loop().time
        stall_seconds = self._stall_timeout_seconds
        tick = min(_WATCHDOG_TICK_SECONDS, stall_seconds / 4, ceiling_seconds / 4)
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None and process.stderr is not None
        spawned_at = clock()
        last_progress_at = spawned_at
        tracker = _ProgressTracker()
        stderr_tail: deque[bytes] = deque(maxlen=_STDERR_TAIL_LINES)

        def on_progress_line(line: bytes) -> None:
            nonlocal last_progress_at
            if tracker.feed(line):
                last_progress_at = clock()

        readers = [
            asyncio.create_task(_read_lines(process.stdout, on_progress_line)),
            asyncio.create_task(_read_lines(process.stderr, stderr_tail.append)),
        ]
        exited = asyncio.create_task(process.wait())
        timeout_reason: str | None = None
        try:
            while True:
                done, _ = await asyncio.wait({exited}, timeout=tick)
                if exited in done:
                    break
                now = clock()
                if now - last_progress_at > stall_seconds:
                    reason = (
                        "ffmpeg stalled: no progress for "
                        f"{_format_seconds(stall_seconds)}"
                    )
                elif now - spawned_at > ceiling_seconds:
                    reason = (
                        "ffmpeg exceeded max_job_seconds "
                        f"({_format_seconds(ceiling_seconds)})"
                    )
                else:
                    continue
                # A child that exited on this very tick finished on its own; its
                # exit status decides the outcome, not the watchdog.
                if process.returncode is None:
                    timeout_reason = reason
                    logger.warning(
                        "killing ffmpeg pid=%s: %s (last stderr: %s)",
                        process.pid,
                        reason,
                        _last_nonempty_line(stderr_tail) or "none",
                    )
                    await self._terminate(process)
                break
            # Let the readers collect the final output; the pipes reach EOF once
            # ffmpeg is gone. Bounded, in case something else holds them open.
            await asyncio.wait(readers, timeout=_READER_GRACE_SECONDS)
        except asyncio.CancelledError:
            await self._terminate(process)
            self._remove_partial(output_path)
            raise
        finally:
            unfinished = [task for task in (*readers, exited) if not task.done()]
            for task in unfinished:
                task.cancel()
            await asyncio.gather(*readers, exited, return_exceptions=True)

        if timeout_reason is not None:
            self._remove_partial(output_path)
            raise EncoderTimeoutError(timeout_reason)

        if process.returncode != 0:
            self._remove_partial(output_path)
            detail = _last_nonempty_line(stderr_tail) or "no output"
            return RenderResult(
                success=False,
                output_path=None,
                duration_seconds=None,
                browser_streamable=browser_ok,
                codec=output.codec,
                container=output.container,
                error=f"ffmpeg failed: {detail}",
            )

        duration = time.monotonic() - started
        return RenderResult(
            success=True,
            output_path=output_path,
            duration_seconds=duration,
            browser_streamable=browser_ok,
            codec=output.codec,
            container=output.container,
            error=None,
        )

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        """Kill the ffmpeg child and reap it, tolerating an already-dead process."""
        if process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()

    @staticmethod
    def _remove_partial(output_path: Path) -> None:
        """Delete a partial output file if one was created; never raise."""
        with contextlib.suppress(FileNotFoundError):
            output_path.unlink()
