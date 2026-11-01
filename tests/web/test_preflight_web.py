"""Web tests for the storage pre-flight route and create-form wiring."""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo

import pytest
from fastapi.testclient import TestClient

from timelapse_manager.db.models import Camera
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context


def _seed_camera() -> None:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        db.add(Camera(name="pf-cam", address="10.0.0.9", protocol="vapix"))
        db.flush()


def test_preflight_returns_banner(admin_client: TestClient) -> None:
    r = admin_client.get(
        "/projects/preflight?capture_interval_value=5&capture_interval_unit=minutes"
    )
    assert r.status_code == 200
    assert "preflight-banner" in r.text
    assert "/day" in r.text


def test_preflight_empty_for_invalid_interval(admin_client: TestClient) -> None:
    r = admin_client.get(
        "/projects/preflight?capture_interval_value=&capture_interval_unit=minutes"
    )
    assert r.status_code == 200
    assert r.text.strip() == ""


def test_preflight_requires_operator(viewer_client: TestClient) -> None:
    r = viewer_client.get(
        "/projects/preflight?capture_interval_value=5&capture_interval_unit=minutes"
    )
    assert r.status_code in (401, 403)


def test_new_project_form_has_chips_and_preflight(admin_client: TestClient) -> None:
    _seed_camera()  # the create form only renders when a camera exists
    html = admin_client.get("/projects/new").text
    assert "chip-group" in html
    assert 'data-chip-target="#capture_interval_value"' in html
    assert 'id="preflight-container"' in html


# --- #22 / D13: the pre-flight reads the whole schedule, not just the interval --


def _seed_geo_camera(name: str, *, geo: bool) -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(
            name=name,
            address="10.0.0.10",
            protocol="vapix",
            geolocation_latitude=34.123245 if geo else None,
            geolocation_longitude=-83.94052 if geo else None,
        )
        db.add(cam)
        db.flush()
        return cam.id


def _anchor_only_query(**extra: str) -> list[tuple[str, str]]:
    """A solar / scheduled-times create form with one new 12:00 clock row."""
    params = [
        ("capture_mode", "solar"),
        ("capture_schedule_preset", "always"),
        ("capture_schedule_timezone", "America/New_York"),
        ("anchor_id", ""),
        ("anchor_kind", "clock"),
        ("anchor_time", "12:00"),
        ("anchor_offset", ""),
        ("anchor_enabled", "new:0"),
    ]
    params.extend(extra.items())
    return params


def test_preflight_anchor_only_form_shows_one_frame_a_day(
    admin_client: TestClient,
) -> None:
    r = admin_client.get("/projects/preflight", params=_anchor_only_query())
    assert r.status_code == 200
    assert "preflight-banner" in r.text
    # One capture a day at the default 512 KB frame.
    assert "512.0 KB/day" in r.text


def test_preflight_solar_mode_ignores_the_interval(admin_client: TestClient) -> None:
    # A stale interval value in solar mode must not be counted.
    r = admin_client.get(
        "/projects/preflight",
        params=_anchor_only_query(
            capture_interval_value="1", capture_interval_unit="seconds"
        ),
    )
    assert "512.0 KB/day" in r.text


class _MidJuneUtc(datetime):
    """``datetime`` whose ``now()`` is a UTC midnight in a week with no DST change."""

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> _MidJuneUtc:  # type: ignore[override]
        fixed = cls(2026, 6, 1, 0, 0, tzinfo=UTC)
        return fixed if tz is None else fixed.astimezone(tz)


