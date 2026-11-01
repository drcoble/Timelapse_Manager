"""Abuse tests: the SSRF guard on the camera *stream* URI (ffmpeg input).

The deny-list is validated at camera-add time against the device *address*, but
the RTSP/ONVIF stream URI that is actually handed to ffmpeg is separately
attacker-influenceable (an ONVIF ``GetStreamUri`` SOAP response is returned by
the device). These tests prove that URI is routed through the guard *before*
ffmpeg opens any socket, that a configured private camera still works, that the
check runs on every capture (DNS-rebinding via a cached/re-resolved URI), and
that the device-controlled SOAP URI is rejected before it is cached.

Adversarial invariant under test: when a stream URI's host is denied, ffmpeg is
**never spawned** -- the guard fails the capture before
``asyncio.create_subprocess_exec`` is reached.

No real network or subprocess: ``create_subprocess_exec`` is replaced with a
sentinel that records whether it was called (and fails the test if a denied URI
ever reaches it), and DNS resolution is exercised only against literal IPs so no
real lookup occurs.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from tests.conftest import make_jpeg
from timelapse_manager.cameras.base import UnreachableCaptureError
from timelapse_manager.cameras.rtsp import RtspAdapter

# The admin opt-in used by the "configured private camera still works" case.
_ALLOWED_PRIVATE = ["10.0.0.0/8", "192.168.0.0/16"]


@pytest.fixture()
def _ssrf_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a minimal app context exposing only ``settings.ssrf``.

    The stream-URI guard reads ``get_context().settings.ssrf.allowed_private_subnets``
    and nothing else, so a lightweight stand-in avoids building a full context
    (engine, session factory, ...) for a pure-guard test.
    """
    ssrf = SimpleNamespace(allowed_private_subnets=list(_ALLOWED_PRIVATE))
    ctx = SimpleNamespace(settings=SimpleNamespace(ssrf=ssrf))
    monkeypatch.setattr("timelapse_manager.runtime.get_context", lambda: ctx)


