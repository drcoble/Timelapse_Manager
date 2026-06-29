"""Unit tests for the ``localdt`` Jinja2 template filter.

The filter converts naive-UTC datetimes to the viewer's preferred timezone and
formats them for display. These tests exercise correctness, DST transitions,
and graceful handling of invalid or absent timezone names.
"""

from __future__ import annotations

import datetime

from timelapse_manager.web.dependencies import _localdt_filter


def _make_ctx(tz: str | None) -> dict:
    """Build a minimal fake Jinja2 context mapping for the filter."""

    class FakeCtx(dict):
        """Minimal dict subclass that satisfies jinja2.pass_context."""

        def get(self, key: str, default: object = None) -> object:  # type: ignore[override]
            return super().get(key, default)

    return FakeCtx(viewer_timezone=tz)


class TestLocaldtFilter:
    """Correctness tests for the localdt filter."""

    def test_none_returns_empty_string(self) -> None:
        ctx = _make_ctx(None)
        result = _localdt_filter(ctx, None)  # type: ignore[arg-type]
        assert result == ""

    def test_utc_fallback_when_no_tz(self) -> None:
        """Without a stored timezone the filter formats in UTC."""
        ctx = _make_ctx(None)
        dt = datetime.datetime(2026, 6, 15, 14, 30, 0)
        result = _localdt_filter(ctx, dt)  # type: ignore[arg-type]
        assert result == "2026-06-15 14:30 UTC"

    def test_converts_to_target_timezone(self) -> None:
        """A UTC instant is correctly converted to the viewer's zone."""
        ctx = _make_ctx("America/New_York")
        # 2026-06-15 14:30 UTC = 10:30 EDT (UTC-4 in summer)
        dt = datetime.datetime(2026, 6, 15, 14, 30, 0)
        result = _localdt_filter(ctx, dt)  # type: ignore[arg-type]
        assert result == "2026-06-15 10:30 EDT"

    def test_dst_winter_offset(self) -> None:
        """In winter the same timezone shows a different offset (EST = UTC-5)."""
        ctx = _make_ctx("America/New_York")
        # 2026-01-15 14:30 UTC = 09:30 EST (UTC-5 in winter)
        dt = datetime.datetime(2026, 1, 15, 14, 30, 0)
        result = _localdt_filter(ctx, dt)  # type: ignore[arg-type]
        assert result == "2026-01-15 09:30 EST"

    def test_invalid_tz_falls_back_to_utc(self) -> None:
        """An unrecognised IANA name silently falls back to UTC display."""
        ctx = _make_ctx("Not/ATimezone")
        dt = datetime.datetime(2026, 6, 15, 14, 30, 0)
        result = _localdt_filter(ctx, dt)  # type: ignore[arg-type]
        assert result == "2026-06-15 14:30 UTC"

    def test_empty_string_tz_falls_back_to_utc(self) -> None:
        """An empty string timezone name falls back to UTC."""
        ctx = _make_ctx("")
        dt = datetime.datetime(2026, 6, 15, 12, 0, 0)
        result = _localdt_filter(ctx, dt)  # type: ignore[arg-type]
        assert result == "2026-06-15 12:00 UTC"

    def test_other_timezone(self) -> None:
        """Test a non-US timezone for broad coverage."""
        ctx = _make_ctx("Europe/London")
        # 2026-06-15 14:30 UTC = 15:30 BST (UTC+1 in summer)
        dt = datetime.datetime(2026, 6, 15, 14, 30, 0)
        result = _localdt_filter(ctx, dt)  # type: ignore[arg-type]
        assert result == "2026-06-15 15:30 BST"

    def test_format_includes_date_hour_minute_tz(self) -> None:
        """Output always contains date, hour, minute, and a timezone abbreviation."""
        ctx = _make_ctx("UTC")
        dt = datetime.datetime(2026, 3, 10, 7, 5, 0)
        result = _localdt_filter(ctx, dt)  # type: ignore[arg-type]
        # Should be "2026-03-10 07:05 UTC"
        parts = result.split(" ")
        assert len(parts) == 3
        assert parts[0] == "2026-03-10"
        assert parts[1] == "07:05"
        assert parts[2]  # timezone abbreviation is non-empty
