"""Unit tests for discovery URI enrichment.

Covers:
- ``OnvifAdapter.resolve_uris`` returns both URIs on success, ``(None, None)``
  when both resolvers fail (without raising), and a partial pair when one fails.
- ``resolve_discovered_uris`` enriches an ONVIF camera with the supplied
  credentials, leaves a non-ONVIF camera untouched, skips a camera whose address
  is denied by the SSRF guard, isolates a per-camera failure/timeout, passes the
  credentials through to the adapter, and never overwrites an already-set URI.

No real network sockets are opened; the SOAP resolvers and the adapter are
mocked throughout.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx

from timelapse_manager.cameras.base import (
    DiscoveredCamera,
    OtherCaptureError,
    UnreachableCaptureError,
)
from timelapse_manager.cameras.discovery import resolve_discovered_uris
from timelapse_manager.cameras.onvif import OnvifAdapter
from timelapse_manager.security.ssrf import SsrfError

_ENRICH = "timelapse_manager.cameras.discovery"


def _onvif(
    address: str = "192.0.2.10",
    snapshot_uri: str | None = None,
    stream_uri: str | None = None,
) -> DiscoveredCamera:
    return DiscoveredCamera(
        address=address,
        protocol="onvif",
        snapshot_uri=snapshot_uri,
        stream_uri=stream_uri,
        geolocation=None,
        vendor=None,
    )


# ---------------------------------------------------------------------------
# OnvifAdapter.resolve_uris
# ---------------------------------------------------------------------------


class TestResolveUris:
    async def test_returns_both_when_resolvers_succeed(self) -> None:
        client = httpx.AsyncClient()
        try:
            adapter = OnvifAdapter(client, address="192.0.2.10")
            with (
                patch.object(
                    adapter,
                    "_resolve_snapshot_uri",
                    AsyncMock(return_value="http://192.0.2.10/snap.jpg"),
                ),
                patch.object(
                    adapter,
                    "_resolve_stream_uri",
                    AsyncMock(return_value="rtsp://192.0.2.10/stream"),
                ),
            ):
                result = await adapter.resolve_uris()
            assert result == (
                "http://192.0.2.10/snap.jpg",
                "rtsp://192.0.2.10/stream",
            )
        finally:
            await client.aclose()

    async def test_returns_none_pair_when_both_fail(self) -> None:
        client = httpx.AsyncClient()
        try:
            adapter = OnvifAdapter(client, address="192.0.2.10")
            with (
                patch.object(
                    adapter,
                    "_resolve_snapshot_uri",
                    AsyncMock(side_effect=OtherCaptureError("no snapshot")),
                ),
                patch.object(
                    adapter,
                    "_resolve_stream_uri",
                    AsyncMock(side_effect=UnreachableCaptureError("blocked")),
                ),
            ):
                result = await adapter.resolve_uris()
            assert result == (None, None)
        finally:
            await client.aclose()

    async def test_partial_one_ok_one_fails(self) -> None:
        client = httpx.AsyncClient()
        try:
            adapter = OnvifAdapter(client, address="192.0.2.10")
            # Snapshot resolves; the stream resolver fails (e.g. its SSRF guard
            # surfaced an UnreachableCaptureError for a poisoned stream host).
            with (
                patch.object(
                    adapter,
                    "_resolve_snapshot_uri",
                    AsyncMock(return_value="http://192.0.2.10/snap.jpg"),
                ),
                patch.object(
                    adapter,
                    "_resolve_stream_uri",
                    AsyncMock(side_effect=UnreachableCaptureError("stream blocked")),
                ),
            ):
                result = await adapter.resolve_uris()
            assert result == ("http://192.0.2.10/snap.jpg", None)
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# resolve_discovered_uris
# ---------------------------------------------------------------------------


class TestResolveDiscoveredUris:
    async def test_enriches_onvif_with_supplied_credentials(self) -> None:
        camera = _onvif()
        client = AsyncMock()
        resolve = AsyncMock(
            return_value=("http://192.0.2.10/snap.jpg", "rtsp://192.0.2.10/s")
        )
        with (
            patch(f"{_ENRICH}.resolve_camera_host", side_effect=lambda a: a),
            patch.object(OnvifAdapter, "resolve_uris", resolve),
            patch.object(OnvifAdapter, "close", AsyncMock()),
        ):
            result = await resolve_discovered_uris([camera], ("joe", "secret"), client)
        assert result[0].snapshot_uri == "http://192.0.2.10/snap.jpg"
        assert result[0].stream_uri == "rtsp://192.0.2.10/s"

    async def test_credentials_reach_the_adapter(self) -> None:
        camera = _onvif()
        client = AsyncMock()
        captured: dict[str, object] = {}

        original_init = OnvifAdapter.__init__

        def _spy_init(self: OnvifAdapter, *args: object, **kwargs: object) -> None:
            captured["credentials"] = kwargs.get("credentials")
            original_init(self, *args, **kwargs)  # type: ignore[arg-type]

        with (
            patch(f"{_ENRICH}.resolve_camera_host", side_effect=lambda a: a),
            patch.object(OnvifAdapter, "__init__", _spy_init),
            patch.object(
                OnvifAdapter, "resolve_uris", AsyncMock(return_value=(None, None))
            ),
            patch.object(OnvifAdapter, "close", AsyncMock()),
        ):
            await resolve_discovered_uris([camera], ("joe", "secret"), client)
        assert captured["credentials"] == ("joe", "secret")

    async def test_non_onvif_camera_left_untouched(self) -> None:
        camera = DiscoveredCamera(
            address="192.0.2.20",
            protocol="rtsp",
            snapshot_uri=None,
            stream_uri=None,
            geolocation=None,
            vendor=None,
        )
        client = AsyncMock()
        resolve = AsyncMock(return_value=("http://x/snap", "rtsp://x/s"))
        with (
            patch(f"{_ENRICH}.resolve_camera_host", side_effect=lambda a: a),
            patch.object(OnvifAdapter, "resolve_uris", resolve),
            patch.object(OnvifAdapter, "close", AsyncMock()),
        ):
            result = await resolve_discovered_uris([camera], ("joe", "pw"), client)
        assert result[0].snapshot_uri is None
        assert result[0].stream_uri is None
        resolve.assert_not_called()

    async def test_denied_address_is_skipped(self) -> None:
        camera = _onvif(address="192.0.2.30")
        client = AsyncMock()
        resolve = AsyncMock(return_value=("http://x/snap", "rtsp://x/s"))
        with (
            patch(
                f"{_ENRICH}.resolve_camera_host",
                side_effect=SsrfError("address denied"),
            ),
            patch.object(OnvifAdapter, "resolve_uris", resolve),
            patch.object(OnvifAdapter, "close", AsyncMock()),
        ):
            result = await resolve_discovered_uris([camera], ("joe", "pw"), client)
        assert result[0].snapshot_uri is None
        assert result[0].stream_uri is None
        # A denied address never builds an adapter or resolves anything.
        resolve.assert_not_called()

    async def test_one_failure_does_not_sink_the_others(self) -> None:
        good = _onvif(address="192.0.2.40")
        bad = _onvif(address="192.0.2.41")

        async def _resolve(self: OnvifAdapter) -> tuple[str | None, str | None]:
            if self._address == "192.0.2.41":
                # A non-OSError, non-TimeoutError failure proves the per-camera
                # isolation is general (mirrors detect_protocols' broad catch),
                # not limited to network-shaped errors.
                raise ValueError("unexpected device fault")
            return ("http://192.0.2.40/snap.jpg", "rtsp://192.0.2.40/s")

        client = AsyncMock()
        with (
            patch(f"{_ENRICH}.resolve_camera_host", side_effect=lambda a: a),
            patch.object(OnvifAdapter, "resolve_uris", _resolve),
            patch.object(OnvifAdapter, "close", AsyncMock()),
        ):
            result = await resolve_discovered_uris([good, bad], ("joe", "pw"), client)
        assert result[0].snapshot_uri == "http://192.0.2.40/snap.jpg"
        # The failing camera is returned unchanged.
        assert result[1].snapshot_uri is None
        assert result[1].stream_uri is None

    async def test_already_set_uri_not_overwritten(self) -> None:
        camera = _onvif(snapshot_uri="http://operator/confirmed.jpg", stream_uri=None)
        client = AsyncMock()
        resolve = AsyncMock(return_value=("http://device/other.jpg", "rtsp://device/s"))
        with (
            patch(f"{_ENRICH}.resolve_camera_host", side_effect=lambda a: a),
            patch.object(OnvifAdapter, "resolve_uris", resolve),
            patch.object(OnvifAdapter, "close", AsyncMock()),
        ):
            result = await resolve_discovered_uris([camera], ("joe", "pw"), client)
        # The pre-set snapshot is preserved; only the None stream is filled.
        assert result[0].snapshot_uri == "http://operator/confirmed.jpg"
        assert result[0].stream_uri == "rtsp://device/s"

    async def test_camera_with_both_uris_skips_resolution(self) -> None:
        camera = _onvif(snapshot_uri="http://x/snap.jpg", stream_uri="rtsp://x/s")
        client = AsyncMock()
        resolve = AsyncMock(return_value=("http://new/snap", "rtsp://new/s"))
        with (
            patch(f"{_ENRICH}.resolve_camera_host", side_effect=lambda a: a),
            patch.object(OnvifAdapter, "resolve_uris", resolve),
            patch.object(OnvifAdapter, "close", AsyncMock()),
        ):
            await resolve_discovered_uris([camera], ("joe", "pw"), client)
        resolve.assert_not_called()
