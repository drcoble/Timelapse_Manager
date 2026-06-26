"""Unit tests for geolocation resolution precedence.

Covers:
- manual_override: returns GeoLocation when source=='manual' with both coords
- manual_override: returns None when source is not 'manual'
- manual_override: returns None when lat or lon missing
- get_camera_geolocation: manual override wins over device geo
- get_camera_geolocation: adapter geo returned when no manual override
- get_camera_geolocation: adapter geo returned when camera is None
- get_camera_geolocation: adapter errors are suppressed, returns None

No network access.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from timelapse_manager.cameras.base import GeoLocation
from timelapse_manager.cameras.geolocation import (
    get_camera_geolocation,
    manual_override,
)

# ---------------------------------------------------------------------------
# manual_override
# ---------------------------------------------------------------------------


class TestManualOverride:
    def _camera(self, source, lat=None, lon=None):
        cam = MagicMock()
        cam.geolocation_source = source
        cam.geolocation_latitude = lat
        cam.geolocation_longitude = lon
        return cam

    def test_returns_geolocation_when_manual_with_both_coords(self) -> None:
        cam = self._camera("manual", lat=51.5, lon=-0.1)
        geo = manual_override(cam)
        assert geo is not None
        assert geo.latitude == pytest.approx(51.5)
        assert geo.longitude == pytest.approx(-0.1)
        assert geo.source == "manual"

    def test_returns_none_when_source_is_camera(self) -> None:
        cam = self._camera("camera", lat=51.5, lon=-0.1)
        assert manual_override(cam) is None

    def test_returns_none_when_source_is_none(self) -> None:
        cam = self._camera(None, lat=51.5, lon=-0.1)
        assert manual_override(cam) is None

    def test_returns_none_when_latitude_missing(self) -> None:
        cam = self._camera("manual", lat=None, lon=-0.1)
        assert manual_override(cam) is None

    def test_returns_none_when_longitude_missing(self) -> None:
        cam = self._camera("manual", lat=51.5, lon=None)
        assert manual_override(cam) is None

    def test_returns_none_when_both_coords_missing(self) -> None:
        cam = self._camera("manual", lat=None, lon=None)
        assert manual_override(cam) is None

    def test_coordinates_coerced_to_float(self) -> None:
        cam = self._camera("manual", lat="51.5", lon="-0.1")
        geo = manual_override(cam)
        assert geo is not None
        assert isinstance(geo.latitude, float)
        assert isinstance(geo.longitude, float)


# ---------------------------------------------------------------------------
# get_camera_geolocation
# ---------------------------------------------------------------------------


class TestGetCameraGeolocation:
    def _make_adapter(self, geo=None, raises=False):
        adapter = MagicMock()
        if raises:
            adapter.get_geolocation = AsyncMock(
                side_effect=RuntimeError("device geo failed")
            )
        else:
            adapter.get_geolocation = AsyncMock(return_value=geo)
        return adapter

    def _camera(self, source=None, lat=None, lon=None):
        cam = MagicMock()
        cam.geolocation_source = source
        cam.geolocation_latitude = lat
        cam.geolocation_longitude = lon
        return cam

    async def test_manual_override_wins_over_device_geo(self) -> None:
        device_geo = GeoLocation(latitude=0.0, longitude=0.0, source="camera")
        adapter = self._make_adapter(geo=device_geo)
        cam = self._camera("manual", lat=51.5, lon=-0.1)

        geo = await get_camera_geolocation(adapter, camera=cam)

        assert geo is not None
        assert geo.source == "manual"
        assert geo.latitude == pytest.approx(51.5)
        # Adapter must not have been called
        adapter.get_geolocation.assert_not_called()

    async def test_device_geo_returned_when_no_manual_override(self) -> None:
        device_geo = GeoLocation(latitude=40.7128, longitude=-74.006, source="camera")
        adapter = self._make_adapter(geo=device_geo)
        cam = self._camera("camera", lat=40.0, lon=-74.0)  # source != "manual"

        geo = await get_camera_geolocation(adapter, camera=cam)

        assert geo is not None
        assert geo.source == "camera"
        adapter.get_geolocation.assert_called_once()

    async def test_returns_adapter_geo_when_camera_is_none(self) -> None:
        device_geo = GeoLocation(latitude=35.6762, longitude=139.6503, source="camera")
        adapter = self._make_adapter(geo=device_geo)

        geo = await get_camera_geolocation(adapter, camera=None)

        assert geo is not None
        assert geo.latitude == pytest.approx(35.6762)

    async def test_adapter_geo_none_propagates(self) -> None:
        adapter = self._make_adapter(geo=None)
        cam = self._camera("camera")

        geo = await get_camera_geolocation(adapter, camera=cam)

        assert geo is None

    async def test_adapter_error_is_suppressed_returns_none(self) -> None:
        adapter = self._make_adapter(raises=True)
        cam = self._camera("camera")

        # Should not raise; errors from adapter.get_geolocation are swallowed
        geo = await get_camera_geolocation(adapter, camera=cam)

        assert geo is None
