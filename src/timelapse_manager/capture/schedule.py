"""Pure, side-effect-free evaluator for capture *gating* schedules.

A schedule answers exactly one question: **is capture permitted right now, and
when does that permission next change?** It deliberately knows nothing about the
capture *interval* (how often a frame is taken while the gate is open) — that is
a separate concern owned by the capture supervisor.

Design rules (all load-bearing for testability):

* **No clock, no I/O.** Every function takes the current instant — and, when a
  sun-based window is in play, the camera's latitude/longitude — as explicit
  parameters. There is no hidden ``datetime.now()``, no database access and no
  file access anywhere in this module.
* **Timezone-aware throughout.** Callers pass ``now`` as an aware UTC datetime
  and receive aware UTC datetimes back. Internally the evaluator converts to the
  schedule's own timezone via :mod:`zoneinfo` so wall-clock windows and sunrise/
  sunset offsets are computed correctly across daylight-saving transitions.
* **Backward compatible.** A project with no schedule, an empty schedule, empty
  windows, or ``enabled=False`` has an **always-open** gate. This preserves the
  plain fixed-interval capture behaviour for projects that never opt in to a
  schedule.

Gate semantics
--------------
At any instant the gate is **open** when *all* of the following hold:

#. ``enabled`` is true (a disabled schedule is treated as "no schedule" and is
   therefore always open);
#. the instant falls within the optional ``[start_date, end_date)`` campaign
   bounds;
#. the instant's *local* weekday is allowed by ``day_of_week_mask``; and
#. the instant falls inside *any* configured clock window **or** the configured
   sun window. When neither clock windows nor a sun window is configured this
   sub-condition is vacuously true, which is what makes an otherwise-empty
   schedule always open.

All time ranges are **half-open** ``[start, end)``: the gate is open *at* a
window's start instant and closed *at* its end instant. ``is_within_window`` and
``next_transition`` share this convention, so evaluating ``is_within_window`` at
a candidate edge yields the post-transition state — which is precisely the value
that :func:`next_transition` reports as the next change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astral import Observer
from astral.sun import elevation, noon, sun

__all__ = [
    "Window",
    "SunAnchor",
    "Schedule",
    "parse_schedule",
    "is_within_window",
    "next_transition",
    "compute_sun_times",
    "compute_solar_noon",
]

# How far ahead :func:`next_transition` is willing to scan before giving up and
# reporting "no future transition". A little over a year covers every annual
# pattern (including a date that only recurs once a year) with margin to spare.
_MAX_SCAN_DAYS = 400

_FULL_WEEK_MASK = 0b1111111


def _parse_hhmm(value: Any, field_path: str) -> time:
    """Parse a ``"HH:MM"`` string into a :class:`datetime.time`.

    Raises :class:`ValueError` naming *field_path* when the value is not a valid
    24-hour ``HH:MM`` clock time.
    """
    if not isinstance(value, str):
        raise ValueError(f"{field_path}: expected a 'HH:MM' string, got {value!r}")
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"{field_path}: expected 'HH:MM', got {value!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(f"{field_path}: non-numeric time {value!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"{field_path}: time out of range {value!r}")
    return time(hour=hour, minute=minute)


def _parse_aware_datetime(value: Any, field_path: str) -> datetime:
    """Parse an ISO-8601 datetime, coercing naive values to UTC."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                f"{field_path}: invalid ISO-8601 datetime {value!r}"
            ) from exc
    else:
        raise ValueError(f"{field_path}: expected an ISO-8601 datetime, got {value!r}")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


@dataclass(frozen=True)
class Window:
    """A daily wall-clock capture window in the schedule's local timezone.

    ``start_time`` and ``end_time`` are ``"HH:MM"`` strings. A window may span
    midnight: when ``end_time`` is less than or equal to ``start_time`` the
    window wraps to the following local day (e.g. ``22:00``–``02:00``). A window
    whose start equals its end is treated as a full 24-hour window.
    """

    start_time: str
    end_time: str

    def contains(self, local_now: datetime) -> bool:
        """Return whether *local_now* (an aware local datetime) is in the window."""
        start = _parse_hhmm(self.start_time, "window.start_time")
        end = _parse_hhmm(self.end_time, "window.end_time")
        current = local_now.time()
        if start == end:
            # Degenerate equal endpoints mean "always" rather than "never".
            return True
        if start < end:
            return start <= current < end
        # Wrapping window (spans local midnight): open from start to midnight and
        # from midnight to end.
        return current >= start or current < end


