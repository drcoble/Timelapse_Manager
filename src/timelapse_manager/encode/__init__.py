"""Video encoding: the encoder interface and the FFmpeg implementation.

The public surface is the :class:`Encoder` contract and its data shapes
(:class:`RenderSpec`, :class:`OutputSettings`, :class:`OverlayConfig`,
:class:`Chapter`, :class:`FrameRef`, :class:`FrameSequence`,
:class:`RenderResult`), the concrete :class:`FfmpegEncoder`, the encoder
exceptions, and the helpers that prepare a render: gathering active frames,
computing chapters, and deciding browser streamability.
"""

from __future__ import annotations

from .browser_streamable import is_browser_streamable
from .chapters import Milestone, compute_chapters
from .encoder import (
    Chapter,
    Encoder,
    EncoderCapabilityError,
    EncoderError,
    FrameRef,
    FrameSequence,
    OutputSettings,
    OverlayConfig,
    RenderResult,
    RenderSpec,
)
from .ffmpeg_impl import FfmpegEncoder
from .frame_source import gather_frames
from .registry import SUPPORTED_ENGINES, build_encoder

__all__ = [
    "SUPPORTED_ENGINES",
    "Chapter",
    "Encoder",
    "EncoderCapabilityError",
    "EncoderError",
    "FfmpegEncoder",
    "FrameRef",
    "FrameSequence",
    "Milestone",
    "OutputSettings",
    "OverlayConfig",
    "RenderResult",
    "RenderSpec",
    "build_encoder",
    "compute_chapters",
    "gather_frames",
    "is_browser_streamable",
]
