"""Unit tests for the time-ribbon SVG generator (pure, no DB/browser)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from timelapse_manager.web import ribbon

TZ = ZoneInfo("America/Los_Angeles")
# Seattle-ish; well away from polar latitudes so sun times are always finite.
LAT, LON = 47.6062, -122.3321

START = datetime(2024, 6, 1, 0, 0, tzinfo=UTC)
END = datetime(2024, 6, 4, 0, 0, tzinfo=UTC)  # 3-day span
NOW = datetime(2024, 6, 3, 12, 0, tzinfo=UTC)


def test_frac_clamps() -> None:
    assert ribbon._frac(START, START, END) == 0.0
    assert ribbon._frac(END, START, END) == 1.0
    assert ribbon._frac(START - timedelta(days=1), START, END) == 0.0
    assert ribbon._frac(END + timedelta(days=1), START, END) == 1.0
    mid = START + (END - START) / 2
    assert abs(ribbon._frac(mid, START, END) - 0.5) < 1e-6


def test_day_bands_present_for_known_location() -> None:
    bands = ribbon.day_bands(START, END, LAT, LON)
    # ~3 daylight intervals over a 3-day span.
    assert 2 <= len(bands) <= 4
    for a, b in bands:
        assert 0.0 <= a < b <= 1.0


def test_day_bands_empty_without_geolocation() -> None:
    assert ribbon.day_bands(START, END, None, None) == []
    assert ribbon.day_bands(START, END, LAT, None) == []


def test_day_bands_empty_for_degenerate_span() -> None:
    assert ribbon.day_bands(END, START, LAT, LON) == []


def test_detect_gaps_finds_large_gap() -> None:
    # Frames clustered at the start, then a long gap, then one near the end.
    times = [START + timedelta(hours=i) for i in range(3)]
    times.append(END - timedelta(hours=1))
    gaps = ribbon.detect_gaps(times, START, END)
    assert len(gaps) == 1
    a, b = gaps[0]
    assert 0.0 < a < b < 1.0


def test_detect_gaps_none_when_uniform() -> None:
    # One frame per hour across the span — no gap exceeds the threshold.
    n = int((END - START).total_seconds() // 3600)
    times = [START + timedelta(hours=i) for i in range(n)]
    assert ribbon.detect_gaps(times, START, END) == []


def test_downsample_caps_length() -> None:
    items = [START + timedelta(minutes=i) for i in range(1000)]
    out = ribbon._downsample(items, 400)
    assert len(out) == 400
    assert ribbon._downsample(items, 0) is items


def test_build_svg_structure() -> None:
    frames = [START + timedelta(hours=i) for i in range(0, 72, 6)]
    renders = [(START + timedelta(days=1), START + timedelta(days=1, hours=2))]
    svg = ribbon.build_svg(
        start=START,
        end=END,
        now=NOW,
        height=20,
        frame_times=frames,
        render_spans=renders,
        latitude=LAT,
        longitude=LON,
        label="Capture timeline",
    )
    assert svg.startswith("<svg")
    assert svg.endswith("</svg>")
    assert "<title>Capture timeline</title>" in svg
    assert 'class="ribbon-day"' in svg  # geolocated -> day bands
    assert 'class="ribbon-render"' in svg
    assert 'class="ribbon-tick"' in svg
    assert 'class="ribbon-now"' in svg
    assert 'viewBox="0 0 1000 20"' in svg


def test_build_svg_no_bands_without_geo() -> None:
    svg = ribbon.build_svg(
        start=START,
        end=END,
        now=NOW,
        frame_times=[],
        label="x",
    )
    assert 'class="ribbon-day"' not in svg
    assert 'class="ribbon-now"' in svg  # cursor always present


def test_build_svg_standalone_is_labelled_image() -> None:
    """A standalone ribbon (no wrapping control) names itself via role=img.

    This is the default for every caller except the frames scrubber — including
    the interactive ribbon on the project-detail page, which has no labelled
    wrapper, so it must keep its own accessible name.
    """
    for interactive in (False, True):
        svg = ribbon.build_svg(
            start=START,
            end=END,
            now=NOW,
            label="Timeline X",
            interactive=interactive,
        )
        assert 'role="img"' in svg
        assert 'aria-label="Timeline X"' in svg
        assert "aria-hidden" not in svg


def test_build_svg_decorative_svg_is_hidden() -> None:
    """When embedded in a labelled control, the SVG is decorative (hidden)."""
    svg = ribbon.build_svg(
        start=START, end=END, now=NOW, label="Timeline X", decorative=True
    )
    assert 'aria-hidden="true"' in svg
    assert 'role="presentation"' in svg
    # The redundant graphic label is dropped (the wrapper carries the name).
    assert 'aria-label="Timeline X"' not in svg
    assert 'role="img"' not in svg


def test_build_svg_escapes_label() -> None:
    svg = ribbon.build_svg(start=START, end=END, now=NOW, label='a<b>&"x')
    assert "<title>a&lt;b&gt;&amp;&quot;x</title>" in svg
    assert "<b>" not in svg