@dataclass(frozen=True)
class SunAnchor:
    """An instant relative to local sunrise or sunset.

    ``offset_minutes`` shifts the anchor: negative values are *before* the
    astronomical event, positive values *after* it. For example
    ``SunAnchor("sunrise", -30)`` is 30 minutes before sunrise.
    """

    anchor: Literal["sunrise", "sunset"]
    offset_minutes: int = 0


@dataclass
class Schedule:
    """A parsed capture-gating schedule.

    Attributes
    ----------
    enabled:
        When false the schedule is inert and the gate is always open.
    timezone:
        IANA timezone name (e.g. ``"America/Chicago"``) used to interpret
        windows, the day-of-week mask and sun offsets.
    windows:
        Wall-clock windows; the gate is open inside any of them.
    sun_window:
        Optional ``(open_anchor, close_anchor)`` pair describing a window that
        opens and closes relative to sunrise/sunset (e.g. sunrise−30 to
        sunset+45). May be combined with ``windows``; the gate is open when
        inside *either* the clock windows *or* the sun window.
    day_of_week_mask:
        Bitmask of allowed local weekdays. Bit 0 is Monday … bit 6 is Sunday.
        Defaults to every day.
    start_date / end_date:
        Optional aware campaign bounds, treated as the half-open interval
        ``[start_date, end_date)``. Outside these bounds the gate is closed.
    """

    enabled: bool = True
    timezone: str = "UTC"
    windows: list[Window] = field(default_factory=list)
    sun_window: tuple[SunAnchor, SunAnchor] | None = None
    day_of_week_mask: int = _FULL_WEEK_MASK
    start_date: datetime | None = None
    end_date: datetime | None = None

    def __post_init__(self) -> None:
        # Campaign bounds are compared against aware UTC instants, so coerce any
        # naive datetime (e.g. from direct construction) to UTC. This keeps the
        # evaluator from raising on naive/aware comparison; `parse_schedule`
        # already produces aware values, so this only matters for callers that
        # build a Schedule by hand.
        if self.start_date is not None and self.start_date.tzinfo is None:
            self.start_date = self.start_date.replace(tzinfo=UTC)
        if self.end_date is not None and self.end_date.tzinfo is None:
            self.end_date = self.end_date.replace(tzinfo=UTC)

    def tzinfo(self) -> ZoneInfo:
        """Return the schedule's timezone as a :class:`zoneinfo.ZoneInfo`."""
        return ZoneInfo(self.timezone)

    @property
    def is_always_open(self) -> bool:
        """True when nothing about this schedule can ever close the gate.

        That is the case for a disabled schedule, or an enabled one with no
        clock windows, no sun window, every weekday allowed and no date bounds.
        """
        return self.enabled is False or (
            not self.windows
            and self.sun_window is None
            and self.day_of_week_mask == _FULL_WEEK_MASK
            and self.start_date is None
            and self.end_date is None
        )


