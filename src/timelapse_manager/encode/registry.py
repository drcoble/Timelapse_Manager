"""Encoder engine selection.

The :class:`~.encoder.Encoder` interface admits interchangeable encoding engines;
which one a render uses is chosen by the ``render.encoder_engine`` setting.
:func:`build_encoder` is the single construction point that maps that name to a
concrete engine. Only the bundled FFmpeg engine ships today, but routing
construction through here keeps the selection seam explicit -- adding a second
engine is a new branch, not a change to every call site -- and makes an
unrecognised name fail loudly rather than silently falling back.
"""

from __future__ import annotations

from .encoder import Encoder, EncoderError
from .ffmpeg_impl import FfmpegEncoder

# Engine names accepted by :func:`build_encoder`, in preference order.
SUPPORTED_ENGINES: tuple[str, ...] = ("ffmpeg",)


def build_encoder(
    engine: str | None,
    *,
    ffmpeg_binary: str | None = None,
    font_path: str | None = None,
    hwaccel_enabled: bool = False,
    hwaccel_api: str | None = None,
    hwaccel_device: str | None = None,
) -> Encoder:
    """Construct the encoder engine named by ``engine``.

    :param engine: the configured engine name (case-insensitive); ``None`` or
        empty selects the default FFmpeg engine.
    :param ffmpeg_binary: explicit ffmpeg path for the FFmpeg engine.
    :param font_path: overlay font path for the FFmpeg engine.
    :param hwaccel_enabled: enable GPU-accelerated encoding for the FFmpeg engine.
        Off by default, which keeps encoding entirely on the software path. When
        on, the engine probes the local ffmpeg once for available hardware
        encoders and falls back to software automatically when hardware is
        unavailable -- a render never fails merely because hardware is missing.
    :param hwaccel_api: the hardware encode API when enabled (``"nvenc"``,
        ``"qsv"``, or ``"vaapi"``), or ``None`` for software-only.
    :param hwaccel_device: optional device selector for the chosen API (a VA-API
        render-node path or NVENC GPU index); the API default is used when unset.
    :raises EncoderError: if ``engine`` names an unknown engine.
    """
    normalized = (engine or "ffmpeg").strip().lower()
    if normalized == "ffmpeg":
        # Only pass an explicit binary when given; otherwise let FfmpegEncoder use
        # its own default (the bundled/`PATH` ffmpeg).
        if ffmpeg_binary is not None:
            return FfmpegEncoder(
                ffmpeg_binary=ffmpeg_binary,
                font_path=font_path,
                hwaccel_enabled=hwaccel_enabled,
                hwaccel_api=hwaccel_api,
                hwaccel_device=hwaccel_device,
            )
        return FfmpegEncoder(
            font_path=font_path,
            hwaccel_enabled=hwaccel_enabled,
            hwaccel_api=hwaccel_api,
            hwaccel_device=hwaccel_device,
        )
    raise EncoderError(
        f"unknown encoder engine {engine!r}; supported: {', '.join(SUPPORTED_ENGINES)}"
    )
