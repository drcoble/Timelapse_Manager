"""When did a project last capture, when will it next, and has it missed one? (#5)

Pure helpers for the project views. Before 0.1.1 the UI read "last capture" from
the interval runner's in-memory cadence anchor (never set by anchor or event
captures, and lost on restart) and estimated "next capture" as last frame +
interval -- so an anchor-only project always showed both as blank and could
never be flagged overdue. These helpers work from stored data and the same
anchor arithmetic the runner uses (:mod:`timelapse_manager.capture.anchors`).
How many captures a day the full schedule plans (anchors plus interval ticks
inside the schedule windows), for the storage estimates, is
:mod:`timelapse_manager.capture.cadence`, which reuses these helpers (#22).
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

from . import anchors as anchors_mod
from . import geo
from .schedule import parse_schedule

_UTC = dt.UTC


def as_utc(value: dt.datetime | None) -> dt.datetime | None:
    """Treat a naive timestamp as UTC (the storage convention)."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=_UTC)


def _schedule_tz(schedule_raw: dict[str, Any] | None) -> ZoneInfo:
    try:
        return parse_schedule(schedule_raw).tzinfo()
    except Exception:  # noqa: BLE001 - a bad schedule must not break a page
        return ZoneInfo("UTC")


def _enabled_anchors(raw: list[Any] | None) -> list[anchors_mod.Anchor]:
    if not raw:
        return []
    try:
        return [a for a in anchors_mod.parse_anchors(raw) if a.enabled]
    except ValueError:
        return []


def next_capture_instant(
    *,
    now: dt.datetime,
    interval_seconds: int | None,
    last_captured_at: dt.datetime | None,
    anchors_raw: list[Any] | None,
    schedule_raw: dict[str, Any] | None,
    latitude: float | None,
    longitude: float | None,
    start_date: dt.datetime | None = None,
    end_date: dt.datetime | None = None,
    finished: bool = False,
) -> dt.datetime | None:
    """Return the earliest upcoming capture attempt, or ``None`` if none is planned.

    The minimum of the next exact-time anchor fire (clock anchors in the schedule
    timezone, solar anchors in the camera's coordinate timezone) and, for an
    interval project, the next interval tick after the last capture (``now`` when
    there is none yet or it is already due). A future ``start_date`` defers it;
    a past ``end_date`` or a finished campaign means no next capture. The
    interval tick does not model schedule windows.
    """
    now = as_utc(now) or dt.datetime.now(_UTC)
    start = as_utc(start_date)
    end = as_utc(end_date)
    if finished or (end is not None and end <= now):
        return None

    candidates: list[dt.datetime] = []
    anchors = _enabled_anchors(anchors_raw)
    if anchors:
        wake = anchors_mod.next_anchor_wake(
            anchors,
            max(now, start) if start is not None else now,
            _schedule_tz(schedule_raw),
            latitude,
            longitude,
            solar_tz=geo.resolve_zoneinfo(latitude, longitude),
        )
        if wake is not None:
            candidates.append(wake)
    if interval_seconds:
        last = as_utc(last_captured_at)
        tick = (
            now
            if last is None
            else max(now, last + dt.timedelta(seconds=interval_seconds))
        )
        candidates.append(tick)
    if not candidates:
        return None
    soonest = min(candidates)
    if start is not None and soonest < start:
        soonest = start
    if end is not None and soonest >= end:
        return None
    return soonest


def missed_anchor_instant(
    *,
    now: dt.datetime,
    anchors_raw: list[Any] | None,
    schedule_raw: dict[str, Any] | None,
    latitude: float | None,
    longitude: float | None,
    last_captured_at: dt.datetime | None,
    active_since: dt.datetime | None,
    grace: dt.timedelta = anchors_mod.EXACT_TIME_GRACE,
) -> dt.datetime | None:
    """Return the latest anchor instant that passed without a capture, or ``None``.

    Looks at each enabled anchor's fire instants for today and yesterday (local)
    whose grace window has fully elapsed and which fall after ``active_since``
    (so a project created after the instant is not blamed for it). If the most
    recent such instant has no capture at or after it, it is returned -- the
    anchor-aware "overdue" signal that catches the #6 stall.
    """
    now = as_utc(now) or dt.datetime.now(_UTC)
    since = as_utc(active_since)
    tz = _schedule_tz(schedule_raw)
    solar_tz = geo.resolve_zoneinfo(latitude, longitude)
    latest: dt.datetime | None = None
    for anchor in _enabled_anchors(anchors_raw):
        eff_tz = anchors_mod._effective_tz(anchor, tz, solar_tz)  # as the runner
        today = now.astimezone(eff_tz).date()
        for base in (today - dt.timedelta(days=1), today):
            instant = anchors_mod.anchor_fire_instant(
                anchor, base, eff_tz, latitude, longitude
            )
            if instant is None or instant + grace > now:
                continue
            if since is not None and instant < since:
                continue
            if latest is None or instant > latest:
                latest = instant
    if latest is None:
        return None
    last = as_utc(last_captured_at)
    if last is not None and last >= latest:
        return None
    return latest


__all__ = ["as_utc", "missed_anchor_instant", "next_capture_instant"]
