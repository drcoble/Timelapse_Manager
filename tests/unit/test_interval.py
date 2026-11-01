"""Unit tests for capture-interval value/unit conversion and decomposition.

These exercise the shared table directly, with no app or database, so the
factors and round-trip behaviour are pinned independently of the form handlers.
"""

from __future__ import annotations

import pytest

from timelapse_manager.web.interval import (
    UNIT_FACTORS,
    decompose_seconds,
    format_interval_compact,
    parse_interval_to_seconds,
)


class TestParseIntervalToSeconds:
    @pytest.mark.parametrize(
        ("unit", "expected"),
        [
            ("seconds", 1),
            ("minutes", 60),
            ("hours", 3600),
            ("days", 86400),
            ("weeks", 604800),
            ("months", 2592000),
        ],
    )
    def test_each_unit_converts_with_value_one(self, unit: str, expected: int) -> None:
        seconds, err = parse_interval_to_seconds("1", unit)
        assert err is None
        assert seconds == expected

    def test_months_factor_is_thirty_day_approximation(self) -> None:
        assert UNIT_FACTORS["months"] == 2592000
        seconds, err = parse_interval_to_seconds("1", "months")
        assert err is None
        assert seconds == 2592000

    def test_value_multiplies_the_unit_factor(self) -> None:
        seconds, err = parse_interval_to_seconds("5", "minutes")
        assert err is None
        assert seconds == 300

    @pytest.mark.parametrize("value", ["0", "-1", "1.5", "abc", ""])
    def test_invalid_value_returns_message(self, value: str) -> None:
        seconds, err = parse_interval_to_seconds(value, "minutes")
        assert seconds is None
        assert err is not None

    def test_unknown_unit_returns_message(self) -> None:
        seconds, err = parse_interval_to_seconds("5", "fortnights")
        assert seconds is None
        assert err is not None

    def test_missing_unit_returns_message(self) -> None:
        seconds, err = parse_interval_to_seconds("5", "")
        assert seconds is None
        assert err is not None


class TestDecomposeSeconds:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (2592000, (1, "months")),
            (604800, (1, "weeks")),
            (7200, (2, "hours")),
            (90, (90, "seconds")),
        ],
    )
    def test_clean_values_pick_largest_unit(
        self, seconds: int, expected: tuple[int, str]
    ) -> None:
        assert decompose_seconds(seconds) == expected

    def test_twenty_eight_days_decomposes_to_four_weeks(self) -> None:
        # 28 days is a clean multiple of weeks but not of 30-day months, so it
        # round-trips to weeks rather than months. This is the documented
        # consequence of the 30-day month approximation.
        twenty_eight_days = 28 * 86400
        assert decompose_seconds(twenty_eight_days) == (4, "weeks")

    @pytest.mark.parametrize(
        ("value", "unit"),
        [
            (1, "months"),
            (1, "weeks"),
            (2, "hours"),
            (90, "seconds"),
            (5, "minutes"),
        ],
    )
    def test_round_trip_value_unit(self, value: int, unit: str) -> None:
        seconds, err = parse_interval_to_seconds(str(value), unit)
        assert err is None
        assert seconds is not None
        assert decompose_seconds(seconds) == (value, unit)

    def test_none_falls_back_to_default(self) -> None:
        # Nullable column: an unset interval must not divide by None.
        assert decompose_seconds(None) == (1, "minutes")


class TestFormatIntervalCompact:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (30, "30s"),
            (90, "90s"),
            (60, "1m"),
            (900, "15m"),
            (7200, "2h"),
            (5400, "90m"),
            (86400, "1d"),
            (604800, "7d"),
            (1, "1s"),
            (3600, "1h"),
            (129600, "36h"),
        ],
    )
    def test_uses_the_largest_whole_unit(self, seconds: int, expected: str) -> None:
        assert format_interval_compact(seconds) == expected

    @pytest.mark.parametrize("seconds", [None, 0, -60])
    def test_no_interval_is_a_dash(self, seconds: int | None) -> None:
        assert format_interval_compact(seconds) == "\u2014"
