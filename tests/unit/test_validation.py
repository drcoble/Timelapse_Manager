"""Unit tests for ValidationResult typing and build_adapter factory validation.

Covers:
- ValidationFailure is a str-mixin enum (each value is a plain str)
- ValidationResult fields typed correctly
- build_adapter raises ValueError on None protocol
- build_adapter raises ValueError on unknown protocol
- build_adapter raises ValueError when required field is missing per protocol
- build_adapter succeeds with valid minimal config per protocol

No network or filesystem access.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from timelapse_manager.cameras.base import (
    ValidationFailure,
    ValidationResult,
)
from timelapse_manager.cameras.registry import build_adapter

# ---------------------------------------------------------------------------
# ValidationFailure enum
# ---------------------------------------------------------------------------


class TestValidationFailureEnum:
    def test_auth_value_is_string(self) -> None:
        assert ValidationFailure.AUTH == "auth"
        assert isinstance(ValidationFailure.AUTH, str)

    def test_unreachable_value_is_string(self) -> None:
        assert ValidationFailure.UNREACHABLE == "unreachable"
        assert isinstance(ValidationFailure.UNREACHABLE, str)

    def test_timeout_value_is_string(self) -> None:
        assert ValidationFailure.TIMEOUT == "timeout"
        assert isinstance(ValidationFailure.TIMEOUT, str)

    def test_unsupported_codec_value_is_string(self) -> None:
        assert ValidationFailure.UNSUPPORTED_CODEC == "unsupported_codec"
        assert isinstance(ValidationFailure.UNSUPPORTED_CODEC, str)

    def test_other_value_is_string(self) -> None:
        assert ValidationFailure.OTHER == "other"
        assert isinstance(ValidationFailure.OTHER, str)

    def test_members_count(self) -> None:
        # Exactly the five documented failure reasons exist.
        assert len(ValidationFailure) == 5


class TestValidationResult:
    def test_ok_true_has_none_reason(self) -> None:
        result = ValidationResult(ok=True, reason=None, message="connected")
        assert result.ok is True
        assert result.reason is None
        assert result.message == "connected"

    def test_ok_false_has_reason(self) -> None:
        result = ValidationResult(
            ok=False, reason=ValidationFailure.AUTH, message="bad creds"
        )
        assert result.ok is False
        assert result.reason is ValidationFailure.AUTH

    def test_reason_value_is_string(self) -> None:
        result = ValidationResult(
            ok=False, reason=ValidationFailure.TIMEOUT, message="timed out"
        )
        # The reason value should equal the plain string "timeout"
        assert result.reason == "timeout"


# ---------------------------------------------------------------------------
# build_adapter: error cases
# ---------------------------------------------------------------------------


def _make_camera(**kwargs):
    """Return a simple namespace mimicking a Camera ORM row."""
    cam = MagicMock()
    cam.protocol = kwargs.get("protocol")
    cam.address = kwargs.get("address")
    cam.credentials = kwargs.get("credentials")
    cam.snapshot_uri = kwargs.get("snapshot_uri")
    cam.stream_uri = kwargs.get("stream_uri")
    cam.default_resolution = kwargs.get("default_resolution")
    return cam


class TestBuildAdapterErrors:
    def setup_method(self) -> None:
        self._http_client = httpx.AsyncClient()

    def teardown_method(self) -> None:
        import asyncio

        asyncio.get_event_loop().run_until_complete(self._http_client.aclose())

    def test_raises_when_protocol_is_none(self) -> None:
        cam = _make_camera(protocol=None)
        with pytest.raises(ValueError, match="no protocol"):
            build_adapter(cam, self._http_client)

    def test_raises_when_protocol_is_unknown(self) -> None:
        cam = _make_camera(protocol="ftp")
        with pytest.raises(ValueError, match="unsupported camera protocol"):
            build_adapter(cam, self._http_client)

    def test_http_protocol_raises_when_snapshot_uri_missing(self) -> None:
        cam = _make_camera(protocol="http", snapshot_uri=None)
        with pytest.raises(ValueError, match="snapshot_uri"):
            build_adapter(cam, self._http_client)

    def test_rtsp_protocol_raises_when_stream_uri_missing(self) -> None:
        cam = _make_camera(protocol="rtsp", stream_uri=None)
        with pytest.raises(ValueError, match="stream_uri"):
            build_adapter(cam, self._http_client)

    def test_vapix_protocol_raises_when_address_missing(self) -> None:
        cam = _make_camera(protocol="vapix", address=None)
        with pytest.raises(ValueError, match="address"):
            build_adapter(cam, self._http_client)

    def test_onvif_protocol_raises_when_address_missing(self) -> None:
        cam = _make_camera(protocol="onvif", address=None)
        with pytest.raises(ValueError, match="address"):
            build_adapter(cam, self._http_client)


class TestBuildAdapterSuccess:
    def setup_method(self) -> None:
        self._http_client = httpx.AsyncClient()

    def teardown_method(self) -> None:
        import asyncio

        asyncio.get_event_loop().run_until_complete(self._http_client.aclose())

    def test_http_adapter_created_with_snapshot_uri(self) -> None:
        from timelapse_manager.cameras.http_jpeg import HttpJpegAdapter

        cam = _make_camera(
            protocol="http", snapshot_uri="http://cam.local/snapshot.jpg"
        )
        adapter = build_adapter(cam, self._http_client)
        assert isinstance(adapter, HttpJpegAdapter)

    def test_rtsp_adapter_created_with_stream_uri(self) -> None:
        from timelapse_manager.cameras.rtsp import RtspAdapter

        cam = _make_camera(protocol="rtsp", stream_uri="rtsp://cam.local/stream")
        adapter = build_adapter(cam, self._http_client)
        assert isinstance(adapter, RtspAdapter)

    def test_vapix_adapter_created_with_address(self) -> None:
        from timelapse_manager.cameras.vapix import VapixAdapter

        cam = _make_camera(protocol="vapix", address="10.0.0.1")
        adapter = build_adapter(cam, self._http_client)
        assert isinstance(adapter, VapixAdapter)

    def test_onvif_adapter_created_with_address(self) -> None:
        from timelapse_manager.cameras.onvif import OnvifAdapter

        cam = _make_camera(protocol="onvif", address="10.0.0.1")
        adapter = build_adapter(cam, self._http_client)
        assert isinstance(adapter, OnvifAdapter)
