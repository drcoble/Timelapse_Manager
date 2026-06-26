"""Pure, side-effect-free evaluator for exact-time capture anchors.

An *anchor* fires a single capture exactly once per local day, independent of the
interval-capture cadence and of the schedule gate (it honours only the universal
disk gate). An anchor is either an exact wall-clock time (``HH:MM`` in the
project's schedule timezone) or *solar noon* (computed from the camera's
geolocation for that local day). Either kind may carry a ``+/-`` minute offset.

This module mirrors the testability discipline of :mod:`.schedule`:

* **No clock, no I/O, no database.** Every function takes the current instant,
  the timezone, and the camera latitude/longitude as explicit parameters and
  returns aware-UTC instants or plain decision records. The durable fire-log
  (the once-per-day idempotency guard) lives in the database and is consulted by
  the caller; this module only computes *when* an anchor would fire and *which*
  anchors are due now.
* **Stable identity.** Each anchor carries a generated string ``id`` stored in
  the anchor object -- never a list index -- so the fire-log row for an anchor
  stays bound to it across reordering, insertion and deletion of sibling anchors.

The grace window :data:`EXACT_TIME_GRACE` is the single knob governing
late/missed firing: when the runner wakes after an anchor's instant but within
the grace window, it fires late; past the grace window it records a miss instead.
This one rule covers both normal firing and startup catch-up after the process
was down when the anchor was due. An anchor is keyed once per *base local date*
(the day its configured time falls on), so an offset that shifts the actual fire
instant across midnight still fires exactly once for the right date.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from .geo import resolve_zoneinfo
from .schedule import compute_solar_noon, compute_sun_times

__all__ = [
    "EXACT_TIME_GRACE",
    "Anchor",
    "AnchorDecision",
    "parse_anchors",
    "serialize_anchor",
    "anchor_fire_instant",
    "next_anchor_wake",
    "next_solar_capture_instant",
    "due_anchors",
]

# How long after an anchor's fire instant the runner will still capture a late
# frame. Past this window (or once the local day has rolled over) the anchor is
# recorded as missed rather than fired. A single documented constant -- not
# per-anchor config -- keeps the rule simple; the seam to make it per-anchor
# exists in the anchor object if it is ever needed.
EXACT_TIME_GRACE = timedelta(minutes=30)

# Anchor offsets are bounded to a sane range so a fat-fingered value cannot push
# a "noon" anchor onto a different day or wildly off the clock anchor. +/- 12h.
_MAX_OFFSET_MINUTES = 12 * 60

_KIND_CLOCK = "clock"
_KIND_SOLAR_NOON = "solar_noon"
_KIND_SUNRISE = "sunrise"
_KIND_SUNSET = "sunset"
# Solar kinds are computed from the camera's geolocation (and so require it) and
# are governed by the camera's coordinate-derived timezone; clock kinds are not.
_SOLAR_KINDS = (_KIND_SOLAR_NOON, _KIND_SUNRISE, _KIND_SUNSET)
_VALID_KINDS = (_KIND_CLOCK, *_SOLAR_KINDS)

AnchorKind = Literal["clock", "solar_noon", "sunrise", "sunset"]


def _is_solar(kind: str) -> bool:
    """Whether *kind* is a sun-derived anchor (needs geolocation + camera zone)."""
    return kind in _SOLAR_KINDS


@dataclass(frozen=True)
class Anchor:
    """A parsed exact-time capture anchor.

    Attributes
    ----------
    id:
        Stable generated token (uuid4 hex) identifying this anchor across edits.
        The durable fire-log keys on it, so it must not be a list index.
    kind:
        ``"clock"`` (fire at the wall-clock ``time``) or a solar event for the
        camera's location: ``"solar_noon"``, ``"sunrise"`` or ``"sunset"``.
    time:
        ``"HH:MM"`` in the schedule timezone for ``kind == "clock"``; ``None``
        for the solar kinds.
    offset_minutes:
        Minutes to shift the fire instant; negative is earlier, positive later.
    enabled:
        A disabled anchor never fires and contributes no wake time.
    """

    id: str
    kind: AnchorKind
    time: str | None
    offset_minutes: int
    enabled: bool


@dataclass(frozen=True)
class AnchorDecision:
    """A due-now decision for one anchor, for the caller to act on.

    The caller (which owns the durable fire-log and the capture path) turns this
    into a fire-log row and, when appropriate, a capture. This record carries
    only computed facts; no database lookup has happened yet.

    Attributes
    ----------
    anchor:
        The anchor this decision is about.
    local_date:
        The local calendar day (``YYYY-MM-DD`` in the schedule timezone) the
        anchor is firing for. Together with ``anchor.id`` this is the
        once-per-day idempotency key.
    instant:
        The anchor's fire instant (aware UTC), or ``None`` for a ``solar_noon``
        anchor that cannot be resolved (no geolocation, or a math failure).
    within_grace:
        Whether *now* is at or after ``instant`` but no later than ``instant +
        grace``. When ``True`` the caller captures; when ``False`` it records a
        missed fire.
    has_geo:
        ``False`` only for a ``solar_noon`` anchor evaluated without a usable
        latitude/longitude pair; the caller records a "no geolocation" skip.
        Always ``True`` for clock anchors.
    """

    anchor: Anchor
    local_date: str
    instant: datetime | None
    within_grace: bool
    has_geo: bool


def _parse_offset(value: Any, field_path: str) -> int:
    """Parse and bounds-check an anchor's ``offset_minutes`` (default 0)."""
    if value is None or value == "":
        return 0
    if isinstance(value, bool):  # bool is an int subclass; reject it explicitly
        raise ValueError(f"{field_path}: expected an integer, got {value!r}")
    if isinstance(value, int):
        offset = value
    elif isinstance(value, str):
        try:
            offset = int(value.strip())
        except ValueError as exc:
            raise ValueError(
                f"{field_path}: expected an integer, got {value!r}"
            ) from exc
    else:
        raise ValueError(f"{field_path}: expected an integer, got {value!r}")
    if not (-_MAX_OFFSET_MINUTES <= offset <= _MAX_OFFSET_MINUTES):
        raise ValueError(
            f"{field_path}: offset out of range "
            f"(+/-{_MAX_OFFSET_MINUTES} minutes), got {offset}"
        )
    return offset


