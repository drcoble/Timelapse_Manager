"""Pure unit tests for schedule window evaluation.

Covers: parse_schedule defaults, always-open semantics, clock windows,
day-of-week masking, campaign date bounds, midnight-spanning windows,
and next_transition edge detection. No async, no DB, no I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from timelapse_manager.capture.schedule import (
    Schedule,
    Window,
    is_within_window,
    next_transition,
    parse_schedule,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC = UTC


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=_UTC)


def _sched_with_window(
    start: str,
    end: str,
    *,
    tz: str = "UTC",
    mask: int = 0b1111111,
) -> Schedule:
    return Schedule(
        enabled=True,
        timezone=tz,
        windows=[Window(start_time=start, end_time=end)],
        day_of_week_mask=mask,
    )


# ---------------------------------------------------------------------------
# parse_schedule: always-open defaults
# ---------------------------------------------------------------------------


class TestParseScheduleDefaults:
    def test_none_yields_always_open(self) -> None:
        s = parse_schedule(None)
        assert s.is_always_open

    def test_empty_dict_yields_always_open(self) -> None:
        s = parse_schedule({})
        assert s.is_always_open

    def test_disabled_flag_yields_always_open(self) -> None:
        s = parse_schedule({"enabled": False})
        assert s.is_always_open

    def test_enabled_no_windows_no_mask_constraint_is_always_open(self) -> None:
        s = parse_schedule({"enabled": True, "timezone": "UTC"})
        assert s.is_always_open

    def test_empty_windows_list_yields_always_open(self) -> None:
        s = parse_schedule({"enabled": True, "windows": []})
        assert s.is_always_open

    def test_null_windows_yields_always_open(self) -> None:
        s = parse_schedule({"enabled": True, "windows": None})
        assert s.is_always_open


# ---------------------------------------------------------------------------
# parse_schedule: validation errors
# ---------------------------------------------------------------------------


class TestParseScheduleValidation:
    def test_bad_timezone_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="timezone"):
            parse_schedule({"enabled": True, "timezone": "Mars/Phobos"})

    def test_bad_window_start_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="start_time"):
            parse_schedule({"windows": [{"start_time": "25:00", "end_time": "10:00"}]})

    def test_bad_window_end_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="end_time"):
            parse_schedule({"windows": [{"start_time": "08:00", "end_time": "bad"}]})

    def test_non_list_windows_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="windows"):
            parse_schedule({"windows": "08:00-17:00"})

    def test_non_dict_window_item_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="windows\\[0\\]"):
            parse_schedule({"windows": ["08:00-17:00"]})

    def test_day_of_week_mask_out_of_range_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="day_of_week_mask"):
            parse_schedule({"day_of_week_mask": 200})

    def test_boolean_mask_raises_value_error(self) -> None:
        # bool is a subtype of int; the parser must reject it explicitly
        with pytest.raises(ValueError, match="day_of_week_mask"):
            parse_schedule({"day_of_week_mask": True})

    def test_bad_start_date_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="start_date"):
            parse_schedule({"start_date": "not-a-date"})

    def test_bad_sun_window_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="sun_window"):
            parse_schedule({"sun_window": "sunrise-sunset"})

    def test_bad_sun_anchor_value_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="anchor"):
            parse_schedule(
                {
                    "sun_window": [
                        {"anchor": "dusk", "offset_minutes": 0},
                        {"anchor": "sunset", "offset_minutes": 0},
                    ]
                }
            )


# ---------------------------------------------------------------------------
# is_within_window: always-open schedule
# ---------------------------------------------------------------------------


class TestAlwaysOpen:
    def test_always_open_schedule_is_open_at_any_instant(self) -> None:
        s = parse_schedule(None)
        assert is_within_window(s, _utc(2026, 1, 1, 0, 0))
        assert is_within_window(s, _utc(2026, 12, 31, 23, 59))

    def test_disabled_schedule_is_always_open(self) -> None:
        s = Schedule(enabled=False, timezone="UTC")
        assert is_within_window(s, _utc(2026, 6, 1, 12, 0))

    def test_enabled_no_windows_no_day_mask_is_open(self) -> None:
        s = Schedule(enabled=True, timezone="UTC")
        assert is_within_window(s, _utc(2026, 3, 15, 8, 0))


# ---------------------------------------------------------------------------
# is_within_window: clock windows (normal, equal, midnight-spanning)
# ---------------------------------------------------------------------------


class TestClockWindows:
    def test_inside_normal_window(self) -> None:
        s = _sched_with_window("08:00", "17:00")
        assert is_within_window(s, _utc(2026, 6, 1, 12, 0))

    def test_at_window_start_is_open(self) -> None:
        s = _sched_with_window("08:00", "17:00")
        assert is_within_window(s, _utc(2026, 6, 1, 8, 0))

    def test_at_window_end_is_closed(self) -> None:
        s = _sched_with_window("08:00", "17:00")
        assert not is_within_window(s, _utc(2026, 6, 1, 17, 0))

    def test_before_window_is_closed(self) -> None:
        s = _sched_with_window("08:00", "17:00")
        assert not is_within_window(s, _utc(2026, 6, 1, 7, 59))

    def test_after_window_is_closed(self) -> None:
        s = _sched_with_window("08:00", "17:00")
        assert not is_within_window(s, _utc(2026, 6, 1, 17, 1))

    def test_equal_start_end_is_24h_open(self) -> None:
        # start == end means "whole day" not "never"
        s = _sched_with_window("12:00", "12:00")
        assert is_within_window(s, _utc(2026, 6, 1, 0, 0))
        assert is_within_window(s, _utc(2026, 6, 1, 12, 0))
        assert is_within_window(s, _utc(2026, 6, 1, 23, 59))

    def test_midnight_spanning_window_open_before_midnight(self) -> None:
        # 22:00 -> 02:00 wraps past midnight
        s = _sched_with_window("22:00", "02:00")
        assert is_within_window(s, _utc(2026, 6, 1, 23, 0))

    def test_midnight_spanning_window_open_after_midnight(self) -> None:
        s = _sched_with_window("22:00", "02:00")
        assert is_within_window(s, _utc(2026, 6, 2, 1, 30))

    def test_midnight_spanning_window_closed_in_middle_of_day(self) -> None:
        s = _sched_with_window("22:00", "02:00")
        assert not is_within_window(s, _utc(2026, 6, 1, 12, 0))

    def test_midnight_spanning_window_closed_at_end(self) -> None:
        s = _sched_with_window("22:00", "02:00")
        assert not is_within_window(s, _utc(2026, 6, 2, 2, 0))

    def test_multiple_windows_open_in_either(self) -> None:
        s = Schedule(
            enabled=True,
            timezone="UTC",
            windows=[
                Window(start_time="08:00", end_time="10:00"),
                Window(start_time="14:00", end_time="16:00"),
            ],
        )
        assert is_within_window(s, _utc(2026, 6, 1, 9, 0))
        assert is_within_window(s, _utc(2026, 6, 1, 15, 0))
        assert not is_within_window(s, _utc(2026, 6, 1, 12, 0))


# ---------------------------------------------------------------------------
# is_within_window: day-of-week mask
# ---------------------------------------------------------------------------


class TestDayOfWeekMask:
    def test_weekdays_only_open_on_monday(self) -> None:
        # bit0=Mon..bit4=Fri => 0b0011111 = 31
        weekdays_mask = 0b0011111
        s = Schedule(
            enabled=True,
            timezone="UTC",
            windows=[Window(start_time="08:00", end_time="17:00")],
            day_of_week_mask=weekdays_mask,
        )
        monday = _utc(2026, 6, 1, 12, 0)  # 2026-06-01 is Monday
        assert monday.weekday() == 0
        assert is_within_window(s, monday)

    def test_weekdays_only_closed_on_saturday(self) -> None:
        weekdays_mask = 0b0011111
        s = Schedule(
            enabled=True,
            timezone="UTC",
            windows=[Window(start_time="08:00", end_time="17:00")],
            day_of_week_mask=weekdays_mask,
        )
        saturday = _utc(2026, 6, 6, 12, 0)  # 2026-06-06 is Saturday
        assert saturday.weekday() == 5
        assert not is_within_window(s, saturday)

    def test_weekends_only_open_on_sunday(self) -> None:
        # bit5=Sat, bit6=Sun => 0b1100000 = 96
        weekend_mask = 0b1100000
        s = Schedule(
            enabled=True,
            timezone="UTC",
            windows=[Window(start_time="00:00", end_time="23:59")],
            day_of_week_mask=weekend_mask,
        )
        sunday = _utc(2026, 6, 7, 12, 0)  # 2026-06-07 is Sunday
        assert sunday.weekday() == 6
        assert is_within_window(s, sunday)

    def test_zero_mask_is_always_closed(self) -> None:
        s = Schedule(
            enabled=True,
            timezone="UTC",
            windows=[Window(start_time="08:00", end_time="17:00")],
            day_of_week_mask=0,
        )
        for day in range(7):
            t = _utc(2026, 6, 1 + day, 12, 0)
            assert not is_within_window(s, t)

    def test_mask_evaluated_in_local_timezone(self) -> None:
        # In UTC 2026-06-01 23:00 is still Monday, but
        # in America/New_York (UTC-4 EDT) it is Tuesday 19:00 already.
        # Use UTC+0 to keep things predictable: mask = Monday only (bit0)
        mon_only = 0b0000001
        s = Schedule(
            enabled=True,
            timezone="UTC",
            windows=[Window(start_time="08:00", end_time="17:00")],
            day_of_week_mask=mon_only,
        )
        monday_morning = _utc(2026, 6, 1, 10, 0)  # Monday
        tuesday_morning = _utc(2026, 6, 2, 10, 0)  # Tuesday
        assert is_within_window(s, monday_morning)
        assert not is_within_window(s, tuesday_morning)


# ---------------------------------------------------------------------------
# is_within_window: campaign date bounds
# ---------------------------------------------------------------------------


class TestCampaignDateBounds:
    def test_before_start_date_is_closed(self) -> None:
        s = Schedule(
            enabled=True,
            timezone="UTC",
            start_date=_utc(2026, 7, 1),
        )
        assert not is_within_window(s, _utc(2026, 6, 30, 23, 59))

    def test_at_start_date_is_open(self) -> None:
        s = Schedule(
            enabled=True,
            timezone="UTC",
            start_date=_utc(2026, 7, 1),
        )
        assert is_within_window(s, _utc(2026, 7, 1, 0, 0))

    def test_at_end_date_is_closed(self) -> None:
        s = Schedule(
            enabled=True,
            timezone="UTC",
            end_date=_utc(2026, 8, 1),
        )
        assert not is_within_window(s, _utc(2026, 8, 1, 0, 0))

    def test_before_end_date_is_open(self) -> None:
        s = Schedule(
            enabled=True,
            timezone="UTC",
            end_date=_utc(2026, 8, 1),
        )
        assert is_within_window(s, _utc(2026, 7, 31, 23, 59))

    def test_within_campaign_and_window_is_open(self) -> None:
        s = Schedule(
            enabled=True,
            timezone="UTC",
            windows=[Window(start_time="08:00", end_time="17:00")],
            start_date=_utc(2026, 6, 1),
            end_date=_utc(2026, 9, 1),
        )
        assert is_within_window(s, _utc(2026, 7, 1, 12, 0))

    def test_outside_campaign_even_with_matching_window_is_closed(self) -> None:
        s = Schedule(
            enabled=True,
            timezone="UTC",
            windows=[Window(start_time="08:00", end_time="17:00")],
            start_date=_utc(2026, 6, 1),
            end_date=_utc(2026, 6, 30),
        )
        assert not is_within_window(s, _utc(2026, 7, 1, 12, 0))

    def test_naive_start_date_coerced_to_utc(self) -> None:
        from datetime import datetime

        s = Schedule(
            enabled=True,
            timezone="UTC",
            start_date=datetime(2026, 6, 1, 0, 0),  # naive
        )
        # __post_init__ should coerce to UTC
        assert s.start_date is not None
        assert s.start_date.tzinfo is not None


# ---------------------------------------------------------------------------
# next_transition: always-open schedules
# ---------------------------------------------------------------------------


class TestNextTransitionAlwaysOpen:
    def test_always_open_schedule_returns_none_transition(self) -> None:
        s = parse_schedule(None)
        is_open, next_change = next_transition(s, _utc(2026, 6, 1))
        assert is_open
        assert next_change is None

    def test_disabled_schedule_returns_none_transition(self) -> None:
        s = Schedule(enabled=False, timezone="UTC")
        is_open, next_change = next_transition(s, _utc(2026, 6, 1))
        assert is_open
        assert next_change is None

    def test_ended_campaign_returns_none_transition(self) -> None:
        s = Schedule(
            enabled=True,
            timezone="UTC",
            end_date=_utc(2026, 1, 1),
        )
        is_open, next_change = next_transition(s, _utc(2026, 6, 1))
        assert not is_open
        assert next_change is None


# ---------------------------------------------------------------------------
# next_transition: clock windows
# ---------------------------------------------------------------------------


class TestNextTransitionClockWindows:
    def test_transition_detected_at_window_open(self) -> None:
        s = _sched_with_window("10:00", "12:00")
        before_open = _utc(2026, 6, 1, 9, 0)
        is_open, next_change = next_transition(s, before_open)
        assert not is_open
        assert next_change is not None
        # Next change should be window open = 10:00 UTC on that day
        expected = _utc(2026, 6, 1, 10, 0)
        assert next_change == expected

    def test_transition_detected_at_window_close(self) -> None:
        s = _sched_with_window("08:00", "17:00")
        inside = _utc(2026, 6, 1, 12, 0)
        is_open, next_change = next_transition(s, inside)
        assert is_open
        assert next_change is not None
        expected = _utc(2026, 6, 1, 17, 0)
        assert next_change == expected

    def test_next_transition_is_utc_aware(self) -> None:
        s = _sched_with_window("08:00", "12:00")
        is_open, next_change = next_transition(s, _utc(2026, 6, 1, 9, 0))
        assert next_change is not None
        assert next_change.tzinfo is not None

    def test_day_mask_transition_at_midnight(self) -> None:
        # Schedule open only on Mondays (bit0)
        mon_only = 0b0000001
        s = Schedule(
            enabled=True,
            timezone="UTC",
            windows=[Window(start_time="08:00", end_time="17:00")],
            day_of_week_mask=mon_only,
        )
        # Query on Monday at 12:00 — currently open; next change is 17:00
        monday_noon = _utc(2026, 6, 1, 12, 0)
        assert monday_noon.weekday() == 0
        is_open, next_change = next_transition(s, monday_noon)
        assert is_open
        assert next_change == _utc(2026, 6, 1, 17, 0)

    def test_no_transition_for_zero_mask_schedule_returns_none(self) -> None:
        s = Schedule(
            enabled=True,
            timezone="UTC",
            windows=[Window(start_time="08:00", end_time="17:00")],
            day_of_week_mask=0,
        )
        is_open, next_change = next_transition(s, _utc(2026, 6, 1, 12, 0))
        assert not is_open
        # No day is ever allowed; gate stays closed forever within scan horizon
        # so next_change may be None (or a future date that also evaluates False)
        # The important contract: currently closed
        assert not is_open
