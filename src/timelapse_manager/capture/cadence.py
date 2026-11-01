"""How many captures does a project plan per day? (#22)

Pure helper for the storage estimates (growth rate, projected storage and the
create-time pre-flight). Before #22 they counted captures per day only as
``86400 / capture_interval_seconds``, so a project that captures only at
exact-time anchors -- every imported prod project -- showed no growth rate even
though :mod:`.next_due` planned its Next Capture. This module counts captures
from the full schedule, by the same rules the runner fires them:

* **Anchors** fire once per local day each, in the anchor's governing timezone
  (the schedule timezone for clock anchors, the camera's coordinate timezone for
  solar ones), and ignore the schedule gate: its windows, sun window and
  weekday mask (``capture/supervisor.py::_run_cycle``; ``anchors.due_anchors``
  takes no gate input). A solar anchor without coordinates, or a sunrise or
  sunset that does not happen on a polar day, fires nothing, so it counts 0.
* **Interval ticks** run only while the gate is open. Open time comes from the
  gate changes :func:`.schedule.next_transition` would report, found in one
  candidate pass by :func:`.schedule.open_stretches` -- never by sampling the
  gate once per interval: the dashboard re-renders every project card every
  30 s on a Raspberry Pi.

No clock, no I/O, no database: ``now`` and the camera coordinates are explicit
parameters, as in :mod:`.next_due` and :mod:`.schedule`.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any
from zoneinfo import ZoneInfo

from . import anchors as anchors_mod
from . import geo
from .next_due import _enabled_anchors, _schedule_tz, as_utc
from .schedule import Schedule, open_stretches, parse_schedule

_UTC = dt.UTC

# The window the rate is measured over: the next week of local days, so a
# weekday mask or a solar anchor's drift averages out the way the operator
# thinks of the schedule.
HORIZON = dt.timedelta(days=7)

_SECONDS_PER_DAY = 86400.0


def _gate_schedule(schedule_raw: dict[str, Any] | None) -> Schedule:
    """Parse the gate schedule; a malformed one is always open, as the runner."""
    try:
        return parse_schedule(schedule_raw)
    except ValueError:
        return parse_schedule(None)


def _anchor_fires(
    anchors: list[anchors_mod.Anchor],
    begin: dt.datetime,
    stop: dt.datetime,
    schedule_tz: ZoneInfo,
    latitude: float | None,
    longitude: float | None,
) -> int:
    """Count the anchor fire instants in ``[begin, stop)``.

    Each anchor fires once per base local date in its effective timezone. The
    base dates scanned run from the day before *begin* to the day after *stop*
    because an offset can move an instant onto an adjacent calendar day; only
    instants inside the horizon count. The schedule gate is not consulted.
    """
    if not anchors:
        return 0
    solar_tz = (
        geo.resolve_zoneinfo(latitude, longitude)
        if any(a.kind != "clock" for a in anchors)
        else None
    )
    one_day = dt.timedelta(days=1)
    fires = 0
    for anchor in anchors:
        # The anchor's governing zone, exactly as the runner picks it.
        eff_tz = anchors_mod._effective_tz(anchor, schedule_tz, solar_tz)
        day = begin.astimezone(eff_tz).date() - one_day
        last = stop.astimezone(eff_tz).date() + one_day
        while day <= last:
            instant = anchors_mod.anchor_fire_instant(
                anchor, day, eff_tz, latitude, longitude
            )
            if instant is not None and begin <= instant < stop:
                fires += 1
            day += one_day
    return fires


def _interval_ticks(
    interval_seconds: int,
    schedule: Schedule,
    begin: dt.datetime,
    stop: dt.datetime,
    latitude: float | None,
    longitude: float | None,
) -> float:
    """Count the interval captures in ``[begin, stop)`` while the gate is open.

    An always-open gate short-cuts to ``horizon / interval``. Otherwise the open
    stretches come from :func:`.schedule.open_stretches` (the gate changes
    :func:`.schedule.next_transition` reports, found in one pass). In each open
    stretch the runner captures as soon as the gate opens (when the interval has
    elapsed since its last capture) and then every interval until it closes, so
    a stretch that opens inside the horizon counts ``ceil(open / interval)``
    from the first due instant: a 12:00-12:30 window with a 1 h interval is one
    capture a day, not half. A stretch already open at *begin* has an unknown
    phase (the last capture may have been a moment ago), so it counts its
    expected ``open / interval``.
    """
    if schedule.is_always_open:
        return (stop - begin).total_seconds() / interval_seconds

    interval = dt.timedelta(seconds=interval_seconds)
    ticks = 0.0
    due = begin
    for opens, closes in open_stretches(
        schedule, begin, stop, latitude=latitude, longitude=longitude
    ):
        if opens == begin:
            ticks += (closes - opens).total_seconds() / interval_seconds
            due = closes
            continue
        first = max(opens, due)
        if first < closes:
            count = math.ceil((closes - first) / interval)
            ticks += count
            due = first + count * interval
    return ticks


def captures_per_day(
    *,
    now: dt.datetime,
    interval_seconds: int | None,
    anchors_raw: list[Any] | None,
    schedule_raw: dict[str, Any] | None,
    latitude: float | None,
    longitude: float | None,
    start_date: dt.datetime | None = None,
    end_date: dt.datetime | None = None,
    finished: bool = False,
) -> float | None:
    """Return how many captures a project plans per day, or ``None`` if none.

    The count runs over the next :data:`HORIZON` (7 local days, wall clock in
    the schedule timezone) from ``max(now, start_date)``, cut short at
    ``end_date``. It adds every enabled anchor's fire instants (anchors ignore
    the schedule gate) and the interval ticks while the gate is open (see
    :func:`_interval_ticks`), then divides by the horizon's **actual** length in
    local days, so a campaign that ends in two days still shows its real daily
    rate. Measuring in local days keeps a daily anchor at exactly one a day in
    a week with a daylight-saving change (168 real hours would drop or add an
    anchor depending on the time of day); that week's interval rate carries its
    23 h or 25 h day, as the gate really does.

    Returns ``None`` when the project is finished, its ``end_date`` has passed,
    or it plans no capture in the horizon (no interval and no enabled anchor
    that fires, e.g. only a solar anchor on a camera without coordinates).
    Naive datetimes are read as UTC, the storage convention.
    """
    if finished:
        return None
    now = as_utc(now) or dt.datetime.now(_UTC)
    start = as_utc(start_date)
    end = as_utc(end_date)
    begin = max(now, start) if start is not None else now
    schedule_tz = _schedule_tz(schedule_raw)
    begin_wall = begin.astimezone(schedule_tz).replace(tzinfo=None)
    stop = (begin_wall + HORIZON).replace(tzinfo=schedule_tz).astimezone(_UTC)
    if end is not None:
        stop = min(stop, end)
    if stop <= begin:
        return None
    stop_wall = stop.astimezone(schedule_tz).replace(tzinfo=None)
    horizon_days = (stop_wall - begin_wall).total_seconds() / _SECONDS_PER_DAY
    if horizon_days <= 0:  # a sub-hour horizon inside a fall-back repeat
        horizon_days = (stop - begin).total_seconds() / _SECONDS_PER_DAY

    total = float(
        _anchor_fires(
            _enabled_anchors(anchors_raw),
            begin,
            stop,
            schedule_tz,
            latitude,
            longitude,
        )
    )
    if interval_seconds is not None and interval_seconds > 0:
        total += _interval_ticks(
            interval_seconds,
            _gate_schedule(schedule_raw),
            begin,
            stop,
            latitude,
            longitude,
        )
    if total <= 0:
        return None
    return total / horizon_days


__all__ = ["HORIZON", "captures_per_day"]