def _parse_hhmm(value: Any, field_path: str) -> str:
    """Validate a ``"HH:MM"`` clock string and return it normalised."""
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
    return f"{hour:02d}:{minute:02d}"


def parse_anchors(raw: list[Any] | None) -> list[Anchor]:
    """Build a list of :class:`Anchor` from the project's stored JSON list.

    ``None`` or an empty list yields ``[]`` (no anchors), preserving the plain
    interval-only behaviour for projects that never configure an anchor.

    Each item must be an object with a ``kind`` of ``"clock"`` or
    ``"solar_noon"``. A clock anchor requires a valid ``"HH:MM"`` ``time``; a
    solar-noon anchor ignores ``time``. ``offset_minutes`` defaults to ``0`` and
    is bounds-checked. ``enabled`` defaults to ``True``. An ``id`` is generated
    (uuid4 hex) when absent so a freshly added anchor gets a stable identity.

    Raises :class:`ValueError`, naming the offending field, when an item is
    malformed -- so a bad stored value fails loudly at parse time rather than
    silently dropping an anchor at fire time.
    """
    if not raw:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"anchors: expected a list, got {raw!r}")

    anchors: list[Anchor] = []
    for index, item in enumerate(raw):
        path = f"anchors[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{path}: expected an object, got {item!r}")

        kind = item.get("kind")
        if kind not in _VALID_KINDS:
            raise ValueError(
                f"{path}.kind: expected one of {_VALID_KINDS}, got {kind!r}"
            )

        if kind == _KIND_CLOCK:
            clock_time: str | None = _parse_hhmm(item.get("time"), f"{path}.time")
        else:
            clock_time = None

        offset = _parse_offset(item.get("offset_minutes"), f"{path}.offset_minutes")
        enabled = bool(item.get("enabled", True))

        raw_id = item.get("id")
        anchor_id = raw_id.strip() if isinstance(raw_id, str) and raw_id.strip() else ""
        if not anchor_id:
            anchor_id = uuid.uuid4().hex

        anchors.append(
            Anchor(
                id=anchor_id,
                kind=kind,
                time=clock_time,
                offset_minutes=offset,
                enabled=enabled,
            )
        )
    return anchors


def serialize_anchor(anchor: Anchor) -> dict[str, Any]:
    """Serialize an :class:`Anchor` back to its stored JSON object shape."""
    return {
        "id": anchor.id,
        "kind": anchor.kind,
        "time": anchor.time,
        "offset_minutes": anchor.offset_minutes,
        "enabled": anchor.enabled,
    }


