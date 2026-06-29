"""Offline coordinate -> IANA timezone resolution and coordinate validation.

A camera's geographic coordinates determine the local timezone its solar capture
times should be computed in and displayed in. This module wraps an offline
coordinate-to-timezone lookup (no network, data shipped with the dependency) and
provides operator-facing validation of a coordinate pair.

The rest of the codebase always passes ``(latitude, longitude)`` in that natural
order. The wrapped lookup library takes them in the opposite order
(``longitude`` first); that swap is contained entirely within this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import tzfpy

__all__ = [
    "CoordinateCheck",
    "resolve_timezone",
    "resolve_zoneinfo",
    "validate_coordinates",
]

_LAT_MIN, _LAT_MAX = -90.0, 90.0
_LON_MIN, _LON_MAX = -180.0, 180.0


def _in_range(latitude: float, longitude: float) -> bool:
    """Return whether a coordinate pair is within valid geographic bounds."""
    return _LAT_MIN <= latitude <= _LAT_MAX and _LON_MIN <= longitude <= _LON_MAX


def resolve_timezone(latitude: float | None, longitude: float | None) -> str | None:
    """Return the IANA timezone name for a coordinate, or ``None``.

    ``None`` is returned when either coordinate is missing or outside the valid
    range. Over open water the lookup yields an ``Etc/GMT*`` offset zone rather
    than ``None`` -- a real, usable zone; :func:`validate_coordinates` flags that
    case as *approximate* so the UI can warn about it.

    The wrapped ``tzfpy.get_tz`` takes ``(longitude, latitude)`` -- longitude
    first. This function takes ``(latitude, longitude)`` and performs the swap.
    """
    if latitude is None or longitude is None:
        return None
    if not _in_range(latitude, longitude):
        return None
    # tzfpy.get_tz expects (longitude, latitude) -- longitude first.
    return tzfpy.get_tz(longitude, latitude)


def resolve_zoneinfo(
    latitude: float | None, longitude: float | None
) -> ZoneInfo | None:
    """Return a :class:`~zoneinfo.ZoneInfo` for a coordinate, or ``None``.

    ``None`` when the coordinate is missing/out of range, or in the unlikely
    event the resolved name is not present in the timezone database.
    """
    name = resolve_timezone(latitude, longitude)
    if name is None:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


@dataclass(frozen=True)
class CoordinateCheck:
    """Result of validating a camera coordinate pair for solar scheduling.

    Attributes
    ----------
    ok:
        Whether the coordinate is usable for solar computations. ``False`` only
        for missing or out-of-range coordinates; a suspect or open-water
        coordinate is still usable (``ok=True``) but carries a warning message.
    code:
        One of ``"ok"``, ``"missing"``, ``"out_of_range"``,
        ``"suspect_null_island"`` (exactly 0, 0 -- likely an unset default), or
        ``"approximate"`` (open water, only an offset zone available).
    message:
        A human-readable, operator-facing message suitable for display.
    timezone:
        The resolved IANA timezone name, or ``None`` when unresolved.
    approximate:
        ``True`` when the resolved zone is an ``Etc/GMT*`` offset zone (no land
        timezone for the location) rather than a named regional zone.
    """

    ok: bool
    code: str
    message: str
    timezone: str | None
    approximate: bool


def validate_coordinates(
    latitude: float | None, longitude: float | None
) -> CoordinateCheck:
    """Validate a coordinate pair and resolve its timezone for display.

    Checks, in order: both values present; both within range; the (0, 0) "null
    island" default; an open-water approximate zone; otherwise a clean resolved
    zone.
    """
    if latitude is None or longitude is None:
        return CoordinateCheck(
            ok=False,
            code="missing",
            message=(
                "Latitude and longitude are both required to compute solar "
                "capture times for this camera."
            ),
            timezone=None,
            approximate=False,
        )

    if not _in_range(latitude, longitude):
        return CoordinateCheck(
            ok=False,
            code="out_of_range",
            message=(
                "Coordinates are out of range. Latitude must be between -90 and "
                "90 and longitude between -180 and 180."
            ),
            timezone=None,
            approximate=False,
        )

    tz = resolve_timezone(latitude, longitude)
    if tz is None:
        # Range is already validated above, so a missing zone here is unexpected.
        return CoordinateCheck(
            ok=False,
            code="out_of_range",
            message="Could not determine a timezone for these coordinates.",
            timezone=None,
            approximate=False,
        )

    approximate = tz.startswith("Etc/")

    if latitude == 0.0 and longitude == 0.0:
        return CoordinateCheck(
            ok=True,
            code="suspect_null_island",
            message=(
                "Coordinates are 0, 0 -- this is usually an unset default. "
                "Verify the camera's real location before relying on solar "
                "capture times."
            ),
            timezone=tz,
            approximate=approximate,
        )

    if approximate:
        return CoordinateCheck(
            ok=True,
            code="approximate",
            message=(
                "No land timezone was found for these coordinates (open water); "
                f"using the approximate offset zone {tz}."
            ),
            timezone=tz,
            approximate=True,
        )

    return CoordinateCheck(
        ok=True,
        code="ok",
        message=f"Coordinates resolve to the {tz} timezone.",
        timezone=tz,
        approximate=False,
    )
