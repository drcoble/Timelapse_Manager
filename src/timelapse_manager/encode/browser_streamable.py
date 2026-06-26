"""Whether a codec/container pair plays natively in a browser ``<video>``.

The render queue records this on each output so the UI knows whether it can
stream a result inline or must offer it as a download. The rule is intentionally
conservative: only combinations with broad, dependency-free browser support
return ``True``.
"""

from __future__ import annotations

# (codec, container) pairs treated as natively streamable in a browser
# ``<video>`` element. H.264 in MP4 is the universal, dependency-free baseline.
# H.265/HEVC (patchy, gated browser support) and other combinations are treated
# as download-only, so the UI offers them as a file rather than inline playback.
_STREAMABLE_PAIRS: frozenset[tuple[str, str]] = frozenset(
    {
        ("h264", "mp4"),
    }
)

# Logical codec aliases collapse onto a single canonical name for the lookup.
_CODEC_ALIASES: dict[str, str] = {
    "h264": "h264",
    "libx264": "h264",
    "h265": "h265",
    "hevc": "h265",
    "libx265": "h265",
    "vp9": "vp9",
    "libvpx-vp9": "vp9",
    "av1": "av1",
    "libsvtav1": "av1",
}


def is_browser_streamable(codec: str, container: str) -> bool:
    """Return whether ``(codec, container)`` plays natively in a browser."""
    canonical = _CODEC_ALIASES.get(codec.lower(), codec.lower())
    return (canonical, container.lower()) in _STREAMABLE_PAIRS