def anchor_fire_instant(
    anchor: Anchor,
    local_date: date | datetime,
    tz: ZoneInfo,
    latitude: float | None,
    longitude: float | None,
) -> datetime | None:
    """Return the aware-UTC instant *anchor* fires on *local_date*.

    For a clock anchor this is the wall-clock ``time`` on *local_date* in *tz*,
    shifted by ``offset_minutes``. For a solar anchor it is the sun event for the
    camera's location on *local_date* -- solar noon, sunrise or sunset -- shifted
    by ``offset_minutes``.

    Returns ``None`` for a solar anchor when *latitude*/*longitude* is missing (no
    geolocation), or when the sun event does not exist on *local_date* (sunrise or
    sunset during polar day/night, or a math failure) -- the caller distinguishes
    the no-geolocation case via :func:`due_anchors`' ``has_geo`` flag.
    """
    on_date = local_date.date() if isinstance(local_date, datetime) else local_date
    offset = timedelta(minutes=anchor.offset_minutes)

    if anchor.kind == _KIND_CLOCK:
        assert anchor.time is not None  # guaranteed by parse_anchors for clock
        hhmm = _parse_hhmm(anchor.time, "anchor.time")
        hour, minute = (int(part) for part in hhmm.split(":"))
        local_dt = datetime.combine(on_date, time(hour, minute), tzinfo=tz)
        return (local_dt + offset).astimezone(UTC)

    # Solar anchors require a location.
    if latitude is None or longitude is None:
        return None

    if anchor.kind == _KIND_SOLAR_NOON:
        event_utc = compute_solar_noon(latitude, longitude, on_date, tz)
    else:
        sunrise_utc, sunset_utc = compute_sun_times(latitude, longitude, on_date, tz)
        # Sunrise/sunset can be undefined on a given day (polar day/night), in
        # which case there is no fire for that day -- the same None contract as a
        # solar-noon math failure.
        event_utc = sunrise_utc if anchor.kind == _KIND_SUNRISE else sunset_utc

    if event_utc is None:
        return None
    return event_utc + offset


def _effective_tz(anchor: Anchor, tz: ZoneInfo, solar_tz: ZoneInfo | None) -> ZoneInfo:
    """Return the timezone that governs *anchor*.

    Solar-noon anchors are tied to the camera's physical location: when a
    coordinate-derived *solar_tz* is supplied it is the single source of truth
    for both the fire instant and the once-per-day key, so the displayed time and
    the idempotency key can never drift apart across midnight. Clock anchors
    always use the operator-chosen schedule timezone *tz*. With no *solar_tz*
    (coordinates absent or unresolvable) the schedule timezone is used as before.
    """
    if _is_solar(anchor.kind) and solar_tz is not None:
        return solar_tz
    return tz


def _local_date_of(now: datetime, tz: ZoneInfo) -> date:
    """Return the local calendar date of the aware instant *now* in *tz*."""
    return now.astimezone(tz).date()


def _local_date_str(local_date: date) -> str:
    """Format a date as the ``YYYY-MM-DD`` fire-log key."""
    return local_date.isoformat()