def parse_schedule(raw: dict[str, Any] | None) -> Schedule:
    """Build a :class:`Schedule` from the project's stored JSON document.

    Absent fields fall back to defaults; ``None`` or ``{}`` yields an
    always-open schedule, preserving backward compatibility for projects that
    have never configured a schedule.

    Raises :class:`ValueError`, naming the offending field, when a present field
    is malformed (bad ``HH:MM`` window, unknown timezone, unknown sun anchor,
    bad date, etc.).
    """
    if not raw:
        return Schedule()

    enabled = bool(raw.get("enabled", True))

    tz_name = raw.get("timezone", "UTC")
    if not isinstance(tz_name, str):
        raise ValueError(f"timezone: expected a string, got {tz_name!r}")
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"timezone: unknown timezone {tz_name!r}") from exc

    windows: list[Window] = []
    raw_windows = raw.get("windows", []) or []
    if not isinstance(raw_windows, list):
        raise ValueError(f"windows: expected a list, got {raw_windows!r}")
    for index, item in enumerate(raw_windows):
        if not isinstance(item, dict):
            raise ValueError(f"windows[{index}]: expected an object, got {item!r}")
        start_raw = item.get("start_time")
        end_raw = item.get("end_time")
        # Validate eagerly so malformed times fail at parse time, not evaluation.
        # _parse_hhmm asserts the value is a "HH:MM" string; round-tripping its
        # parsed result back to a canonical string keeps the stored window typed
        # as `str` and normalises e.g. "6:5" to "06:05".
        start = _parse_hhmm(start_raw, f"windows[{index}].start_time")
        end = _parse_hhmm(end_raw, f"windows[{index}].end_time")
        windows.append(
            Window(start_time=start.strftime("%H:%M"), end_time=end.strftime("%H:%M"))
        )

    sun_window = _parse_sun_window(raw.get("sun_window"))

    mask = raw.get("day_of_week_mask", _FULL_WEEK_MASK)
    if not isinstance(mask, int) or isinstance(mask, bool):
        raise ValueError(f"day_of_week_mask: expected an integer, got {mask!r}")
    if not (0 <= mask <= _FULL_WEEK_MASK):
        raise ValueError(f"day_of_week_mask: out of range 0..127, got {mask!r}")

    start_date = None
    if raw.get("start_date") is not None:
        start_date = _parse_aware_datetime(raw["start_date"], "start_date")
    end_date = None
    if raw.get("end_date") is not None:
        end_date = _parse_aware_datetime(raw["end_date"], "end_date")

    return Schedule(
        enabled=enabled,
        timezone=tz_name,
        windows=windows,
        sun_window=sun_window,
        day_of_week_mask=mask,
        start_date=start_date,
        end_date=end_date,
    )


def _parse_sun_window(raw: Any) -> tuple[SunAnchor, SunAnchor] | None:
    """Parse the optional ``sun_window`` field into an anchor pair."""
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise ValueError(
            f"sun_window: expected a [open_anchor, close_anchor] pair, got {raw!r}"
        )
    open_anchor = _parse_sun_anchor(raw[0], "sun_window[0]")
    close_anchor = _parse_sun_anchor(raw[1], "sun_window[1]")
    return (open_anchor, close_anchor)


def _parse_sun_anchor(raw: Any, field_path: str) -> SunAnchor:
    """Parse a single ``{"anchor": ..., "offset_minutes": ...}`` object."""
    if not isinstance(raw, dict):
        raise ValueError(f"{field_path}: expected an object, got {raw!r}")
    anchor = raw.get("anchor")
    if anchor not in ("sunrise", "sunset"):
        raise ValueError(
            f"{field_path}.anchor: expected 'sunrise' or 'sunset', got {anchor!r}"
        )
    offset = raw.get("offset_minutes", 0)
    if not isinstance(offset, int) or isinstance(offset, bool):
        raise ValueError(
            f"{field_path}.offset_minutes: expected an integer, got {offset!r}"
        )
    return SunAnchor(anchor=anchor, offset_minutes=offset)


def compute_sun_times(
    latitude: float,
    longitude: float,
    on_date: date,
    tz: ZoneInfo,
) -> tuple[datetime | None, datetime | None]:
    """Return ``(sunrise_utc, sunset_utc)`` for *on_date* at the given location.

    The returned datetimes are timezone-aware in UTC. The local *tz* is used so
    that "the sunrise on this local calendar day" is computed for the correct
    24-hour span (which matters near the international date line and at high
    latitudes).

    On polar degeneracy — when the sun never rises or never sets on *on_date* —
    ``(None, None)`` is returned rather than raising. The caller decides what an
    open/closed gate means in that situation; this function reports only that no
    finite sunrise/sunset exists.
    """
    observer = Observer(latitude=latitude, longitude=longitude)
    try:
        events = sun(observer, date=on_date, tzinfo=tz)
    except ValueError:
        # astral raises ValueError when the sun does not cross the horizon on
        # this date (polar day or polar night).
        return (None, None)
    sunrise = events.get("sunrise")
    sunset = events.get("sunset")
    sunrise_utc = sunrise.astimezone(UTC) if sunrise is not None else None
    sunset_utc = sunset.astimezone(UTC) if sunset is not None else None
    return (sunrise_utc, sunset_utc)


