"""Conservative allowlist for the FFmpeg encoder's argv.

Everything that can reach the ffmpeg command line -- codecs, containers, the
filters used to build the filtergraph, and the numeric encode parameters -- is
checked against a fixed allowlist *before* a subprocess is ever spawned. An
unrecognised value raises :class:`~.encoder.EncoderCapabilityError`, naming the
offending option, rather than being passed through to ffmpeg.

This is a defence-in-depth boundary: the higher layers already constrain what a
user can request, but the encoder treats its inputs as untrusted and refuses to
assemble a command containing anything it does not explicitly understand.
"""

from __future__ import annotations

from .encoder import EncoderCapabilityError

# Logical codec name -> the ffmpeg encoder that produces it. The key is what a
# caller (and the stored render settings) speaks in; the value is the concrete
# ``-c:v`` argument. Only *software* encoders appear here -- the logical codec a
# render requests is always a software encoder name, and hardware acceleration
# (if enabled and available) is a later, separate substitution that swaps in a
# hardware ``-c:v`` while keeping the same logical codec. See :mod:`.hwaccel`.
CODEC_ENCODERS: dict[str, str] = {
    "h264": "libx264",
    "libx264": "libx264",
    "h265": "libx265",
    "hevc": "libx265",
    "libx265": "libx265",
    "vp9": "libvpx-vp9",
    "libvpx-vp9": "libvpx-vp9",
    "av1": "libsvtav1",
    "libsvtav1": "libsvtav1",
}

# The concrete hardware encoder names this package may emit on the hw path, each
# tagged with the logical codec family it produces so container pairing (below)
# can treat ``h264_vaapi`` exactly like ``libx264`` and ``av1_qsv`` like
# ``libsvtav1``. These are *not* added to ``CODEC_ENCODERS``: a render never
# requests them directly; the hwaccel resolver substitutes them in.
HW_ENCODER_CODECS: dict[str, str] = {
    "h264_nvenc": "h264",
    "hevc_nvenc": "h265",
    "h264_qsv": "h264",
    "hevc_qsv": "h265",
    "av1_qsv": "av1",
    "h264_vaapi": "h264",
    "hevc_vaapi": "h265",
    "av1_vaapi": "av1",
}

# AV1 is only muxable into MP4 and MKV here; WebM carrying AV1 has too little
# support to offer, so the pairing is rejected before spawn. This is a narrow,
# codec-specific guard -- not a general codec/container matrix. It applies to AV1
# however it is produced: the software encoder and the hardware AV1 encoders.
# The hardware AV1 names are derived from ``HW_ENCODER_CODECS`` so this guard and
# the hardware mapping cannot drift apart.
_AV1_ENCODER = "libsvtav1"
_AV1_ENCODERS: frozenset[str] = frozenset({"libsvtav1"}) | frozenset(
    hw_name for hw_name, codec in HW_ENCODER_CODECS.items() if codec == "av1"
)
_AV1_CONTAINERS: frozenset[str] = frozenset({"mp4", "mkv"})

# Container short name -> the ffmpeg muxer (``-f``) it maps to.
CONTAINER_MUXERS: dict[str, str] = {
    "mp4": "mp4",
    "mkv": "matroska",
    "webm": "webm",
}

# The only filter names the filtergraph builder is permitted to emit. Any filter
# string assembled by this package is validated against this set so a bug (or a
# crafted overlay value that slipped past sanitisation) cannot introduce an
# arbitrary filter.
ALLOWED_FILTERS: frozenset[str] = frozenset(
    {
        "deflicker",
        "drawtext",
        "scale",
        "fps",
        "format",
        "setpts",
        "overlay",
    }
)

# Extra filter names permitted *only* on a hardware-encode path: frame uploads to
# and downloads from GPU memory, and the per-API hardware scalers. They are kept
# out of :data:`ALLOWED_FILTERS` so the software path is not loosened -- a
# software render that somehow tried to emit ``hwupload`` is still rejected.
HW_ALLOWED_FILTERS: frozenset[str] = frozenset(
    {
        "hwupload",
        "hwdownload",
        "scale_vaapi",
        "scale_qsv",
        "scale_cuda",
    }
)

# Inclusive bounds for the numeric encode parameters. These are sanity bounds,
# not codec-specific tuning: they exist so a nonsensical or hostile value cannot
# reach ffmpeg.
_MIN_FPS = 0.1
_MAX_FPS = 240.0
_MIN_DIMENSION = 2
_MAX_DIMENSION = 16384
_MIN_CRF = 0
_MAX_CRF = 63
_MIN_BITRATE_KBPS = 1
_MAX_BITRATE_KBPS = 1_000_000


