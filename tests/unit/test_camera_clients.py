"""Separate camera HTTP clients; a local pool wait is not a camera failure (#69 CAM-P2).

* Captures, event listeners and probes each get their own client and pool.
* ``httpx.PoolTimeout`` (no free local connection) maps to a *local*
  :class:`OtherCaptureError` at every adapter request site, with the original
  error kept as ``__cause__``; other timeouts stay camera timeouts.
* The pool wait is bounded below the capture budget, so an exhausted pool is
  reported as local from inside a real capture rather than as a capture timeout.
* A probe burst that fills the probe pool does not delay a capture.

The pool tests use a real :class:`httpx.AsyncClient` against a local asyncio
server: ``httpx.MockTransport`` has no connection pool.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic import ValidationError

from tests.unit.camera_net_support import LocalHttpServer, allow_loopback_dial
from timelapse_manager.cameras import http_jpeg
from timelapse_manager.cameras.base import (
    CaptureError,
    OtherCaptureError,
    TimeoutCaptureError,
)
from timelapse_manager.cameras.clients import CameraHttpClients, request_timeout
from timelapse_manager.cameras.http_jpeg import HttpJpegAdapter, http_get_image
from timelapse_manager.cameras.onvif import OnvifAdapter
from timelapse_manager.cameras.vapix import VapixAdapter
from timelapse_manager.capture.camera_health import is_local_failure
from timelapse_manager.capture.supervisor import CaptureSupervisor, CaptureTarget
from timelapse_manager.config.settings import (
    CaptureSettings,
    DatabaseSettings,
    LoggingSettings,
    PathsSettings,
    Settings,
)


@pytest.fixture(autouse=True)
def _loopback_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the snapshot helper reach the local test server.

    The SSRF guard always denies loopback, and the body cap is read from the
    application context, which a unit test has not installed.
    """
    monkeypatch.setattr(http_jpeg, "_guard_snapshot_url", AsyncMock())
    allow_loopback_dial(monkeypatch)
    monkeypatch.setattr(http_jpeg, "_resolve_max_image_bytes", lambda: 1024 * 1024)


@pytest.fixture
async def server() -> AsyncIterator[LocalHttpServer]:
    async with LocalHttpServer() as srv:
        yield srv


async def _wait_for_hanging(server: LocalHttpServer, count: int) -> None:
    for _ in range(200):
        if server.hanging >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{server.hanging} of {count} requests reached the server")


def _make_settings(tmp_path: Path, **capture: Any) -> Settings:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    return Settings(
        database=DatabaseSettings(url=f"sqlite:///{tmp_path / 'test.db'}"),
        logging=LoggingSettings(level="WARNING", format="text"),
        paths=PathsSettings(
            data_dir=data_dir,
            frames_root=data_dir / "frames",
            token_file=data_dir / ".local-token",
        ),
        capture=CaptureSettings(autostart=False, **capture),
    )


class TestClientsAndSettings:
    async def test_three_distinct_clients_by_purpose(self) -> None:
        clients = CameraHttpClients(CaptureSettings())
        try:
            assert clients.for_purpose("capture") is clients.capture
            assert clients.for_purpose("events") is clients.events
            assert clients.for_purpose("probe") is clients.probe
            assert (
                len({id(clients.capture), id(clients.events), id(clients.probe)}) == 3
            )
            with pytest.raises(ValueError, match="purpose"):
                clients.for_purpose("render")  # type: ignore[arg-type]
        finally:
            await clients.aclose()
        assert clients.capture.is_closed
        assert clients.events.is_closed
        assert clients.probe.is_closed

    async def test_redirects_stay_off(self) -> None:
        clients = CameraHttpClients(CaptureSettings())
        try:
            for purpose in ("capture", "events", "probe"):
                assert clients.for_purpose(purpose).follow_redirects is False
        finally:
            await clients.aclose()

    async def test_pool_wait_is_half_the_capture_timeout(self) -> None:
        clients = CameraHttpClients(CaptureSettings(timeout_seconds=8.0))
        try:
            for purpose in ("capture", "events", "probe"):
                timeout = clients.for_purpose(purpose).timeout
                assert timeout.read == 8.0
                assert timeout.pool == 4.0
        finally:
            await clients.aclose()

    async def test_aclose_closes_every_client_when_one_fails(self) -> None:
        clients = CameraHttpClients(CaptureSettings())
        boom = RuntimeError("boom")
        real_close = clients.capture.aclose

        async def failing_close() -> None:
            await real_close()
            raise boom

        clients.capture.aclose = failing_close  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="boom"):
            await clients.aclose()
        assert clients.events.is_closed
        assert clients.probe.is_closed

    def test_defaults(self) -> None:
        capture = CaptureSettings()
        assert capture.http_max_connections == 24
        assert capture.http_keepalive_connections == 12
        assert capture.http_keepalive_expiry_seconds == 30.0
        assert capture.event_http_max_connections == 16
        assert capture.probe_http_max_connections == 12

    def test_keepalive_above_max_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="http_keepalive_connections"):
            CaptureSettings(http_max_connections=4, http_keepalive_connections=5)

    @pytest.mark.parametrize(
        "field",
        [
            "http_max_connections",
            "event_http_max_connections",
            "probe_http_max_connections",
        ],
    )
    def test_pool_size_must_be_positive(self, field: str) -> None:
        with pytest.raises(ValidationError):
            CaptureSettings(**{field: 0})


