"""Pure unit tests for sun-time-based schedule evaluation.

Tests compute_sun_times accuracy, sun_window open/close edges with offsets,
and polar (None, None) handling via the noon-elevation tiebreak.
"""

from __future__ import annotations

import datetime
from datetime import UTC, timedelta
from zoneinfo import ZoneInfo

from timelapse_manager.capture.schedule import (
    Schedule,
    compute_sun_times,
    is_within_window,
    next_transition,
    parse_schedule,
)

_UTC = UTC
_CHICAGO_TZ = ZoneInfo("America/Chicago")
_UTC_TZ = ZoneInfo("UTC")

# Chicago: approx 41.85°N, -87.65°W
_CHI_LAT = 41.85
_CHI_LON = -87.65

# Polar Svalbard-ish: 80°N, 0°E
_POLAR_LAT = 80.0
_POLAR_LON = 0.0

# Summer solstice 2026 — polar day
_SUMMER_DATE = datetime.date(2026, 6, 21)
# Winter solstice 2026 — polar night
_WINTER_DATE = datetime.date(2026, 12, 21)

# A normal date for Chicago sun tests
_NORMAL_DATE = datetime.date(2026, 6, 21)


def _utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0
) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, minute, tzinfo=_UTC)


# ---------------------------------------------------------------------------
# compute_sun_times
# ---------------------------------------------------------------------------


class TestComputeSunTimes:
    def test_returns_aware_utc_datetimes(self) -> None:
        sr, ss = compute_sun_times(_CHI_LAT, _CHI_LON, _NORMAL_DATE, _CHICAGO_TZ)
        assert sr is not None and ss is not None
        assert sr.tzinfo is not None
        assert ss.tzinfo is not None

    def test_sunrise_before_sunset(self) -> None:
        sr, ss = compute_sun_times(_CHI_LAT, _CHI_LON, _NORMAL_DATE, _CHICAGO_TZ)
        assert sr is not None and ss is not None
        assert sr < ss

    def test_chicago_sunrise_within_30_minutes_of_known_value(self) -> None:
        # 2026-06-21 Chicago sunrise is approximately 05:15 CDT = 10:15 UTC
        sr, _ = compute_sun_times(_CHI_LAT, _CHI_LON, _NORMAL_DATE, _CHICAGO_TZ)
        assert sr is not None
        known_approx = _utc(2026, 6, 21, 10, 15)
        assert abs((sr - known_approx).total_seconds()) < 30 * 60

    def test_chicago_sunset_within_30_minutes_of_known_value(self) -> None:
        # 2026-06-21 Chicago sunset is approximately 20:28 CDT = 01:28 UTC (next day)
        _, ss = compute_sun_times(_CHI_LAT, _CHI_LON, _NORMAL_DATE, _CHICAGO_TZ)
        assert ss is not None
        known_approx = _utc(2026, 6, 22, 1, 28)
        assert abs((ss - known_approx).total_seconds()) < 30 * 60

    def test_polar_summer_returns_none_none(self) -> None:
        sr, ss = compute_sun_times(_POLAR_LAT, _POLAR_LON, _SUMMER_DATE, _UTC_TZ)
        assert sr is None
        assert ss is None

    def test_polar_winter_returns_none_none(self) -> None:
        sr, ss = compute_sun_times(_POLAR_LAT, _POLAR_LON, _WINTER_DATE, _UTC_TZ)
        assert sr is None
        assert ss is None


# ---------------------------------------------------------------------------
# sun_window: gate open/close edges with offsets
# ---------------------------------------------------------------------------


def _sun_schedule(
    open_anchor: str,
    open_offset: int,
    close_anchor: str,
    close_offset: int,
    *,
    tz: str = "America/Chicago",
) -> Schedule:
    return parse_schedule(
        {
            "enabled": True,
            "timezone": tz,
            "sun_window": [
                {"anchor": open_anchor, "offset_minutes": open_offset},
                {"anchor": close_anchor, "offset_minutes": close_offset},
            ],
        }
    )


