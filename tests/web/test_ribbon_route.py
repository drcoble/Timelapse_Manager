"""Web tests for the time-ribbon partial route."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context


def _seed(*, name: str, with_geo: bool) -> int:
    """Seed a Camera (+/- geolocation) + a bounded Project + a few frames."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(
            name=f"{name}-cam",
            address="127.0.0.1",
            protocol="vapix",
            geolocation_latitude=47.6062 if with_geo else None,
            geolocation_longitude=-122.3321 if with_geo else None,
            geolocation_source="manual" if with_geo else None,
        )
        db.add(cam)
        db.flush()
        proj = Project(
            camera_id=cam.id,
            name=name,
            capture_interval_seconds=3600,
            lifecycle_state="active",
            start_date=datetime(2026, 1, 1, 0, 0),
            end_date=datetime(2026, 1, 4, 0, 0),
        )
        db.add(proj)
        db.flush()
        pid = proj.id
        for seq in range(6):
            db.add(
                Frame(
                    project_id=pid,
                    sequence_index=seq,
                    capture_timestamp=datetime(2026, 1, 1, 0, 0)
                    .replace(tzinfo=UTC)
                    .replace(hour=seq * 4 % 24)
                    .replace(tzinfo=None),
                    file_path=f"/frames/{pid}/{seq:08d}.jpg",
                    capture_status="captured",
                    origin="captured",
                    lifecycle_state="active",
                )
            )
        db.flush()
        return pid


def test_ribbon_renders_svg(admin_client: TestClient) -> None:
    pid = _seed(name="ribbon-geo", with_geo=True)
    resp = admin_client.get(f"/partials/projects/{pid}/ribbon")
    assert resp.status_code == 200
    body = resp.text
    assert "<svg" in body
    assert 'class="time-ribbon"' in body
    assert "ribbon-now" in body  # cursor always present
    assert "ribbon-tick" in body  # frames -> ticks
    assert "ribbon-day" in body  # geolocated -> day bands
    assert "data-start=" in body and "data-end=" in body


def test_ribbon_without_geolocation_has_no_day_bands(
    admin_client: TestClient,
) -> None:
    pid = _seed(name="ribbon-nogeo", with_geo=False)
    body = admin_client.get(f"/partials/projects/{pid}/ribbon").text
    assert "ribbon-day" not in body
    assert "ribbon-now" in body


def test_ribbon_detail_variant_is_interactive(admin_client: TestClient) -> None:
    pid = _seed(name="ribbon-detail", with_geo=True)
    body = admin_client.get(f"/partials/projects/{pid}/ribbon?h=36").text
    assert "time-ribbon--detail" in body
    assert "time-ribbon-svg--interactive" in body
    assert 'viewBox="0 0 1000 36"' in body


def test_interactive_ribbon_keeps_label_without_decorative(
    admin_client: TestClient,
) -> None:
    """An interactive ribbon NOT wrapped in a control (the project-detail page,
    which omits decorative) must keep its own accessible name."""
    pid = _seed(name="ribbon-detail-label", with_geo=True)
    body = admin_client.get(f"/partials/projects/{pid}/ribbon?h=36").text
    assert 'role="img"' in body
    assert "aria-label=" in body
    assert 'role="presentation"' not in body


def test_decorative_flag_hides_svg_from_assistive_tech(
    admin_client: TestClient,
) -> None:
    """decorative=1 (used by the labelled scrubber wrapper) hides the SVG."""
    pid = _seed(name="ribbon-decorative", with_geo=True)
    body = admin_client.get(f"/partials/projects/{pid}/ribbon?h=36&decorative=1").text
    assert 'role="presentation"' in body
    assert 'aria-hidden="true"' in body
    assert 'role="img"' not in body


def test_ribbon_compact_variant(admin_client: TestClient) -> None:
    pid = _seed(name="ribbon-compact", with_geo=False)
    body = admin_client.get(f"/partials/projects/{pid}/ribbon?h=12").text
    assert "time-ribbon--compact" in body


