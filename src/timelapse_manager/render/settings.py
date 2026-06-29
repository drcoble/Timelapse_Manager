"""Render settings: the enumerated encode choices a project stores.

A project's automatic-render configuration is kept in its ``render_schedule``
JSON document. The web form edits it through a small set of dropdowns rather than
raw JSON; this module is the single source of truth for what those dropdowns
offer, which encoder/container combinations are actually muxable, and how the
stored choices translate into the encoder's ``output_settings`` shape.

The stored document is a flat object::

    {
        "enabled": false,
        "interval_seconds": 86400,
        "encoder": "libx264",
        "container": "mp4",
        "fps": 24,
        "resolution": "1920x1080",
        "auto_prune": true
    }

``resolution`` is either ``WIDTHxHEIGHT`` or the literal ``"source"`` (keep the
captured frames' native size, no scaling). Everything here is pure -- no I/O, no
database -- so both the web layer and the render path can share it and a test can
exercise the rules directly.
"""

from __future__ import annotations

from typing import Any

# The encoders offered in the UI, as ``(value, label)`` pairs. The value is the
# ffmpeg encoder name, which is also a key the encode allowlist already accepts,
# so the form value, the validity rules here, and the stored ``codec`` all speak
# one vocabulary with no translation.
ENCODER_OPTIONS: tuple[tuple[str, str], ...] = (
    ("libx264", "H.264 (libx264)"),
    ("libx265", "H.265 (libx265)"),
    ("libvpx-vp9", "VP9 (libvpx-vp9)"),
    ("libsvtav1", "AV1 (libsvtav1)"),
)

# The containers offered in the UI, as ``(value, label)`` pairs.
CONTAINER_OPTIONS: tuple[tuple[str, str], ...] = (
    ("mp4", "MP4 (mp4)"),
    ("mkv", "MKV (mkv)"),
    ("webm", "WebM (webm)"),
)

# Frame-rate bounds. Any positive integer in this inclusive range is accepted;
# the values are clamped to sane limits rather than a fixed preset list.
MIN_FPS = 1
MAX_FPS = 240

# The resolutions offered in the UI, as ``(value, label)`` pairs. ``"source"``
# keeps the captured frames' native size (no scaling).
RESOLUTION_OPTIONS: tuple[tuple[str, str], ...] = (
    ("1280x720", "1280 x 720 (720p)"),
    ("1920x1080", "1920 x 1080 (1080p)"),
    ("2560x1440", "2560 x 1440 (1440p)"),
    ("3840x2160", "3840 x 2160 (2160p / 4K)"),
    ("source", "Source (no scaling)"),
)

# The render-frequency choices, as ``(seconds, label)`` pairs.
FREQUENCY_OPTIONS: tuple[tuple[int, str], ...] = (
    (3600, "Every hour"),
    (21600, "Every 6 hours"),
    (43200, "Every 12 hours"),
    (86400, "Daily"),
    (604800, "Weekly"),
)

# The defaults a fresh or never-configured project presents.
DEFAULT_ENCODER = "libx264"
DEFAULT_CONTAINER = "mp4"
DEFAULT_FPS = 24
DEFAULT_RESOLUTION = "1920x1080"
DEFAULT_FREQUENCY_SECONDS = 86400

# Auto-prune (deleting the stills a render consumed) is enabled by default. The
# stored document carries it under this key; a schedule that predates the feature
# has no key and is treated as enabled.
AUTO_PRUNE_KEY = "auto_prune"
DEFAULT_AUTO_PRUNE = True

# Auto chapter markers: insert a chapter at each week or month boundary in the
# rendered video. Stored under this key alongside the other encode choices; the
# literal "none" (also the value for a missing or unrecognised key) means no
# chapter markers are emitted. The accepted values mirror the manual-render
# vocabulary so a scheduled render and a manual one drive the same code path.
AUTO_CHAPTERS_KEY = "auto_chapters"
DEFAULT_AUTO_CHAPTERS = "none"
_AUTO_CHAPTER_GRANULARITIES: frozenset[str] = frozenset({"weekly", "monthly"})

_VALID_ENCODERS: frozenset[str] = frozenset(v for v, _ in ENCODER_OPTIONS)
_VALID_CONTAINERS: frozenset[str] = frozenset(v for v, _ in CONTAINER_OPTIONS)
_VALID_FREQUENCIES: frozenset[int] = frozenset(s for s, _ in FREQUENCY_OPTIONS)

# Which encoders each container can actually mux. MP4 carries H.264/H.265/AV1 but
# not VP9; WebM carries only VP9; MKV (Matroska) carries all of them. Keyed by the
# same ffmpeg encoder names the form submits.
_CONTAINER_ENCODERS: dict[str, frozenset[str]] = {
    "mp4": frozenset({"libx264", "libx265", "libsvtav1"}),
    "webm": frozenset({"libvpx-vp9"}),
    "mkv": frozenset({"libx264", "libx265", "libvpx-vp9", "libsvtav1"}),
}

