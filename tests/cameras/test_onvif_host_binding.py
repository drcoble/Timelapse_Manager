"""A device cannot send the camera's login to another host (#69 CAM-S5, #193).

An ONVIF device answers ``GetSnapshotUri``, ``GetStreamUri``, ``GetServices`` and
``CreatePullPointSubscription`` with URIs of its own choosing, and the camera's
credentials go to whatever host they name. Decision for #193: **refuse** a
device-supplied URI whose host is not the configured camera's, rather than add
a per-camera opt-in. An operator-stored ``snapshot_uri`` / ``stream_uri`` is
the explicit per-camera acceptance of its host and is used as stored.

Each test records every request the mock transport receives, so "never sent to
the other host" is checked on the wire, not inferred from an exception.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from tests.conftest import make_jpeg
from timelapse_manager.cameras.base import (
    OtherCaptureError,
    UnreachableCaptureError,
)
from timelapse_manager.cameras.onvif import OnvifAdapter

_CAMERA = "10.0.0.5"
_ROGUE = "10.0.0.66"  # private and allowed by the SSRF policy: only binding stops it
_NS = (
    'xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
    'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
    'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
    'xmlns:tev="http://www.onvif.org/ver10/events/wsdl" '
    'xmlns:tt="http://www.onvif.org/ver10/schema" '
    'xmlns:wsa5="http://www.w3.org/2005/08/addressing"'
)


def _envelope(body: str) -> str:
    return (
        f'<?xml version="1.0"?><s:Envelope {_NS}><s:Body>{body}</s:Body></s:Envelope>'
    )


def _uri_response(kind: str, uri: str) -> str:
    return _envelope(
        f"<trt:Get{kind}UriResponse><trt:MediaUri><tt:Uri>{uri}</tt:Uri>"
        f"</trt:MediaUri></trt:Get{kind}UriResponse>"
    )


_PROFILES = _envelope(
    '<trt:GetProfilesResponse><trt:Profiles token="p1"/></trt:GetProfilesResponse>'
)


@pytest.fixture(autouse=True)
def _ssrf_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow ``10.0.0.0/8``, so only the host binding can refuse the rogue host."""
    ssrf = SimpleNamespace(allowed_private_subnets=["10.0.0.0/8"])
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            ssrf=ssrf, capture=SimpleNamespace(max_image_bytes=1_000_000)
        )
    )
    monkeypatch.setattr("timelapse_manager.runtime.get_context", lambda: ctx)


class _Device:
    """A mock ONVIF device with attacker-chosen answers; records every request."""

    def __init__(
        self,
        *,
        snapshot: str | None = None,
        stream: str | None = None,
        services_xaddr: str | None = None,
        subscription: str | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.stream = stream
        self.services_xaddr = services_xaddr
        self.subscription = subscription
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith(".jpg"):
            return httpx.Response(200, content=make_jpeg(64, 48))
        body = request.content.decode()
        if "GetProfiles" in body:
            return httpx.Response(200, text=_PROFILES)
        if "GetSnapshotUri" in body and self.snapshot:
            return httpx.Response(200, text=_uri_response("Snapshot", self.snapshot))
        if "GetStreamUri" in body and self.stream:
            return httpx.Response(200, text=_uri_response("Stream", self.stream))
        if "GetServices" in body and self.services_xaddr:
            return httpx.Response(
                200,
                text=_envelope(
                    "<tds:GetServicesResponse><tds:Service>"
                    f"<tds:Namespace>{'http://www.onvif.org/ver10/events/wsdl'}"
                    f"</tds:Namespace><tds:XAddr>{self.services_xaddr}</tds:XAddr>"
                    "</tds:Service></tds:GetServicesResponse>"
                ),
            )
        if "CreatePullPointSubscription" in body and self.subscription:
            return httpx.Response(
                200,
                text=_envelope(
                    "<tev:CreatePullPointSubscriptionResponse>"
                    "<tev:SubscriptionReference>"
                    f"<wsa5:Address>{self.subscription}</wsa5:Address>"
                    "</tev:SubscriptionReference>"
                    "</tev:CreatePullPointSubscriptionResponse>"
                ),
            )
        return httpx.Response(200, text="<nothing/>")

    def hosts(self) -> set[str]:
        return {request.url.host for request in self.requests}

    def sent_credentials_to(self, host: str) -> bool:
        """Whether any request to ``host`` carried the login (header or SOAP)."""
        for request in self.requests:
            if request.url.host != host:
                continue
            if request.headers.get("authorization") or b"secret-pw" in request.content:
                return True
            if b"Password" in request.content:
                return True
        return False


def _adapter(device: _Device, **kwargs: object) -> OnvifAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(device.handle))
    return OnvifAdapter(
        client,
        kwargs.pop("address", _CAMERA),
        credentials=("u", "secret-pw"),
        **kwargs,  # type: ignore[arg-type]
    )