def test_ribbon_zoom_window_clamps_and_filters(admin_client: TestClient) -> None:
    """window_start/window_end magnify a sub-range: the returned bounds are the
    window, out-of-window frame ticks drop, the strip is forced interactive +
    zoom-variant regardless of height, and a past window drops the now-cursor."""
    pid = _seed(name="ribbon-zoom", with_geo=True)
    # Campaign is 2026-01-01 .. 2026-01-04; all frames fall on 2026-01-01.
    ws = int(datetime(2026, 1, 2, tzinfo=UTC).timestamp())
    we = int(datetime(2026, 1, 3, tzinfo=UTC).timestamp())
    body = admin_client.get(
        f"/partials/projects/{pid}/ribbon?window_start={ws}&window_end={we}"
    ).text
    assert "time-ribbon--zoom" in body
    assert "time-ribbon-svg--interactive" in body  # forced, not height-derived
    assert f'data-start="{ws}"' in body and f'data-end="{we}"' in body
    assert "ribbon-tick" not in body  # Jan-1 frames are outside the Jan 2-3 window
    assert "ribbon-now" not in body  # "now" is outside the window -> no live cursor


def test_ribbon_zoom_window_clamped_to_campaign(admin_client: TestClient) -> None:
    """A window overflowing the campaign clamps to the campaign bounds; a window
    that does include the frames keeps their ticks."""
    pid = _seed(name="ribbon-zoom-clamp", with_geo=False)
    camp_start = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp())
    ws = int(datetime(2025, 12, 1, tzinfo=UTC).timestamp())  # before campaign start
    we = int(datetime(2026, 1, 2, tzinfo=UTC).timestamp())  # includes the Jan-1 frames
    body = admin_client.get(
        f"/partials/projects/{pid}/ribbon?window_start={ws}&window_end={we}"
    ).text
    assert f'data-start="{camp_start}"' in body  # clamped up to the campaign start
    assert f'data-end="{we}"' in body
    assert "ribbon-tick" in body  # Jan-1 frames are inside this window


def test_ribbon_partial_window_param_is_not_zoom(admin_client: TestClient) -> None:
    """Supplying only one window bound is not a zoom request -> full-span ribbon."""
    pid = _seed(name="ribbon-zoom-partial", with_geo=False)
    ws = int(datetime(2026, 1, 2, tzinfo=UTC).timestamp())
    body = admin_client.get(f"/partials/projects/{pid}/ribbon?window_start={ws}").text
    assert "time-ribbon--zoom" not in body
    assert "ribbon-now" in body  # the full-span ribbon keeps its cursor


def test_ribbon_out_of_range_window_epoch_is_not_a_500(
    admin_client: TestClient,
) -> None:
    """A garbage (out-of-range) epoch is a malformed request, not a server
    error: the window is ignored and the full-span ribbon renders."""
    pid = _seed(name="ribbon-zoom-bad-epoch", with_geo=False)
    resp = admin_client.get(
        f"/partials/projects/{pid}/ribbon?window_start=10000000000000000"
        "&window_end=20000000000000000"
    )
    assert resp.status_code == 200
    assert "time-ribbon--zoom" not in resp.text


def test_ribbon_nonexistent_project_404(admin_client: TestClient) -> None:
    assert admin_client.get("/partials/projects/999999/ribbon").status_code == 404


def test_viewer_can_access_ribbon(viewer_client: TestClient) -> None:
    pid = _seed(name="ribbon-viewer", with_geo=True)
    assert viewer_client.get(f"/partials/projects/{pid}/ribbon").status_code == 200


def test_ribbon_assets_served(anon_client: TestClient) -> None:
    css = anon_client.get("/static/css/components/time-ribbon.css")
    assert css.status_code == 200 and ".time-ribbon" in css.text
    assert anon_client.get("/static/js/ribbon.js").status_code == 200
    assert "components/time-ribbon.css" in anon_client.get("/static/css/app.css").text


def test_shell_loads_ribbon_script(admin_client: TestClient) -> None:
    assert "/static/js/ribbon.js" in admin_client.get("/").text


def test_dashboard_card_has_ribbon_slot(admin_client: TestClient) -> None:
    _seed(name="ribbon-slot", with_geo=True)
    html = admin_client.get("/").text
    assert "time-ribbon-slot" in html
    assert "/ribbon?h=20" in html