# Resolution token -> (width, height). ``"source"`` is intentionally absent: it
# means "no explicit size", handled by the callers as ``None`` dimensions.
_RESOLUTION_DIMENSIONS: dict[str, tuple[int, int]] = {
    "1280x720": (1280, 720),
    "1920x1080": (1920, 1080),
    "2560x1440": (2560, 1440),
    "3840x2160": (3840, 2160),
}

SOURCE_RESOLUTION = "source"


def is_supported_combination(encoder: str, container: str) -> bool:
    """Return whether ``container`` can mux ``encoder``.

    The single rule used by both the live combo-check endpoint and the
    server-side save validation, so the client warning and the server's refusal
    can never disagree. An unknown encoder or container reads as unsupported.
    """
    allowed = _CONTAINER_ENCODERS.get(container.lower())
    if allowed is None:
        return False
    return encoder.lower() in allowed


def combination_warning(encoder: str, container: str) -> str | None:
    """Return a human warning for an invalid encoder/container pair, else ``None``."""
    if is_supported_combination(encoder, container):
        return None
    enc_label = dict(ENCODER_OPTIONS).get(encoder, encoder)
    con_label = dict(CONTAINER_OPTIONS).get(container, container)
    return (
        f"{enc_label} cannot be stored in a {con_label} file. "
        "Choose another combination."
    )


def normalize_render_settings(
    *,
    enabled: bool,
    interval_seconds: int,
    encoder: str,
    container: str,
    fps: int,
    resolution: str,
    auto_prune: bool = DEFAULT_AUTO_PRUNE,
    auto_chapters: str = DEFAULT_AUTO_CHAPTERS,
) -> dict[str, Any]:
    """Build the stored ``render_schedule`` document from chosen values.

    The shape is flat and self-describing; the resolution is kept as its token
    (``WIDTHxHEIGHT`` or ``"source"``) and only decomposed into width/height when
    a render is actually built. ``auto_prune`` defaults to enabled for a freshly
    normalized document.

    This is the validating builder: ``fps`` must be a whole number of frames per
    second in the inclusive ``MIN_FPS..MAX_FPS`` range. A non-integer or
    out-of-range value raises :class:`ValueError` so a save can refuse it rather
    than persist a frame rate the encoder cannot honour.

    ``auto_chapters`` is normalized to ``none``/``weekly``/``monthly``; an
    unrecognised value is stored as ``"none"`` (no chapter markers).
    """
    fps = _validate_fps(fps)
    return {
        "enabled": enabled,
        "interval_seconds": interval_seconds,
        "encoder": encoder,
        "container": container,
        "fps": fps,
        "resolution": resolution,
        AUTO_PRUNE_KEY: bool(auto_prune),
        AUTO_CHAPTERS_KEY: normalize_auto_chapters(auto_chapters),
    }


def render_settings_view(schedule: dict[str, Any] | None) -> dict[str, Any]:
    """Read a stored schedule into the values the edit form prefills.

    Backward tolerant: a ``None`` schedule, an old ``{enabled, interval_seconds}``
    shape, or any missing key falls back to the defaults, so an existing project
    always renders sensible dropdown selections rather than crashing. Unknown
    encoder/container/resolution tokens fall back to their defaults too, and an
    fps outside the accepted range falls back to the default rather than raising.

    Auto-prune is exposed under ``"autoprune"`` for the template and is also
    echoed back under the stored ``"auto_prune"`` key, so that persisting this
    view as the schedule round-trips the setting (the same key
    :func:`auto_prune_enabled` reads). A schedule with no stored ``auto_prune``
    key reads as enabled.
    """
    raw = schedule if isinstance(schedule, dict) else {}

    encoder = str(raw.get("encoder") or DEFAULT_ENCODER)
    if encoder not in _VALID_ENCODERS:
        encoder = DEFAULT_ENCODER
    container = str(raw.get("container") or DEFAULT_CONTAINER)
    if container not in _VALID_CONTAINERS:
        container = DEFAULT_CONTAINER

    fps = _coerce_int(raw.get("fps"), DEFAULT_FPS)
    if fps < MIN_FPS or fps > MAX_FPS:
        fps = DEFAULT_FPS

    resolution = str(raw.get("resolution") or DEFAULT_RESOLUTION)
    if resolution != SOURCE_RESOLUTION and resolution not in _RESOLUTION_DIMENSIONS:
        resolution = DEFAULT_RESOLUTION

    interval = _coerce_int(raw.get("interval_seconds"), DEFAULT_FREQUENCY_SECONDS)
    if interval not in _VALID_FREQUENCIES:
        interval = DEFAULT_FREQUENCY_SECONDS

    auto_prune = auto_prune_enabled(raw)

    return {
        "enabled": bool(raw.get("enabled", False)),
        "interval_seconds": interval,
        "encoder": encoder,
        "container": container,
        "fps": fps,
        "resolution": resolution,
        # Stored key (round-trips through the save path and is what
        # ``auto_prune_enabled`` reads) plus the template-facing alias.
        AUTO_PRUNE_KEY: auto_prune,
        "autoprune": auto_prune,
    }


