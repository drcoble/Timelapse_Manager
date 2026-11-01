"""Captures per day from the full schedule (#22).

Pure tests of :func:`timelapse_manager.capture.cadence.captures_per_day` and
the gate helper it walks (:func:`timelapse_manager.capture.schedule.open_stretches`):
anchors ignore the gate, interval ticks only run while it is open, the horizon
and its divisor, daylight-saving days, and a window that crosses midnight.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from timelapse_manager.capture.cadence import captures_per_day
from timelapse_manager.capture.schedule import (
    next_transition,
    open_stretches,
    parse_schedule,
)

NY = ZoneInfo("America/New_York")
UTC = dt.UTC
# Buford, GA -- the deployment's coordinates (America/New_York by location).
LAT, LON = 34.123245, -83.94052
MINUTE = 60


def _ny(*args: int) -> dt.datetime:
    return dt.datetime(*args, tzinfo=NY).astimezone(UTC)


def _clock(time: str, *, enabled: bool = True, anchor_id: str = "a") -> dict[str, Any]:
    return {"id": anchor_id, "kind": "clock", "time": time, "enabled": enabled}


def _window(
    start: str, end: str, tz: str = "America/New_York", **extra: Any
) -> dict[str, Any]:
    return {
        "enabled": True,
        "timezone": tz,
        "windows": [{"start_time": start, "end_time": end}],
        **extra,
    }


def _per_day(now: dt.datetime, **kw: Any) -> float | None:
    base: dict[str, Any] = {
        "now": now,
        "interval_seconds": None,
        "anchors_raw": None,
        "schedule_raw": {"enabled": True, "timezone": "America/New_York"},
        "latitude": LAT,
        "longitude": LON,
    }
    base.update(kw)
    return captures_per_day(**base)


# 2026-10-26 is a Monday; 05:00 local is outside every window used below, so
# every open stretch opens inside the horizon and counts exactly.
MONDAY_5AM = _ny(2026, 10, 26, 5, 0)


class TestAnchors:
    def test_one_clock_anchor_is_one_a_day(self) -> None:
        assert _per_day(MONDAY_5AM, anchors_raw=[_clock("12:00")]) == 1.0

    def test_two_anchors_are_two_a_day(self) -> None:
        anchors = [_clock("07:00", anchor_id="a"), _clock("19:00", anchor_id="b")]
        assert _per_day(MONDAY_5AM, anchors_raw=anchors) == 2.0

    def test_disabled_anchor_counts_nothing(self) -> None:
        assert (
            _per_day(MONDAY_5AM, anchors_raw=[_clock("12:00", enabled=False)]) is None
        )

    def test_anchors_ignore_the_window_and_weekday_mask(self) -> None:
        # Gate open only 08:00-09:00 on weekdays; a 12:00 anchor still fires daily.
        schedule = _window("08:00", "09:00", day_of_week_mask=0b0011111)
        assert (
            _per_day(MONDAY_5AM, anchors_raw=[_clock("12:00")], schedule_raw=schedule)
            == 1.0
        )

    def test_solar_noon_with_coordinates_is_one_a_day(self) -> None:
        anchors = [{"id": "s", "kind": "solar_noon", "enabled": True}]
        assert _per_day(MONDAY_5AM, anchors_raw=anchors) == pytest.approx(1.0)

    def test_solar_anchor_without_coordinates_plans_nothing(self) -> None:
        anchors = [{"id": "s", "kind": "solar_noon", "enabled": True}]
        assert (
            _per_day(MONDAY_5AM, anchors_raw=anchors, latitude=None, longitude=None)
            is None
        )

    def test_sunrise_in_polar_night_fires_nothing(self) -> None:
        # Svalbard in December: no sunrise, so a sunrise anchor never fires.
        anchors = [{"id": "r", "kind": "sunrise", "enabled": True}]
        now = dt.datetime(2026, 12, 10, 12, 0, tzinfo=UTC)
        assert (
            _per_day(now, anchors_raw=anchors, latitude=78.22, longitude=15.65) is None
        )

    def test_malformed_anchor_list_counts_nothing(self) -> None:
        assert _per_day(MONDAY_5AM, anchors_raw=[{"kind": "bogus"}]) is None


class TestInterval:
    def test_always_open_is_seconds_per_day_over_interval(self) -> None:
        assert (
            _per_day(MONDAY_5AM, interval_seconds=MINUTE, schedule_raw=None) == 1440.0
        )

    def test_window_counts_only_open_hours(self) -> None:
        # 08:00-18:00 every day with a 1-minute interval: 10 h of ticks.
        rate = _per_day(
            MONDAY_5AM, interval_seconds=MINUTE, schedule_raw=_window("08:00", "18:00")
        )
        assert rate == 600.0

    def test_weekday_mask_reduces_interval(self) -> None:
        rate = _per_day(
            MONDAY_5AM,
            interval_seconds=MINUTE,
            schedule_raw=_window("08:00", "18:00", day_of_week_mask=0b0011111),
        )
        assert rate == pytest.approx(600 * 5 / 7)

    def test_short_window_takes_one_capture_when_it_opens(self) -> None:
        # The noon preset (12:00-12:30) with a 1 h interval: the runner captures
        # once when the window opens -- one a day, not half.
        rate = _per_day(
            MONDAY_5AM, interval_seconds=3600, schedule_raw=_window("12:00", "12:30")
        )
        assert rate == 1.0

    def test_interval_and_anchors_both_count(self) -> None:
        rate = _per_day(
            MONDAY_5AM,
            interval_seconds=3600,
            anchors_raw=[_clock("12:00")],
            schedule_raw=None,
        )
        assert rate == 25.0

    def test_malformed_schedule_is_always_open_as_the_runner(self) -> None:
        rate = _per_day(
            MONDAY_5AM,
            interval_seconds=3600,
            schedule_raw={"enabled": True, "timezone": "Not/AZone"},
        )
        assert rate == 24.0

    def test_already_open_at_now_counts_expected_ticks(self) -> None:
        # Mid-window now: the stretch already open has an unknown phase and
        # counts open / interval; the total stays 10 h a day. (A week with no
        # DST change, so the horizon's far edge is also 13:00 local.)
        now = _ny(2026, 10, 12, 13, 0)
        rate = _per_day(
            now, interval_seconds=MINUTE, schedule_raw=_window("08:00", "18:00")
        )
        assert rate == pytest.approx(600.0)

    def test_sun_window(self) -> None:
        schedule = {
            "enabled": True,
            "timezone": "America/New_York",
            "sun_window": [
                {"anchor": "sunrise", "offset_minutes": 0},
                {"anchor": "sunset", "offset_minutes": 0},
            ],
        }
        rate = _per_day(MONDAY_5AM, interval_seconds=MINUTE, schedule_raw=schedule)
        # Late October in Georgia: roughly 10.8 h of daylight.
        assert rate is not None
        assert 10 * 60 < rate < 11.5 * 60


class TestHorizon:
    def test_finished_plans_nothing(self) -> None:
        assert (
            _per_day(
                MONDAY_5AM, interval_seconds=MINUTE, schedule_raw=None, finished=True
            )
            is None
        )

    def test_past_end_date_plans_nothing(self) -> None:
        assert (
            _per_day(
                MONDAY_5AM,
                interval_seconds=MINUTE,
                anchors_raw=[_clock("12:00")],
                end_date=MONDAY_5AM - dt.timedelta(days=1),
            )
            is None
        )

    def test_end_date_inside_horizon_divides_by_real_length(self) -> None:
        # Two days left: 2 x 1440 ticks + 2 noon anchors over 2 days.
        rate = _per_day(
            MONDAY_5AM,
            interval_seconds=MINUTE,
            anchors_raw=[_clock("12:00")],
            schedule_raw=None,
            end_date=MONDAY_5AM + dt.timedelta(days=2),
        )
        assert rate == pytest.approx(1441.0)

    def test_naive_end_date_is_utc(self) -> None:
        end = (MONDAY_5AM + dt.timedelta(days=2)).replace(tzinfo=None)
        rate = _per_day(
            MONDAY_5AM, interval_seconds=MINUTE, schedule_raw=None, end_date=end
        )
        assert rate == pytest.approx(1440.0)

    def test_future_start_measures_from_the_start(self) -> None:
        # Weekday-only window; the week from a Saturday start still averages 5/7.
        start = _ny(2026, 10, 31, 5, 0)
        rate = _per_day(
            MONDAY_5AM,
            interval_seconds=MINUTE,
            schedule_raw=_window("08:00", "18:00", day_of_week_mask=0b0011111),
            start_date=start,
        )
        assert rate == pytest.approx(600 * 5 / 7)

    def test_no_interval_and_no_anchor_plans_nothing(self) -> None:
        assert _per_day(MONDAY_5AM) is None


class TestDaylightSaving:
    # US spring-forward 2026-03-08 (02:00 -> 03:00), fall-back 2026-11-01
    # (02:00 -> 01:00), America/New_York.

    def test_spring_forward_day_has_an_hour_less_of_window(self) -> None:
        # 00:00-04:00 is 3 real hours on 2026-03-08; the 7-day horizon from
        # 03-05 05:00 covers the windows on 03-06 .. 03-12.
        now = _ny(2026, 3, 5, 5, 0)
        rate = _per_day(
            now, interval_seconds=MINUTE, schedule_raw=_window("00:00", "04:00")
        )
        assert rate == pytest.approx((6 * 240 + 180) / 7)

    def test_fall_back_day_has_an_hour_more_of_window(self) -> None:
        # 00:00-04:00 is 5 real hours on 2026-11-01.
        now = _ny(2026, 10, 29, 5, 0)
        rate = _per_day(
            now, interval_seconds=MINUTE, schedule_raw=_window("00:00", "04:00")
        )
        assert rate == pytest.approx((6 * 240 + 300) / 7)

    def test_anchor_in_the_spring_forward_gap_still_fires_once(self) -> None:
        # 02:30 does not exist on 2026-03-08; the runner shifts it to 03:00.
        now = _ny(2026, 3, 5, 5, 0)
        assert _per_day(now, anchors_raw=[_clock("02:30")]) == 1.0

    @pytest.mark.parametrize("hour", [0, 6, 11, 12, 13, 23])
    def test_daily_anchor_stays_one_a_day_across_fall_back(self, hour: int) -> None:
        # The horizon is 7 local days, not 168 h: measured from just after noon
        # in the fall-back week, 168 h would end before the seventh noon.
        now = _ny(2026, 10, 28, hour, 30)
        assert _per_day(now, anchors_raw=[_clock("12:00")]) == 1.0

    def test_anchor_in_the_repeated_fall_back_hour_fires_once(self) -> None:
        now = _ny(2026, 10, 29, 5, 0)
        assert _per_day(now, anchors_raw=[_clock("01:30")]) == 1.0


class TestMidnightCrossingWindow:
    def test_window_across_midnight_counts_both_sides(self) -> None:
        # 22:00-02:00 UTC: 4 h a night.
        now = dt.datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
        rate = _per_day(
            now,
            interval_seconds=MINUTE,
            schedule_raw=_window("22:00", "02:00", tz="UTC"),
        )
        assert rate == pytest.approx(240.0)

    def test_weekday_mask_cuts_at_local_midnight(self) -> None:
        # Saturday off: Fri 22:00-24:00 still runs (Friday), Sat 00:00-02:00 and
        # Sat 22:00-24:00 do not, Sun 00:00-02:00 does.
        now = dt.datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
        no_saturday = 0b1011111
        rate = _per_day(
            now,
            interval_seconds=MINUTE,
            schedule_raw=_window(
                "22:00", "02:00", tz="UTC", day_of_week_mask=no_saturday
            ),
        )
        assert rate == pytest.approx((7 * 240 - 120 - 120) / 7)


class TestOpenStretches:
    def _walk(
        self, raw: dict[str, Any], start: dt.datetime, end: dt.datetime
    ) -> list[tuple[dt.datetime, dt.datetime]]:
        """The reference: walk next_transition from change to change."""
        schedule = parse_schedule(raw)
        stretches: list[tuple[dt.datetime, dt.datetime]] = []
        cursor = start
        while cursor < end:
            is_open, change = next_transition(
                schedule, cursor, latitude=LAT, longitude=LON
            )
            stop = end if change is None else min(change, end)
            if is_open:
                if stretches and stretches[-1][1] == cursor:
                    stretches[-1] = (stretches[-1][0], stop)
                else:
                    stretches.append((cursor, stop))
            if change is None:
                break
            cursor = change
        return stretches

    @pytest.mark.parametrize(
        "raw",
        [
            _window("08:00", "18:00", day_of_week_mask=0b0011111),
            _window("22:00", "02:00", day_of_week_mask=0b1011111),
            {
                "enabled": True,
                "timezone": "America/New_York",
                "sun_window": [
                    {"anchor": "sunrise", "offset_minutes": -30},
                    {"anchor": "sunset", "offset_minutes": 45},
                ],
            },
        ],
    )
    def test_matches_walking_next_transition(self, raw: dict[str, Any]) -> None:
        start = _ny(2026, 10, 28, 13, 7)
        end = start + dt.timedelta(days=7)
        assert open_stretches(
            parse_schedule(raw), start, end, latitude=LAT, longitude=LON
        ) == self._walk(raw, start, end)

    def test_always_open_is_one_stretch(self) -> None:
        start = _ny(2026, 10, 28, 13, 7)
        end = start + dt.timedelta(days=7)
        assert open_stretches(parse_schedule(None), start, end) == [(start, end)]

    def test_empty_range(self) -> None:
        start = _ny(2026, 10, 28, 13, 7)
        assert (
            open_stretches(parse_schedule(_window("08:00", "18:00")), start, start)
            == []
        )
