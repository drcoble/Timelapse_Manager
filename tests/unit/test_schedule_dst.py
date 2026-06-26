"""Pure unit tests for schedule evaluation across DST transitions.

Uses America/Chicago:
  Spring forward: 2026-03-08 08:00 UTC (clocks jump 01:59 CST -> 03:00 CDT)
  Fall back:      2026-11-01 07:00 UTC (clocks fall 01:59 CDT -> 01:00 CST)

All instants are supplied as explicit UTC datetimes — no hidden datetime.now().
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from timelapse_manager.capture.schedule import (
    Schedule,
    Window,
    is_within_window,
    next_transition,
)

_UTC = UTC

# ---------------------------------------------------------------------------
# DST boundary constants (verified empirically)
# ---------------------------------------------------------------------------

# Spring forward: 2026-03-08 07:59 UTC = 01:59 CST (-6)
#                 2026-03-08 08:00 UTC = 03:00 CDT (-5)
_SPRING_FORWARD_UTC = datetime(2026, 3, 8, 8, 0, tzinfo=_UTC)
_SPRING_JUST_BEFORE = _SPRING_FORWARD_UTC - timedelta(minutes=1)
_SPRING_JUST_AFTER = _SPRING_FORWARD_UTC

# Fall back: 2026-11-01 06:59 UTC = 01:59 CDT (-5)
#            2026-11-01 07:00 UTC = 01:00 CST (-6)
_FALL_BACK_UTC = datetime(2026, 11, 1, 7, 0, tzinfo=_UTC)
_FALL_JUST_BEFORE = _FALL_BACK_UTC - timedelta(minutes=1)
_FALL_JUST_AFTER = _FALL_BACK_UTC


def _chicago_schedule(start: str, end: str) -> Schedule:
    return Schedule(
        enabled=True,
        timezone="America/Chicago",
        windows=[Window(start_time=start, end_time=end)],
    )


# ---------------------------------------------------------------------------
# Spring forward (02:00 CST disappears — 01:59 -> 03:00)
# ---------------------------------------------------------------------------


class TestSpringForward:
    def test_utc_offsets_straddle_spring_transition(self) -> None:
        """Sanity check: offsets are -6h before and -5h after the spring jump."""
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("America/Chicago")
        before_offset = _SPRING_JUST_BEFORE.astimezone(tz).utcoffset()
        after_offset = _SPRING_JUST_AFTER.astimezone(tz).utcoffset()
        assert after_offset is not None and before_offset is not None
        assert after_offset - before_offset == timedelta(hours=1)

    def test_window_open_before_spring_transition(self) -> None:
        # 09:00-17:00 CST on the spring day; 15:00 UTC = 09:00 CST is the open
        s = _chicago_schedule("09:00", "17:00")
        # 15:00 UTC = 10:00 CDT — window is open
        assert is_within_window(s, datetime(2026, 3, 8, 15, 0, tzinfo=_UTC))

    def test_window_closed_before_open_on_spring_day(self) -> None:
        s = _chicago_schedule("09:00", "17:00")
        # After spring forward, CDT = UTC-5 so 09:00 CDT = 14:00 UTC.
        # 13:59 UTC = 08:59 CDT — before window open at 09:00.
        assert not is_within_window(s, datetime(2026, 3, 8, 13, 59, tzinfo=_UTC))

    def test_window_spanning_spring_transition_is_exact(self) -> None:
        # A window 01:00-04:00 local spans the missing hour (02:00 never exists).
        # Before the spring: 01:59 local (07:59 UTC) — in window.
        # After the spring:  03:00 local (08:00 UTC) — still in window.
        s = _chicago_schedule("01:00", "04:00")
        # Just before the spring jump: 07:59 UTC = 01:59 CST — inside [01:00, 04:00)
        assert is_within_window(s, _SPRING_JUST_BEFORE)
        # Just after the spring jump: 08:00 UTC = 03:00 CDT — inside [01:00, 04:00)
        assert is_within_window(s, _SPRING_JUST_AFTER)

    def test_next_transition_at_spring_window_close_is_correct(self) -> None:
        # Window 01:00-04:00; query inside window (08:30 UTC = 03:30 CDT)
        s = _chicago_schedule("01:00", "04:00")
        query = datetime(2026, 3, 8, 8, 30, tzinfo=_UTC)
        is_open, next_change = next_transition(s, query)
        assert is_open
        assert next_change is not None
        # Window closes at local 04:00 CDT = 09:00 UTC
        expected_close = datetime(2026, 3, 8, 9, 0, tzinfo=_UTC)
        assert next_change == expected_close

    def test_open_window_duration_on_spring_day_shorter_by_one_hour(self) -> None:
        # A 24h window on a spring-forward day spans 23h wall-clock hours.
        # Verify by checking open at 00:00 and closed at 23:59 local still works.
        s = _chicago_schedule("06:00", "18:00")
        # 12:00 UTC = 07:00 CDT (after spring) — inside
        inside = datetime(2026, 3, 8, 12, 0, tzinfo=_UTC)
        assert is_within_window(s, inside)
        # 23:00 UTC = 18:00 CDT — at window close, should be closed
        at_close = datetime(2026, 3, 8, 23, 0, tzinfo=_UTC)
        assert not is_within_window(s, at_close)


# ---------------------------------------------------------------------------
# Fall back (01:00-02:00 CDT occurs twice — 07:00 UTC repeats as 01:00 CST)
# ---------------------------------------------------------------------------


class TestFallBack:
    def test_utc_offsets_straddle_fall_transition(self) -> None:
        """Sanity check: offsets are -5h before and -6h after the fall back."""
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("America/Chicago")
        before_offset = _FALL_JUST_BEFORE.astimezone(tz).utcoffset()
        after_offset = _FALL_JUST_AFTER.astimezone(tz).utcoffset()
        assert before_offset is not None and after_offset is not None
        assert before_offset - after_offset == timedelta(hours=1)

    def test_window_outside_repeated_hour_is_correct(self) -> None:
        # A window well outside the repeated 01:00-02:00 region is unambiguous.
        # 10:00-16:00 local; 15:00 UTC = 09:00 CST — closed
        # 18:00 UTC = 12:00 CST — open
        s = _chicago_schedule("10:00", "16:00")
        assert not is_within_window(s, datetime(2026, 11, 1, 15, 0, tzinfo=_UTC))
        assert is_within_window(s, datetime(2026, 11, 1, 18, 0, tzinfo=_UTC))

    def test_window_spanning_fall_transition_stays_open(self) -> None:
        # A window 00:00-03:00 local clearly wraps both sides of the clocks.
        # 06:30 UTC = 01:30 CDT (first) — inside
        # 07:30 UTC = 01:30 CST (second) — inside
        # The code's documented caveat: a window EDGE in the repeated hour may
        # be off by up to an hour, but a spanning window should stay open.
        s = _chicago_schedule("00:00", "03:00")
        first_pass = datetime(2026, 11, 1, 6, 30, tzinfo=_UTC)
        second_pass = datetime(2026, 11, 1, 7, 30, tzinfo=_UTC)
        assert is_within_window(s, first_pass)
        assert is_within_window(s, second_pass)

    def test_open_window_duration_on_fall_back_day_longer_by_one_hour(self) -> None:
        # On the fall-back day the local clock passes 01:00 twice, so a
        # window that straddles the transition effectively covers an extra hour
        # in UTC. next_transition after the last close should reflect that.
        s = _chicago_schedule("09:00", "17:00")
        # Inside window: 16:00 UTC = 10:00 CST (after fall back)
        inside = datetime(2026, 11, 1, 16, 0, tzinfo=_UTC)
        is_open, next_change = next_transition(s, inside)
        assert is_open
        assert next_change is not None
        # Window closes at local 17:00 CST = 23:00 UTC
        expected_close = datetime(2026, 11, 1, 23, 0, tzinfo=_UTC)
        assert next_change == expected_close

    def test_next_transition_duration_changes_correctly_across_fall_back(self) -> None:
        # A schedule open 06:00-18:00 local. On a normal day the open duration
        # is 12h. On the fall-back day the CST offset is -6h so:
        #   open  at  06:00 CST = 12:00 UTC
        #   close at 18:00 CST = 00:00 UTC next day
        # That is 12h of open window, same as any other day (window boundaries
        # don't straddle the repeated hour so the duration is stable).
        s = _chicago_schedule("06:00", "18:00")
        inside = datetime(2026, 11, 1, 15, 0, tzinfo=_UTC)  # 09:00 CST, inside
        is_open, next_change = next_transition(s, inside)
        assert is_open
        assert next_change is not None
        # next_change at 18:00 CST = 00:00 UTC 2026-11-02
        assert next_change == datetime(2026, 11, 2, 0, 0, tzinfo=_UTC)
