"""RFC 6381 codec strings and container MIME types for finished renders.

A browser decides whether it can play a video from its MIME type and codec
string (``video/mp4; codecs="av01.0.05M.08"``), via ``canPlayType`` or
``navigator.mediaCapabilities.decodingInfo``. ffprobe does not print that
string: ``-show_streams`` reports ``codec_name``, ``profile``, ``level``,
``pix_fmt`` and ``codec_tag_string``. :func:`codec_string` builds the string
from those fields.

The module is pure -- no I/O, no subprocess -- so the rule tables are tested
directly against ffprobe output recorded from the pinned FFmpeg build
(``tests/fixtures/ffprobe/n9_streams.json``). Anything it does not recognise
yields ``None``; the page then falls back to a MIME-only check.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePath
from typing import Any

# Output file extension -> the MIME type it is served (and checked) as. The
# stream route and the encode-time probe share this one table.
MEDIA_TYPES: dict[str, str] = {
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
}

# Logical container name (as stored in a render's output settings) -> MIME type.
CONTAINER_MEDIA_TYPES: dict[str, str] = {
    "mp4": "video/mp4",
    "mkv": "video/x-matroska",
    "webm": "video/webm",
}

_FALLBACK_MEDIA_TYPE = "application/octet-stream"

# Logical container name -> the short name a viewer recognises ("H.264 in MKV").
_CONTAINER_LABELS: dict[str, str] = {
    "mp4": "MP4",
    "mkv": "MKV",
    "webm": "WebM",
}

# Logical codec or encoder name -> the short name a viewer recognises, used in
# "plays only in browsers that support <codec>" style messages.
_CODEC_LABELS: dict[str, str] = {
    "h264": "H.264",
    "libx264": "H.264",
    "h265": "H.265",
    "hevc": "H.265",
    "libx265": "H.265",
    "vp9": "VP9",
    "libvpx-vp9": "VP9",
    "av1": "AV1",
    "libsvtav1": "AV1",
}

# H.264 profile name -> (profile_idc, constraint byte), both as two hex digits.
# Only Constrained Baseline sets constraint flags (set0 + set1 + set2 = E0); the
# other profiles use 00, which every browser accepts.
_AVC_PROFILES: dict[str, tuple[str, str]] = {
    "constrained baseline": ("42", "E0"),
    "baseline": ("42", "00"),
    "main": ("4D", "00"),
    "extended": ("58", "00"),
    "high": ("64", "00"),
    "high 10": ("6E", "00"),
    "high 4:2:2": ("7A", "00"),
    "high 4:4:4 predictive": ("F4", "00"),
}

# H.265 profile name -> (general_profile_idc, profile compatibility flags in
# the reversed-bit hex form RFC 6381 / ISO 14496-15 use).
_HEVC_PROFILES: dict[str, tuple[int, str]] = {
    "main": (1, "6"),
    "main 10": (2, "4"),
}

# AV1 profile name -> seq_profile.
_AV1_PROFILES: dict[str, int] = {
    "main": 0,
    "high": 1,
    "professional": 2,
}

# ffprobe reports an unknown VP9 level as -99 (libvpx does not signal one).
# Level 1.0 is the conventional placeholder and ``canPlayType`` accepts it.
_VP9_DEFAULT_LEVEL = 10


def media_type_for_path(path: str | PurePath) -> str:
    """Return the MIME type a render file is served as, from its extension."""
    suffix = PurePath(path).suffix.lower()
    return MEDIA_TYPES.get(suffix, _FALLBACK_MEDIA_TYPE)


def media_type_for_container(container: str | None) -> str | None:
    """Return the MIME type for a logical container name, or ``None``."""
    if not container:
        return None
    return CONTAINER_MEDIA_TYPES.get(container.lower())


def codec_label(codec: str | None) -> str:
    """Return a viewer-facing name for a logical codec or encoder name."""
    if not codec:
        return "this codec"
    return _CODEC_LABELS.get(codec.lower(), codec)


def container_label(container: str | None) -> str | None:
    """Return a viewer-facing name for a logical container, or ``None``.

    ``container`` is a logical name (``"mkv"``) or a file extension
    (``".mkv"``); an unknown one is shown upper-cased, an empty one is ``None``.
    """
    if not container:
        return None
    key = container.lower().lstrip(".")
    if not key:
        return None
    return _CONTAINER_LABELS.get(key, key.upper())


def playback_label(codec: str | None, container: str | None) -> str:
    """Return the codec-and-container name for "can't play" style messages.

    Whether a browser plays a render depends on both: Safari plays H.264 in
    MP4 but not in MKV, so a message naming only the codec misleads. Returns
    e.g. ``"H.264 in MKV"``; with no known container, just the codec label.
    """
    codec_name = codec_label(codec)
    container_name = container_label(container)
    if container_name is None:
        return codec_name
    return f"{codec_name} in {container_name}"


def codec_string(stream: Mapping[str, Any]) -> str | None:
    """Build the RFC 6381 codec string for one ffprobe video stream.

    ``stream`` is a ``-show_streams`` JSON object. Returns ``None`` for a codec
    this module has no rule for, or when a field it needs is missing or
    malformed.
    """
    codec_name = str(stream.get("codec_name") or "").lower()
    builder = _BUILDERS.get(codec_name)
    if builder is None:
        return None
    try:
        return builder(stream)
    except (TypeError, ValueError):
        return None


def _profile(stream: Mapping[str, Any]) -> str:
    """The stream's profile name, lower-cased and trimmed."""
    return str(stream.get("profile") or "").strip().lower()


