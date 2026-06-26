"""Hardware-accelerated encoder probing and selection.

GPU encoding is an opt-in optimisation layered over the always-available software
encoders. Whether a given hardware encoder can actually be used depends on the
ffmpeg build and the host's drivers/devices, neither of which can be assumed --
so this module *probes* the local ffmpeg once (``ffmpeg -hide_banner -encoders``)
and parses which hardware encoders are really present.

The design keeps every decision pure and unit-testable without a GPU:

* :func:`parse_hw_encoders` turns the captured ``-encoders`` text into the set of
  hardware encoder names ffmpeg reports -- a pure function over a string.
* :func:`probe_hw_encoders` runs the subprocess once and caches the parsed set;
  it is the only impure entry point and is never needed by the resolver's tests.
* :func:`resolve_encoder` decides, from a requested logical codec, the hwaccel
  config, and a probe result, which concrete encoder to use -- always returning a
  working encoder, falling back to software whenever hardware is disabled,
  unavailable, or unsupported for that codec. Pure and table-driven.

Nothing here ever fails a render: if hardware cannot be used the software encoder
is selected instead.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass

from .allowlist import resolve_codec

logger = logging.getLogger(__name__)

# The hardware APIs this module understands. ``None`` (or any other value) means
# software-only. These are the accepted values of the ``hwaccel_api`` setting.
HWACCEL_APIS: frozenset[str] = frozenset({"nvenc", "qsv", "vaapi"})

# Logical codec -> the concrete hardware encoder name per API. A codec missing
# from an API's map has no hardware encoder for that API (e.g. VP9 has none here,
# and AV1 has none for NVENC), so it transparently falls back to software. Keys
# are the canonical software encoder names produced by :func:`resolve_codec`, so
# every logical alias (``h264``/``hevc``/...) routes through the same entry.
_HW_ENCODERS: dict[str, dict[str, str]] = {
    "nvenc": {
        "libx264": "h264_nvenc",
        "libx265": "hevc_nvenc",
    },
    "qsv": {
        "libx264": "h264_qsv",
        "libx265": "hevc_qsv",
        "libsvtav1": "av1_qsv",
    },
    "vaapi": {
        "libx264": "h264_vaapi",
        "libx265": "hevc_vaapi",
        "libsvtav1": "av1_vaapi",
    },
}

# Every hardware encoder name this module may select. Used by the allowlist to
# recognise hardware ``-c:v`` values and by the argv builder to pick the per-API
# init/filter shape. Derived from ``_HW_ENCODERS`` so the two never drift.
ALL_HW_ENCODERS: frozenset[str] = frozenset(
    name for api_map in _HW_ENCODERS.values() for name in api_map.values()
)

# Maps a concrete hardware encoder name back to the hwaccel API that produces it,
# so the argv builder knows which init/filter chain to emit for a given ``-c:v``.
HW_ENCODER_API: dict[str, str] = {
    name: api for api, api_map in _HW_ENCODERS.items() for name in api_map.values()
}

# An ``ffmpeg -encoders`` row lists flags, then the encoder name, then a
# description: e.g. `` V....D h264_nvenc          NVIDIA NVENC H.264 encoder``.
# We capture the second whitespace-delimited token on each row whose first token
# is the 6-char capability-flag block (so headings and banners are ignored).
_ENCODER_ROW = re.compile(r"^\s*[A-Z.]{6}\s+(\S+)")


def parse_hw_encoders(encoders_output: str) -> frozenset[str]:
    """Return the hardware encoder names present in ``ffmpeg -encoders`` output.

    Pure: it inspects only the supplied text, so a test can assert the parse
    without a GPU or even ffmpeg. Only names this module knows how to drive (see
    :data:`ALL_HW_ENCODERS`) are returned; any other encoder ffmpeg reports --
    software encoders, hardware encoders we do not support -- is ignored.

    :param encoders_output: the raw stdout of ``ffmpeg -hide_banner -encoders``.
    :returns: the subset of :data:`ALL_HW_ENCODERS` ffmpeg actually advertises.
    """
    present: set[str] = set()
    for line in encoders_output.splitlines():
        match = _ENCODER_ROW.match(line)
        if match is None:
            continue
        name = match.group(1)
        if name in ALL_HW_ENCODERS:
            present.add(name)
    return frozenset(present)


# Cache of the parsed probe result, keyed by ffmpeg binary path so a process that
# somehow drives two different ffmpeg builds does not cross-contaminate.
_PROBE_CACHE: dict[str, frozenset[str]] = {}


def probe_hw_encoders(
    ffmpeg_binary: str, *, timeout_seconds: float = 10.0
) -> frozenset[str]:
    """Run ``ffmpeg -encoders`` once and return the available hardware encoders.

    The result is cached per ``ffmpeg_binary``, so the subprocess runs at most
    once per binary for the process's lifetime. Any failure to run or parse
    ffmpeg yields an empty set (treated as "no hardware available"), so probing
    can never break a render -- the resolver simply falls back to software.

    This is the only impure function here; :func:`parse_hw_encoders` and
    :func:`resolve_encoder` carry all the logic and are tested directly.
    """
    cached = _PROBE_CACHE.get(ffmpeg_binary)
    if cached is not None:
        return cached
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
            [ffmpeg_binary, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        result = parse_hw_encoders(completed.stdout)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(
            "hardware-encoder probe failed for %s (%s); using software encoders",
            ffmpeg_binary,
            exc,
        )
        result = frozenset()
    _PROBE_CACHE[ffmpeg_binary] = result
    return result


def reset_probe_cache() -> None:
    """Clear the probe cache. Intended for tests that vary the ffmpeg binary."""
    _PROBE_CACHE.clear()


@dataclass(frozen=True)
class EncoderChoice:
    """The outcome of resolving a requested codec against the hwaccel config.

    ``encoder_name`` is the concrete ffmpeg ``-c:v`` value to use.
    ``hwaccel_api`` is the API driving it (one of :data:`HWACCEL_APIS`) when a
    hardware encoder was selected, or ``None`` for the software path.
    ``is_hardware`` is the convenience predicate ``hwaccel_api is not None``.
    ``fallback_reason`` is a short human-readable note set only when hardware was
    requested but software was chosen instead (so the caller can log one clear
    line); it is ``None`` on the software-by-default and hardware-selected paths.
    """

    encoder_name: str
    hwaccel_api: str | None
    fallback_reason: str | None = None

    @property
    def is_hardware(self) -> bool:
        """Whether a hardware encoder was selected."""
        return self.hwaccel_api is not None


def resolve_encoder(
    codec: str,
    *,
    hwaccel_enabled: bool,
    hwaccel_api: str | None,
    available: frozenset[str],
) -> EncoderChoice:
    """Choose the concrete encoder for ``codec`` given the hwaccel configuration.

    Pure and total: it never raises for a hardware miss and always returns a
    usable encoder. The software encoder for ``codec`` is the default and the
    guaranteed fallback; a hardware encoder is chosen only when *all* hold:

    * ``hwaccel_enabled`` is true,
    * ``hwaccel_api`` is a recognised API,
    * that API has a hardware encoder for ``codec``, and
    * that encoder is in ``available`` (the probe result).

    When hardware was requested (enabled) but any condition fails, the returned
    choice is software with a ``fallback_reason`` explaining why, so the caller
    can emit a single clear warning. With ``hwaccel_enabled`` false the result is
    plain software with no reason (today's behaviour, unchanged).

    :raises EncoderCapabilityError: only if ``codec`` itself is not allowlisted
        (delegated to :func:`resolve_codec`); a valid codec always resolves.
    """
    # Always resolve the software encoder first: it is the default return and the
    # fallback. An unknown codec raises here, before any hardware consideration.
    software = resolve_codec(codec)

    if not hwaccel_enabled:
        return EncoderChoice(encoder_name=software, hwaccel_api=None)

    if hwaccel_api not in HWACCEL_APIS:
        return EncoderChoice(
            encoder_name=software,
            hwaccel_api=None,
            fallback_reason=f"unknown hwaccel_api {hwaccel_api!r}",
        )

    hw_name = _HW_ENCODERS[hwaccel_api].get(software)
    if hw_name is None:
        return EncoderChoice(
            encoder_name=software,
            hwaccel_api=None,
            fallback_reason=f"{hwaccel_api} has no hardware encoder for {codec!r}",
        )

    if hw_name not in available:
        return EncoderChoice(
            encoder_name=software,
            hwaccel_api=None,
            fallback_reason=f"{hw_name} not available in this ffmpeg build",
        )

    return EncoderChoice(encoder_name=hw_name, hwaccel_api=hwaccel_api)
