"""The encoder contract: data shapes, exceptions, and the abstract interface.

This module defines the vocabulary the rest of the application speaks when it
wants a video produced from a project's frames. Concrete encoders (see
:class:`~.ffmpeg_impl.FfmpegEncoder`) implement :class:`Encoder`; callers build a
:class:`RenderSpec` and receive a :class:`RenderResult`.

All the dataclasses are frozen: a render request is an immutable description of
work, safe to pass between the queue, the encoder, and any logging without fear
of mutation. None of these types touch the filesystem or the database -- they
are pure value objects.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


class EncoderError(Exception):
    """Base class for every error raised by the encoding layer."""


class EncoderCapabilityError(EncoderError):
    """Raised when a render asks for something the encoder will not produce.

    Covers an unsupported codec, container, or numeric parameter, a disallowed
    filter, or a feature combination the chosen container cannot carry (such as
    chapters in a container without chapter support). The :attr:`option`
    attribute names the offending knob so callers can surface a precise message.
    """

    def __init__(self, message: str, *, option: str) -> None:
        super().__init__(message)
        self.option = option


@dataclass(frozen=True)
class OutputSettings:
    """The encode target: geometry, rate, codec, container, and quality.

    Exactly the quality knob that applies to the chosen codec should be set:
    ``crf`` for constant-quality encoding or ``bitrate_kbps`` for a target
    bitrate. Both may be left unset to accept the encoder's default.

    ``width`` and ``height`` may both be left unset (``None``) to keep the source
    frames' native size: no scale filter is added and no dimension validation is
    applied.
    """

    fps: float
    width: int | None
    height: int | None
    codec: str
    container: str
    bitrate_kbps: int | None = None
    crf: int | None = None


@dataclass(frozen=True)
class OverlayConfig:
    """What to burn into the output video, and where.

    Each overlay is independently toggled. The timestamp overlay renders the
    frame's true capture wall-clock (see the implementation); ``timestamp_format``
    is an strftime pattern and ``timestamp_timezone`` an IANA zone name used to
    localise it. The text overlay renders a fixed caption. The image overlay
    composites a watermark/logo from ``image_path``. ``placement`` selects a
    corner shared by all enabled overlays.
    """

    timestamp_enabled: bool = False
    timestamp_format: str = "%Y-%m-%d %H:%M:%S"
    timestamp_timezone: str = "UTC"
    text_enabled: bool = False
    text_content: str = ""
    image_enabled: bool = False
    image_path: str | None = None
    placement: str = "top_left"


@dataclass(frozen=True)
class Chapter:
    """A named marker at a playback offset, in output-timeline seconds."""

    timecode_seconds: float
    label: str


@dataclass(frozen=True)
class FrameRef:
    """One source still: its order, true capture time, and resolved location.

    ``absolute_path`` is the fully resolved on-disk path (already passed through
    the storage resolver); the encoder reads it but never modifies it.
    ``capture_timestamp`` is tz-aware UTC and drives the timestamp overlay.
    """

    sequence_index: int
    capture_timestamp: datetime
    absolute_path: Path
    width: int | None
    height: int | None


@dataclass(frozen=True)
class FrameSequence:
    """An ordered set of frames belonging to one project, ready to encode."""

    project_id: int
    frames: list[FrameRef] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.frames)


@dataclass(frozen=True)
class RenderSpec:
    """A complete, self-contained description of one render.

    The encoder needs nothing beyond this: the frames to encode, the output
    settings, overlays, chapters, whether to deflicker, where to write the
    output, and the project's render root that the output (and any overlay image)
    must stay confined within.
    """

    project_id: int
    frames: FrameSequence
    output_settings: OutputSettings
    overlay: OverlayConfig
    chapters: list[Chapter]
    deflicker: bool
    output_path: Path
    project_render_root: Path


@dataclass(frozen=True)
class RenderResult:
    """The outcome of a render attempt.

    On success, ``output_path`` points at the produced file and
    ``duration_seconds`` is its playback length; on failure, ``error`` carries a
    human-readable reason and ``output_path`` is ``None``. ``browser_streamable``
    reflects whether the produced codec/container plays natively in a browser.
    """

    success: bool
    output_path: Path | None
    duration_seconds: float | None
    browser_streamable: bool
    codec: str
    container: str
    error: str | None = None


class Encoder(ABC):
    """Abstract video encoder.

    Two operations: :meth:`validate` checks a target up front (so the caller can
    reject a bad request before any frames are gathered), and :meth:`render`
    performs the encode. Implementations must never modify source frame files and
    must honour cancellation by terminating any child process and removing the
    partial output.
    """

    @abstractmethod
    async def validate(self, output: OutputSettings, *, has_chapters: bool) -> None:
        """Validate a target without encoding.

        :raises EncoderCapabilityError: if the codec, container, or any parameter
            is unsupported, or if ``has_chapters`` is requested for a container
            that cannot carry chapters.
        """

    @abstractmethod
    async def render(self, spec: RenderSpec) -> RenderResult:
        """Encode ``spec`` into a video file and return the outcome.

        Honours cancellation: if the awaiting task is cancelled, the child
        process is killed and any partial output removed before the
        :class:`asyncio.CancelledError` propagates. Never mutates source frames.
        """