def auto_prune_enabled(schedule: dict[str, Any] | None) -> bool:
    """Return whether auto-prune is enabled for a stored schedule.

    Auto-prune is enabled by default: a ``None`` schedule, or one that predates
    the feature and so carries no ``auto_prune`` key, reads as enabled. When the
    key is present its stored boolean is honoured (a falsey value disables it).
    """
    if not isinstance(schedule, dict) or AUTO_PRUNE_KEY not in schedule:
        return DEFAULT_AUTO_PRUNE
    return bool(schedule[AUTO_PRUNE_KEY])


def normalize_auto_chapters(value: Any) -> str:
    """Coerce an auto-chapters choice to ``none``, ``weekly``, or ``monthly``.

    Anything outside the two real granularities -- a missing key, ``None``, an
    unrecognised string, or the explicit ``"none"`` -- collapses to ``"none"`` so
    a stale or hand-edited schedule still presents a valid choice and a render
    that should have no chapters never gets a chapter request.
    """
    if isinstance(value, str) and value in _AUTO_CHAPTER_GRANULARITIES:
        return value
    return DEFAULT_AUTO_CHAPTERS


def auto_chapters_choice(schedule: dict[str, Any] | None) -> str:
    """Return the normalized auto-chapters choice stored on a schedule.

    A ``None`` schedule, one that predates the feature (no ``auto_chapters``
    key), or one carrying an unrecognised value all read as ``"none"``.
    """
    raw = schedule if isinstance(schedule, dict) else {}
    return normalize_auto_chapters(raw.get(AUTO_CHAPTERS_KEY))


def suggested_fps(capture_interval_seconds: float | int) -> list[int]:
    """Suggest a few sensible playback frame rates for a capture cadence.

    Returns a short, ascending, de-duplicated list of integer fps the UI can
    offer as hints. The set is anchored on common video frame rates and is
    deterministic and side-effect-free; a faster capture cadence (smaller
    interval) leans toward higher frame rates. The chosen frame rate is never
    forced -- any whole number in ``MIN_FPS..MAX_FPS`` is accepted on save.
    """
    interval = float(capture_interval_seconds)
    if interval < 5:
        # Sub-5s cadence captures enough frames for smooth, high-rate playback.
        candidates = (24, 30, 60)
    elif interval <= 60:
        candidates = (12, 24, 30)
    elif interval <= 3600:
        candidates = (10, 24, 30)
    else:
        # Slow cadence (hours+): fewer frames, so lower playback rates read best.
        candidates = (6, 12, 24)
    ordered = sorted({f for f in candidates if MIN_FPS <= f <= MAX_FPS})
    return ordered or [DEFAULT_FPS]


def output_settings_from_schedule(
    schedule: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Derive a render job's ``output_settings`` from a stored schedule.

    Returns the ``{fps, codec, container, width, height}`` dict the spec builder
    consumes, or ``None`` when the schedule carries no usable encode choices (so
    the spec builder applies its own defaults).

    For a ``"source"`` resolution, width and height are omitted entirely so the
    encoder keeps the captured frames' native size (no scale filter).

    Backward tolerant: a legacy schedule that nests an ``output_settings`` dict
    (rather than carrying flat ``encoder``/``container``/... fields) is passed
    through unchanged, so an older-shaped or hand-written schedule still works.
    """
    if not isinstance(schedule, dict):
        return None

    flat_keys = {"encoder", "container", "fps", "resolution"}
    if not (flat_keys & schedule.keys()):
        nested = schedule.get("output_settings")
        return nested if isinstance(nested, dict) else None

    view = render_settings_view(schedule)
    output: dict[str, Any] = {
        "fps": view["fps"],
        "codec": view["encoder"],
        "container": view["container"],
    }
    dims = _RESOLUTION_DIMENSIONS.get(view["resolution"])
    if dims is not None:
        output["width"], output["height"] = dims
    # Mirror the manual-render output shape: only a real granularity carries the
    # key, so a "none" choice emits no chapter request at all (matching how a
    # manual render omits it).
    auto_chapters = auto_chapters_choice(schedule)
    if auto_chapters in _AUTO_CHAPTER_GRANULARITIES:
        output[AUTO_CHAPTERS_KEY] = auto_chapters
    return output


def _coerce_int(value: Any, default: int) -> int:
    """Coerce ``value`` to ``int``, falling back to ``default`` on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _validate_fps(value: Any) -> int:
    """Return ``value`` as a frame rate, or raise ``ValueError`` if invalid.

    A valid frame rate is a whole number (``int``, not ``bool``) in the inclusive
    ``MIN_FPS..MAX_FPS`` range. Floats -- even whole-valued ones like ``24.0`` --
    and out-of-range values are rejected so an unsupported cadence is refused at
    save time rather than silently coerced.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"fps must be a whole number between {MIN_FPS} and {MAX_FPS}, "
            f"got {value!r}."
        )
    if value < MIN_FPS or value > MAX_FPS:
        raise ValueError(f"fps must be between {MIN_FPS} and {MAX_FPS}, got {value}.")
    return int(value)