class TestRequestTimeout:
    async def test_keeps_the_client_pool_bound(self) -> None:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, pool=5.0)) as c:
            timeout = request_timeout(c, 10.0)
        assert (timeout.connect, timeout.read, timeout.write) == (10.0, 10.0, 10.0)
        assert timeout.pool == 5.0

    async def test_never_waits_longer_than_the_request(self) -> None:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, pool=5.0)) as c:
            assert request_timeout(c, 2.0).pool == 2.0

    def test_client_without_a_timeout_uses_the_request_value(self) -> None:
        assert request_timeout(MagicMock(spec=[]), 3.0).pool == 3.0


def _raising_client(exc: Exception) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _snapshot(client: httpx.AsyncClient) -> None:
    await http_get_image(client, "http://192.168.1.50/snap.jpg", None, 1.0)


async def _vapix_soap(client: httpx.AsyncClient) -> None:
    adapter = VapixAdapter(client, address="192.168.1.50")
    await adapter._post_soap("http://192.168.1.50/vapix/services", "<x/>")


async def _onvif_soap(client: httpx.AsyncClient) -> None:
    adapter = OnvifAdapter(client, address="192.168.1.50")
    adapter._guard_soap_url = AsyncMock()  # type: ignore[method-assign]
    await adapter._soap_call("http://192.168.1.50/onvif/device_service", "<x/>")


async def _onvif_event_soap(client: httpx.AsyncClient) -> None:
    adapter = OnvifAdapter(client, address="192.168.1.50")
    adapter._guard_event_url = AsyncMock()  # type: ignore[method-assign]
    await adapter._guarded_event_soap_call(
        "http://192.168.1.50/onvif/events", "<x/>", action="urn:test"
    )


_SITES: list[Callable[[httpx.AsyncClient], Any]] = [
    _snapshot,
    _vapix_soap,
    _onvif_soap,
    _onvif_event_soap,
]


class TestPoolTimeoutIsLocal:
    @pytest.mark.parametrize("site", _SITES, ids=lambda f: f.__name__)
    async def test_pool_timeout_maps_to_local_error(
        self, site: Callable[[httpx.AsyncClient], Any]
    ) -> None:
        async with _raising_client(httpx.PoolTimeout("pool")) as client:
            with pytest.raises(OtherCaptureError) as info:
                await site(client)
        assert info.value.local is True
        assert "local connection pool exhausted" in info.value.message
        assert isinstance(info.value.__cause__, httpx.PoolTimeout)
        assert is_local_failure(info.value)

    @pytest.mark.parametrize("site", _SITES, ids=lambda f: f.__name__)
    async def test_other_timeouts_stay_camera_timeouts(
        self, site: Callable[[httpx.AsyncClient], Any]
    ) -> None:
        async with _raising_client(httpx.ReadTimeout("slow")) as client:
            with pytest.raises(TimeoutCaptureError) as info:
                await site(client)
        assert info.value.local is False
        assert not is_local_failure(info.value)