def compute_solar_noon(
    latitude: float,
    longitude: float,
    on_date: date,
    tz: ZoneInfo,
) -> datetime | None:
    """Return the UTC instant of solar noon for *on_date* at the given location.

    Solar noon is the moment the sun reaches its highest point in the sky. The
    returned datetime is timezone-aware in UTC. The local *tz* is used so that
    "noon on this local calendar day" is computed for the correct 24-hour span
    (which matters near the international date line and at high latitudes).

    Unlike sunrise/sunset, solar noon is defined even in polar day or polar
    night -- the sun still has a daily highest point -- so this rarely fails.
    ``None`` is returned only on a pure-math failure, kept symmetric with
    :func:`compute_sun_times` so callers handle a missing instant the same way.
    """
    observer = Observer(latitude=latitude, longitude=longitude)
    try:
        when = noon(observer, date=on_date, tzinfo=tz)
    except ValueError:
        return None
    return when.astimezone(UTC)


def _sun_is_up_at_noon(
    latitude: float, longitude: float, on_date: date, tz: ZoneInfo
) -> bool:
    """Tiebreak for polar degeneracy: is the sun above the horizon at local noon?

    When sunrise/sunset are undefined we cannot tell polar *day* (sun never sets,
    gate should be open) from polar *night* (sun never rises, gate should be
    closed) from the absence of events alone. The sign of the solar elevation at
    local noon disambiguates: positive ⇒ continuous day ⇒ open.
    """
    observer = Observer(latitude=latitude, longitude=longitude)
    local_noon = datetime.combine(on_date, time(12, 0), tzinfo=tz)
    return elevation(observer, local_noon) > 0.0


def _in_sun_window(
    schedule: Schedule,
    local_now: datetime,
    latitude: float | None,
    longitude: float | None,
) -> bool:
    """Whether *local_now* falls inside the schedule's sun window.

    Without a sun window this is vacuously false (the caller unions it with the
    clock windows). Without a location a sun window cannot be evaluated, so it is
    treated as closed. On polar degeneracy the noon-elevation tiebreak decides.
    """
    if schedule.sun_window is None:
        return False
    if latitude is None or longitude is None:
        return False

    tz = schedule.tzinfo()
    open_anchor, close_anchor = schedule.sun_window
    on_date = local_now.date()
    sunrise_utc, sunset_utc = compute_sun_times(latitude, longitude, on_date, tz)
    if sunrise_utc is None or sunset_utc is None:
        return _sun_is_up_at_noon(latitude, longitude, on_date, tz)

    open_at = _anchor_instant(open_anchor, sunrise_utc, sunset_utc)
    close_at = _anchor_instant(close_anchor, sunrise_utc, sunset_utc)
    now_utc = local_now.astimezone(UTC)
    if open_at <= close_at:
        return open_at <= now_utc < close_at
    # Anchors out of order (e.g. opens at sunset, closes at next sunrise): treat
    # as a window that wraps across the day boundary.
    return now_utc >= open_at or now_utc < close_at


def _anchor_instant(
    anchor: SunAnchor, sunrise_utc: datetime, sunset_utc: datetime
) -> datetime:
    """Resolve a :class:`SunAnchor` to a concrete UTC instant."""
    base = sunrise_utc if anchor.anchor == "sunrise" else sunset_utc
    return base + timedelta(minutes=anchor.offset_minutes)


def is_within_window(
    schedule: Schedule,
    now: datetime,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
) -> bool:
    """Return whether the capture gate is open at the aware-UTC instant *now*.

    The gate is open when the schedule is enabled, *now* is within the campaign
    date bounds, the local weekday is allowed, and *now* falls inside any clock
    window or the sun window (or no windows are configured at all). See the
    module docstring for the full rule and the half-open boundary convention.
    """
    if schedule.enabled is False:
        return True

    now_utc = now.astimezone(UTC)
    if schedule.start_date is not None and now_utc < schedule.start_date:
        return False
    if schedule.end_date is not None and now_utc >= schedule.end_date:
        return False

    local_now = now_utc.astimezone(schedule.tzinfo())

    # weekday(): Monday == 0, matching bit 0 of the mask.
    if not (schedule.day_of_week_mask >> local_now.weekday()) & 1:
        return False

    has_clock = bool(schedule.windows)
    has_sun = schedule.sun_window is not None
    if not has_clock and not has_sun:
        # No time restriction beyond mask/date bounds: open for the whole day.
        return True

    in_clock = has_clock and any(
        window.contains(local_now) for window in schedule.windows
    )
    in_sun = has_sun and _in_sun_window(schedule, local_now, latitude, longitude)
    return in_clock or in_sun


