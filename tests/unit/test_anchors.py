"""Sanity tests for the pure exact-time anchor evaluator."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from timelapse_manager.capture import anchors as anchors_mod
from timelapse_manager.capture.anchors import (
    EXACT_TIME_GRACE,
    Anchor,
    anchor_fire_instant,
    due_anchors,
    next_anchor_wake,
    next_solar_capture_instant,
    parse_anchors,
)

_CHICAGO = ZoneInfo("America/Chicago")
_UTC = ZoneInfo("UTC")


def test_parse_generates_stable_id_when_absent() -> None:
    anchors = parse_anchors([{"kind": "clock", "time": "6:5"}])
    assert len(anchors) == 1
    assert anchors[0].id  # generated uuid hex
    assert anchors[0].time == "06:05"  # normalised
    assert anchors[0].enabled is True
    assert anchors[0].offset_minutes == 0


def test_parse_preserves_existing_id() -> None:
    anchors = parse_anchors(
        [{"id": "abc123", "kind": "solar_noon", "offset_minutes": -15}]
    )
    assert anchors[0].id == "abc123"
    assert anchors[0].kind == "solar_noon"
    assert anchors[0].time is None
    assert anchors[0].offset_minutes == -15


def test_parse_none_is_empty() -> None:
    assert parse_anchors(None) == []
    assert parse_anchors([]) == []


@pytest.mark.parametrize(
    "raw",
    [
        [{"kind": "clock"}],  # clock without time
        [{"kind": "clock", "time": "25:00"}],  # out of range
        [{"kind": "bogus"}],  # unknown kind
        [{"kind": "clock", "time": "06:00", "offset_minutes": 99999}],  # offset bound
    ],
)
def test_parse_rejects_malformed(raw: list) -> None:
    with pytest.raises(ValueError):
        parse_anchors(raw)


def test_clock_fire_instant_respects_tz_and_offset() -> None:
    anchor = Anchor(
        id="x", kind="clock", time="06:30", offset_minutes=-15, enabled=True
    )
    # 2026-06-23 in Chicago is CDT (UTC-5); 06:30 - 15m = 06:15 CDT = 11:15 UTC.
    instant = anchor_fire_instant(
        anchor, datetime(2026, 6, 23).date(), _CHICAGO, None, None
    )
    assert instant == datetime(2026, 6, 23, 11, 15, tzinfo=UTC)


def test_solar_noon_without_geo_returns_none() -> None:
    anchor = Anchor(
        id="x", kind="solar_noon", time=None, offset_minutes=0, enabled=True
    )
    assert (
        anchor_fire_instant(anchor, datetime(2026, 6, 23).date(), _CHICAGO, None, None)
        is None
    )


def test_due_within_grace_then_missed() -> None:
    anchor = Anchor(id="x", kind="clock", time="06:00", offset_minutes=0, enabled=True)
    fire = anchor_fire_instant(
        anchor, datetime(2026, 6, 23).date(), _CHICAGO, None, None
    )
    assert fire is not None

    # Just after fire: due and within grace.
    decisions = due_anchors([anchor], fire + timedelta(minutes=5), _CHICAGO, None, None)
    assert len(decisions) == 1
    assert decisions[0].within_grace is True
    assert decisions[0].has_geo is True

    # Past the grace window: due but missed.
    late = due_anchors(
        [anchor], fire + EXACT_TIME_GRACE + timedelta(minutes=1), _CHICAGO, None, None
    )
    assert late[0].within_grace is False


def test_due_not_yet_fired_is_empty() -> None:
    anchor = Anchor(id="x", kind="clock", time="23:00", offset_minutes=0, enabled=True)
    # Local mid-morning (09:00 CDT = 14:00 UTC): yesterday's 23:00 fire is long
    # past grace (so not returned as a fresh capture) and today's has not arrived,
    # so the only possible decision is yesterday's missed fire -- never a capture.
    now = datetime(2026, 6, 23, 14, 0, tzinfo=UTC)
    decisions = due_anchors([anchor], now, _CHICAGO, None, None)
    for decision in decisions:
        assert decision.within_grace is False  # nothing freshly fireable


def test_disabled_anchor_never_due_or_woken() -> None:
    anchor = Anchor(id="x", kind="clock", time="06:00", offset_minutes=0, enabled=False)
    fire = anchor_fire_instant(
        anchor, datetime(2026, 6, 23).date(), _CHICAGO, None, None
    )
    assert fire is not None
    assert (
        due_anchors([anchor], fire + timedelta(minutes=1), _CHICAGO, None, None) == []
    )
    assert (
        next_anchor_wake([anchor], fire - timedelta(hours=1), _CHICAGO, None, None)
        is None
    )


def test_solar_noon_due_without_geo_flagged() -> None:
    anchor = Anchor(
        id="x", kind="solar_noon", time=None, offset_minutes=0, enabled=True
    )
    now = datetime(2026, 6, 23, 18, 0, tzinfo=UTC)
    decisions = due_anchors([anchor], now, _CHICAGO, None, None)
    assert len(decisions) == 1
    assert decisions[0].has_geo is False
    assert decisions[0].instant is None


def test_next_wake_is_earliest_future_instant() -> None:
    a1 = Anchor(id="a", kind="clock", time="06:00", offset_minutes=0, enabled=True)
    a2 = Anchor(id="b", kind="clock", time="18:00", offset_minutes=0, enabled=True)
    # 09:00 UTC local-morning: 06:00 already passed today, next is 18:00 today.
    now = datetime(2026, 6, 23, 14, 0, tzinfo=UTC)  # ~09:00 CDT
    wake = next_anchor_wake([a1, a2], now, _CHICAGO, None, None)
    assert wake is not None
    assert wake == anchor_fire_instant(
        a2, now.astimezone(_CHICAGO).date(), _CHICAGO, None, None
    )


def test_midnight_crossing_offset_fires_exactly_once() -> None:
    """A late base time shifted past midnight fires once for its base date.

    Anchor "23:50" + 30m has its fire instant at 00:20 the *next* calendar day.
    Stepping ``now`` across that boundary must yield a due decision for the base
    date exactly once, and ``next_anchor_wake`` must not perpetually point at an
    unreachable future instant.
    """
    anchor = Anchor(id="x", kind="clock", time="23:50", offset_minutes=30, enabled=True)
    # Base date 2026-06-23 (CDT): 23:50 + 30m = 00:20 on 2026-06-24 local =
    # 05:20 UTC on 2026-06-24.
    base_dt = anchor_fire_instant(
        anchor, datetime(2026, 6, 23).date(), _CHICAGO, None, None
    )
    assert base_dt == datetime(2026, 6, 24, 5, 20, tzinfo=UTC)

    # Before the instant: not due, and the wake points at it.
    before = base_dt - timedelta(minutes=5)
    assert due_anchors([anchor], before, _CHICAGO, None, None) == []
    assert next_anchor_wake([anchor], before, _CHICAGO, None, None) == base_dt

    # Just after the instant (now is on the *next* local day): due, keyed to the
    # base date (2026-06-23), within grace.
    after = base_dt + timedelta(minutes=5)
    decisions = due_anchors([anchor], after, _CHICAGO, None, None)
    assert len(decisions) == 1
    assert decisions[0].local_date == "2026-06-23"
    assert decisions[0].within_grace is True

    # The next wake now points at tomorrow's base-date instant, strictly future
    # (never the already-past one).
    wake = next_anchor_wake([anchor], after, _CHICAGO, None, None)
    assert wake is not None
    assert wake > after
    assert wake == base_dt + timedelta(days=1)


def test_early_offset_before_midnight_keys_to_base_date() -> None:
    """A 00:10 base time with -30m offset (= 23:40 previous day) keys correctly."""
    anchor = Anchor(
        id="x", kind="clock", time="00:10", offset_minutes=-30, enabled=True
    )
    # Base date 2026-06-23: 00:10 - 30m = 23:40 on 2026-06-22 local = 04:40 UTC.
    instant = anchor_fire_instant(
        anchor, datetime(2026, 6, 23).date(), _CHICAGO, None, None
    )
    assert instant == datetime(2026, 6, 23, 4, 40, tzinfo=UTC)

    after = instant + timedelta(minutes=5)
    decisions = due_anchors([anchor], after, _CHICAGO, None, None)
    assert len(decisions) == 1
    assert decisions[0].local_date == "2026-06-23"


def test_serialize_round_trips() -> None:
    anchor = Anchor(id="x", kind="clock", time="06:30", offset_minutes=5, enabled=False)
    data = anchors_mod.serialize_anchor(anchor)
    again = parse_anchors([data])
    assert again[0] == anchor


def test_solar_instant_is_driven_by_coordinates_not_schedule_zone() -> None:
    """The solar-noon instant depends only on the camera location, not the zone.

    Solar noon is an absolute moment fixed by latitude/longitude; the timezone
    only governs which calendar day it is keyed to and how it is displayed. So the
    computed instant must be identical whether the schedule zone is UTC or Tokyo.
    """
    anchor = parse_anchors([{"kind": "solar_noon"}])[0]
    base = date(2026, 6, 25)
    via_utc = anchor_fire_instant(anchor, base, _UTC, 41.85, -87.65)
    via_tokyo = anchor_fire_instant(
        anchor, base, ZoneInfo("Asia/Tokyo"), 41.85, -87.65
    )
    assert via_utc is not None
    assert via_utc == via_tokyo


def test_effective_tz_makes_camera_zone_the_solar_source_of_truth() -> None:
    """Solar anchors are governed by the camera zone; clock anchors by schedule.

    This is the wiring that keeps a solar anchor's displayed time and its
    once-per-day key on the same timezone -- the camera's coordinate-derived zone
    -- so they cannot drift apart. Clock anchors stay on the operator's schedule
    zone, and with no resolvable camera zone solar anchors fall back to it.
    """
    schedule_tz = _UTC
    camera_tz = _CHICAGO
    solar = Anchor(id="s", kind="solar_noon", time=None, offset_minutes=0, enabled=True)
    clock = Anchor(id="c", kind="clock", time="06:00", offset_minutes=0, enabled=True)

    assert anchors_mod._effective_tz(solar, schedule_tz, camera_tz) is camera_tz
    assert anchors_mod._effective_tz(clock, schedule_tz, camera_tz) is schedule_tz
    assert anchors_mod._effective_tz(solar, schedule_tz, None) is schedule_tz


def test_solar_due_decision_keyed_in_camera_zone() -> None:
    """A due solar anchor keys to the camera-zone calendar day via ``solar_tz``."""
    anchor = parse_anchors([{"kind": "solar_noon"}])[0]
    # Chicago solar noon on 2026-06-25 is ~17:53 UTC; an hour later it is due.
    now = datetime(2026, 6, 25, 19, 0, tzinfo=UTC)
    decisions = due_anchors(
        [anchor], now, _UTC, 41.85, -87.65, solar_tz=_CHICAGO
    )
    assert len(decisions) == 1
    assert decisions[0].has_geo is True
    # The key is the camera-zone calendar day of the base date.
    assert decisions[0].local_date == "2026-06-25"
    assert decisions[0].within_grace is False  # an hour later is past the grace


def test_next_solar_capture_instant_uses_coordinates() -> None:
    anchor = parse_anchors([{"kind": "solar_noon"}])[0]
    now = datetime(2026, 6, 25, 0, 0, tzinfo=UTC)

    # Valid coordinates -> a future, aware-UTC instant.
    upcoming = next_solar_capture_instant(anchor, now, 41.85, -87.65)
    assert upcoming is not None
    assert upcoming.tzinfo is not None
    assert upcoming > now

    # Missing coordinates -> nothing to compute.
    assert next_solar_capture_instant(anchor, now, None, None) is None


def test_next_solar_capture_instant_ignores_clock_and_disabled() -> None:
    now = datetime(2026, 6, 25, 0, 0, tzinfo=UTC)
    clock = Anchor(id="c", kind="clock", time="06:00", offset_minutes=0, enabled=True)
    assert next_solar_capture_instant(clock, now, 41.85, -87.65) is None

    disabled = Anchor(
        id="s", kind="solar_noon", time=None, offset_minutes=0, enabled=False
    )
    assert next_solar_capture_instant(disabled, now, 41.85, -87.65) is None


# ---------------------------------------------------------------------------
# Sunrise / sunset solar anchors
# ---------------------------------------------------------------------------

_CHI_LAT, _CHI_LON = 41.85, -87.65


def test_parse_accepts_sunrise_and_sunset() -> None:
    anchors = parse_anchors(
        [
            {"kind": "sunrise", "offset_minutes": -10},
            {"kind": "sunset", "offset_minutes": 5},
        ]
    )
    assert [a.kind for a in anchors] == ["sunrise", "sunset"]
    assert all(a.time is None for a in anchors)  # solar kinds ignore time
    assert anchors[0].offset_minutes == -10
    assert anchors[1].offset_minutes == 5


def test_sunrise_sunset_fire_instants_match_sun_times() -> None:
    from timelapse_manager.capture.schedule import compute_sun_times

    base = date(2026, 6, 25)
    sunrise_utc, sunset_utc = compute_sun_times(_CHI_LAT, _CHI_LON, base, _CHICAGO)

    sr = Anchor(id="sr", kind="sunrise", time=None, offset_minutes=0, enabled=True)
    ss = Anchor(id="ss", kind="sunset", time=None, offset_minutes=0, enabled=True)
    sr_inst = anchor_fire_instant(sr, base, _CHICAGO, _CHI_LAT, _CHI_LON)
    ss_inst = anchor_fire_instant(ss, base, _CHICAGO, _CHI_LAT, _CHI_LON)
    assert sr_inst == sunrise_utc
    assert ss_inst == sunset_utc

    # Offsets shift the event instant.
    sr10 = Anchor(id="sr", kind="sunrise", time=None, offset_minutes=-10, enabled=True)
    assert anchor_fire_instant(sr10, base, _CHICAGO, _CHI_LAT, _CHI_LON) == (
        sunrise_utc - timedelta(minutes=10)
    )


def test_solar_event_ordering_sunrise_noon_sunset() -> None:
    base = date(2026, 6, 25)
    sr = Anchor(id="sr", kind="sunrise", time=None, offset_minutes=0, enabled=True)
    noon = Anchor(id="n", kind="solar_noon", time=None, offset_minutes=0, enabled=True)
    ss = Anchor(id="ss", kind="sunset", time=None, offset_minutes=0, enabled=True)
    a = anchor_fire_instant(sr, base, _CHICAGO, _CHI_LAT, _CHI_LON)
    b = anchor_fire_instant(noon, base, _CHICAGO, _CHI_LAT, _CHI_LON)
    c = anchor_fire_instant(ss, base, _CHICAGO, _CHI_LAT, _CHI_LON)
    assert a < b < c


def test_sunrise_sunset_without_geo_is_none() -> None:
    base = date(2026, 6, 25)
    for kind in ("sunrise", "sunset"):
        anchor = Anchor(id="x", kind=kind, time=None, offset_minutes=0, enabled=True)
        assert anchor_fire_instant(anchor, base, _CHICAGO, None, None) is None


def test_sunrise_sunset_polar_day_is_none() -> None:
    # Svalbard in late June: the sun never sets -> no sunrise/sunset that day.
    svalbard = ZoneInfo("Arctic/Longyearbyen")
    base = date(2026, 6, 25)
    sr = Anchor(id="sr", kind="sunrise", time=None, offset_minutes=0, enabled=True)
    ss = Anchor(id="ss", kind="sunset", time=None, offset_minutes=0, enabled=True)
    assert anchor_fire_instant(sr, base, svalbard, 78.22, 15.65) is None
    assert anchor_fire_instant(ss, base, svalbard, 78.22, 15.65) is None


def test_solar_kinds_routed_through_camera_zone() -> None:
    schedule_tz = _UTC
    camera_tz = _CHICAGO
    for kind in ("solar_noon", "sunrise", "sunset"):
        anchor = Anchor(id="x", kind=kind, time=None, offset_minutes=0, enabled=True)
        assert anchors_mod._effective_tz(anchor, schedule_tz, camera_tz) is camera_tz


def test_next_solar_capture_instant_for_sunrise_and_sunset() -> None:
    now = datetime(2026, 6, 25, 0, 0, tzinfo=UTC)
    for kind in ("sunrise", "sunset"):
        anchor = Anchor(id="x", kind=kind, time=None, offset_minutes=0, enabled=True)
        upcoming = next_solar_capture_instant(anchor, now, _CHI_LAT, _CHI_LON)
        assert upcoming is not None
        assert upcoming > now
        assert next_solar_capture_instant(anchor, now, None, None) is None
