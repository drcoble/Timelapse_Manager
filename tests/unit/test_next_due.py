"""Last/Next capture and anchor-aware overdue for the project views (#5)."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from timelapse_manager.capture.next_due import (
    missed_anchor_instant,
    next_capture_instant,
)

NY = ZoneInfo("America/New_York")
NOON = [{"id": "a", "kind": "clock", "time": "12:00", "enabled": True}]
SCHED = {"enabled": True, "timezone": "America/New_York"}
# Buford, GA -- the deployment's coordinates (America/New_York by location).
LAT, LON = 34.123245, -83.94052


def _ny(*args: int) -> dt.datetime:
    return dt.datetime(*args, tzinfo=NY).astimezone(dt.UTC)


def _next(now: dt.datetime, **kw: object) -> dt.datetime | None:
    base: dict[str, object] = {
        "now": now,
        "interval_seconds": None,
        "last_captured_at": None,
        "anchors_raw": NOON,
        "schedule_raw": SCHED,
        "latitude": LAT,
        "longitude": LON,
    }
    base.update(kw)
    return next_capture_instant(**base)  # type: ignore[arg-type]


class TestNextCapture:
    def test_anchor_only_after_todays_capture_is_tomorrow_noon(self) -> None:
        now = _ny(2026, 10, 14, 12, 0, 30)
        assert _next(now, last_captured_at=_ny(2026, 10, 14, 12, 0, 1)) == _ny(
            2026, 10, 15, 12, 0
        )

    def test_anchor_only_before_noon_is_today(self) -> None:
        assert _next(_ny(2026, 10, 15, 9, 0)) == _ny(2026, 10, 15, 12, 0)

    def test_dst_fall_back_keeps_wall_clock_noon(self) -> None:
        # 2026-11-01 is the US fall-back day: noon local is 17:00 UTC, not 16:00.
        nxt = _next(_ny(2026, 10, 31, 13, 0))
        assert nxt == dt.datetime(2026, 11, 1, 17, 0, tzinfo=dt.UTC)

    def test_solar_noon_uses_the_cameras_timezone(self) -> None:
        anchors = [{"id": "s", "kind": "solar_noon", "enabled": True}]
        # Schedule says UTC, the camera is in Georgia: solar noon ~13:3x EDT.
        nxt = _next(
            _ny(2026, 10, 15, 6, 0),
            anchors_raw=anchors,
            schedule_raw={"enabled": True, "timezone": "UTC"},
        )
        assert nxt is not None
        local = nxt.astimezone(NY)
        assert local.date() == dt.date(2026, 10, 15)
        assert 13 <= local.hour <= 14

    def test_interval_project_is_last_plus_interval(self) -> None:
        now = _ny(2026, 10, 15, 9, 0)
        last = now - dt.timedelta(seconds=100)
        assert _next(
            now, anchors_raw=None, interval_seconds=300, last_captured_at=last
        ) == (last + dt.timedelta(seconds=300))

    def test_overdue_interval_tick_is_now(self) -> None:
        now = _ny(2026, 10, 15, 9, 0)
        last = now - dt.timedelta(hours=2)
        assert (
            _next(now, anchors_raw=None, interval_seconds=300, last_captured_at=last)
            == now
        )

    def test_interval_and_anchor_take_the_sooner(self) -> None:
        now = _ny(2026, 10, 15, 11, 58)
        assert _next(now, interval_seconds=3600, last_captured_at=now) == _ny(
            2026, 10, 15, 12, 0
        )

    def test_future_start_date_defers(self) -> None:
        start = _ny(2026, 10, 20, 0, 0)
        assert _next(_ny(2026, 10, 15, 9, 0), start_date=start) == _ny(
            2026, 10, 20, 12, 0
        )

    def test_past_end_date_or_finished_means_none(self) -> None:
        now = _ny(2026, 10, 15, 9, 0)
        assert _next(now, end_date=now - dt.timedelta(days=1)) is None
        assert _next(now, finished=True) is None
        # End before the next anchor: nothing more will be captured.
        assert _next(now, end_date=_ny(2026, 10, 15, 11, 0)) is None

    def test_naive_values_are_utc(self) -> None:
        now = _ny(2026, 10, 15, 9, 0).replace(tzinfo=None)
        assert _next(now) == _ny(2026, 10, 15, 12, 0)

    def test_no_anchor_and_no_interval_is_none(self) -> None:
        assert _next(_ny(2026, 10, 15, 9, 0), anchors_raw=None) is None


class TestMissedAnchor:
    def _missed(self, now: dt.datetime, last: dt.datetime | None, since: dt.datetime):
        return missed_anchor_instant(
            now=now,
            anchors_raw=NOON,
            schedule_raw=SCHED,
            latitude=LAT,
            longitude=LON,
            last_captured_at=last,
            active_since=since,
        )

    def test_missed_noon_is_flagged_after_grace(self) -> None:
        since = _ny(2026, 10, 13, 0, 0)
        yesterday = _ny(2026, 10, 13, 12, 0, 1)
        assert self._missed(_ny(2026, 10, 14, 13, 34), yesterday, since) == _ny(
            2026, 10, 14, 12, 0
        )

    def test_not_flagged_inside_grace_or_after_a_capture(self) -> None:
        since = _ny(2026, 10, 13, 0, 0)
        yesterday = _ny(2026, 10, 13, 12, 0, 1)
        assert self._missed(_ny(2026, 10, 14, 12, 10), yesterday, since) is None
        assert (
            self._missed(_ny(2026, 10, 14, 13, 0), _ny(2026, 10, 14, 12, 0, 2), since)
            is None
        )

    def test_a_project_created_after_the_instant_is_not_blamed(self) -> None:
        assert (
            self._missed(_ny(2026, 10, 14, 13, 0), None, _ny(2026, 10, 14, 12, 15))
            is None
        )