def _level(stream: Mapping[str, Any]) -> int | None:
    """The stream's integer level, or ``None`` when ffprobe reports none."""
    raw = stream.get("level")
    if raw is None or isinstance(raw, bool):
        return None
    level = int(raw)
    return None if level < 0 else level


def _bit_depth(stream: Mapping[str, Any]) -> int:
    """The luma bit depth implied by the pixel format (8 unless it says more)."""
    pix_fmt = str(stream.get("pix_fmt") or "")
    for depth in (12, 10):
        if f"p{depth}" in pix_fmt:
            return depth
    return 8


def _avc(stream: Mapping[str, Any]) -> str | None:
    """``avc1.PPCCLL``: profile_idc, constraint flags, level_idc (hex)."""
    profile = _AVC_PROFILES.get(_profile(stream))
    level = _level(stream)
    if profile is None or level is None or level > 0xFF:
        return None
    profile_idc, constraints = profile
    return f"avc1.{profile_idc}{constraints}{level:02X}"


def _hevc(stream: Mapping[str, Any]) -> str | None:
    """``hvc1.<profile>.<compat>.L<level>.B0`` (``hev1`` when tagged so).

    ffprobe's HEVC ``level`` is already ``general_level_idc`` (level x 30, so
    93 is level 3.1) and is used as is. Main tier (``L``) is assumed; the
    encoders the app drives do not produce High tier.
    """
    profile = _HEVC_PROFILES.get(_profile(stream))
    level = _level(stream)
    if profile is None or level is None:
        return None
    profile_idc, compat = profile
    tag = str(stream.get("codec_tag_string") or "").lower()
    sample_entry = "hev1" if tag == "hev1" else "hvc1"
    return f"{sample_entry}.{profile_idc}.{compat}.L{level}.B0"


def _vp9(stream: Mapping[str, Any]) -> str | None:
    """``vp09.PP.LL.DD``: profile, level (default 10 when unknown), bit depth."""
    profile_name = _profile(stream)
    if not profile_name.startswith("profile "):
        return None
    profile = int(profile_name.removeprefix("profile "))
    level = _level(stream)
    if level is None:
        level = _VP9_DEFAULT_LEVEL
    return f"vp09.{profile:02d}.{level:02d}.{_bit_depth(stream):02d}"


def _av1(stream: Mapping[str, Any]) -> str | None:
    """``av01.P.LLM.DD``: seq_profile, seq_level_idx + Main tier, bit depth."""
    profile = _AV1_PROFILES.get(_profile(stream))
    level = _level(stream)
    if profile is None or level is None:
        return None
    return f"av01.{profile}.{level:02d}M.{_bit_depth(stream):02d}"


_BUILDERS = {
    "h264": _avc,
    "hevc": _hevc,
    "vp9": _vp9,
    "av1": _av1,
}
