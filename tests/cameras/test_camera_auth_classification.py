"""Authentication failures are classified as AUTH everywhere (#69 CAM-R9, CAM-S1).

* ONVIF: a ``401``/``403``, or a SOAP fault naming ``NotAuthorized`` or
  ``FailedAuthentication``, is an :class:`AuthCaptureError`, and an auth failure
  never falls back to an RTSP grab (the same login would fail there too, and
  camera health must see it as an auth failure).
* VAPIX SOAP: two ``WWW-Authenticate`` lines (Basic + Digest) are answered with
  Digest, and the adapter's auth state is reused on the next call.
* The adapter factory hands each HTTP-based adapter the session's auth state, and
  a saved camera's Basic-over-HTTP flag reaches the policy.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from tests.unit.camera_auth_support import FakeAxisCamera
from timelapse_manager.cameras import vapix
from timelapse_manager.cameras.base import (
    AuthCaptureError,
    OtherCaptureError,
    ValidationFailure,
)
from timelapse_manager.cameras.http_auth import BasicPolicy, CameraAuthState
from timelapse_manager.cameras.onvif import OnvifAdapter, soap_auth_failure
from timelapse_manager.cameras.registry import auth_state_for, build_adapter

_NOT_AUTHORIZED = (
    '<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/'
    'soap-envelope" xmlns:ter="http://www.onvif.org/ver10/error"><s:Body>'
    "<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode><s:Value>"
    "ter:NotAuthorized</s:Value></s:Subcode></s:Code></s:Fault></s:Body>"
    "</s:Envelope>"
)
_FAILED_AUTH = _NOT_AUTHORIZED.replace("ter:NotAuthorized", "wsse:FailedAuthentication")
_OTHER_FAULT = _NOT_AUTHORIZED.replace("ter:NotAuthorized", "ter:ActionNotSupported")


@pytest.fixture(autouse=True)
def _open_guards() -> Iterator[None]:
    with (
        patch.object(OnvifAdapter, "_guard_soap_url", AsyncMock()),
        patch.object(OnvifAdapter, "_guard_event_url", AsyncMock()),
        patch("timelapse_manager.cameras.http_jpeg._guard_snapshot_url"),
        patch(
            "timelapse_manager.cameras.http_jpeg._resolve_max_image_bytes",
            return_value=1 << 20,
        ),
    ):
        yield


def _onvif(handler: object) -> OnvifAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return OnvifAdapter(client, "10.0.0.5", credentials=("u", "p"))


class TestSoapAuthFailure:
    @pytest.mark.parametrize(
        ("status", "body", "auth"),
        [
            (401, "", True),
            (403, "", True),
            (400, _NOT_AUTHORIZED, True),
            (500, _NOT_AUTHORIZED, True),
            (400, _FAILED_AUTH, True),
            (400, _OTHER_FAULT, False),
            (500, "", False),
            (404, _NOT_AUTHORIZED, False),
        ],
    )
    def test_classification(self, status: int, body: str, auth: bool) -> None:
        assert soap_auth_failure(status, body) is auth


class TestOnvifSoapCalls:
    @pytest.mark.parametrize(
        ("status", "body"), [(401, ""), (400, _NOT_AUTHORIZED), (500, _FAILED_AUTH)]
    )
    async def test_device_call_auth_failure_is_auth(
        self, status: int, body: str
    ) -> None:
        adapter = _onvif(lambda _r: httpx.Response(status, text=body))
        with pytest.raises(AuthCaptureError) as caught:
            await adapter._soap_call("http://10.0.0.5/onvif/device_service", "<x/>")
        assert caught.value.reason is ValidationFailure.AUTH

    async def test_event_call_auth_fault_is_auth(self) -> None:
        adapter = _onvif(lambda _r: httpx.Response(400, text=_NOT_AUTHORIZED))
        with pytest.raises(AuthCaptureError):
            await adapter._guarded_event_soap_call(
                "http://10.0.0.5/onvif/services", "<x/>", action="urn:a"
            )

    async def test_other_faults_stay_other(self) -> None:
        adapter = _onvif(lambda _r: httpx.Response(400, text=_OTHER_FAULT))
        with pytest.raises(OtherCaptureError, match="returned 400"):
            await adapter._soap_call("http://10.0.0.5/onvif/device_service", "<x/>")


class TestOnvifRtspFallback:
    async def test_an_auth_failure_never_falls_back_to_rtsp(self) -> None:
        adapter = _onvif(lambda _r: httpx.Response(400, text=_NOT_AUTHORIZED))
        with (
            patch("timelapse_manager.cameras.onvif.RtspAdapter") as rtsp,
            pytest.raises(AuthCaptureError),
        ):
            await adapter.capture()
        rtsp.assert_not_called()

    async def test_a_missing_snapshot_endpoint_still_falls_back(self) -> None:
        profiles = (
            '<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/'
            '05/soap-envelope" xmlns:trt="http://www.onvif.org/ver10/media/wsdl">'
            '<s:Body><trt:GetProfilesResponse><trt:Profiles token="p1"/>'
            "</trt:GetProfilesResponse></s:Body></s:Envelope>"
        )
        stream = profiles.replace(
            '<trt:GetProfilesResponse><trt:Profiles token="p1"/>'
            "</trt:GetProfilesResponse>",
            "<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri xmlns:tt="
            '"http://www.onvif.org/ver10/schema">rtsp://10.0.0.5/s</tt:Uri>'
            "</trt:MediaUri></trt:GetStreamUriResponse>",
        )

        def handler(request: httpx.Request) -> httpx.Response:
            body = request.content.decode()
            if "GetProfiles" in body:
                return httpx.Response(200, text=profiles)
            if "GetSnapshotUri" in body:
                return httpx.Response(200, text="<nothing/>")
            return httpx.Response(200, text=stream)

        adapter = _onvif(handler)
        with (
            patch("timelapse_manager.cameras.onvif._guard_stream_uri"),
            patch("timelapse_manager.cameras.onvif.RtspAdapter") as rtsp,
        ):
            rtsp.return_value.capture = AsyncMock(return_value="frame")
            assert await adapter.capture() == "frame"
        rtsp.assert_called_once()


class TestVapixSoapNegotiation:
    async def test_post_soap_answers_basic_plus_digest_with_digest_and_reuses(
        self,
    ) -> None:
        camera = FakeAxisCamera(body=lambda _r: b"<ok/>")
        client = httpx.AsyncClient(transport=camera.transport())
        adapter = vapix.VapixAdapter(
            client, "10.0.0.5", credentials=(camera.username, camera.password)
        )
        assert await adapter._post_soap("http://10.0.0.5/vapix/services", "<x/>")
        assert await adapter._post_soap("http://10.0.0.5/vapix/services", "<x/>")

        assert len(camera.requests) == 3  # challenge, answer, then up-front reuse
        assert camera.accepted == ["digest", "digest"]

    async def test_post_soap_basic_only_over_http_is_refused(self) -> None:
        camera = FakeAxisCamera(offer_digest=False, body=lambda _r: b"<ok/>")
        client = httpx.AsyncClient(transport=camera.transport())
        adapter = vapix.VapixAdapter(
            client, "10.0.0.5", credentials=(camera.username, camera.password)
        )
        with pytest.raises(AuthCaptureError, match="https://"):
            await adapter._post_soap("http://10.0.0.5/vapix/services", "<x/>")
        assert len(camera.requests) == 1


class TestAdapterFactory:
    def test_the_session_state_reaches_every_http_adapter(self) -> None:
        state = CameraAuthState()
        client = httpx.AsyncClient()
        for protocol in ("http", "vapix", "onvif"):
            camera = SimpleNamespace(
                protocol=protocol,
                address="https://cam.test",
                credentials=None,
                credentials_inherit_default=False,
                snapshot_uri="https://cam.test/snap.jpg",
                stream_uri=None,
                default_resolution=None,
            )
            adapter = build_adapter(camera, client, auth_state=state)
            assert adapter._auth_state is state  # type: ignore[attr-defined]

    @pytest.mark.parametrize("flag", [False, True])
    def test_a_saved_camera_flag_sets_the_policy(self, flag: bool) -> None:
        state = auth_state_for(SimpleNamespace(allow_basic_over_http=flag))
        assert state.policy == BasicPolicy.for_camera(flag)

    def test_a_probe_without_the_attribute_gets_the_probe_policy(self) -> None:
        assert auth_state_for(SimpleNamespace()).policy == BasicPolicy.for_probe()
