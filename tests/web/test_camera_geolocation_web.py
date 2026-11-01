"""Web tests for the camera geolocation feature (F1).

Covers:
- GET /cameras/{id}/geolocation — the four outcomes (success, unreachable,
  no_location, access_denied).  Every camera-side failure renders at HTTP 200
  (HTMX fragment); only the role gate returns a non-200.
- POST /cameras (create) persists latitude/longitude/geo_source.
- POST /cameras/{id}/edit updates geo fields; blank values clear them.

Adapter stubbing follows the pattern from test_stream_profile_select.py:
patch ``timelapse_manager.cameras.resolve_camera_host`` (pass-through) and
``timelapse_manager.cameras.build_adapter`` to return a mock whose
``get_geolocation`` and ``close`` are AsyncMocks.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.cameras.base import GeoLocation, GeolocationAccessDenied
from timelapse_manager.db.models import Camera
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context

# 192.168.1.50 is a private address only reachable in tests that use
# settings_no_autostart (which whitelists RFC-1918). The web_client fixture
# uses web_settings which blocks private addresses via the SSRF guard.
# For tests that POST camera forms through admin_client, use a globally-routable
# address that Python 3.11 does not classify as is_private (8.8.8.8 passes).
# The seeded cameras in _seed_camera() write directly to DB so any address is fine.
_ALLOWED_ADDRESS = "8.8.8.8"


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_camera(
    *,
    name: str,
    latitude: float | None = None,
    longitude: float | None = None,
    geo_source: str | None = None,
) -> int:
    """Insert a camera and return its id."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(
            name=name,
            address=_ALLOWED_ADDRESS,
            protocol="vapix",
            snapshot_uri=f"http://{_ALLOWED_ADDRESS}/snap",
            geolocation_latitude=latitude,
            geolocation_longitude=longitude,
            geolocation_source=geo_source,
        )
        db.add(cam)
        db.flush()
        return cam.id


