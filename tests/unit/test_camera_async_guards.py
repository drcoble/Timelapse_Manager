"""Camera SSRF checks and the RTSP probe do not block the event loop (#69 CAM-P3/P4).

The adapters' pre-request checks used the blocking resolver inline, so one slow
DNS answer stalled every capture. They now resolve in a worker thread through
the shared helper, and the RTSP port probe is a plain asyncio connection under a
timeout. Every guard keeps its verdicts: a denied or unresolvable host is an
:class:`UnreachableCaptureError`.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Awaitable, Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from timelapse_manager.cameras import http_jpeg, probing
from timelapse_manager.cameras.base import (
    UnreachableCaptureError,
    ValidationFailure,
)
from timelapse_manager.cameras.host_resolution import (
    camera_allowed_subnets,
    resolve_camera_host_async,
)
from timelapse_manager.cameras.onvif import OnvifAdapter
from timelapse_manager.cameras.vapix import VapixAdapter
from timelapse_manager.security import ssrf
from timelapse_manager.security.ssrf import SsrfError

_NAME = "axis-b8a44f17384a"


@pytest.fixture(autouse=True)
def _policy(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    context = MagicMock()
    context.settings.ssrf.allowed_private_subnets = ["10.0.16.0/24"]
    monkeypatch.setattr("timelapse_manager.runtime.get_context", lambda: context)
    ssrf.clear_resolution_cache()
    yield
    ssrf.clear_resolution_cache()


def _slow_resolver(
    monkeypatch: pytest.MonkeyPatch, answer: str, delay: float = 0.3
) -> list[str]:
    """Make ``getaddrinfo`` take ``delay`` seconds (blocking, as a real one does)."""
    calls: list[str] = []

    def getaddrinfo(host: str, *_args: object, **_kwargs: object) -> list[Any]:
        calls.append(host)
        time.sleep(delay)
        family = socket.AF_INET6 if ":" in answer else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (answer, 0))]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", getaddrinfo)
    return calls


async def _max_loop_lag(work: Awaitable[Any]) -> float:
    """Run ``work`` while a ticker measures how long the loop stays unresponsive."""
    lags: list[float] = []
    running = True

    async def ticker() -> None:
        while running:
            before = time.monotonic()
            await asyncio.sleep(0.005)
            lags.append(time.monotonic() - before - 0.005)

    tick = asyncio.create_task(ticker())
    try:
        await work
    finally:
        running = False
        await tick
    return max(lags)


def _guards() -> list[tuple[str, Callable[[], Awaitable[None]]]]:
    # The guards make no request; a never-used client holds no connection.
    client = httpx.AsyncClient()
    onvif = OnvifAdapter(client, address=_NAME)
    vapix = VapixAdapter(client, address=_NAME)
    url = f"https://{_NAME}/x"
    return [
        ("snapshot", lambda: http_jpeg._guard_snapshot_url(url)),
        ("onvif-soap", lambda: onvif._guard_soap_url(url)),
        ("onvif-event", lambda: onvif._guard_event_url(url)),
        ("vapix-event", lambda: vapix._guard_event_url(url)),
    ]


@pytest.mark.parametrize("index", range(4))
class TestGuardsDoNotBlockTheLoop:
    async def test_a_slow_lookup_does_not_stall_the_loop(
        self, index: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _slow_resolver(monkeypatch, "10.0.16.61")
        _label, guard = _guards()[index]
        lag = await _max_loop_lag(guard())
        assert calls == [_NAME]
        assert lag < 0.15, f"loop stalled {lag:.3f}s inside the guard"

    async def test_a_denied_answer_is_unreachable(
        self, index: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _slow_resolver(monkeypatch, "127.0.0.1", delay=0)
        _label, guard = _guards()[index]
        with pytest.raises(UnreachableCaptureError, match="blocked"):
            await guard()

    async def test_an_unresolvable_host_is_unreachable(
        self, index: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def nxdomain(*_a: object, **_k: object) -> list[Any]:
            raise socket.gaierror("nxdomain")

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", nxdomain)
        _label, guard = _guards()[index]
        with pytest.raises(UnreachableCaptureError, match="blocked"):
            await guard()

    async def test_a_repeat_check_is_served_from_the_cache(
        self, index: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _slow_resolver(monkeypatch, "10.0.16.61", delay=0)
        _label, guard = _guards()[index]
        await guard()
        await guard()
        assert calls == [_NAME]


class TestRtspProbe:
    @pytest.fixture(autouse=True)
    def _validated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.addresses: tuple[str, ...] = ("10.0.16.61",)
        monkeypatch.setattr(
            probing,
            "pinned_addresses_async",
            AsyncMock(side_effect=lambda *_a, **_k: self.addresses),
        )

    async def test_a_blackholed_port_honours_its_timeout(self) -> None:
        async def blackhole(*_args: object, **_kwargs: object) -> Any:
            await asyncio.sleep(30)

        with patch.object(probing.asyncio, "open_connection", blackhole):
            started = time.monotonic()
            lag_probe = probing._probe_rtsp(_NAME, timeout=0.2)
            candidate = await lag_probe
        assert time.monotonic() - started < 2.0
        assert candidate.ok is False
        assert candidate.reason is ValidationFailure.TIMEOUT

    async def test_a_blackholed_port_holds_no_thread_and_no_loop_time(self) -> None:
        async def blackhole(*_args: object, **_kwargs: object) -> Any:
            await asyncio.sleep(30)

        with patch.object(probing.asyncio, "open_connection", blackhole):
            lag = await _max_loop_lag(probing._probe_rtsp(_NAME, timeout=0.3))
        assert lag < 0.15

    async def test_a_refused_port_is_unreachable(self) -> None:
        with patch.object(
            probing.asyncio,
            "open_connection",
            AsyncMock(side_effect=ConnectionRefusedError("refused")),
        ):
            candidate = await probing._probe_rtsp(_NAME, timeout=1.0)
        assert candidate.ok is False
        assert candidate.reason is ValidationFailure.UNREACHABLE

    async def test_a_dual_stack_camera_falls_back_to_the_next_address(self) -> None:
        self.addresses = ("fd00::61", "10.0.16.61")
        writer = MagicMock()
        attempts: list[str] = []

        async def open_connection(host: str, port: int) -> Any:
            attempts.append(host)
            if ":" in host:
                raise OSError("network unreachable")
            return MagicMock(), writer

        with patch.object(probing.asyncio, "open_connection", open_connection):
            candidate = await probing._probe_rtsp(_NAME, timeout=1.0)
        assert attempts == ["fd00::61", "10.0.16.61"]
        assert candidate.ok is True
        writer.close.assert_called_once()

    async def test_a_denied_host_is_never_connected_to(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            probing,
            "pinned_addresses_async",
            AsyncMock(side_effect=SsrfError("target 127.0.0.1 is denied")),
        )
        opener = AsyncMock()
        with patch.object(probing.asyncio, "open_connection", opener):
            candidate = await probing._probe_rtsp("127.0.0.1", timeout=1.0)
        assert candidate.ok is False
        assert candidate.reason is ValidationFailure.UNREACHABLE
        opener.assert_not_called()


class TestResolveCameraHostAsync:
    async def test_returns_an_allowed_address_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _slow_resolver(monkeypatch, "10.0.16.61", delay=0)
        assert await resolve_camera_host_async(_NAME) == _NAME

    async def test_an_empty_address_passes_through(self) -> None:
        assert await resolve_camera_host_async("") == ""

    async def test_an_unresolvable_name_is_allowed_through_like_the_sync_seam(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def nxdomain(*_a: object, **_k: object) -> list[Any]:
            raise socket.gaierror("nxdomain")

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", nxdomain)
        assert await resolve_camera_host_async(_NAME) == _NAME

    @pytest.mark.parametrize("address", ["127.0.0.1", "::1", "169.254.169.254"])
    async def test_a_denied_literal_is_rejected(self, address: str) -> None:
        with pytest.raises(SsrfError):
            await resolve_camera_host_async(address)

    async def test_a_slow_lookup_does_not_stall_the_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _slow_resolver(monkeypatch, "10.0.16.61")
        lag = await _max_loop_lag(resolve_camera_host_async(_NAME))
        assert lag < 0.15


class TestCameraAllowedSubnets:
    def test_reads_the_current_setting(self) -> None:
        assert camera_allowed_subnets() == ("10.0.16.0/24",)

    def test_without_a_context_there_is_no_opt_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_context() -> Any:
            raise RuntimeError("no context")

        monkeypatch.setattr("timelapse_manager.runtime.get_context", no_context)
        assert camera_allowed_subnets() == ()
