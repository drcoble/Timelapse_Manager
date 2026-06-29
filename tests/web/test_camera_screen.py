"""Web tests for the Meridian Cameras screen elements (row actions, badges)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from timelapse_manager.db.models import Camera
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context


def _seed_camera(*, name: str, geo_source: str | None) -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(
            name=name,
            address="10.0.0.5",
            protocol="vapix",
            geolocation_latitude=34.0 if geo_source else None,
            geolocation_longitude=-83.0 if geo_source else None,
            geolocation_source=geo_source,
        )
        db.add(cam)
        db.flush()
        return cam.id


def test_cameras_page_uses_svg_icons(admin_client: TestClient) -> None:
    html = admin_client.get("/cameras").text
    assert 'href="#icon-camera"' in html
    assert 'href="#icon-query"' in html  # scan button


def test_camera_row_has_row_actions_menu_for_operator(
    admin_client: TestClient,
) -> None:
    _seed_camera(name="rowcam", geo_source="manual")
    html = admin_client.get("/cameras").text
    assert "row-actions-menu" in html
    assert "row-actions-popover" in html


def test_camera_row_device_badge_for_camera_reported_geo(
    admin_client: TestClient,
) -> None:
    _seed_camera(name="geocam", geo_source="camera")
    html = admin_client.get("/cameras").text
    assert "badge-device" in html
    assert 'href="#icon-device"' in html


def test_camera_row_manual_badge_for_manual_geo(admin_client: TestClient) -> None:
    _seed_camera(name="mancam", geo_source="manual")
    html = admin_client.get("/cameras").text
    assert "badge-manual" in html


def test_viewer_sees_no_row_actions(viewer_client: TestClient) -> None:
    _seed_camera(name="vcam", geo_source="manual")
    html = viewer_client.get("/cameras").text
    assert "row-actions-menu" not in html


class TestDiscoverModal:
    """Discovery lives in a centered modal opened from the Cameras page."""

    def test_page_carries_discover_modal_markup(self, admin_client: TestClient) -> None:
        html = admin_client.get("/cameras").text
        # The opener trigger and the centered dialog itself.
        assert 'data-modal-open="#discover-modal"' in html
        assert 'class="discover-modal"' in html
        assert 'id="discover-modal"' in html
        # Dialog semantics, starting hidden until the controller opens it.
        assert 'role="dialog"' in html
        assert 'aria-modal="true"' in html
        assert 'aria-hidden="true"' in html
        # A close affordance the modal controller honours.
        assert "data-modal-close" in html

    def test_modal_scan_form_posts_to_discover(self, admin_client: TestClient) -> None:
        html = admin_client.get("/cameras").text
        # The scan form keeps the existing discover contract and result target,
        # with a native action for the no-JS path.
        assert 'action="/cameras/discover"' in html
        assert 'hx-post="/cameras/discover"' in html
        assert 'hx-target="#scan-results"' in html
        assert 'name="scan_range"' in html
        # Exactly one results container (it moved into the modal — no duplicate).
        assert html.count('id="scan-results"') == 1

    def test_viewer_sees_no_discover_modal(self, viewer_client: TestClient) -> None:
        html = viewer_client.get("/cameras").text
        assert 'data-modal-open="#discover-modal"' not in html
        assert 'class="discover-modal"' not in html