class TestSnapshotUri:
    async def test_another_host_is_refused_and_nothing_is_sent_to_it(self) -> None:
        device = _Device(snapshot=f"http://{_ROGUE}/snap.jpg")
        adapter = _adapter(device)
        with (
            patch("timelapse_manager.cameras.onvif.RtspAdapter") as rtsp,
            pytest.raises(UnreachableCaptureError, match="another host"),
        ):
            await adapter.capture()
        assert device.hosts() == {_CAMERA}
        assert not device.sent_credentials_to(_ROGUE)
        # Refused: not cached, and not an "other" failure that would fall back to
        # an RTSP grab with the same credentials.
        assert adapter._snapshot_uri is None  # noqa: SLF001
        rtsp.assert_not_called()

    async def test_the_error_names_the_host_never_the_uri(self) -> None:
        device = _Device(snapshot=f"http://{_ROGUE}/snap.jpg?token=abc")
        with pytest.raises(UnreachableCaptureError) as caught:
            await _adapter(device).capture()
        assert _ROGUE in str(caught.value)
        assert "token" not in str(caught.value)

    async def test_the_configured_host_works_whatever_scheme_and_port(self) -> None:
        device = _Device(snapshot=f"https://{_CAMERA}:8443/snap.jpg")
        frame = await _adapter(device).capture()
        assert frame.width == 64
        assert device.hosts() == {_CAMERA}

    async def test_a_name_that_resolves_to_the_cameras_address_is_accepted(
        self,
    ) -> None:
        # Added by name; the device reports its IP.
        device = _Device(snapshot=f"http://{_CAMERA}/snap.jpg")
        adapter = _adapter(device, address="cam.lan")

        async def resolve(host: str, **_kwargs: object) -> tuple[str, ...]:
            return {"cam.lan": (_CAMERA,), _CAMERA: (_CAMERA,)}[host]

        with (
            patch("timelapse_manager.cameras.onvif.pinned_addresses_async", resolve),
            patch(
                "timelapse_manager.cameras.onvif._device_url",
                lambda a: f"http://{_CAMERA}/d",
            ),
        ):
            await adapter._resolve_snapshot_uri()  # noqa: SLF001
        assert adapter._snapshot_uri == f"http://{_CAMERA}/snap.jpg"  # noqa: SLF001

    async def test_another_name_sharing_the_cameras_address_is_refused(self) -> None:
        # DNS rebinding: the capture client is not pinned, so it would resolve
        # ``other.name`` again at connect time. Only an IP literal is an alias.
        device = _Device(snapshot="http://other.name/snap.jpg")
        adapter = _adapter(device, address="cam.lan")

        async def resolve(host: str, **_kwargs: object) -> tuple[str, ...]:
            return (_CAMERA,)  # every name resolves to the camera, for now

        with (
            patch("timelapse_manager.cameras.onvif.pinned_addresses_async", resolve),
            patch(
                "timelapse_manager.cameras.onvif._device_url",
                lambda address: f"http://{_CAMERA}/d",
            ),
            pytest.raises(UnreachableCaptureError, match="another host"),
        ):
            await adapter._resolve_snapshot_uri()  # noqa: SLF001
        assert "other.name" not in device.hosts()
        assert adapter._snapshot_uri is None  # noqa: SLF001

    async def test_a_literal_the_name_does_not_resolve_to_is_refused(self) -> None:
        device = _Device(snapshot=f"http://{_ROGUE}/snap.jpg")
        adapter = _adapter(device, address="cam.lan")

        async def resolve(host: str, **_kwargs: object) -> tuple[str, ...]:
            return (_CAMERA,)

        with (
            patch("timelapse_manager.cameras.onvif.pinned_addresses_async", resolve),
            patch(
                "timelapse_manager.cameras.onvif._device_url",
                lambda address: f"http://{_CAMERA}/d",
            ),
            pytest.raises(UnreachableCaptureError),
        ):
            await adapter._resolve_snapshot_uri()  # noqa: SLF001

    async def test_a_name_resolving_elsewhere_is_refused(self) -> None:
        device = _Device(snapshot=f"http://{_ROGUE}/snap.jpg")
        adapter = _adapter(device)

        async def resolve(host: str, **_kwargs: object) -> tuple[str, ...]:
            return (host,)

        with (
            patch("timelapse_manager.cameras.onvif.pinned_addresses_async", resolve),
            pytest.raises(UnreachableCaptureError),
        ):
            await adapter._resolve_snapshot_uri()  # noqa: SLF001

    @pytest.mark.parametrize(
        "uri",
        [
            f"http://attacker:pw@{_CAMERA}/snap.jpg",  # userinfo
            f"http://evil\\@{_CAMERA}/snap.jpg",  # backslash
            f"http://{_CAMERA} .evil/snap.jpg",  # whitespace
            "http:///snap.jpg",  # no host
        ],
    )
    async def test_a_malformed_uri_is_refused(self, uri: str) -> None:
        device = _Device(snapshot=uri)
        with pytest.raises(UnreachableCaptureError, match="malformed"):
            await _adapter(device).capture()

    async def test_an_operator_stored_uri_is_the_explicit_acceptance(self) -> None:
        # Stored on the camera form: not a device answer, used as stored.
        device = _Device()
        adapter = _adapter(device, snapshot_uri=f"http://{_ROGUE}/snap.jpg")
        await adapter.capture()
        assert device.hosts() == {_ROGUE}


