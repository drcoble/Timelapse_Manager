"""Cross-cutting helpers shared across the web routers: the running settings,
audit-event writes, project lookup, form-field parsing, camera-probe
enumeration, and the LDAP/settings page context."""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import urllib.parse
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session as DbSession

from ...cameras.base import PTZPresetsResult, StreamProfileResult
from ...capture import anchors as anchors_mod
from ...capture import event_triggers as event_triggers_mod
from ...capture import geo
from ...capture.schedule import parse_schedule
from ...config import Settings
from ...db.models import Camera, Event, Project
from ...render import settings as render_settings
from ...runtime import get_context
from ...security.camera_defaults_service import (
    load_settings as load_camera_defaults,
)
from ...security.camera_defaults_service import (
    resolve_default_credentials,
)
from ...security.ldap_directory import (
    LdapOutcome,
)
from ...security.ldap_settings_service import load_settings as load_ldap_settings
from ...security.ssrf_settings_service import load_settings as load_ssrf_settings
from .. import dependencies as deps
from ..dependencies import (
    AdminUser,
    DbDep,
)

logger = logging.getLogger(__name__)


def _settings() -> Settings:
    """Return the running process settings."""
    return get_context().settings


def _audit(
    db: DbSession,
    *,
    scope: str,
    scope_id: int | None,
    actor_user_id: int,
    message: str,
    level: str = "info",
) -> None:
    """Write an audit event attributed to the acting user. Never logs secrets."""
    db.add(
        Event(
            scope=scope,
            scope_id=scope_id,
            level=level,
            message=message,
            actor_user_id=actor_user_id,
            timestamp=datetime.datetime.now(datetime.UTC).replace(tzinfo=None),
        )
    )


def _parse_optional_json_field(raw: str | None, label: str) -> tuple[Any, str | None]:
    """Parse a textarea's JSON, or return ``(None, error)``.

    An empty/blank field clears the config (``(None, None)``); any other value is
    parsed as JSON, with a friendly per-field message on a syntax error.
    """
    text = (raw or "").strip()
    if not text:
        return None, None
    try:
        return json.loads(text), None
    except (ValueError, TypeError):
        return None, f"{label}: invalid JSON."