def _camera_geo(camera_id: int) -> tuple[float | None, float | None, str | None]:
    """Return (latitude, longitude, geo_source) for a stored camera."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = db.get(Camera, camera_id)
        assert cam is not None
        return (
            cam.geolocation_latitude,
            cam.geolocation_longitude,
            cam.geolocation_source,
        )


# ---------------------------------------------------------------------------
# Adapter stub helpers
# ---------------------------------------------------------------------------


def _geo_adapter(geolocation_result: GeoLocation | None) -> MagicMock:
    """Return a mock adapter whose get_geolocation returns ``geolocation_result``."""
    adapter = MagicMock()
    adapter.get_geolocation = AsyncMock(return_value=geolocation_result)
    adapter.close = AsyncMock()
    return adapter


def _geo_adapter_raises() -> MagicMock:
    """Return a mock adapter whose get_geolocation raises an exception (unreachable)."""
    adapter = MagicMock()
    adapter.get_geolocation = AsyncMock(side_effect=Exception("connection refused"))
    adapter.close = AsyncMock()
    return adapter


def _geo_patches(adapter: MagicMock):
    """Patch SSRF guard and build_adapter to return ``adapter``."""
    return (
        patch(
            "timelapse_manager.cameras.resolve_camera_host",
            side_effect=lambda a: a,
        ),
        patch(
            "timelapse_manager.cameras.build_adapter",
            return_value=adapter,
        ),
    )


# ---------------------------------------------------------------------------
# F1 — GET /cameras/{id}/geolocation
# ---------------------------------------------------------------------------


class TestCameraGeolocationRoute:
    def test_success_renders_coordinates(self, admin_client: TestClient) -> None:
        """A camera that reports a location renders coords and 'Camera reports:'."""
        camera_id = _seed_camera(name="geo-success")
        location = GeoLocation(latitude=48.8566, longitude=2.3522, source="camera")
        adapter = _geo_adapter(location)
        guard, builder = _geo_patches(adapter)
        with guard, builder:
            resp = admin_client.get(f"/cameras/{camera_id}/geolocation")
        assert resp.status_code == 200
        html = resp.text
        assert "Camera reports:" in html
        assert "48.8566" in html
        assert "2.3522" in html
        assert "Use these values" in html
        assert "alert success" in html

    def test_unreachable_renders_error_fragment(self, admin_client: TestClient) -> None:
        """A camera that cannot be reached renders the error alert at HTTP 200."""
        camera_id = _seed_camera(name="geo-unreachable")
        adapter = _geo_adapter_raises()
        guard, builder = _geo_patches(adapter)
        with guard, builder:
            resp = admin_client.get(f"/cameras/{camera_id}/geolocation")
        assert resp.status_code == 200
        html = resp.text
        assert "alert error" in html
        assert "Could not reach this camera" in html
        assert "Camera reports:" not in html

    def test_no_location_renders_info_fragment(self, admin_client: TestClient) -> None:
        """A reachable camera with no location renders the info alert at HTTP 200."""
        camera_id = _seed_camera(name="geo-no-location")
        adapter = _geo_adapter(None)  # get_geolocation() returns None
        guard, builder = _geo_patches(adapter)
        with guard, builder:
            resp = admin_client.get(f"/cameras/{camera_id}/geolocation")
        assert resp.status_code == 200
        html = resp.text
        assert "alert info" in html
        assert "Camera did not report a location" in html
        assert "Camera reports:" not in html
        # A clean "no location" offers no admin read: the account was accepted.
        assert "Read with admin account" not in html

    def test_access_denied_renders_role_warning(self, admin_client: TestClient) -> None:
        """A refused read names the role and offers the one-time admin read."""
        camera_id = _seed_camera(name="geo-denied")
        adapter = MagicMock()
        adapter.get_geolocation = AsyncMock(
            side_effect=GeolocationAccessDenied("vapix", "Administrator", 401)
        )
        adapter.close = AsyncMock()
        guard, builder = _geo_patches(adapter)
        with guard, builder:
            resp = admin_client.get(f"/cameras/{camera_id}/geolocation")
        assert resp.status_code == 200
        html = resp.text
        assert "alert warning" in html
        assert "Administrator" in html
        assert "did not report a location" not in html
        assert "Could not reach" not in html
        assert "Read with admin account" in html
        assert f"/cameras/{camera_id}/geolocation/admin-read" in html
        # The one-time inputs never share the camera credential field names, and
        # are detached from the camera form so its Save cannot submit them.
        assert 'name="geo_admin_username"' in html
        assert 'name="geo_admin_password"' in html
        assert 'name="username"' not in html
        assert 'name="password"' not in html
        assert f'form="cam-{camera_id}-geo-admin-detached"' in html
        assert 'autocomplete="new-password"' in html
        assert 'hx-params="geo_admin_username,geo_admin_password"' in html
        assert "not saved" in html
        adapter.close.assert_awaited_once()

    def test_forbidden_for_viewer(self, viewer_client: TestClient) -> None:
        """Viewers cannot poll geolocation; the role gate returns 403."""
        camera_id = _seed_camera(name="geo-viewer-block")
        resp = viewer_client.get(
            f"/cameras/{camera_id}/geolocation",
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_allowed_for_operator(self, operator_client: TestClient) -> None:
        """Operators may poll geolocation."""
        camera_id = _seed_camera(name="geo-operator-ok")
        location = GeoLocation(latitude=51.5, longitude=-0.1, source="camera")
        adapter = _geo_adapter(location)
        guard, builder = _geo_patches(adapter)
        with guard, builder:
            resp = operator_client.get(f"/cameras/{camera_id}/geolocation")
        assert resp.status_code == 200
        assert "Camera reports:" in resp.text


# ---------------------------------------------------------------------------
# F1 — POST /cameras (create) persists geo fields
# ---------------------------------------------------------------------------


class TestCreateCameraPersistsGeo:
    def test_create_with_geo_persists_coordinates(
        self, admin_client: TestClient
    ) -> None:
        """Creating a camera with lat/lon/geo_source stores all three columns."""
        csrf = csrf_of(admin_client, "/cameras")
        resp = admin_client.post(
            "/cameras",
            data={
                "name": "Geo Create Camera",
                "address": _ALLOWED_ADDRESS,
                "protocol": "vapix",
                "latitude": "48.8566",
                "longitude": "2.3522",
                "geo_source": "camera",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        # Retrieve the created camera by name.
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            cam = (
                db.query(Camera)
                .filter(Camera.name == "Geo Create Camera")
                .one_or_none()
            )
            assert cam is not None
            assert cam.geolocation_latitude == pytest.approx(48.8566)
            assert cam.geolocation_longitude == pytest.approx(2.3522)
            assert cam.geolocation_source == "camera"

    def test_create_without_geo_stores_nulls(self, admin_client: TestClient) -> None:
        """Creating a camera without geo fields stores null coordinates."""
        csrf = csrf_of(admin_client, "/cameras")
        resp = admin_client.post(
            "/cameras",
            data={
                "name": "Geo Null Camera",
                "address": _ALLOWED_ADDRESS,
                "protocol": "vapix",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            cam = (
                db.query(Camera).filter(Camera.name == "Geo Null Camera").one_or_none()
            )
            assert cam is not None
            assert cam.geolocation_latitude is None
            assert cam.geolocation_longitude is None
            assert cam.geolocation_source is None


# ---------------------------------------------------------------------------
# F1 — POST /cameras/{id}/edit updates geo fields
# ---------------------------------------------------------------------------


class TestEditCameraGeo:
    def test_edit_sets_new_geo_coordinates(self, admin_client: TestClient) -> None:
        """Editing a camera with lat/lon updates the stored geo columns."""
        camera_id = _seed_camera(name="Geo Edit Set")
        csrf = csrf_of(admin_client, "/cameras")
        resp = admin_client.post(
            f"/cameras/{camera_id}/edit",
            data={
                "name": "Geo Edit Set",
                "address": _ALLOWED_ADDRESS,
                "protocol": "vapix",
                "latitude": "51.5074",
                "longitude": "-0.1278",
                "geo_source": "manual",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 200, resp.text
        lat, lon, source = _camera_geo(camera_id)
        assert lat == pytest.approx(51.5074)
        assert lon == pytest.approx(-0.1278)
        assert source == "manual"

    def test_edit_clears_geo_coordinates_when_blank(
        self, admin_client: TestClient
    ) -> None:
        """Editing a camera with blank lat/lon clears the stored geo columns."""
        camera_id = _seed_camera(
            name="Geo Edit Clear",
            latitude=48.8566,
            longitude=2.3522,
            geo_source="camera",
        )
        csrf = csrf_of(admin_client, "/cameras")
        resp = admin_client.post(
            f"/cameras/{camera_id}/edit",
            data={
                "name": "Geo Edit Clear",
                "address": _ALLOWED_ADDRESS,
                "protocol": "vapix",
                "latitude": "",
                "longitude": "",
                "geo_source": "manual",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 200, resp.text
        lat, lon, source = _camera_geo(camera_id)
        assert lat is None
        assert lon is None
        assert source is None

    def test_edit_preserves_geo_when_fields_absent(
        self, admin_client: TestClient
    ) -> None:
        """A form submission that omits geo fields entirely leaves stored values intact.

        The edit handler touches geo columns only when at least one geo field is
        present in the submission (geo_in_form logic). Omitting all three leaves
        the row unchanged.
        """
        camera_id = _seed_camera(
            name="Geo Edit Preserve",
            latitude=10.0,
            longitude=20.0,
            geo_source="camera",
        )
        csrf = csrf_of(admin_client, "/cameras")
        # Submit a form with no latitude/longitude/geo_source keys.
        resp = admin_client.post(
            f"/cameras/{camera_id}/edit",
            data={
                "name": "Geo Edit Preserve",
                "address": _ALLOWED_ADDRESS,
                "protocol": "vapix",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 200, resp.text
        lat, lon, source = _camera_geo(camera_id)
        assert lat == pytest.approx(10.0)
        assert lon == pytest.approx(20.0)
        assert source == "camera"


# ---------------------------------------------------------------------------
# WP-69-T (CAM-S6): non-finite coordinates are refused on create and edit
# ---------------------------------------------------------------------------

_NON_FINITE = [("nan", "2.0"), ("2.0", "nan"), ("inf", "2.0"), ("2.0", "-inf")]


class TestNonFiniteCoordinatesRefused:
    @pytest.mark.parametrize(("lat", "lon"), _NON_FINITE)
    def test_create_refuses_non_finite(
        self, admin_client: TestClient, lat: str, lon: str
    ) -> None:
        name = f"Geo NonFinite Create {lat} {lon}"
        csrf = csrf_of(admin_client, "/cameras")
        resp = admin_client.post(
            "/cameras",
            data={
                "name": name,
                "address": _ALLOWED_ADDRESS,
                "protocol": "vapix",
                "latitude": lat,
                "longitude": lon,
                "geo_source": "manual",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 400, resp.text
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            assert db.query(Camera).filter(Camera.name == name).one_or_none() is None

    @pytest.mark.parametrize(("lat", "lon"), _NON_FINITE)
    def test_edit_refuses_non_finite(
        self, admin_client: TestClient, lat: str, lon: str
    ) -> None:
        name = f"Geo NonFinite Edit {lat} {lon}"
        camera_id = _seed_camera(
            name=name, latitude=10.0, longitude=20.0, geo_source="manual"
        )
        csrf = csrf_of(admin_client, "/cameras")
        resp = admin_client.post(
            f"/cameras/{camera_id}/edit",
            data={
                "name": name,
                "address": _ALLOWED_ADDRESS,
                "protocol": "vapix",
                "latitude": lat,
                "longitude": lon,
                "geo_source": "manual",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        # The edit form re-renders inline at 200 with the error message.
        assert resp.status_code == 200, resp.text
        assert "must be between" in resp.text
        assert _camera_geo(camera_id) == (10.0, 20.0, "manual")