class TestStreamUri:
    async def test_another_host_is_refused_before_ffmpeg(self) -> None:
        device = _Device(stream=f"rtsp://{_ROGUE}:554/live")
        adapter = _adapter(device)
        with pytest.raises(UnreachableCaptureError, match="another host"):
            await adapter._resolve_stream_uri()  # noqa: SLF001
        assert adapter._stream_uri is None  # noqa: SLF001

    async def test_rtsp_on_the_configured_host_is_accepted(self) -> None:
        device = _Device(stream=f"rtsp://{_CAMERA}:554/live")
        adapter = _adapter(device)
        with patch("timelapse_manager.cameras.onvif._guard_stream_uri", AsyncMock()):
            assert await adapter._resolve_stream_uri() == f"rtsp://{_CAMERA}:554/live"  # noqa: SLF001

    async def test_resolve_uris_for_discovery_yields_none_for_a_refused_uri(
        self,
    ) -> None:
        device = _Device(
            snapshot=f"http://{_ROGUE}/snap.jpg", stream=f"rtsp://{_ROGUE}/live"
        )
        assert await _adapter(device).resolve_uris() == (None, None)


class TestEvents:
    async def test_a_services_xaddr_on_another_host_falls_back(self) -> None:
        device = _Device(services_xaddr=f"http://{_ROGUE}/onvif/events")
        url = await _adapter(device)._resolve_events_url()  # noqa: SLF001
        assert url == f"http://{_CAMERA}/onvif/services"
        assert device.hosts() == {_CAMERA}

    async def test_a_services_xaddr_on_the_camera_is_used(self) -> None:
        device = _Device(services_xaddr=f"http://{_CAMERA}/onvif/events")
        url = await _adapter(device)._resolve_events_url()  # noqa: SLF001
        assert url == f"http://{_CAMERA}/onvif/events"

    async def test_a_subscription_address_on_another_host_is_refused(self) -> None:
        device = _Device(subscription=f"http://{_ROGUE}/onvif/services")
        adapter = _adapter(device)
        source = adapter.open_event_source()
        with pytest.raises(UnreachableCaptureError, match="another host"):
            await anext(source)
        assert device.hosts() == {_CAMERA}
        assert not device.sent_credentials_to(_ROGUE)


class TestOtherFailureStillFallsBack:
    async def test_a_missing_snapshot_endpoint_is_not_a_host_refusal(self) -> None:
        # Guards the error-type choice: only "no snapshot URI" is an
        # OtherCaptureError (which falls back to RTSP); a refused host is not.
        device = _Device(stream=f"rtsp://{_CAMERA}/live")
        adapter = _adapter(device)
        with (
            patch("timelapse_manager.cameras.onvif._guard_stream_uri", AsyncMock()),
            patch("timelapse_manager.cameras.onvif.RtspAdapter") as rtsp,
        ):
            rtsp.return_value.capture = AsyncMock(return_value="frame")
            assert await adapter.capture() == "frame"
        assert not issubclass(UnreachableCaptureError, OtherCaptureError)