def resolve_codec(codec: str) -> str:
    """Return the ffmpeg encoder name for an allowlisted logical codec.

    :raises EncoderCapabilityError: if ``codec`` is not on the allowlist.
    """
    encoder = CODEC_ENCODERS.get(codec.lower())
    if encoder is None:
        raise EncoderCapabilityError(f"unsupported codec: {codec!r}", option="codec")
    return encoder


def resolve_container(container: str) -> str:
    """Return the ffmpeg muxer name for an allowlisted container.

    :raises EncoderCapabilityError: if ``container`` is not on the allowlist.
    """
    muxer = CONTAINER_MUXERS.get(container.lower())
    if muxer is None:
        raise EncoderCapabilityError(
            f"unsupported container: {container!r}", option="container"
        )
    return muxer


def validate_codec_container(encoder: str, container: str) -> None:
    """Reject a codec/container pairing the encoder cannot meaningfully produce.

    Only AV1 is constrained here: it is restricted to MP4 and MKV. All other
    codecs are unconstrained at this layer. This is a defence-in-depth check,
    not a full pairing matrix.

    :raises EncoderCapabilityError: if AV1 is paired with an unsupported
        container.
    """
    if encoder in _AV1_ENCODERS and container.lower() not in _AV1_CONTAINERS:
        raise EncoderCapabilityError(
            f"container {container!r} cannot carry AV1", option="container"
        )


def ensure_filters_allowed(filter_names: list[str], *, allow_hw: bool = False) -> None:
    """Reject any filter name not on the active allowlist.

    The software allowlist (:data:`ALLOWED_FILTERS`) is always in force. The
    hardware filters (:data:`HW_ALLOWED_FILTERS`) are accepted only when
    ``allow_hw`` is true -- i.e. when a hardware encoder was selected -- so the
    software path is never loosened by the existence of the hardware path.

    :param filter_names: the filter names the filtergraph builder intends to emit.
    :param allow_hw: permit the hardware-only filters in addition to the software
        ones; pass ``True`` only on a confirmed hardware-encode path.
    :raises EncoderCapabilityError: naming the first disallowed filter.
    """
    permitted = ALLOWED_FILTERS | HW_ALLOWED_FILTERS if allow_hw else ALLOWED_FILTERS
    for name in filter_names:
        if name not in permitted:
            raise EncoderCapabilityError(
                f"disallowed filter: {name!r}", option="filter"
            )


def validate_fps(fps: float) -> None:
    """Reject a frame rate outside the supported range.

    :raises EncoderCapabilityError: if ``fps`` is non-finite or out of range.
    """
    if not (_MIN_FPS <= fps <= _MAX_FPS):
        raise EncoderCapabilityError(
            f"fps out of range [{_MIN_FPS}, {_MAX_FPS}]: {fps!r}", option="fps"
        )


def validate_dimensions(width: int, height: int) -> None:
    """Reject output dimensions outside the supported range or not even.

    H.264/H.265 with ``yuv420p`` require even dimensions; rather than silently
    pad, an odd dimension is rejected so the caller resolves it explicitly.

    :raises EncoderCapabilityError: if a dimension is out of range or odd.
    """
    for label, value in (("width", width), ("height", height)):
        if not (_MIN_DIMENSION <= value <= _MAX_DIMENSION):
            raise EncoderCapabilityError(
                f"{label} out of range [{_MIN_DIMENSION}, {_MAX_DIMENSION}]: {value!r}",
                option=label,
            )
        if value % 2 != 0:
            raise EncoderCapabilityError(
                f"{label} must be even for yuv420p encoding: {value!r}",
                option=label,
            )


def validate_crf(crf: int) -> None:
    """Reject a constant-rate-factor value outside the supported range.

    :raises EncoderCapabilityError: if ``crf`` is out of range.
    """
    if not (_MIN_CRF <= crf <= _MAX_CRF):
        raise EncoderCapabilityError(
            f"crf out of range [{_MIN_CRF}, {_MAX_CRF}]: {crf!r}", option="crf"
        )


def validate_bitrate_kbps(bitrate_kbps: int) -> None:
    """Reject a bitrate outside the supported range.

    :raises EncoderCapabilityError: if ``bitrate_kbps`` is out of range.
    """
    if not (_MIN_BITRATE_KBPS <= bitrate_kbps <= _MAX_BITRATE_KBPS):
        raise EncoderCapabilityError(
            f"bitrate_kbps out of range [{_MIN_BITRATE_KBPS}, "
            f"{_MAX_BITRATE_KBPS}]: {bitrate_kbps!r}",
            option="bitrate_kbps",
        )
