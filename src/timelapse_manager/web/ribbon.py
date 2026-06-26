"""Time-ribbon SVG generation.

A ribbon is a horizontal band that visualises a capture campaign over time:
day/night bands from the camera's location, a tick per captured frame, gap
markers where capture lapsed, render-span overlays, and a "now" cursor.

The SVG is generated server-side as plain markup (no dependencies beyond the
existing sun-time helper). Colours are applied via CSS classes — SVG
presentation attributes do not resolve CSS custom properties, so the palette
lives in the stylesheet and is theme-aware.

Coordinate space: a fixed viewBox width of ``VBW`` units with
``preserveAspectRatio="none"`` so the ribbon stretches to its container width;
vertical features use the full height.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from ..capture.schedule import compute_sun_times

VBW = 1000.0  # viewBox width units


def _solar_tz(longitude: float) -> ZoneInfo:
    """Approximate local timezone from longitude (15° per hour).

    Day/night bands must be computed against the camera's *solar* day, not UTC:
    for a location hours away from UTC, a UTC calendar day splits one daylight
    span across two dates. We bucket by an Etc/GMT zone at the nearest hour
    offset (Etc/GMT signs are inverted: Etc/GMT-8 == UTC+8).
    """
    offset = round(longitude / 15.0)
    if offset == 0:
        return ZoneInfo("UTC")
    name = f"Etc/GMT{'-' if offset > 0 else '+'}{abs(offset)}"
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def _frac(t: datetime, start: datetime, end: datetime) -> float:
    """Position of *t* within [start, end] as a fraction in [0, 1]."""
    span = (end - start).total_seconds()
    if span <= 0:
        return 0.0
    f = (t - start).total_seconds() / span
    return 0.0 if f < 0 else 1.0 if f > 1 else f


def day_bands(
    start: datetime,
    end: datetime,
    latitude: float | None,
    longitude: float | None,
) -> list[tuple[float, float]]:
    """Daylight intervals within [start, end] as (frac_start, frac_end) pairs.

    Uses the camera's geolocation to compute sunrise/sunset per *solar* day
    (longitude-derived local tz). Returns an empty list when the location is
    unknown (graceful degradation) or on polar degeneracy for a given day
    (that day contributes no band).
    """
    if latitude is None or longitude is None or end <= start:
        return []
    local = _solar_tz(longitude)
    bands: list[tuple[float, float]] = []
    cur = start.astimezone(local).date()
    last = end.astimezone(local).date()
    while cur <= last:
        sunrise, sunset = compute_sun_times(latitude, longitude, cur, local)
        if sunrise is not None and sunset is not None and sunset > sunrise:
            a = _frac(sunrise, start, end)
            b = _frac(sunset, start, end)
            if b > a:
                bands.append((a, b))
        cur += timedelta(days=1)
    return bands


def detect_gaps(
    frame_times: list[datetime],
    start: datetime,
    end: datetime,
    *,
    min_gap_fraction: float = 0.04,
) -> list[tuple[float, float]]:
    """Capture gaps between consecutive frames, as (frac_start, frac_end).

    A gap is any interval between two consecutive frames longer than
    *min_gap_fraction* of the whole span — a simple, interval-agnostic way to
    surface "the camera stopped capturing here".
    """
    if len(frame_times) < 2 or end <= start:
        return []
    ordered = sorted(frame_times)
    threshold = (end - start).total_seconds() * min_gap_fraction
    gaps: list[tuple[float, float]] = []
    for prev, nxt in zip(ordered, ordered[1:], strict=False):
        if (nxt - prev).total_seconds() > threshold:
            gaps.append((_frac(prev, start, end), _frac(nxt, start, end)))
    return gaps


def _downsample(items: list[datetime], limit: int) -> list[datetime]:
    """Evenly thin *items* to at most *limit* entries (ticks merge past that)."""
    if len(items) <= limit or limit <= 0:
        return items
    step = len(items) / limit
    return [items[int(i * step)] for i in range(limit)]


def build_svg(
    *,
    start: datetime,
    end: datetime,
    now: datetime,
    height: int = 20,
    frame_times: list[datetime] | None = None,
    render_spans: list[tuple[datetime, datetime]] | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    label: str,
    interactive: bool = False,
    decorative: bool = False,
    draw_now: bool = True,
    max_ticks: int = 400,
) -> str:
    """Render the ribbon as an SVG string.

    Layers bottom→top: day bands, render spans, frame ticks, gap markers, now
    cursor. ``label`` becomes the accessible ``<title>``.

    ``decorative`` hides the SVG from assistive tech (``role=presentation``).
    Set it only when the SVG is embedded in a labelled control (e.g. the frames
    scrubber's ``role=slider`` wrapper), so the graphic is not announced twice;
    otherwise the SVG names itself with ``role=img`` + ``aria-label`` — that is
    its only accessible name when it stands alone (dashboard card, project
    header).

    ``draw_now`` draws the live "now" cursor; pass ``False`` for a zoom window
    that lies entirely in the past, where a clamped edge-line would mislead.
    """
    frame_times = frame_times or []
    render_spans = render_spans or []
    h = height
    parts: list[str] = []

    cls = "time-ribbon-svg" + (" time-ribbon-svg--interactive" if interactive else "")
    if decorative:
        # Embedded in a labelled control: hide the SVG so it is not announced as
        # a second, redundant graphic alongside the control's own name.
        a11y = 'role="presentation" aria-hidden="true"'
    else:
        # Standalone: the SVG's own label is its only accessible name.
        a11y = f'role="img" aria-label="{escape(label)}"'
    parts.append(
        f'<svg class="{cls}" viewBox="0 0 {int(VBW)} {h}" '
        f'preserveAspectRatio="none" {a11y} data-draw="1">'
    )
    parts.append(f"<title>{escape(label)}</title>")

    # 1. Day/night bands (day = washed; the track background reads as night).
    for a, b in day_bands(start, end, latitude, longitude):
        x = a * VBW
        w = (b - a) * VBW
        parts.append(
            f'<rect class="ribbon-day" x="{x:.2f}" y="0" '
            f'width="{w:.2f}" height="{h}"></rect>'
        )

    # 2. Render spans.
    for s_start, s_end in render_spans:
        a = _frac(s_start, start, end)
        b = _frac(s_end, start, end)
        if b >= a:
            x = a * VBW
            w = max((b - a) * VBW, 1.0)
            parts.append(
                f'<rect class="ribbon-render" x="{x:.2f}" y="0" '
                f'width="{w:.2f}" height="{h}"></rect>'
            )

    # 3. Frame ticks (downsampled; they merge into a solid fill at density).
    for t in _downsample(frame_times, max_ticks):
        x = _frac(t, start, end) * VBW
        parts.append(
            f'<line class="ribbon-tick" x1="{x:.2f}" y1="0" '
            f'x2="{x:.2f}" y2="{h}"></line>'
        )

    # 4. Gap markers.
    for a, b in detect_gaps(frame_times, start, end):
        x = a * VBW
        w = max((b - a) * VBW, 1.5)
        parts.append(
            f'<rect class="ribbon-gap" x="{x:.2f}" y="0" '
            f'width="{w:.2f}" height="{h}"></rect>'
        )

    # 5. Now cursor. The caller suppresses it (``draw_now=False``) for a zoom
    # window that does not contain "now", where a clamped edge-line would falsely
    # read as the live position.
    if draw_now:
        nx = _frac(now, start, end) * VBW
        parts.append(
            f'<line class="ribbon-now" x1="{nx:.2f}" y1="0" '
            f'x2="{nx:.2f}" y2="{h}"></line>'
        )

    parts.append("</svg>")
    return "".join(parts)
