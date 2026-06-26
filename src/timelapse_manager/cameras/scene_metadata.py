"""Display normalization for per-frame scene metadata.

A captured frame may carry a small JSON envelope describing the camera's scene
settings at capture time (resolution, compression, exposure, ...). The envelope
is firmware-dependent: only ``schema_version``, ``source`` and
``captured_resolution`` are always present, and the remaining keys appear only
when the camera exposed them.

This module turns that raw envelope into display-ready, grouped rows so a
template can stay dumb -- iterate groups, iterate rows, print label/value -- with
no formatting or presence logic of its own. Everything here is a pure function:
no I/O, no exceptions on unexpected value types, and a stable group/row order.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

# A value formatter: takes any JSON value and returns its display string. Every
# formatter must be total -- it must never raise, even on an unexpected type.
_Formatter = Callable[[object], str]


@dataclass(frozen=True)
class SceneRow:
    """A single label/value pair ready to render in a scene-metadata view."""

    label: str
    value: str


@dataclass(frozen=True)
class SceneGroup:
    """A titled set of rows. Emitted only when it has at least one row."""

    title: str
    rows: list[SceneRow]


# The resolution separator a normalized value uses: a true multiplication sign
# rather than the ASCII ``x`` the camera reports.
_RESOLUTION_SEPARATOR = "×"  # ×

# The degree sign appended to a numeric rotation value.
_DEGREE_SIGN = "°"  # °


def scene_schema_version(meta: dict[str, object] | None) -> int | None:
    """Return the envelope's integer ``schema_version``, or ``None``.

    ``None`` is returned for a missing envelope, a missing key, or a value that
    is not an integer (a bare ``bool`` is rejected too -- it is an ``int``
    subclass but never a meaningful schema version).
    """
    if not meta:
        return None
    value = meta.get("schema_version")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def normalize_scene_metadata(meta: dict[str, object] | None) -> list[SceneGroup]:
    """Turn a raw scene-metadata envelope into ordered, display-ready groups.

    Returns groups in the fixed order Capture -> Appearance -> Exposure, each
    holding only the rows whose source key is present (and non-``None``). A group
    with no present rows is omitted entirely so the view never shows an empty
    header. ``None`` or an empty envelope yields an empty list, which a template
    renders as a "no metadata" null state.

    Values may be any JSON type (``str``/``int``/``float``/``list``/``None``);
    this function never raises on an unexpected type, coercing to a sensible
    string instead.
    """
    if not meta:
        return []

    groups: list[SceneGroup] = []
    for title, builder in (
        ("Capture", _capture_rows),
        ("Appearance", _appearance_rows),
        ("Exposure", _exposure_rows),
    ):
        rows = builder(meta)
        if rows:
            groups.append(SceneGroup(title=title, rows=rows))
    return groups


def _capture_rows(meta: dict[str, object] | None) -> list[SceneRow]:
    rows: list[SceneRow] = []
    _append(rows, meta, "captured_resolution", "Resolution", _format_resolution)
    _append(rows, meta, "source", "Source", _coerce_str)
    return rows


def _appearance_rows(meta: dict[str, object] | None) -> list[SceneRow]:
    rows: list[SceneRow] = []
    _append(
        rows, meta, "appearance_resolution", "Stream resolution", _format_resolution
    )
    _append(rows, meta, "compression", "Compression", _coerce_str)
    _append(rows, meta, "rotation", "Rotation", _format_rotation)
    _append(rows, meta, "overlays", "Overlays", _format_overlays)
    return rows


def _exposure_rows(meta: dict[str, object] | None) -> list[SceneRow]:
    rows: list[SceneRow] = []
    _append(rows, meta, "brightness", "Brightness", _coerce_str)
    _append(rows, meta, "contrast", "Contrast", _coerce_str)
    _append(rows, meta, "saturation", "Saturation", _coerce_str)
    _append(rows, meta, "sharpness", "Sharpness", _coerce_str)
    _append(rows, meta, "color_enabled", "Color", _coerce_str)
    _append(rows, meta, "exposure_value", "Exposure", _format_exposure_value)
    _append(rows, meta, "exposure_priority", "Exposure priority", _coerce_str)
    return rows


# --- row assembly -----------------------------------------------------------


def _append(
    rows: list[SceneRow],
    meta: dict[str, object] | None,
    key: str,
    label: str,
    formatter: _Formatter,
) -> None:
    """Append a formatted row for ``key`` when it is present and non-``None``.

    A key that is absent, or present with a ``None`` value, is skipped: an
    explicit ``None`` is treated the same as an omitted key so the view never
    shows a blank value.
    """
    if meta is None:
        return
    value = meta.get(key)
    if value is None:
        return
    rows.append(SceneRow(label=label, value=formatter(value)))


# --- value formatting -------------------------------------------------------
#
# Every formatter accepts ``object`` and must never raise: a value of an
# unexpected type falls back to a plain string rather than an error.


def _coerce_str(value: object) -> str:
    """Coerce any JSON value to a display string without raising.

    Strings pass through verbatim; everything else (``int``/``float``/``list``/
    other) is rendered with ``str()``.
    """
    if isinstance(value, str):
        return value
    return str(value)


def _format_resolution(value: object) -> str:
    """Reformat a ``WxH`` resolution as ``W × H``.

    Accepts ``x`` or ``×`` as the separator. A value that is not a simple
    ``<digits><sep><digits>`` pair is returned coerced as-is, so an unexpected
    shape degrades gracefully rather than being mangled.
    """
    text = _coerce_str(value).strip()
    for separator in ("x", "X", _RESOLUTION_SEPARATOR):
        if separator in text:
            left, _, right = text.partition(separator)
            left, right = left.strip(), right.strip()
            if left.isdigit() and right.isdigit():
                return f"{left} {_RESOLUTION_SEPARATOR} {right}"
            break
    return text


def _format_rotation(value: object) -> str:
    """Append a degree sign to a numeric rotation; pass non-numeric as-is."""
    text = _coerce_str(value).strip()
    if _is_number(text):
        return f"{text}{_DEGREE_SIGN}"
    return text


def _format_overlays(value: object) -> str:
    """Render overlays, mapping an empty value (string or list) to ``"none"``.

    A list of overlay names is joined with ``", "``; any other non-empty value
    is coerced to a string.
    """
    if isinstance(value, list):
        items = [_coerce_str(item) for item in value if item is not None]
        items = [item for item in items if item.strip()]
        return ", ".join(items) if items else "none"
    text = _coerce_str(value).strip()
    return text if text else "none"


def _format_exposure_value(value: object) -> str:
    """Render an exposure value as a signed ``EV`` reading, e.g. ``+1.3 EV``.

    Only applied when the value parses as a number: a positive number gains a
    leading ``+`` and a negative keeps its ``-`` (both suffixed with `` EV``).
    Anything that does not parse as a number -- or already carries an explicit
    sign in its text -- is returned coerced as-is so no malformed ``++`` reading
    is produced.
    """
    text = _coerce_str(value).strip()
    if not _is_number(text):
        return text
    if text.startswith(("+", "-")):
        # Already signed by the camera: trust its sign rather than re-deriving.
        return f"{text} EV"
    number = float(text)
    sign = "+" if number >= 0 else "-"
    return f"{sign}{text} EV"


def _is_number(text: str) -> bool:
    """Return whether ``text`` parses as an ``int`` or ``float``."""
    try:
        float(text)
    except (TypeError, ValueError):
        return False
    return True