def _schedule_field_error(value: Any, label: str) -> str | None:
    """Return a validation error for a schedule config, or ``None`` if valid.

    A schedule must be a JSON object; an *enabled* schedule must carry a positive
    ``interval_seconds`` (mirroring the API's schedule validation).
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        return f"{label} must be a JSON object."
    if value.get("enabled"):
        try:
            interval = float(value.get("interval_seconds"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            interval = 0.0
        if interval <= 0:
            return f"{label}: an enabled schedule needs a positive interval_seconds."
    return None


def _post_actions_field_error(value: Any) -> str | None:
    """Return a validation error for a post-render-action list, or ``None``.

    Must be a JSON list whose every element is an object with a non-empty
    ``type`` (mirroring the API's post-action validation).
    """
    if value is None:
        return None
    if not isinstance(value, list):
        return "Post-render actions must be a JSON list."
    for action in value:
        if not isinstance(action, dict) or not str(action.get("type") or "").strip():
            return "Each post-render action must be an object with a 'type'."
    return None


def _parse_render_fps_field(form: Any) -> tuple[int | None, str | None]:
    """Read and validate the frame-rate field, or return a friendly error.

    A present ``render_fps`` must be a whole number in the accepted range; an
    out-of-range or non-integer value is rejected here rather than silently
    clamped by the tolerant view, so a bad frame rate re-renders the form with an
    error instead of quietly reverting. When the field is absent (e.g. a form with
    no render UI) the result is ``(None, None)`` so the caller keeps the default.
    """
    raw = form.get("render_fps")
    if raw is None:
        return None, None
    text = str(raw).strip()
    if not text:
        return None, None
    try:
        value = int(text)
    except ValueError:
        return None, (
            f"Frame rate must be a whole number between "
            f"{render_settings.MIN_FPS} and {render_settings.MAX_FPS}."
        )
    if value < render_settings.MIN_FPS or value > render_settings.MAX_FPS:
        return None, (
            f"Frame rate must be between "
            f"{render_settings.MIN_FPS} and {render_settings.MAX_FPS}."
        )
    return value, None


def _parse_render_settings_field(
    form: Any,
) -> tuple[dict[str, Any], str | None]:
    """Read the edit form's render-settings dropdowns into a stored schedule.

    Returns ``(schedule_dict, None)`` on success or ``({}, error)`` when the
    chosen encoder/container combination cannot be muxed, or the frame rate is out
    of range. Each control falls back to its default when absent or unrecognised
    (so a create form with no render UI, or a tampered value, yields a safe
    disabled-default schedule rather than an error). The combination is validated
    with the same shared rule the live combo-check endpoint uses, so the client
    warning and this refusal agree.

    Auto-prune is read from the ``render_autoprune`` checkbox. Because a checkbox
    submits nothing when unticked, an always-present hidden ``render_autoprune_present``
    marker accompanies it on the form so a deliberate "off" can be told apart from
    a form that simply carries no auto-prune control at all (the create form). With
    the marker present, an absent checkbox means off; without the marker the stored
    default (enabled) is kept, so a create has its auto-prune default preserved.
    """
    fps_value, fps_error = _parse_render_fps_field(form)
    if fps_error is not None:
        return {}, fps_error

    if form.get("render_autoprune_present"):
        auto_prune = form.get("render_autoprune") is not None
    else:
        auto_prune = render_settings.DEFAULT_AUTO_PRUNE

    view = render_settings.render_settings_view(
        {
            "enabled": form.get("render_enabled") is not None,
            "interval_seconds": form.get("render_frequency"),
            "encoder": form.get("render_encoder"),
            "container": form.get("render_container"),
            "fps": fps_value,
            "resolution": form.get("render_resolution"),
            render_settings.AUTO_PRUNE_KEY: auto_prune,
        }
    )
    warning = render_settings.combination_warning(view["encoder"], view["container"])
    if warning is not None:
        return {}, warning
    return view, None


def _parse_optional_datetime(
    raw: str | None, label: str
) -> tuple[datetime.datetime | None, str | None]:
    """Parse a ``datetime-local`` field as a naive datetime, or return an error.

    A blank field clears the bound (``(None, None)``). A value is parsed with
    :func:`datetime.datetime.fromisoformat`, which accepts the widget's
    ``YYYY-MM-DDTHH:MM`` (and ``...:SS``) form; the result is stored as-is
    (treated as UTC, matching the rest of the app). A malformed value yields a
    friendly per-field message.
    """
    text = (raw or "").strip()
    if not text:
        return None, None
    try:
        return datetime.datetime.fromisoformat(text), None
    except ValueError:
        return None, f"{label}: enter a valid date and time."


def _parse_optional_positive_int(
    raw: str | None, label: str
) -> tuple[int | None, str | None]:
    """Parse an optional positive integer field, or return an error.

    A blank field clears the value (``(None, None)``); any other value must parse
    as an integer greater than zero, else a friendly per-field message.
    """
    text = (raw or "").strip()
    if not text:
        return None, None
    try:
        value = int(text)
    except ValueError:
        return None, f"{label} must be a whole number."
    if value <= 0:
        return None, f"{label} must be greater than zero."
    return value, None


def _parse_optional_int(raw: str | None, label: str) -> tuple[int | None, str | None]:
    """Parse an optional integer field, or return an error.

    A blank field clears the value (``(None, None)``); any other value must parse
    as an integer (negative and zero allowed -- ranges are the caller's concern,
    e.g. a sun offset may be negative for "minutes before"), else a friendly
    per-field message.
    """
    text = (raw or "").strip()
    if not text:
        return None, None
    try:
        return int(text), None
    except ValueError:
        return None, f"{label} must be a whole number."


def _parse_optional_float(
    raw: str | None, label: str
) -> tuple[float | None, str | None]:
    """Parse an optional float field, or return an error.

    A blank field clears the value (``(None, None)``); any other value must parse
    as a float (negative and zero allowed -- ranges are the caller's concern),
    else a friendly per-field message.
    """
    text = (raw or "").strip()
    if not text:
        return None, None
    try:
        return float(text), None
    except ValueError:
        return None, f"{label} must be a number."


def _parse_coordinate(
    raw: str | None, label: str, *, limit: float
) -> tuple[float | None, str | None]:
    """Parse an optional latitude/longitude, bounded to ``[-limit, +limit]``.

    Blank clears the value (``(None, None)``); a non-numeric or out-of-range value
    yields a friendly per-field message.
    """
    value, err = _parse_optional_float(raw, label)
    if err is not None:
        return None, err
    if value is not None and not (-limit <= value <= limit):
        return None, f"{label} must be between {-limit:g} and {limit:g}."
    return value, None


def _hostname_source(form: Any) -> str:
    """Read the submitted device-hostname source, defaulting to ``"manual"``.

    Mirrors how the geolocation source is read: only the two known values are
    honoured, and anything else (or an absent field) reads as operator-entered.
    The form sets this to ``"camera"`` when the value was filled from a camera
    query, so a typed value records as ``"manual"``.
    """
    return (
        form.get("device_hostname_source")
        if (form.get("device_hostname_source") in ("camera", "manual"))
        else "manual"
    )


def _parse_ptz_fields(
    form: Any,
) -> tuple[str | None, float | None, float | None, float | None, str | None]:
    """Read a project's PTZ selection from the form.

    Returns ``(preset, pan, tilt, zoom, error)``. The preset id is taken verbatim
    (blank -> ``None``); pan/tilt/zoom each parse as an optional float (blank ->
    ``None``, ``0.0`` is a legitimate value, negatives allowed) with ranges left
    to the camera. On the first non-numeric position field the parsed values are
    discarded and a friendly message is returned in the last slot.
    """
    preset = (form.get("ptz_preset_id") or "").strip() or None
    pan, err = _parse_optional_float(form.get("ptz_pan"), "Pan")
    if err is not None:
        return None, None, None, None, err
    tilt, err = _parse_optional_float(form.get("ptz_tilt"), "Tilt")
    if err is not None:
        return None, None, None, None, err
    zoom, err = _parse_optional_float(form.get("ptz_zoom"), "Zoom")
    if err is not None:
        return None, None, None, None, err
    return preset, pan, tilt, zoom, None


def _campaign_bounds_error(
    start_date: datetime.datetime | None, end_date: datetime.datetime | None
) -> str | None:
    """Return an error if both dates are set and end is not after start."""
    if start_date is not None and end_date is not None and end_date <= start_date:
        return "End date must be after the start date."
    return None


# The capture-schedule form's day checkboxes, in the order their bit lives in the
# evaluator's ``day_of_week_mask``: bit 0 is Monday ... bit 6 is Sunday (matching
# :meth:`datetime.date.weekday`). Index in this tuple == bit position.
_SCHEDULE_DAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Monday-through-Friday mask (bits 0..4 set): 0b0011111 == 31.
_BUSINESS_DAY_MASK = 0b0011111
# Every weekday allowed.
_FULL_WEEK_MASK = 0b1111111

# A curated, ordered set of IANA timezones for the schedule picker. A full
# ``zoneinfo.available_timezones()`` is hundreds of entries -- overwhelming in a
# dropdown -- so this common subset (UTC plus the busiest US and EU zones) covers
# the overwhelming majority of deployments while staying scannable. The stored
# value is a free-form IANA string, so an operator is never limited to this list
# (the API and a future typed field accept any zone the system knows).
COMMON_TIMEZONES: tuple[str, ...] = (
    "UTC",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Anchorage",
    "Pacific/Honolulu",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Europe/Madrid",
    "Europe/Rome",
    "Europe/Amsterdam",
    "Europe/Athens",
    "Europe/Moscow",
)


async def _form_getlist(request: Request, key: str) -> list[str]:
    """Return every value submitted for a repeated form field.

    The shared form parser collapses repeated keys to a single value (last wins),
    which loses repeated checkboxes like ``capture_days``. Starlette caches the
    raw request body after the first read, so re-reading it here and re-parsing
    only reads that cache -- it does not re-consume the stream -- and yields all
    values for *key*. A non-urlencoded or empty body yields an empty list.
    """
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("application/x-www-form-urlencoded"):
        return []
    body = await request.body()
    if not body:
        return []
    pairs = urllib.parse.parse_qsl(body.decode("utf-8"))
    return [value for name, value in pairs if name == key]


def _days_to_mask(days: list[str]) -> int:
    """Build the evaluator's weekday bitmask from the checked day names.

    Unknown names are ignored; an empty selection yields ``0`` (no day allowed),
    which a caller may treat as "select at least one day" rather than silently
    capturing on no day at all.
    """
    mask = 0
    for day in days:
        if day in _SCHEDULE_DAYS:
            mask |= 1 << _SCHEDULE_DAYS.index(day)
    return mask


def _mask_to_days(mask: int) -> list[str]:
    """Inverse of :func:`_days_to_mask`: list the day names a mask allows."""
    return [name for bit, name in enumerate(_SCHEDULE_DAYS) if (mask >> bit) & 1]


def _build_schedule_from_form(
    form: Any, days: list[str]
) -> tuple[dict[str, Any] | None, str | None]:
    """Map the capture-schedule form fields to a stored schedule dict.

    Returns ``(schedule_json, None)`` on success or ``(None, error)`` with a
    friendly inline message when the chosen preset's inputs are invalid (verified
    by parsing the result through the evaluator, so the UI and the evaluator agree
    on what is well-formed).

    The repeated ``capture_days`` checkbox cannot be read from ``form`` (the
    project's form parser collapses repeated keys to the last value), so the
    handler reads the full list from the request body and passes it in as *days*.

    Preset -> schedule mapping:

    * ``always``  -> a timezone-only schedule (always-open gate).
    * ``business``-> 09:00-17:00 on Monday through Friday.
    * ``sun``     -> a sunrise-to-sunset window shifted by the given offsets.
    * ``noon``    -> 12:00-12:30 every day.
    * ``custom``  -> the given window on the selected days.

    Every result carries the chosen IANA timezone.
    """
    preset = (form.get("capture_schedule_preset") or "always").strip()
    timezone = (form.get("capture_schedule_timezone") or "UTC").strip() or "UTC"

    schedule: dict[str, Any] = {"enabled": True, "timezone": timezone}

    if preset == "always":
        # No windows, no mask restriction: an always-open gate that still records
        # the operator's chosen timezone for when they switch to a timed preset.
        pass
    elif preset == "business":
        schedule["windows"] = [{"start_time": "09:00", "end_time": "17:00"}]
        schedule["day_of_week_mask"] = _BUSINESS_DAY_MASK
    elif preset == "noon":
        schedule["windows"] = [{"start_time": "12:00", "end_time": "12:30"}]
        schedule["day_of_week_mask"] = _FULL_WEEK_MASK
    elif preset == "sun":
        start_offset, err = _parse_optional_int(
            form.get("sun_offset_start_min"), "Sunrise offset"
        )
        if err is not None:
            return None, err
        end_offset, err = _parse_optional_int(
            form.get("sun_offset_end_min"), "Sunset offset"
        )
        if err is not None:
            return None, err
        schedule["sun_window"] = [
            {"anchor": "sunrise", "offset_minutes": start_offset or 0},
            {"anchor": "sunset", "offset_minutes": end_offset or 0},
        ]
    elif preset == "custom":
        window_start = (form.get("capture_window_start") or "").strip()
        window_end = (form.get("capture_window_end") or "").strip()
        if not window_start or not window_end:
            return None, "Set both a start and end time for the capture window."
        # The form's documented contract treats "no day checked" as "every day"
        # (the checkboxes are an allow-list, and leaving them blank means no
        # restriction) rather than "capture on no day".
        mask = _days_to_mask(days) or _FULL_WEEK_MASK
        schedule["windows"] = [{"start_time": window_start, "end_time": window_end}]
        schedule["day_of_week_mask"] = mask
    else:
        return None, f"Unknown schedule preset {preset!r}."

    # Validate by parsing through the evaluator: a bad HH:MM, unknown timezone or
    # out-of-range mask surfaces here as an inline error instead of a 500 later.
    try:
        parse_schedule(schedule)
    except ValueError as exc:
        return None, f"Capture schedule: {exc}"
    return schedule, None


def _schedule_to_form(schedule: dict[str, Any] | None) -> dict[str, Any]:
    """Reconstruct the schedule form's fields from a stored schedule dict.

    Reverse of :func:`_build_schedule_from_form`: derives ``preset`` by matching
    the stored shape against the known presets, falling back to ``custom`` for
    any shape the presets do not cover (e.g. a hand-edited window). The returned
    dict carries every field the template reads -- ``preset``, ``timezone``,
    ``window_start``/``window_end``, ``days`` (a list), and the two sun offsets --
    so a blank/absent value is always present rather than undefined.
    """
    schedule = schedule or {}
    timezone = schedule.get("timezone") or "UTC"
    windows = schedule.get("windows") or []
    mask = schedule.get("day_of_week_mask", _FULL_WEEK_MASK)
    sun_window = schedule.get("sun_window")

    context: dict[str, Any] = {
        "preset": "always",
        "timezone": timezone,
        "window_start": "",
        "window_end": "",
        "days": _mask_to_days(mask if isinstance(mask, int) else _FULL_WEEK_MASK),
        "sun_offset_start_min": 0,
        "sun_offset_end_min": 0,
    }

    first_window = windows[0] if windows else None
    start_time = first_window.get("start_time") if first_window else None
    end_time = first_window.get("end_time") if first_window else None
    if first_window:
        context["window_start"] = start_time or ""
        context["window_end"] = end_time or ""

    if sun_window:
        context["preset"] = "sun"
        opener = sun_window[0] if len(sun_window) > 0 else {}
        closer = sun_window[1] if len(sun_window) > 1 else {}
        if isinstance(opener, dict):
            context["sun_offset_start_min"] = opener.get("offset_minutes", 0)
        if isinstance(closer, dict):
            context["sun_offset_end_min"] = closer.get("offset_minutes", 0)
    elif not windows and mask == _FULL_WEEK_MASK:
        context["preset"] = "always"
    elif (
        len(windows) == 1
        and start_time == "09:00"
        and end_time == "17:00"
        and mask == _BUSINESS_DAY_MASK
    ):
        context["preset"] = "business"
    elif (
        len(windows) == 1
        and start_time == "12:00"
        and end_time == "12:30"
        and mask == _FULL_WEEK_MASK
    ):
        context["preset"] = "noon"
    else:
        context["preset"] = "custom"

    return context


# ---------------------------------------------------------------------------
# Exact-time anchor form helpers
#
# Anchors live in their own ``project.exact_time_anchors`` JSON column (NOT inside
# ``project.schedule``), so these helpers are independent of the schedule
# form-builder above. The form sends a repeatable list of rows, one entry per row
# across these parallel repeated fields, read from the raw body via
# ``_form_getlist`` (the form parser collapses repeated keys to the last value):
#
#   anchor_id[]      hidden stable id for an existing anchor; blank for a new row
#                    (a fresh id is generated). Always one per row.
#   anchor_kind[]    "clock" or "solar_noon". Always one per row.
#   anchor_time[]    "HH:MM" for a clock row; blank/ignored for solar_noon.
#                    Always one per row (keeps the lists index-aligned).
#   anchor_offset[]  optional signed integer minutes; blank means 0.
#                    Always one per row.
#   anchor_enabled[] a checkbox whose *value* is the row's id (or, for a new row
#                    with no id yet, its zero-based row index as "new:<n>"); an
#                    unchecked row simply omits its value. Read by membership, not
#                    position, so an unchecked row never misaligns the others.
#
# A presence marker ``exact_time_present`` (any value) tells the persistence layer
# the fieldset was on the form, so a POST without it leaves the stored anchors
# untouched (mirroring the schedule fieldset's marker).
# ---------------------------------------------------------------------------

EXACT_TIME_MARKER = "exact_time_present"


async def _build_exact_time_anchors_from_form(
    request: Request, camera: Camera | None
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Map the exact-time anchor form rows to a stored anchor list.

    Returns ``(anchors_json, None)`` on success or ``(None, error)`` with a
    friendly inline message when a row is invalid (bad ``HH:MM`` clock, bad
    offset, or a solar-noon anchor selected for a camera that has no
    geolocation). The repeated row fields are read from the raw request body via
    :func:`_form_getlist`.

    *camera* is the camera the project will be bound to (the submitted camera, not
    necessarily the previously-bound one); its geolocation gates whether a
    solar-noon anchor may be saved. An empty fieldset (no rows) yields ``([],
    None)`` -- the project simply has no anchors.
    """
    ids = await _form_getlist(request, "anchor_id")
    kinds = await _form_getlist(request, "anchor_kind")
    times = await _form_getlist(request, "anchor_time")
    offsets = await _form_getlist(request, "anchor_offset")
    enabled_values = set(await _form_getlist(request, "anchor_enabled"))

    row_count = max(len(ids), len(kinds), len(times), len(offsets))
    has_geo = (
        camera is not None
        and camera.geolocation_latitude is not None
        and camera.geolocation_longitude is not None
    )

    raw_anchors: list[dict[str, Any]] = []
    for index in range(row_count):
        kind = (kinds[index] if index < len(kinds) else "").strip()
        if not kind:
            # A blank row (e.g. a template row never filled in) is skipped.
            continue

        anchor_id = (ids[index] if index < len(ids) else "").strip()
        clock_time = (times[index] if index < len(times) else "").strip()
        offset_raw = (offsets[index] if index < len(offsets) else "").strip()

        # A row is enabled when its identity is in the checked set; an existing
        # row identifies by its id, a new row by its "new:<index>" token.
        identity = anchor_id or f"new:{index}"
        enabled = identity in enabled_values

        if kind == "clock" and not clock_time:
            return None, "Set a time (HH:MM) for each exact-time clock anchor."
        if kind in ("solar_noon", "sunrise", "sunset") and not has_geo:
            return None, (
                "Solar anchors (solar noon, sunrise, sunset) need the camera's "
                "location; set the camera's geolocation or remove the solar anchor."
            )

        offset, err = _parse_optional_int(offset_raw, "Anchor offset")
        if err is not None:
            return None, err

        anchor: dict[str, Any] = {
            "kind": kind,
            "offset_minutes": offset or 0,
            "enabled": enabled,
        }
        if anchor_id:
            anchor["id"] = anchor_id
        if kind == "clock":
            anchor["time"] = clock_time
        raw_anchors.append(anchor)

    # Validate (and normalise + generate ids) through the pure parser, so the UI
    # and the runtime agree on what is well-formed and every saved anchor has a
    # stable id.
    try:
        parsed = anchors_mod.parse_anchors(raw_anchors)
    except ValueError as exc:
        return None, f"Exact-time anchors: {exc}"
    return [anchors_mod.serialize_anchor(a) for a in parsed], None


def _exact_time_anchors_to_form(
    anchors: list[Any] | None,
) -> list[dict[str, Any]]:
    """Reconstruct the anchor form rows from the stored anchor list.

    Returns one dict per anchor carrying every field the template reads --
    ``id``, ``kind``, ``time`` (``""`` for solar_noon), ``offset_minutes`` and
    ``enabled`` -- so a blank/absent value is always present rather than
    undefined. A malformed stored value is tolerated (skipped) so the edit form
    still renders.
    """
    try:
        parsed = anchors_mod.parse_anchors(anchors)
    except ValueError:
        return []
    return [
        {
            "id": anchor.id,
            "kind": anchor.kind,
            "time": anchor.time or "",
            "offset_minutes": anchor.offset_minutes,
            "enabled": anchor.enabled,
        }
        for anchor in parsed
    ]


def capture_mode_of(form: Any) -> str:
    """Read the submitted capture mode from a form, defaulting to ``"interval"``.

    Returns ``"solar"`` for the solar / scheduled-times-only mode (no recurring
    interval) or ``"interval"`` otherwise. Tolerates a missing or non-string
    field (e.g. an upload part) so it never raises on a malformed submission.
    """
    value = form.get("capture_mode")
    return (
        "solar" if isinstance(value, str) and value.strip() == "solar" else "interval"
    )


def _camera_has_geolocation(camera: Camera | None) -> bool:
    """Whether *camera* carries a usable latitude/longitude pair for solar noon."""
    return (
        camera is not None
        and camera.geolocation_latitude is not None
        and camera.geolocation_longitude is not None
    )


_SOLAR_KIND_LABELS = {
    "solar_noon": "Solar noon",
    "sunrise": "Sunrise",
    "sunset": "Sunset",
}


def _solar_preview_item(
    anchor: anchors_mod.Anchor,
    instant: datetime.datetime | None,
    tz: Any,
    label: str | None = None,
) -> dict[str, Any]:
    """Render one upcoming solar-capture row, formatted in the camera's zone."""
    if label is None:
        base = _SOLAR_KIND_LABELS.get(anchor.kind, "Solar")
        offset = anchor.offset_minutes
        label = f"{base} {offset:+d} min" if offset else base
    when = instant.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z") if instant else None
    return {"label": label, "when": when}


def build_solar_preview(
    camera: Camera | None,
    stored_anchors: list[Any] | None,
    *,
    now: datetime.datetime | None = None,
) -> dict[str, Any] | None:
    """Build the solar-capture preview shown alongside the exact-time fieldset.

    Verifies the camera's coordinates and computes the upcoming solar capture
    time(s) rendered in the camera's own coordinate-derived timezone -- so the
    operator sees exactly when, in local time at the camera, the next solar frame
    will be taken, and is warned when the coordinates are missing, out of range,
    or only approximate (open water).

    Returns ``None`` when there is no camera to evaluate (e.g. the new-project
    form before a camera is chosen), so the template renders nothing.
    """
    if camera is None:
        return None

    lat = camera.geolocation_latitude
    lon = camera.geolocation_longitude
    check = geo.validate_coordinates(lat, lon)
    tz = geo.resolve_zoneinfo(lat, lon)
    now = now or datetime.datetime.now(datetime.UTC)

    items: list[dict[str, Any]] = []
    if check.ok and tz is not None:
        try:
            anchors = anchors_mod.parse_anchors(stored_anchors)
        except ValueError:
            anchors = []
        solar = [a for a in anchors if a.kind in _SOLAR_KIND_LABELS and a.enabled]
        if solar:
            for anchor in solar:
                instant = anchors_mod.next_solar_capture_instant(anchor, now, lat, lon)
                items.append(_solar_preview_item(anchor, instant, tz))
        else:
            # No saved enabled solar anchor yet: preview plain solar noon so the
            # operator can see what one would capture before adding it.
            base = anchors_mod.Anchor(
                id="preview",
                kind="solar_noon",
                time=None,
                offset_minutes=0,
                enabled=True,
            )
            instant = anchors_mod.next_solar_capture_instant(base, now, lat, lon)
            items.append(_solar_preview_item(base, instant, tz, label="Solar noon"))

    return {
        "validation": {
            "ok": check.ok,
            "code": check.code,
            "message": check.message,
            "timezone": check.timezone,
            "approximate": check.approximate,
        },
        "tz_name": check.timezone,
        "captures": items,
    }


def build_solar_preview_from_rows(
    camera: Camera | None,
    kinds: list[str],
    offsets: list[str],
    ids: list[str],
    enabled_values: set[str],
    *,
    now: datetime.datetime | None = None,
) -> dict[str, Any] | None:
    """Build a solar preview from raw (unsaved) exact-time form rows.

    Used by the live HTMX preview so the upcoming time reflects edits the
    operator has made to solar-anchor offsets before saving. Only enabled
    solar-noon rows contribute; a blank/invalid offset is treated as zero so a
    half-typed value never breaks the preview.
    """
    rows: list[dict[str, Any]] = []
    for index, kind in enumerate(kinds):
        kind = kind.strip()
        if kind not in _SOLAR_KIND_LABELS:
            continue
        anchor_id = ids[index].strip() if index < len(ids) else ""
        identity = anchor_id or f"new:{index}"
        if identity not in enabled_values:
            continue
        offset_raw = offsets[index].strip() if index < len(offsets) else ""
        try:
            offset = int(offset_raw) if offset_raw else 0
        except ValueError:
            offset = 0
        rows.append({"kind": kind, "offset_minutes": offset, "enabled": True})
    return build_solar_preview(camera, rows, now=now)


# ---------------------------------------------------------------------------
# Event-trigger form helpers
#
# Triggers live in their own ``project.event_triggers`` JSON column. The form
# sends a repeatable list of rows, one entry per row across these parallel
# repeated fields, read from the raw body via ``_form_getlist`` (the form parser
# collapses repeated keys to the last value):
#
#   trigger_id        hidden stable id for an existing trigger; blank for a new
#                     row (a fresh id is generated). Always one per row.
#   trigger_topic     the canonical topic id (the row's <select> value); a blank
#                     topic marks an unfilled row, which is skipped.
#   trigger_cooldown  optional non-negative integer seconds; blank means the
#                     default (10s). Always one per row.
#   trigger_enabled   a checkbox whose *value* is the row's identity -- its id
#                     (or, for a new row with no id yet, its zero-based row index
#                     as "new:<n>"); an unchecked row simply omits its value.
#                     Read by membership, not position, so an unchecked row never
#                     misaligns the others.
#
# A presence marker ``event_triggers_present`` (any value) tells the persistence
# layer the fieldset was on the form, so a POST without it leaves the stored
# triggers untouched (mirroring the schedule and exact-time fieldsets' markers).
#
# The form carries only the topic, cooldown, and enabled state; the human label
# and category are enrichment, recovered by discovering the camera's events and
# matching on canonical topic id. When discovery fails or a topic is not found, a
# prior trigger's label/category (matched by topic id) is carried forward so a
# transient probe failure does not blank the saved labels.
# ---------------------------------------------------------------------------

EVENT_TRIGGERS_MARKER = "event_triggers_present"

# Cooldown applied when a trigger row leaves the cooldown field blank.
_DEFAULT_TRIGGER_COOLDOWN_SECONDS = 10

# Hard ceiling on a synchronous camera probe (event topics, stream profiles, PTZ
# presets) performed while rendering a page. Without it a probe waits the full
# per-request TCP timeout against an unreachable camera -- tens of seconds during
# which the request holds its database connection, starving the concurrent HTMX
# pollers (which then fail). Event discovery is the worst case (ONVIF makes
# sequential SOAP round-trips). A reachable camera answers in well under a
# second; an unreachable one fails fast to the inline "unreachable" notice.
_CAMERA_PROBE_TIMEOUT_SECONDS = 4.0


@dataclass
class EventTopicsResult:
    """Outcome of a best-effort camera event-topic probe.

    Mirrors :class:`~timelapse_manager.cameras.base.StreamProfileResult`: carries
    the discovered events plus a clean reachable/empty signal so a caller renders
    an inline notice instead of catching an exception.

    :param events: the discovered topics, each a small dict the template reads
        (``topic_id``, ``label``, ``category``, ``stateful``, ``protocol``,
        ``requires_app``); empty when ``ok`` is False, and may also be empty when
        the camera was reached but exposes (or the probe could not surface) no
        events.
    :param ok: True when the probe completed (a supervisor was available, the
        address passed the SSRF guard, and the adapter was built and queried
        without an unexpected error); False otherwise.
    :param message: a short human-readable explanation (safe to surface), e.g.
        "no events available" on an empty success or a failure note, else None.
    """

    events: list[dict[str, Any]]
    ok: bool = True
    message: str | None = None


async def _build_event_triggers_from_form(
    request: Request, camera: Camera | None, db: DbSession, existing: Any = None
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Map the event-trigger form rows to a stored trigger list.

    Returns ``(triggers_json, None)`` on success or ``(None, error)`` with a
    friendly inline message when a row is invalid (e.g. a negative cooldown). The
    repeated row fields are read from the raw request body via
    :func:`_form_getlist`.

    Each row with a non-blank ``trigger_topic`` becomes a trigger. The label and
    category are enrichment: the camera's events are discovered best-effort (via
    :func:`_enumerate_event_topics`) and matched on canonical topic id; when
    discovery fails or a topic is not found, the matching trigger in *existing*
    (the project's currently-stored triggers) carries its label/category forward,
    so a transient probe failure never blanks the saved labels. An empty fieldset
    (no rows) yields ``([], None)`` -- the project simply has no triggers.
    """
    ids = await _form_getlist(request, "trigger_id")
    topics = await _form_getlist(request, "trigger_topic")
    cooldowns = await _form_getlist(request, "trigger_cooldown")
    enabled_values = set(await _form_getlist(request, "trigger_enabled"))

    row_count = max(len(ids), len(topics), len(cooldowns))

    # Discover the camera's events once so each row's topic can be enriched with a
    # label/category; a failed probe simply leaves the enrichment to the
    # carry-forward map below.
    discovered: dict[str, dict[str, Any]] = {}
    if camera is not None:
        probe = await _enumerate_event_topics(db, camera)
        for event in probe.events:
            topic_id = event.get("topic_id")
            if isinstance(topic_id, str) and topic_id:
                discovered[topic_id] = event

    # Prior label/category keyed by canonical topic id, used as a fallback when
    # discovery did not surface the topic.
    prior: dict[str, dict[str, str]] = {}
    try:
        for trigger in event_triggers_mod.parse_triggers(existing):
            prior[trigger.topic_id] = {
                "label": trigger.label,
                "category": trigger.category,
            }
    except ValueError:
        prior = {}

    raw_triggers: list[dict[str, Any]] = []
    for index in range(row_count):
        topic = (topics[index] if index < len(topics) else "").strip()
        if not topic:
            # A blank row (e.g. a template row never filled in) is skipped.
            continue

        trigger_id = (ids[index] if index < len(ids) else "").strip()
        cooldown_raw = (cooldowns[index] if index < len(cooldowns) else "").strip()

        # A row is enabled when its identity is in the checked set; an existing row
        # identifies by its id, a new row by its "new:<index>" token.
        identity = trigger_id or f"new:{index}"
        enabled = identity in enabled_values

        cooldown, err = _parse_optional_int(cooldown_raw, "Cooldown")
        if err is not None:
            return None, err
        if cooldown is None:
            cooldown = _DEFAULT_TRIGGER_COOLDOWN_SECONDS

        # Enrich label/category: prefer the live discovery, then the prior stored
        # trigger for this topic, else leave blank.
        found = discovered.get(topic)
        carried = prior.get(topic, {})
        label = (found or {}).get("label") or carried.get("label", "")
        category = (found or {}).get("category") or carried.get("category", "")

        raw_trigger: dict[str, Any] = {
            "topic_id": topic,
            "label": label,
            "category": category,
            "enabled": enabled,
            "cooldown_seconds": cooldown,
        }
        if trigger_id:
            raw_trigger["id"] = trigger_id
        raw_triggers.append(raw_trigger)

    # Validate (and normalise + generate ids) through the pure parser, so the UI
    # and the runtime agree on what is well-formed and every saved trigger has a
    # stable id.
    try:
        parsed = event_triggers_mod.parse_triggers(raw_triggers)
    except ValueError as exc:
        return None, f"Event triggers: {exc}"
    return [event_triggers_mod.serialize_trigger(t) for t in parsed], None


def _event_triggers_to_form(
    triggers: list[Any] | None,
) -> list[dict[str, Any]]:
    """Reconstruct the trigger form rows from the stored trigger list.

    Returns one dict per trigger carrying every field the template reads --
    ``id``, ``topic_id``, ``label``, ``enabled`` and ``cooldown_seconds`` -- so a
    blank/absent value is always present rather than undefined. A malformed stored
    value is tolerated (the whole list is dropped) so the edit form still renders.
    """
    try:
        parsed = event_triggers_mod.parse_triggers(triggers)
    except ValueError:
        return []
    return [
        {
            "id": trigger.id,
            "topic_id": trigger.topic_id,
            "label": trigger.label,
            "enabled": trigger.enabled,
            "cooldown_seconds": trigger.cooldown_seconds,
        }
        for trigger in parsed
    ]


async def _enumerate_event_topics(db: DbSession, camera: Camera) -> EventTopicsResult:
    """Best-effort enumerate a camera's event topics, never raising.

    Mirrors :func:`_enumerate_stream_profiles`: returns an
    :class:`EventTopicsResult` with ``ok=True`` and the events when the camera was
    reached and its topics read, otherwise ``ok=False`` with an empty list.
    *Every* failure mode -- no capture engine, an SSRF-rejected address, an
    invalid adapter configuration, or an unexpected error -- folds into the same
    ``ok=False`` result so the caller renders one inline notice and falls back to
    no enrichment. The camera's address is re-resolved through the SSRF chokepoint
    here (the guard is check-time). The camera row is detached before the probe
    and the adapter is always closed.

    Note: the adapter swallows an ordinary reachability/parse problem into an
    empty list, so an empty success ("no events available") cannot always be told
    apart from an unreachable camera -- both surface as ``ok=True`` with no events.
    """
    from ...cameras import build_adapter, resolve_camera_host
    from ...security.ssrf import SsrfError

    supervisor = get_context().capture_supervisor
    if supervisor is None:
        return EventTopicsResult(events=[], ok=False, message="engine unavailable")

    if camera.address:
        try:
            resolve_camera_host(camera.address)
        except SsrfError:
            return EventTopicsResult(events=[], ok=False, message="address rejected")

    default_credentials = resolve_default_credentials(db)
    db.expunge(camera)
    try:
        adapter = build_adapter(
            camera,
            supervisor.http_client,
            ffmpeg_binary=supervisor.ffmpeg_binary,
            default_credentials=default_credentials,
        )
    except ValueError:
        return EventTopicsResult(events=[], ok=False, message="invalid camera")
    try:
        descriptors = await asyncio.wait_for(
            adapter.list_event_topics(),
            timeout=_CAMERA_PROBE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        # Fail fast rather than hold the request (and its DB connection) for the
        # full per-request network timeout against a slow/unreachable camera.
        return EventTopicsResult(events=[], ok=False, message="unreachable")
    except Exception:  # noqa: BLE001 -- listing must never raise to the caller.
        return EventTopicsResult(events=[], ok=False, message="unreachable")
    finally:
        await adapter.close()

    events = [
        {
            "topic_id": descriptor.topic_id,
            "label": descriptor.label,
            "category": descriptor.category,
            "stateful": descriptor.stateful,
            "protocol": descriptor.protocol,
            "requires_app": descriptor.requires_app,
        }
        for descriptor in descriptors
    ]
    message = None if events else "no events available"
    return EventTopicsResult(events=events, ok=True, message=message)


def _get_project_or_404(db: DbSession, project_id: int) -> Project:
    """Return a project row or raise a 404."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return project


async def _resolve_stream_label(db: DbSession, camera: Camera, stream_id: str) -> str:
    """Resolve a chosen stream id's human label, best-effort, never failing.

    Re-enumerates the camera's streams and returns the matching profile's label.
    When the camera is unreachable at save time, or the chosen id is not among the
    enumerated streams, the id is returned as its own label so the selection is
    still recorded -- a label lookup must never block the save.
    """
    result = await _enumerate_stream_profiles(db, camera)
    if result.ok:
        for profile in result.profiles:
            if profile.id == stream_id:
                return profile.label
    return stream_id


async def _enumerate_stream_profiles(
    db: DbSession, camera: Camera
) -> StreamProfileResult:
    """Best-effort enumerate a camera's stream profiles, never raising.

    Returns a :class:`StreamProfileResult`: ``ok=True`` with the profiles when the
    camera was reached and its streams read, otherwise ``ok=False`` with an empty
    list. *Every* failure mode -- no capture engine, an SSRF-rejected address, an
    invalid adapter configuration, or an unreachable camera -- folds into the same
    ``ok=False`` result so the caller renders one inline notice and capture falls
    back to the camera's default stream. The camera's address is re-resolved
    through the SSRF chokepoint here (the guard is check-time, so a save-time
    validation does not stand in for it). The camera row is detached before the
    probe, matching the manual-validate path; the adapter is always closed.
    """
    from ...cameras import build_adapter, resolve_camera_host
    from ...security.ssrf import SsrfError

    supervisor = get_context().capture_supervisor
    if supervisor is None:
        return StreamProfileResult(profiles=[], ok=False, message="engine unavailable")

    # SSRF chokepoint before contacting the camera, mirroring detect-protocol. A
    # camera may carry only snapshot/stream URIs and no address; skip the resolve
    # in that case rather than crash (matching the create path's `if address`).
    if camera.address:
        try:
            resolve_camera_host(camera.address)
        except SsrfError:
            return StreamProfileResult(
                profiles=[], ok=False, message="address rejected"
            )

    default_credentials = resolve_default_credentials(db)
    db.expunge(camera)
    try:
        adapter = build_adapter(
            camera,
            supervisor.http_client,
            ffmpeg_binary=supervisor.ffmpeg_binary,
            default_credentials=default_credentials,
        )
    except ValueError:
        return StreamProfileResult(profiles=[], ok=False, message="invalid camera")
    try:
        return await asyncio.wait_for(
            adapter.list_stream_profiles(),
            timeout=_CAMERA_PROBE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return StreamProfileResult(profiles=[], ok=False, message="unreachable")
    except Exception:  # noqa: BLE001 -- listing must never raise to the caller.
        return StreamProfileResult(profiles=[], ok=False, message="unreachable")
    finally:
        await adapter.close()


async def _enumerate_ptz_presets(db: DbSession, camera: Camera) -> PTZPresetsResult:
    """Best-effort enumerate a camera's PTZ presets, never raising.

    Mirrors :func:`_enumerate_stream_profiles`: returns a
    :class:`PTZPresetsResult` with ``ok=True`` and the presets when the camera was
    reached and read, otherwise ``ok=False`` with ``ptz_supported=False`` and an
    empty list. *Every* failure mode -- no capture engine, an SSRF-rejected
    address, an invalid adapter configuration, or an unreachable camera -- folds
    into the same ``ok=False`` result so the caller renders one inline notice and
    positioning is skipped. The camera's address is re-resolved through the SSRF
    chokepoint here (the guard is check-time). The camera row is detached before
    the probe and the adapter is always closed.
    """
    from ...cameras import build_adapter, resolve_camera_host
    from ...security.ssrf import SsrfError

    supervisor = get_context().capture_supervisor
    if supervisor is None:
        return PTZPresetsResult(
            presets=[], ptz_supported=False, ok=False, message="engine unavailable"
        )

    if camera.address:
        try:
            resolve_camera_host(camera.address)
        except SsrfError:
            return PTZPresetsResult(
                presets=[], ptz_supported=False, ok=False, message="address rejected"
            )

    default_credentials = resolve_default_credentials(db)
    db.expunge(camera)
    try:
        adapter = build_adapter(
            camera,
            supervisor.http_client,
            ffmpeg_binary=supervisor.ffmpeg_binary,
            default_credentials=default_credentials,
        )
    except ValueError:
        return PTZPresetsResult(
            presets=[], ptz_supported=False, ok=False, message="invalid camera"
        )
    try:
        return await asyncio.wait_for(
            adapter.list_ptz_presets(),
            timeout=_CAMERA_PROBE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return PTZPresetsResult(
            presets=[], ptz_supported=False, ok=False, message="unreachable"
        )
    except Exception:  # noqa: BLE001 -- listing must never raise to the caller.
        return PTZPresetsResult(
            presets=[], ptz_supported=False, ok=False, message="unreachable"
        )
    finally:
        await adapter.close()


# Maps each flat key the System settings panel renders to the dotted settings
# leaf path that backs it. Used to translate the loader's env-provenance set
# (which is in dotted-path space) into the flat-key space the template reads, so
# a per-field "env" chip is shown only for a value the environment controls. Keys
# absent here (e.g. the read-only TLS paths derived from optional fields) simply
# never carry a chip. Kept beside _settings_view so the two cannot drift.
_SETTINGS_FIELD_PATHS: dict[str, str] = {
    "http_port": "server.http_port",
    "https_port": "server.https_port",
    "redirect_http": "server.redirect_http_to_https",
    "tls_cert_path": "tls.cert_path",
    "tls_key_path": "tls.key_path",
    "session_idle_timeout_minutes": "session.idle_timeout_seconds",
    "session_max_age_days": "session.persistent_timeout_seconds",
}


def _settings_view() -> dict[str, Any]:
    """Flatten the current settings into the keys the settings template reads."""
    s = _settings()
    return {
        "http_port": s.server.http_port,
        "https_port": s.server.https_port,
        "redirect_http": s.server.redirect_http_to_https,
        "tls_cert_path": s.tls.cert_path or "",
        "tls_key_path": s.tls.key_path or "",
        "session_idle_timeout_minutes": s.session.idle_timeout_seconds // 60,
        "session_max_age_days": s.session.persistent_timeout_seconds // 86400,
        "notify_capture_error": False,
        "notify_low_disk": False,
        "notify_render_complete": False,
        "throttle_rate_per_minute": s.auth.throttle_max_failures,
    }


def _env_override_keys() -> frozenset[str]:
    """Flat System-panel keys whose effective value came from the environment.

    Translates the loaded settings' env-provenance set (dotted leaf paths) into
    the flat keys the template renders, via :data:`_SETTINGS_FIELD_PATHS`. Only
    keys whose backing leaf is reported env-sourced are included; a value the
    environment did not determine never appears, so the page never marks a field
    as environment-controlled when it is not. Returns an empty set when no
    provenance was recorded (e.g. settings built directly in tests), and the
    template then degrades to its banner with no per-field chips.
    """
    env_paths = get_context().env_overrides
    return frozenset(
        key for key, path in _SETTINGS_FIELD_PATHS.items() if path in env_paths
    )


def _parse_lines(value: str | None) -> list[str]:
    """Split a textarea field into a list of trimmed, non-empty lines."""
    if not value:
        return []
    return [line.strip() for line in value.splitlines() if line.strip()]


_LDAP_TLS_MODES = ("none", "ldaps", "starttls")


_LDAP_MEMBERSHIP_MODES = ("memberof", "group_search")


_LDAP_CONNECTION_MESSAGES: dict[LdapOutcome, str] = {
    LdapOutcome.AUTHENTICATED: "Connected and bind succeeded",
    # A service bind can succeed without matching a real user (no user supplied);
    # NO_SUCH_USER means the server bound and searched — a successful connection.
    LdapOutcome.NO_SUCH_USER: "Connected and bind succeeded",
    LdapOutcome.SERVER_UNREACHABLE: "Server unreachable",
    LdapOutcome.CONFIG_ERROR: "Bind failed — check server URL, bind DN, and password",
    LdapOutcome.INVALID_CREDENTIALS: "Bind failed — invalid credentials",
    LdapOutcome.DISABLED: "LDAP is disabled",
}


def _ldap_context(
    request: Request,
    db: DbDep,
    user: AdminUser,
    *,
    error: str | None = None,
    ssrf_error: str | None = None,
) -> dict[str, Any]:
    """Build the full settings-page template context, with optional inline errors.

    Used by the settings GET handler and by the validation-error paths in the
    LDAP and SSRF POST handlers so each re-renders ``settings.html`` with
    identical context. ``error`` carries an LDAP validation message; ``ssrf_error``
    carries a Network/SSRF one.

    The Network tab shows the config/env subnet baseline as read-only (it cannot
    be edited from the UI -- only the environment or config file sets it) and the
    admin-managed list as editable.
    """
    return deps.base_context(
        request,
        db,
        user,
        settings=_settings_view(),
        env_overrides=_env_override_keys(),
        ldap=load_ldap_settings(db),
        ldap_tls_modes=_LDAP_TLS_MODES,
        ldap_membership_modes=_LDAP_MEMBERSHIP_MODES,
        ldap_error=error,
        camera_defaults=load_camera_defaults(db),
        ssrf=load_ssrf_settings(db),
        ssrf_config_subnets=list(get_context().ssrf_config_subnets),
        ssrf_error=ssrf_error,
    )
