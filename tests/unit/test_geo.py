"""Tests for offline coordinate -> timezone resolution and validation."""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from timelapse_manager.capture.geo import (
    resolve_timezone,
    resolve_zoneinfo,
    validate_coordinates,
)


@pytest.mark.parametrize(
    ("latitude", "longitude", "expected"),
    [
        (41.85, -87.65, "America/Chicago"),
        (48.8566, 2.3522, "Europe/Paris"),
        (35.6762, 139.6503, "Asia/Tokyo"),
        # Regression guard: a swapped (lon, lat) call would land in the ocean and
        # NOT resolve to Australia/Sydney. The wrapper must pass longitude first
        # to the underlying library while accepting (latitude, longitude) here.
        (-33.8688, 151.2093, "Australia/Sydney"),
    ],
)
def test_resolve_timezone_known_coordinates(
    latitude: float, longitude: float, expected: str
) -> None:
    assert resolve_timezone(latitude, longitude) == expected


def test_resolve_timezone_arg_order_regression() -> None:
    # Sydney's coordinates fed in the natural (lat, lon) order must resolve to
    # Sydney. If the wrapper forwarded them in the wrong order the result would
    # be a different (or ocean) zone.
    sydney = resolve_timezone(-33.8688, 151.2093)
    swapped = resolve_timezone(151.2093, -33.8688)  # out of latitude range
    assert sydney == "Australia/Sydney"
    assert swapped is None  # 151.2 is not a valid latitude


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [
        (None, -87.65),
        (41.85, None),
        (None, None),
        (91.0, 0.0),  # latitude out of range
        (0.0, 181.0),  # longitude out of range
        (-90.5, 10.0),
    ],
)
def test_resolve_timezone_missing_or_out_of_range(
    latitude: float | None, longitude: float | None
) -> None:
    assert resolve_timezone(latitude, longitude) is None


def test_resolve_zoneinfo_returns_zoneinfo() -> None:
    tz = resolve_zoneinfo(41.85, -87.65)
    assert isinstance(tz, ZoneInfo)
    assert str(tz) == "America/Chicago"
    assert resolve_zoneinfo(None, None) is None


def test_validate_missing() -> None:
    check = validate_coordinates(None, None)
    assert check.ok is False
    assert check.code == "missing"
    assert check.timezone is None


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [(91.0, 0.0), (0.0, -181.0), (-200.0, 200.0)],
)
def test_validate_out_of_range(latitude: float, longitude: float) -> None:
    check = validate_coordinates(latitude, longitude)
    assert check.ok is False
    assert check.code == "out_of_range"


def test_validate_ok() -> None:
    check = validate_coordinates(41.85, -87.65)
    assert check.ok is True
    assert check.code == "ok"
    assert check.timezone == "America/Chicago"
    assert check.approximate is False


def test_validate_null_island() -> None:
    check = validate_coordinates(0.0, 0.0)
    assert check.ok is True
    assert check.code == "suspect_null_island"
    # 0,0 is open water, so the resolved zone is an approximate offset zone.
    assert check.approximate is True
    assert check.timezone is not None


def test_validate_open_water_is_approximate() -> None:
    # Middle of the South Pacific: no land timezone, an Etc/GMT* offset zone.
    check = validate_coordinates(-40.0, -140.0)
    assert check.ok is True
    assert check.code == "approximate"
    assert check.approximate is True
    assert check.timezone is not None
    assert check.timezone.startswith("Etc/")