def due_anchors(
    anchors: list[Anchor],
    now: datetime,
    tz: ZoneInfo,
    latitude: float | None,
    longitude: float | None,
    grace: timedelta = EXACT_TIME_GRACE,
    solar_tz: ZoneInfo | None = None,
) -> list[AnchorDecision]:
    """Return a decision for every enabled anchor whose fire instant is due.

    An anchor is keyed once per *base local date* -- the calendar day, in the
    anchor's governing timezone, that its ``HH:MM`` (or solar noon) falls on,
    before the offset is applied. The fire instant is that base time shifted by
    ``offset_minutes``, so a large offset may place the instant on a different
    calendar day than the base date; the once-per-day idempotency key stays the
    base date regardless.

    Clock anchors are governed by the schedule timezone *tz*. Solar-noon anchors
    are governed by *solar_tz* -- the camera's coordinate-derived zone -- when it
    is supplied, so the calendar day the fire is keyed to matches the camera's
    real local day; absent *solar_tz* they fall back to *tz*.

    "Due" means the most recent not-yet-future base date whose fire instant is at
    or before *now*. The returned :class:`AnchorDecision` records whether *now* is
    still within the grace window (``now <= instant + grace``) so the caller can
    capture, or whether the fire was missed. Base dates yesterday/today/tomorrow
    are scanned so a midnight-crossing offset (a base time late in the day shifted
    forward, or early shifted back) still fires for the correct date exactly once.

    A solar-noon anchor without a usable location yields a decision with
    ``has_geo=False`` and ``instant=None`` for today's base date, so the caller
    records a "no geolocation" skip once per day rather than silently dropping the
    anchor. Disabled anchors are skipped entirely.
    """
    decisions: list[AnchorDecision] = []

    for anchor in anchors:
        if not anchor.enabled:
            continue

        eff_tz = _effective_tz(anchor, tz, solar_tz)
        today = _local_date_of(now, eff_tz)

        if _is_solar(anchor.kind) and (latitude is None or longitude is None):
            # No location: a solar anchor cannot be computed. Report a
            # no-geo decision for today's base date so the caller records the
            # skip once per day instead of silently ignoring the anchor.
            decisions.append(
                AnchorDecision(
                    anchor=anchor,
                    local_date=_local_date_str(today),
                    instant=None,
                    within_grace=False,
                    has_geo=False,
                )
            )
            continue

        # Find the most recent base date whose instant is already due. The base
        # dates span tomorrow..yesterday because an offset can place an instant on
        # an adjacent calendar day: a negative offset pulls a base time back into
        # the previous day, a positive one pushes it into the next. Picking the
        # latest due instant gives the correct once-per-day fire for the base
        # date that owns the operator's configured time.
        due_instant: datetime | None = None
        due_base: date | None = None
        for base in (
            today + timedelta(days=1),
            today,
            today - timedelta(days=1),
        ):
            instant = anchor_fire_instant(anchor, base, eff_tz, latitude, longitude)
            if instant is None or instant > now:
                continue
            # Keep the most recent due instant across the candidate base dates.
            if due_instant is None or instant > due_instant:
                due_instant = instant
                due_base = base

        if due_instant is None or due_base is None:
            continue  # nothing due yet for this anchor

        within_grace = now <= due_instant + grace
        decisions.append(
            AnchorDecision(
                anchor=anchor,
                local_date=_local_date_str(due_base),
                instant=due_instant,
                within_grace=within_grace,
                has_geo=True,
            )
        )
    return decisions


def next_anchor_wake(
    anchors: list[Anchor],
    now: datetime,
    tz: ZoneInfo,
    latitude: float | None,
    longitude: float | None,
    solar_tz: ZoneInfo | None = None,
) -> datetime | None:
    """Return the earliest future fire instant across all enabled anchors.

    Considers each enabled anchor's instant for the base local dates spanning
    *now* (yesterday through tomorrow, so a midnight-crossing offset is covered)
    and returns the soonest instant strictly after *now* so the runner can sleep
    until it. Clock anchors use the schedule timezone *tz*; solar anchors (noon,
    sunrise, sunset) use the camera's coordinate-derived *solar_tz* when supplied
    (else *tz*). A solar anchor without a usable location contributes no wake (it
    cannot be computed). Returns ``None`` when no enabled anchor has a future fire
    instant within that span.
    """
    soonest: datetime | None = None
    for anchor in anchors:
        if not anchor.enabled:
            continue
        eff_tz = _effective_tz(anchor, tz, solar_tz)
        today = _local_date_of(now, eff_tz)
        for base in (today - timedelta(days=1), today, today + timedelta(days=1)):
            instant = anchor_fire_instant(anchor, base, eff_tz, latitude, longitude)
            if instant is None or instant <= now:
                continue
            if soonest is None or instant < soonest:
                soonest = instant
    return soonest


def next_solar_capture_instant(
    anchor: Anchor,
    now: datetime,
    latitude: float | None,
    longitude: float | None,
) -> datetime | None:
    """Return the soonest future fire instant for one solar *anchor*.

    Works for any sun-derived anchor (solar noon, sunrise or sunset). Computed in
    the camera's coordinate-derived timezone -- the same governing zone used by
    :func:`due_anchors` -- so a previewed "upcoming capture time" matches what will
    actually fire. Returns ``None`` when *anchor* is not a solar anchor, is
    disabled, the coordinates are missing/unresolvable, or no instance of the event
    falls in the scanned span (e.g. polar day/night). The returned instant is aware
    UTC; render it in the resolved zone for display.
    """
    if not _is_solar(anchor.kind) or not anchor.enabled:
        return None
    tz = resolve_zoneinfo(latitude, longitude)
    if tz is None:
        return None
    today = _local_date_of(now, tz)

    soonest: datetime | None = None
    for base in (today - timedelta(days=1), today, today + timedelta(days=1)):
        instant = anchor_fire_instant(anchor, base, tz, latitude, longitude)
        if instant is None or instant <= now:
            continue
        if soonest is None or instant < soonest:
            soonest = instant
    return soonest