class TestRealPool:
    async def test_exhausted_capture_pool_reports_local(
        self, server: LocalHttpServer
    ) -> None:
        clients = CameraHttpClients(
            CaptureSettings(
                timeout_seconds=1.0,
                http_max_connections=1,
                http_keepalive_connections=1,
            )
        )
        holder = asyncio.create_task(
            clients.capture.get(server.url("/hang"), timeout=30.0)
        )
        try:
            await _wait_for_hanging(server, 1)
            with pytest.raises(OtherCaptureError) as info:
                await http_get_image(clients.capture, server.url("/snap"), None, 1.0)
            assert info.value.local is True
            assert isinstance(info.value.__cause__, httpx.PoolTimeout)
        finally:
            server.release()
            await asyncio.gather(holder, return_exceptions=True)
            await clients.aclose()

    async def test_supervisor_capture_reports_pool_wait_as_local(
        self, server: LocalHttpServer, tmp_path: Path
    ) -> None:
        # The pool wait (0.5 s) ends before the capture budget (1.0 s), so the
        # capture fails with the local pool error instead of a timeout that
        # would count against the camera.
        settings = _make_settings(
            tmp_path,
            timeout_seconds=1.0,
            http_max_connections=1,
            http_keepalive_connections=1,
        )
        supervisor = CaptureSupervisor(settings, MagicMock())
        holder = asyncio.create_task(
            supervisor.http_client.get(server.url("/hang"), timeout=30.0)
        )
        try:
            await _wait_for_hanging(server, 1)
            adapter = HttpJpegAdapter(
                supervisor.http_client, server.url("/snap"), None, timeout=1.0
            )
            with pytest.raises(CaptureError) as info:
                await supervisor._capture_with_timeout(adapter)
            assert isinstance(info.value, OtherCaptureError)
            assert is_local_failure(info.value)
            pool_error = info.value
        finally:
            server.release()
            await asyncio.gather(holder, return_exceptions=True)
            await supervisor.stop()

        # Fed into camera health the way every capture path (interval, each
        # anchor attempt, one-shot) reports it, the pool error never takes the
        # camera offline; the same number of camera timeouts does.
        written: list[str] = []
        supervisor._write_camera_health_event = (  # type: ignore[method-assign]
            lambda transition: written.append(transition.kind)
        )
        target = CaptureTarget(
            project_id=1, project_name="p", camera_id=7, interval_seconds=None
        )
        threshold = settings.capture.offline_failure_threshold
        for _ in range(threshold + 2):
            await supervisor._report_camera_outcome(target, pool_error)
        assert not supervisor._camera_health.is_offline(7)
        assert written == ["local"]
        for _ in range(threshold):
            await supervisor._report_camera_outcome(
                target, TimeoutCaptureError("capture timed out")
            )
        assert supervisor._camera_health.is_offline(7)
        assert written == ["local", "offline"]

    async def test_probe_burst_does_not_delay_a_capture(
        self, server: LocalHttpServer
    ) -> None:
        clients = CameraHttpClients(
            CaptureSettings(timeout_seconds=5.0, probe_http_max_connections=1)
        )
        # Four parallel probes (a Detect burst): one holds the only probe
        # connection, three wait for it.
        probes = [
            asyncio.create_task(clients.probe.get(server.url("/hang")))
            for _ in range(4)
        ]
        try:
            await _wait_for_hanging(server, 1)
            started = time.monotonic()
            body = await http_get_image(clients.capture, server.url("/snap"), None, 5.0)
            elapsed = time.monotonic() - started
            assert body.startswith(b"\xff\xd8")
            assert elapsed < 1.0
            # The burst never got more than its own pool.
            assert server.hanging == 1
        finally:
            server.release()
            await asyncio.gather(*probes, return_exceptions=True)
            await clients.aclose()


class TestSupervisorClients:
    async def test_client_for_and_stop(self, tmp_path: Path) -> None:
        supervisor = CaptureSupervisor(_make_settings(tmp_path), MagicMock())
        capture = supervisor.client_for("capture")
        events = supervisor.client_for("events")
        probe = supervisor.client_for("probe")
        assert supervisor.http_client is capture
        assert len({id(capture), id(events), id(probe)}) == 3
        await supervisor.stop()
        assert capture.is_closed and events.is_closed and probe.is_closed