class _FfmpegSpawnSentinel:
    """Replacement for ``asyncio.create_subprocess_exec`` that records calls.

    If a *denied* stream URI ever reaches ffmpeg, the guard has failed open; the
    sentinel records the invocation so the test can assert it was never called.
    On the *allowed* path the sentinel returns a fake process that yields a
    minimal valid JPEG, so the capture completes and we can assert the guard let
    it through.
    """

    def __init__(self) -> None:
        self.called = False
        self.calls: list[tuple[Any, ...]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.called = True
        self.calls.append(args)
        return _FakeProcess()


class _FakeProcess:
    """A stand-in ffmpeg process returning a tiny valid JPEG on stdout."""

    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        # A structurally whole JPEG (SOI, SOF with real dimensions, EOI): the
        # RTSP frame build rejects a body without readable dimensions (#69
        # CAM-R11).
        return (make_jpeg(1, 1), b"")

    def kill(self) -> None:  # pragma: no cover - not reached on the happy path
        return None

    async def wait(self) -> int:  # pragma: no cover - not reached
        return 0


@pytest.fixture()
def _ffmpeg_sentinel(monkeypatch: pytest.MonkeyPatch) -> _FfmpegSpawnSentinel:
    """Patch the ffmpeg spawn point and hand the sentinel back to the test."""
    sentinel = _FfmpegSpawnSentinel()
    monkeypatch.setattr(
        "timelapse_manager.cameras.rtsp.asyncio.create_subprocess_exec",
        sentinel,
    )
    return sentinel


# ---------------------------------------------------------------------------
# Defect 1: a denied stream URI is rejected before ffmpeg is ever spawned
# ---------------------------------------------------------------------------


@pytest.mark.abuse
class TestStreamUriDeniedBeforeFfmpeg:
    @pytest.mark.parametrize(
        "denied_host",
        [
            "127.0.0.1",  # loopback
            "169.254.169.254",  # cloud-metadata endpoint
            "172.31.5.5",  # RFC-1918 (172.16/12) but NOT in the admin opt-in
        ],
    )
    async def test_denied_stream_host_never_reaches_ffmpeg(
        self,
        denied_host: str,
        _ssrf_context: None,
        _ffmpeg_sentinel: _FfmpegSpawnSentinel,
    ) -> None:
        adapter = RtspAdapter(stream_url=f"rtsp://{denied_host}:554/stream")
        with pytest.raises(UnreachableCaptureError):
            await adapter.capture()
        assert _ffmpeg_sentinel.called is False, (
            "ffmpeg must not be spawned for a denied stream URI"
        )

    async def test_denied_host_with_embedded_credentials_still_blocked(
        self,
        _ssrf_context: None,
        _ffmpeg_sentinel: _FfmpegSpawnSentinel,
    ) -> None:
        """Credentials in the URL userinfo must not let a denied host slip past.

        The host is extracted with urlsplit (which strips ``user:pass@``); the
        guard sees the host, not the credentials, and still blocks loopback.
        """
        adapter = RtspAdapter(
            stream_url="rtsp://127.0.0.1/stream",
            credentials=("admin", "s3cr3t"),
        )
        with pytest.raises(UnreachableCaptureError):
            await adapter.capture()
        assert _ffmpeg_sentinel.called is False

    async def test_rtsps_scheme_also_guarded(
        self,
        _ssrf_context: None,
        _ffmpeg_sentinel: _FfmpegSpawnSentinel,
    ) -> None:
        """An RTSPS URL is dialed by ffmpeg too, so its host must be guarded."""
        adapter = RtspAdapter(stream_url="rtsps://169.254.169.254/stream")
        with pytest.raises(UnreachableCaptureError):
            await adapter.capture()
        assert _ffmpeg_sentinel.called is False


# ---------------------------------------------------------------------------
# Configured private camera still works (the "don't break real cameras" proof)
# ---------------------------------------------------------------------------


@pytest.mark.abuse
class TestConfiguredPrivateCameraAllowed:
    async def test_allowed_private_stream_reaches_ffmpeg(
        self,
        _ssrf_context: None,
        _ffmpeg_sentinel: _FfmpegSpawnSentinel,
    ) -> None:
        """A stream host inside an admin-allowed subnet passes the guard.

        This is the explicit guarantee that a legitimately-configured private
        camera (e.g. an Axis on 10.0.0.x) keeps capturing: the guard allows it
        and ffmpeg *is* spawned. Real live RTSP capture is re-verified separately
        on the test box; this asserts the guard does not block it.
        """
        adapter = RtspAdapter(stream_url="rtsp://10.0.0.42:554/stream")
        frame = await adapter.capture()
        assert _ffmpeg_sentinel.called is True
        assert frame.image_bytes  # the fake ffmpeg produced a frame


# ---------------------------------------------------------------------------
# Defect 3: per-capture re-validation (DNS-rebinding via a cached/re-resolved URI)
# ---------------------------------------------------------------------------


@pytest.mark.abuse
class TestPerCaptureRevalidation:
    async def test_cached_uri_is_rechecked_on_every_capture(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _ssrf_context: None,
        _ffmpeg_sentinel: _FfmpegSpawnSentinel,
    ) -> None:
        """A host that is good on capture #1 but rebinds to loopback on #2 is
        rejected on #2 -- proving the guard runs per capture, not once.

        The same ``RtspAdapter`` (a fixed stream URL) is captured twice; the
        resolver is swapped between calls so the second resolution returns a
        denied address. The first capture must spawn ffmpeg; the second must be
        rejected before ffmpeg is reached.
        """
        # First resolution: allowed private. Second: loopback (rebind).
        resolutions = iter(["10.0.0.7", "127.0.0.1"])

        def fake_getaddrinfo(host: str, *args: Any, **kwargs: Any) -> list[Any]:
            addr = next(resolutions)
            return [(None, None, None, "", (addr, 0))]

        # Use a hostname so resolution (not a literal IP) is exercised each time.
        monkeypatch.setattr(
            "timelapse_manager.security.ssrf.socket.getaddrinfo", fake_getaddrinfo
        )
        adapter = RtspAdapter(stream_url="rtsp://camera.example.test:554/stream")

        # Capture #1: resolves to an allowed private address -> ffmpeg spawned.
        await adapter.capture()
        assert _ffmpeg_sentinel.called is True
        first_spawn_count = len(_ffmpeg_sentinel.calls)

        # Capture #2: same URL re-resolves to loopback -> rejected, no new spawn.
        with pytest.raises(UnreachableCaptureError):
            await adapter.capture()
        assert len(_ffmpeg_sentinel.calls) == first_spawn_count, (
            "the re-resolved (rebound) host must be rejected before ffmpeg"
        )


# ---------------------------------------------------------------------------
# Defect 1 (ONVIF): the device-controlled SOAP stream URI is rejected pre-cache
# ---------------------------------------------------------------------------


@pytest.mark.abuse
class TestOnvifSoapStreamUriGuarded:
    async def test_soap_resolved_loopback_uri_rejected_and_not_cached(
        self,
        _ssrf_context: None,
    ) -> None:
        """A rogue ONVIF device that returns a loopback stream URI is rejected
        at ``_resolve_stream_uri`` -- before the URI is cached or used.
        """
        import httpx

        from timelapse_manager.cameras.onvif import OnvifAdapter

        adapter = OnvifAdapter(
            client=httpx.AsyncClient(),
            address="10.0.0.5",
        )

        # Bypass the SOAP round-trip: pretend the device returned a loopback URI.
        async def fake_soap_call(url: str, body: str) -> str:
            return (
                "<env:Envelope xmlns:env='http://www.w3.org/2003/05/soap-envelope'"
                " xmlns:trt='http://www.onvif.org/ver10/media/wsdl'"
                " xmlns:tt='http://www.onvif.org/ver10/schema'>"
                "<env:Body><trt:GetStreamUriResponse><trt:MediaUri>"
                "<tt:Uri>rtsp://127.0.0.1:554/stream</tt:Uri>"
                "</trt:MediaUri></trt:GetStreamUriResponse></env:Body>"
                "</env:Envelope>"
            )

        # Pretend a profile token is already resolved.
        adapter._profile_token = "profile0"  # noqa: SLF001
        adapter._soap_call = fake_soap_call  # type: ignore[method-assign]

        with pytest.raises(UnreachableCaptureError):
            await adapter._resolve_stream_uri()  # noqa: SLF001
        # The poisoned URI must NOT have been cached.
        assert adapter._stream_uri is None  # noqa: SLF001
        await adapter.close()


# ---------------------------------------------------------------------------
# The device/media SOAP chokepoint (_soap_call) guards its target before POST
# ---------------------------------------------------------------------------


class _SoapPostSentinel(httpx.AsyncClient):
    """Stand-in httpx client recording whether a SOAP POST was ever issued.

    If a *denied* device URL ever reaches the POST, the guard has failed open;
    the sentinel records it so the test can assert it was never called. On the
    *allowed* path it returns a minimal GetProfilesResponse so ``_soap_call``
    completes and we can assert the guard let the request through. It is a real
    client over a mock transport, so the streamed, size-capped read runs too.
    """

    def __init__(self) -> None:
        self.posted = False
        super().__init__(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.posted = True
        return httpx.Response(
            200,
            text=(
                "<env:Envelope"
                " xmlns:env='http://www.w3.org/2003/05/soap-envelope'"
                " xmlns:trt='http://www.onvif.org/ver10/media/wsdl'>"
                "<env:Body><trt:GetProfilesResponse>"
                "<trt:Profiles token='profile0'/>"
                "</trt:GetProfilesResponse></env:Body></env:Envelope>"
            ),
        )


@pytest.mark.abuse
class TestOnvifSoapDeviceUrlGuarded:
    """Every device/media SOAP request funnels through ``_soap_call``.

    Its target URL is built from the operator-supplied camera address; a denied
    address (loopback, link-local/metadata, or a private host outside the admin
    opt-in) must be rejected before the POST so an internal SOAP request can
    never be aimed at it. This covers GetProfiles, GetSnapshotUri, GetStreamUri,
    GetServices, GetGeoLocation, and GetHostname at the one shared chokepoint.
    """

    @pytest.mark.parametrize(
        "denied_host",
        [
            "127.0.0.1",  # loopback
            "169.254.169.254",  # cloud-metadata endpoint
            "172.31.5.5",  # RFC-1918 (172.16/12) but NOT in the admin opt-in
        ],
    )
    async def test_denied_soap_target_never_posts(
        self,
        denied_host: str,
        _ssrf_context: None,
    ) -> None:
        from timelapse_manager.cameras.onvif import OnvifAdapter, _device_url

        sentinel = _SoapPostSentinel()
        adapter = OnvifAdapter(client=sentinel, address=denied_host)
        with pytest.raises(UnreachableCaptureError):
            await adapter._soap_call(  # noqa: SLF001
                _device_url(denied_host), "<trt:GetProfiles/>"
            )
        assert sentinel.posted is False, (
            "a denied device URL must be rejected before the SOAP POST"
        )

    async def test_allowed_private_soap_target_reaches_post(
        self,
        _ssrf_context: None,
    ) -> None:
        """A device inside an admin-allowed subnet passes the guard and POSTs.

        The explicit "don't break real cameras" guarantee: a legitimately
        configured private camera (e.g. an Axis on 10.0.0.x) still issues its
        SOAP request.
        """
        from timelapse_manager.cameras.onvif import OnvifAdapter, _device_url

        sentinel = _SoapPostSentinel()
        adapter = OnvifAdapter(client=sentinel, address="10.0.0.42")
        text = await adapter._soap_call(  # noqa: SLF001
            _device_url("10.0.0.42"), "<trt:GetProfiles/>"
        )
        assert sentinel.posted is True
        assert "GetProfilesResponse" in text
