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
* **Untrusted concat list.** Frame paths come from the database and may contain
  any character, so each is written into the concat list with ffmpeg's
  single-quote escaping (``'`` -> ``'\\''``); a bare ``file <path>`` line is
  never written.
* **One timestamp drawtext, ever.** The capture-time overlay is a single
  ``drawtext`` reading each frame's presentation timestamp, so a months-long
  project does not explode into tens of thousands of filters.
* **Source frames are never modified.** Overlays are burned into the output at
  encode time only; the input files are read-only.
* **Cancellation kills the child.** Cancelling the render terminates ffmpeg and
  removes any partial output before propagating.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shlex
import tempfile
import time
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

_DEFAULT_FONT_COLOR = "white"
_DEFAULT_FONT_SIZE = 24


def _escape_concat_path(path: str) -> str:
    """Escape a path for an ffmpeg concat-demuxer ``file`` line.

    The concat demuxer treats a single-quoted argument literally except for the
    quote character itself, which is escaped by closing the quote, inserting an
    escaped quote, and reopening (``'`` -> ``'\\''``). The returned value is the
    inner content; the caller wraps it in ``file '<...>'``.
    """
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
        """
        self._ffmpeg_binary = ffmpeg_binary
        self._font_path = font_path
        self._hwaccel_enabled = hwaccel_enabled
        self._hwaccel_api = hwaccel_api
        self._hwaccel_device = hwaccel_device
        self._available_hw_encoders = available_hw_encoders

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
        they originate from the (untrusted) database.
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
            pre.append(
                f"drawtext=fontfile='{font}':text='{text_expr}'"
                f":x={x_text}:y={y_text}"
                f":fontcolor={_DEFAULT_FONT_COLOR}:fontsize={_DEFAULT_FONT_SIZE}"
                f":box=1:boxcolor=black@0.4"
            )

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
            pre.append(
                f"drawtext=fontfile='{font}':text='{content}'"
                f":x={x_text}:y={y_caption}"
                f":fontcolor={_DEFAULT_FONT_COLOR}:fontsize={_DEFAULT_FONT_SIZE}"
                f":box=1:boxcolor=black@0.4"
            )

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

        Returns ``(filter_string, used_filter_names)``; the names are validated
        against the allowlist (with the hardware filters permitted only on a
        hardware path) so nothing unexpected is emitted.
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
            allowlist.ensure_filters_allowed(used, allow_hw=choice.is_hardware)
            main = ",".join(chain)
            overlay_expr = ",".join(overlay_chain)
            graph = f"[0:v]{main}[v];[v][1:v]{overlay_expr}[out]"
            return graph, used

        allowlist.ensure_filters_allowed(used, allow_hw=choice.is_hardware)
        return ",".join(chain), used

    def _write_chapters_metadata(
        self, chapters: list[Chapter], directory: Path
    ) -> Path:
        """Write an ffmetadata file with one ``[CHAPTER]`` block per chapter.

        Timecodes are in seconds on a 1/1000 timebase. Each chapter ends where
        the next begins; the last extends a nominal second past its start so it
        is non-empty. Titles are written verbatim on their own line, which the
        ffmetadata format takes literally (no filtergraph escaping applies).
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
            # Escape the ffmetadata structural characters (=, ;, #, \, newline).
            title = (
                chapter.label.replace("\\", "\\\\")
                .replace("=", "\\=")
                .replace(";", "\\;")
                .replace("#", "\\#")
                .replace("\n", " ")
            )
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

        argv: list[str] = [self._ffmpeg_binary, "-hide_banner"]
        # Hardware device/decode init goes before the input so ffmpeg builds the
        # device context up front. Empty on the software path -> argv unchanged.
        if choice.is_hardware:
            argv += _hw_init_args(choice, self._hwaccel_device)
        argv += [
            "-loglevel",
            "error",
            "-y",
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
            return await self._run(argv, confined_output, output, browser_ok, started)

    async def _run(
        self,
        argv: list[str],
        output_path: Path,
        output: OutputSettings,
        browser_ok: bool,
        started: float,
    ) -> RenderResult:
        """Spawn ffmpeg, await it, and translate the outcome into a result.

        On cancellation the child is killed and the partial output removed before
        the :class:`asyncio.CancelledError` is re-raised, so no orphan process or
        half-written file survives. The argv is passed as a list with
        ``shell=False`` semantics; nothing is interpreted by a shell.
        """
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await process.communicate()
        except asyncio.CancelledError:
            await self._terminate(process)
            self._remove_partial(output_path)
            raise

        if process.returncode != 0:
            self._remove_partial(output_path)
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            detail = stderr_text.splitlines()[-1] if stderr_text else "no output"
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