def test_preflight_interval_plus_anchor_counts_both(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pinned to a UTC midnight in a week with no daylight-saving change: a
    # 25-hour fall-back day inside the 7-day window really adds a tick.
    from timelapse_manager.web.routers import dashboard

    monkeypatch.setattr(dashboard, "datetime", _MidJuneUtc)
    params = [
        ("capture_mode", "interval"),
        ("capture_interval_value", "1"),
        ("capture_interval_unit", "hours"),
        *[p for p in _anchor_only_query() if p[0] != "capture_mode"],
    ]
    r = admin_client.get("/projects/preflight", params=params)
    # 24 hourly ticks + 1 anchor = 25 x 512 KB = 12.5 MB.
    assert "12.5 MB/day" in r.text


class _MidnightUtc(datetime):
    """``datetime`` whose ``now()`` is a fixed UTC midnight (see below)."""

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> _MidnightUtc:  # type: ignore[override]
        fixed = cls(2026, 3, 2, 0, 0, tzinfo=UTC)
        return fixed if tz is None else fixed.astimezone(tz)


def test_preflight_schedule_window_limits_interval(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The rate is measured over the 7 days from now, so a window already open
    # at "now" counts a fractional stretch and the last day a whole one: at
    # 11:05 UTC that is 70.9 ticks, 5.1 MB/day, and the test failed by time of
    # day (#34). From a UTC midnight the week holds exactly 70.
    from timelapse_manager.web.routers import dashboard

    monkeypatch.setattr(dashboard, "datetime", _MidnightUtc)
    params = [
        ("capture_mode", "interval"),
        ("capture_interval_value", "1"),
        ("capture_interval_unit", "hours"),
        ("capture_schedule_preset", "custom"),
        ("capture_schedule_timezone", "UTC"),
        ("capture_window_start", "08:00"),
        ("capture_window_end", "18:00"),
    ]
    r = admin_client.get("/projects/preflight", params=params)
    # 10 hourly ticks a day x 512 KB = 5.0 MB.
    assert "5.0 MB/day" in r.text


def test_preflight_disabled_anchor_only_is_empty(admin_client: TestClient) -> None:
    params = [p for p in _anchor_only_query() if p[0] != "anchor_enabled"]
    r = admin_client.get("/projects/preflight", params=params)
    assert r.status_code == 200
    assert r.text.strip() == ""


def test_preflight_solar_anchor_needs_camera_coordinates(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pinned to a week with no daylight-saving change, as above: across the
    # fall-back the 7-day window holds an eighth solar noon.
    from timelapse_manager.web.routers import dashboard

    monkeypatch.setattr(dashboard, "datetime", _MidJuneUtc)
    solar_row = {
        "anchor_kind": "solar_noon",
        "anchor_time": "",
    }
    with_geo = _seed_geo_camera("pf-geo-cam", geo=True)
    without_geo = _seed_geo_camera("pf-nogeo-cam", geo=False)

    def query(camera_id: int) -> list[tuple[str, str]]:
        params = [
            (k, solar_row.get(k, v))
            for k, v in _anchor_only_query()
            if k != "anchor_time"
        ]
        params.append(("anchor_time", ""))
        params.append(("camera_id", str(camera_id)))
        return params

    located = admin_client.get("/projects/preflight", params=query(with_geo))
    assert "512.0 KB/day" in located.text
    # No coordinates: the row is kept but fires nothing, so nothing is planned.
    unlocated = admin_client.get("/projects/preflight", params=query(without_geo))
    assert unlocated.status_code == 200
    assert unlocated.text.strip() == ""


def test_preflight_invalid_anchor_row_is_empty(admin_client: TestClient) -> None:
    params = [(k, "" if k == "anchor_time" else v) for k, v in _anchor_only_query()]
    r = admin_client.get("/projects/preflight", params=params)
    assert r.status_code == 200
    assert r.text.strip() == ""


def test_preflight_past_end_date_is_empty(admin_client: TestClient) -> None:
    r = admin_client.get(
        "/projects/preflight", params=_anchor_only_query(end_date="2020-01-01T00:00")
    )
    assert r.text.strip() == ""


def test_new_project_form_preflight_watches_the_whole_form(
    admin_client: TestClient,
) -> None:
    _seed_camera()
    html = admin_client.get("/projects/new").text
    start = html.index('id="preflight-container"')
    tag = html[start : html.index(">", start)]
    assert 'hx-trigger="change from:closest form delay:300ms"' in tag
    for selector in (
        "[name^='capture_']",
        "[name^='anchor_']",
        "[name^='sun_offset_']",
        "#camera_id",
        "#start_date",
        "#end_date",
    ):
        assert selector in tag
    # Never the CSRF token: this is a GET.
    assert "csrf" not in tag
    # After the exact-time fieldset, i.e. outside the interval-only fields that
    # solar mode hides, so it shows in both capture modes.
    assert html.index("Exact-time captures") < start