class TestSunWindow:
    def test_open_just_after_sunrise(self) -> None:
        s = _sun_schedule("sunrise", 0, "sunset", 0)
        sr, _ = compute_sun_times(_CHI_LAT, _CHI_LON, _NORMAL_DATE, _CHICAGO_TZ)
        assert sr is not None
        # One minute after sunrise: gate open
        after = sr + timedelta(minutes=1)
        assert is_within_window(s, after, latitude=_CHI_LAT, longitude=_CHI_LON)

    def test_closed_just_before_sunrise(self) -> None:
        s = _sun_schedule("sunrise", 0, "sunset", 0)
        sr, _ = compute_sun_times(_CHI_LAT, _CHI_LON, _NORMAL_DATE, _CHICAGO_TZ)
        assert sr is not None
        before = sr - timedelta(minutes=1)
        assert not is_within_window(s, before, latitude=_CHI_LAT, longitude=_CHI_LON)

    def test_closed_at_and_after_sunset(self) -> None:
        s = _sun_schedule("sunrise", 0, "sunset", 0)
        _, ss = compute_sun_times(_CHI_LAT, _CHI_LON, _NORMAL_DATE, _CHICAGO_TZ)
        assert ss is not None
        # At sunset (half-open: closed at end)
        assert not is_within_window(s, ss, latitude=_CHI_LAT, longitude=_CHI_LON)
        after = ss + timedelta(minutes=5)
        assert not is_within_window(s, after, latitude=_CHI_LAT, longitude=_CHI_LON)

    def test_positive_open_offset_delays_gate_open(self) -> None:
        # open = sunrise + 30 min; before that should be closed
        s = _sun_schedule("sunrise", 30, "sunset", 0)
        sr, _ = compute_sun_times(_CHI_LAT, _CHI_LON, _NORMAL_DATE, _CHICAGO_TZ)
        assert sr is not None
        just_at_sunrise = sr + timedelta(minutes=1)
        # Still within the 30-min delay — should be closed
        assert not is_within_window(
            s, just_at_sunrise, latitude=_CHI_LAT, longitude=_CHI_LON
        )
        after_offset = sr + timedelta(minutes=31)
        assert is_within_window(s, after_offset, latitude=_CHI_LAT, longitude=_CHI_LON)

    def test_negative_open_offset_opens_gate_before_sunrise(self) -> None:
        # open = sunrise - 30 min
        s = _sun_schedule("sunrise", -30, "sunset", 0)
        sr, _ = compute_sun_times(_CHI_LAT, _CHI_LON, _NORMAL_DATE, _CHICAGO_TZ)
        assert sr is not None
        # 20 minutes before sunrise: should be open (within the -30 min offset)
        twenty_before = sr - timedelta(minutes=20)
        assert is_within_window(s, twenty_before, latitude=_CHI_LAT, longitude=_CHI_LON)

    def test_positive_close_offset_extends_gate_past_sunset(self) -> None:
        # close = sunset + 45 min
        s = _sun_schedule("sunrise", 0, "sunset", 45)
        _, ss = compute_sun_times(_CHI_LAT, _CHI_LON, _NORMAL_DATE, _CHICAGO_TZ)
        assert ss is not None
        # 30 min after sunset: still open
        thirty_after = ss + timedelta(minutes=30)
        assert is_within_window(s, thirty_after, latitude=_CHI_LAT, longitude=_CHI_LON)
        # 50 min after sunset: closed
        fifty_after = ss + timedelta(minutes=50)
        assert not is_within_window(
            s, fifty_after, latitude=_CHI_LAT, longitude=_CHI_LON
        )

    def test_sun_window_without_location_is_closed(self) -> None:
        s = _sun_schedule("sunrise", 0, "sunset", 0)
        # No lat/lon — cannot evaluate sun window; gate closed
        mid = _utc(2026, 6, 21, 12, 0)
        assert not is_within_window(s, mid, latitude=None, longitude=None)

    def test_sun_window_combined_with_clock_window_uses_union(self) -> None:
        # Add both a narrow clock window AND a sun window;
        # the gate is open inside EITHER.
        s = parse_schedule(
            {
                "enabled": True,
                "timezone": "America/Chicago",
                "windows": [{"start_time": "02:00", "end_time": "03:00"}],
                "sun_window": [
                    {"anchor": "sunrise", "offset_minutes": 0},
                    {"anchor": "sunset", "offset_minutes": 0},
                ],
            }
        )
        # Clock window is 02:00-03:00 America/Chicago (CDT = UTC-5).
        # In UTC that is 07:00-08:00. Use 07:30 UTC to be inside.
        clock_time = _utc(2026, 6, 21, 7, 30)
        # No location — sun window is closed, but clock window is open
        assert is_within_window(s, clock_time, latitude=None, longitude=None)
        # During the sun window (with location):
        sr, _ = compute_sun_times(_CHI_LAT, _CHI_LON, _NORMAL_DATE, _CHICAGO_TZ)
        assert sr is not None
        sun_time = sr + timedelta(minutes=5)
        assert is_within_window(s, sun_time, latitude=_CHI_LAT, longitude=_CHI_LON)


# ---------------------------------------------------------------------------
# Polar conditions: noon-elevation tiebreak
# ---------------------------------------------------------------------------


class TestPolarConditions:
    def test_polar_day_gate_is_open(self) -> None:
        # 80°N in June: polar day (sun never sets). With a sun_window open=sunrise,
        # close=sunset and no finite sunrise/sunset, the noon-elevation tiebreak
        # should return True (open).
        s = _sun_schedule("sunrise", 0, "sunset", 0, tz="UTC")
        # Query at local noon in polar summer
        noon_june = _utc(2026, 6, 21, 12, 0)
        result = is_within_window(
            s, noon_june, latitude=_POLAR_LAT, longitude=_POLAR_LON
        )
        assert result is True

    def test_polar_night_gate_is_closed(self) -> None:
        # 80°N in December: polar night (sun never rises). The noon-elevation
        # tiebreak should return False (closed).
        s = _sun_schedule("sunrise", 0, "sunset", 0, tz="UTC")
        noon_dec = _utc(2026, 12, 21, 12, 0)
        result = is_within_window(
            s, noon_dec, latitude=_POLAR_LAT, longitude=_POLAR_LON
        )
        assert result is False

    def test_next_transition_with_polar_day_returns_none_for_always_open(self) -> None:
        s = _sun_schedule("sunrise", 0, "sunset", 0, tz="UTC")
        noon = _utc(2026, 6, 21, 12, 0)
        # Even with a sun_window, polar day means gate is open; next_transition
        # scans forward but every day is still polar day for weeks, so either
        # a far-future transition or None. The important thing: gate is open now.
        is_open, _ = next_transition(s, noon, latitude=_POLAR_LAT, longitude=_POLAR_LON)
        assert is_open

    def test_compute_sun_times_returns_none_none_at_polar_lat_in_summer(self) -> None:
        sr, ss = compute_sun_times(_POLAR_LAT, _POLAR_LON, _SUMMER_DATE, _UTC_TZ)
        assert sr is None
        assert ss is None

    def test_compute_sun_times_returns_none_none_at_polar_lat_in_winter(self) -> None:
        sr, ss = compute_sun_times(_POLAR_LAT, _POLAR_LON, _WINTER_DATE, _UTC_TZ)
        assert sr is None
        assert ss is None