def next_transition(
    schedule: Schedule,
    now: datetime,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
) -> tuple[bool, datetime | None]:
    """Return ``(is_open_now, next_change_at)`` for the gate.

    ``is_open_now`` is the gate state at *now*. ``next_change_at`` is the
    aware-UTC instant the gate next flips (open→closed or closed→open), or
    ``None`` when no transition occurs within the forward scan horizon — for an
    always-open schedule, or one whose campaign has permanently ended.

    The next edge is found by evaluating :func:`is_within_window` at a sorted set
    of candidate instants (local midnights, window starts/ends, sun anchors and
    the campaign bounds) and returning the first whose result differs from the
    current state. Because the gate is piecewise-constant between candidates this
    finds the exact flip instant, and because the candidates are built in the
    schedule's timezone the result is correct across daylight-saving changes.
    """
    open_now = is_within_window(schedule, now, latitude=latitude, longitude=longitude)

    if schedule.is_always_open:
        return (open_now, None)

    now_utc = now.astimezone(UTC)
    tz = schedule.tzinfo()

    # A campaign that has permanently ended never changes again.
    if schedule.end_date is not None and now_utc >= schedule.end_date:
        return (open_now, None)

    for candidate in _iter_candidates(schedule, now_utc, tz, latitude, longitude):
        if candidate <= now_utc:
            continue
        state = is_within_window(
            schedule, candidate, latitude=latitude, longitude=longitude
        )
        if state != open_now:
            return (open_now, candidate)
    return (open_now, None)


def _iter_candidates(
    schedule: Schedule,
    now_utc: datetime,
    tz: ZoneInfo,
    latitude: float | None,
    longitude: float | None,
) -> list[datetime]:
    """Build the sorted, de-duplicated set of candidate transition instants.

    Candidates are every instant at which the gate *could* flip within the scan
    horizon: each local midnight (catches day-mask flips), each clock window's
    start and end on each local day, each resolved sun anchor on each local day,
    and the campaign's ``start_date``/``end_date``. Sun times are computed at
    most once per local day. Instants that fall in a nonexistent or ambiguous
    wall-clock slot (spring-forward / fall-back) are tolerated by ``zoneinfo``'s
    fold/normalisation and never raise.
    """
    candidates: set[datetime] = set()

    if schedule.start_date is not None:
        candidates.add(schedule.start_date)
    if schedule.end_date is not None:
        candidates.add(schedule.end_date)

    local_start_day = now_utc.astimezone(tz).date()
    for day_offset in range(_MAX_SCAN_DAYS + 1):
        local_day = local_start_day + timedelta(days=day_offset)

        # Local midnight: the day-mask flips here.
        candidates.add(_local_wall_to_utc(local_day, time(0, 0), tz))

        for window in schedule.windows:
            start = _parse_hhmm(window.start_time, "window.start_time")
            end = _parse_hhmm(window.end_time, "window.end_time")
            candidates.add(_local_wall_to_utc(local_day, start, tz))
            candidates.add(_local_wall_to_utc(local_day, end, tz))

        if (
            schedule.sun_window is not None
            and latitude is not None
            and longitude is not None
        ):
            sunrise_utc, sunset_utc = compute_sun_times(
                latitude, longitude, local_day, tz
            )
            if sunrise_utc is not None and sunset_utc is not None:
                for anchor in schedule.sun_window:
                    candidates.add(_anchor_instant(anchor, sunrise_utc, sunset_utc))

    horizon = now_utc + timedelta(days=_MAX_SCAN_DAYS)
    return sorted(c for c in candidates if now_utc < c <= horizon)


def _local_wall_to_utc(local_day: date, wall: time, tz: ZoneInfo) -> datetime:
    """Convert a local wall-clock (day, time) to an aware UTC instant."""
    local_dt = datetime.combine(local_day, wall, tzinfo=tz)
    return local_dt.astimezone(UTC)
