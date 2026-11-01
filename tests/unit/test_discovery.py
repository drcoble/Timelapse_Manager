"""Unit tests for ONVIF/WS-Discovery helpers.

Covers:
- _probe_message builds valid UTF-8 XML with a UUID message ID
- _parse_probe_match extracts host and vendor from canned ProbeMatch XML
- _parse_probe_match returns None for malformed XML
- _parse_probe_match returns None when XAddrs absent
- _hosts_from_spec handles CIDR, dash-range, single IP; raises on invalid
- scan_range with an invalid range returns [] without raising
- scan_range host enumeration (patch _unicast_probe to avoid real sockets)
- the datagram endpoints, against responders on loopback (no executor threads)

The only sockets opened are on the loopback interface.
"""

from __future__ import annotations

import asyncio
import socket
import time
import typing
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from xml.etree import ElementTree as ET

import pytest

from timelapse_manager.cameras import discovery
from timelapse_manager.cameras.discovery import (
    _MAX_MULTICAST_BYTES,
    _MAX_MULTICAST_REPLIES,
    InvalidScanRange,
    ScanRangeTooLarge,
    _hosts_from_spec,
    _parse_probe_match,
    _probe_message,
    _unicast_probe,
    check_scan_range,
    count_hosts,
    scan_range,
)

# ---------------------------------------------------------------------------
# Helpers: minimal stub context for SSRF-settings lookup
# ---------------------------------------------------------------------------


@dataclass
class _StubSsrfSettings:
    allowed_private_subnets: list[str] = field(default_factory=list)
    max_scan_hosts: int = 1024


def _stub_context(
    allowed_private_subnets: list[str] | None = None,
    max_scan_hosts: int = 1024,
) -> MagicMock:
    """Return a minimal mock AppContext with configurable SSRF settings."""
    ssrf = _StubSsrfSettings(
        allowed_private_subnets=allowed_private_subnets or [],
        max_scan_hosts=max_scan_hosts,
    )
    ctx = MagicMock()
    ctx.settings.ssrf = ssrf
    return ctx


@pytest.fixture()
def private_scan_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch get_context to return a stub that opts in 10.0.0.0/8."""
    monkeypatch.setattr(
        "timelapse_manager.runtime.get_context",
        lambda: _stub_context(["10.0.0.0/8"]),
    )


@pytest.fixture()
def empty_scan_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch get_context to return a stub with no private opt-ins."""
    monkeypatch.setattr(
        "timelapse_manager.runtime.get_context",
        lambda: _stub_context([]),
    )


# ---------------------------------------------------------------------------
# Helpers: canned ProbeMatch XML
# ---------------------------------------------------------------------------

_PROBE_MATCH_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope
  xmlns:s="http://www.w3.org/2003/05/soap-envelope"
  xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
  xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
  <s:Body>
    <d:ProbeMatches>
      <d:ProbeMatch>
        <d:XAddrs>http://192.168.1.50/onvif/device_service</d:XAddrs>
        <d:Scopes>
          onvif://www.onvif.org/name/Axis
          onvif://www.onvif.org/hardware/Q6135-LE
        </d:Scopes>
      </d:ProbeMatch>
    </d:ProbeMatches>
  </s:Body>
</s:Envelope>"""

_PROBE_MATCH_NO_XADDRS = b"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope
  xmlns:s="http://www.w3.org/2003/05/soap-envelope"
  xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
  <s:Body><d:ProbeMatches><d:ProbeMatch/></d:ProbeMatches></s:Body>
</s:Envelope>"""


# ---------------------------------------------------------------------------
# _probe_message
# ---------------------------------------------------------------------------


class TestProbeMessage:
    def test_returns_bytes(self) -> None:
        msg = _probe_message()
        assert isinstance(msg, bytes)

    def test_is_valid_xml(self) -> None:
        msg = _probe_message()
        root = ET.fromstring(msg)
        assert root is not None

    def test_contains_uuid_message_id(self) -> None:
        msg = _probe_message().decode("utf-8")
        assert "uuid:" in msg

    def test_two_calls_produce_different_message_ids(self) -> None:
        msg1 = _probe_message().decode("utf-8")
        msg2 = _probe_message().decode("utf-8")
        # Extract the uuid: portion; they must differ
        import re

        ids = [re.search(r"uuid:[0-9a-f-]+", m) for m in (msg1, msg2)]
        assert ids[0] is not None and ids[1] is not None
        assert ids[0].group() != ids[1].group()

    def test_probe_targets_networkvideotrasmitter(self) -> None:
        msg = _probe_message().decode("utf-8")
        assert "NetworkVideoTransmitter" in msg


# ---------------------------------------------------------------------------
# _parse_probe_match
# ---------------------------------------------------------------------------


class TestParseProbeMatch:
    def test_extracts_host_from_xaddrs(self) -> None:
        camera = _parse_probe_match(_PROBE_MATCH_XML)
        assert camera is not None
        assert camera.address == "192.168.1.50"

    def test_protocol_is_onvif(self) -> None:
        camera = _parse_probe_match(_PROBE_MATCH_XML)
        assert camera is not None
        assert camera.protocol == "onvif"

    def test_extracts_vendor_from_scopes(self) -> None:
        camera = _parse_probe_match(_PROBE_MATCH_XML)
        assert camera is not None
        # Either "Axis" or "Q6135-LE" (first /name/ scope wins)
        assert camera.vendor is not None
        assert camera.vendor in ("Axis", "Q6135-LE")

    def test_snapshot_and_stream_uri_are_none_at_discovery(self) -> None:
        camera = _parse_probe_match(_PROBE_MATCH_XML)
        assert camera is not None
        assert camera.snapshot_uri is None
        assert camera.stream_uri is None

    def test_returns_none_for_malformed_xml(self) -> None:
        assert _parse_probe_match(b"NOT_XML") is None

    def test_returns_none_when_xaddrs_absent(self) -> None:
        assert _parse_probe_match(_PROBE_MATCH_NO_XADDRS) is None

    def test_returns_none_for_empty_bytes(self) -> None:
        assert _parse_probe_match(b"") is None


# ---------------------------------------------------------------------------
# _hosts_from_spec
# ---------------------------------------------------------------------------


class TestHostsFromSpec:
    def test_single_ip_returns_list_of_one(self) -> None:
        hosts = _hosts_from_spec("10.0.0.1")
        assert hosts == ["10.0.0.1"]

    def test_cidr_slash_30_returns_two_hosts(self) -> None:
        hosts = _hosts_from_spec("192.168.1.0/30")
        assert hosts == ["192.168.1.1", "192.168.1.2"]

    def test_dash_range_returns_correct_count(self) -> None:
        hosts = _hosts_from_spec("10.0.0.1-10.0.0.5")
        assert len(hosts) == 5
        assert hosts[0] == "10.0.0.1"
        assert hosts[-1] == "10.0.0.5"

    def test_single_ip_range_returns_one_host(self) -> None:
        hosts = _hosts_from_spec("10.0.0.1-10.0.0.1")
        assert hosts == ["10.0.0.1"]

    def test_reverse_range_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="range end precedes start"):
            _hosts_from_spec("10.0.0.10-10.0.0.1")

    def test_invalid_ip_raises_value_error(self) -> None:
        with pytest.raises((ValueError, Exception)):
            _hosts_from_spec("not.an.ip")

    def test_invalid_cidr_raises_value_error(self) -> None:
        with pytest.raises((ValueError, Exception)):
            _hosts_from_spec("999.999.999.0/24")


# ---------------------------------------------------------------------------
# count_hosts: arithmetic count that mirrors _hosts_from_spec
# ---------------------------------------------------------------------------


class TestCountHosts:
    def test_single_host_counts_one(self) -> None:
        assert count_hosts("10.0.0.1") == 1

    def test_dotted_range_is_inclusive(self) -> None:
        assert count_hosts("10.0.0.1-10.0.0.5") == 5

    def test_single_address_range_counts_one(self) -> None:
        assert count_hosts("10.0.0.7-10.0.0.7") == 1

    def test_cidr_24_drops_network_and_broadcast(self) -> None:
        # /24 has 256 addresses; .hosts() drops network + broadcast.
        assert count_hosts("192.168.1.0/24") == 254

    def test_cidr_31_counts_both_endpoints(self) -> None:
        assert count_hosts("192.168.1.0/31") == 2

    def test_cidr_32_counts_one(self) -> None:
        assert count_hosts("192.168.1.5/32") == 1

    def test_malformed_raises_invalid_scan_range(self) -> None:
        with pytest.raises(InvalidScanRange):
            count_hosts("192.168.1")

    def test_reversed_range_raises_invalid_scan_range(self) -> None:
        with pytest.raises(InvalidScanRange):
            count_hosts("10.0.0.10-10.0.0.1")

    @pytest.mark.parametrize(
        "spec",
        [
            "10.0.0.1",
            "192.168.1.0/24",
            "192.168.1.0/30",
            "192.168.1.0/31",
            "192.168.1.5/32",
            "10.0.0.1-10.0.0.5",
            "10.0.0.7-10.0.0.7",
        ],
    )
    def test_count_agrees_with_enumeration(self, spec: str) -> None:
        # The pre-scan count must equal the list the scan actually enumerates,
        # or the cap check guards a different size than the one that gets probed.
        assert count_hosts(spec) == len(_hosts_from_spec(spec))


# ---------------------------------------------------------------------------
# check_scan_range: shared parse + count + cap-check seam
# ---------------------------------------------------------------------------


class TestCheckScanRange:
    def test_within_cap_returns_count(self) -> None:
        assert check_scan_range("10.0.0.1-10.0.0.10", max_hosts=1024) == 10

    def test_exactly_at_cap_is_allowed(self) -> None:
        # A dotted range of exactly 1024 hosts sits on the boundary and passes;
        # no CIDR yields exactly 1024 under the network/broadcast rule.
        assert check_scan_range("10.0.0.0-10.0.3.255", max_hosts=1024) == 1024

    def test_one_over_cap_is_rejected(self) -> None:
        with pytest.raises(ScanRangeTooLarge) as info:
            check_scan_range("10.0.0.0-10.0.4.0", max_hosts=1024)
        assert info.value.host_count == 1025
        assert info.value.max_hosts == 1024

    def test_malformed_raises_invalid_scan_range(self) -> None:
        with pytest.raises(InvalidScanRange):
            check_scan_range("garbage", max_hosts=1024)


# ---------------------------------------------------------------------------
# scan_range: invalid range returns [], does not raise
# ---------------------------------------------------------------------------


class TestScanRangeInvalidRange:
    async def test_invalid_range_returns_empty_list_without_raising(
        self, empty_scan_context: None
    ) -> None:
        result = await scan_range("NOT_A_VALID_RANGE")
        assert result == []

    async def test_reversed_range_returns_empty_list_without_raising(
        self, empty_scan_context: None
    ) -> None:
        result = await scan_range("10.0.0.10-10.0.0.1")
        assert result == []

    async def test_oversized_range_raises_value_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A range that expands beyond max_scan_hosts must raise ValueError."""
        monkeypatch.setattr(
            "timelapse_manager.runtime.get_context",
            lambda: _stub_context(["10.0.0.0/8"], max_scan_hosts=5),
        )
        with pytest.raises(ValueError, match="over the limit"):
            await scan_range("10.0.0.1-10.0.0.10")


# ---------------------------------------------------------------------------
# scan_range: host enumeration via patched _unicast_probe
# ---------------------------------------------------------------------------


class TestScanRangeEnumeration:
    async def test_no_responding_hosts_returns_empty_list(
        self, private_scan_context: None
    ) -> None:
        with patch(
            "timelapse_manager.cameras.discovery._unicast_probe", return_value=None
        ):
            result = await scan_range("10.0.0.1-10.0.0.3", per_host_timeout=0.01)
        assert result == []

    async def test_one_responding_host_returns_one_camera(
        self, private_scan_context: None
    ) -> None:
        with patch(
            "timelapse_manager.cameras.discovery._unicast_probe",
            return_value=_PROBE_MATCH_XML,
        ):
            result = await scan_range("10.0.0.1-10.0.0.1", per_host_timeout=0.01)
        assert len(result) == 1
        # address is overwritten with the probed address, not XAddrs
        assert result[0].address == "10.0.0.1"

    async def test_probed_address_overrides_xaddrs_host(
        self, private_scan_context: None
    ) -> None:
        # The XAddrs in _PROBE_MATCH_XML says "192.168.1.50" but we probed
        # "10.0.0.2"; the probed address must win.
        with patch(
            "timelapse_manager.cameras.discovery._unicast_probe",
            return_value=_PROBE_MATCH_XML,
        ):
            result = await scan_range("10.0.0.2-10.0.0.2", per_host_timeout=0.01)
        assert result[0].address == "10.0.0.2"

    async def test_multiple_hosts_probed_all_respond(
        self, private_scan_context: None
    ) -> None:
        call_count = 0

        def _probe_all(host: str, timeout: float) -> bytes:
            nonlocal call_count
            call_count += 1
            return _PROBE_MATCH_XML

        with patch(
            "timelapse_manager.cameras.discovery._unicast_probe",
            side_effect=_probe_all,
        ):
            result = await scan_range("10.0.0.1-10.0.0.4", per_host_timeout=0.01)

        assert call_count == 4
        assert len(result) == 4


# ---------------------------------------------------------------------------
# WP-69-T (CAM-S4): overall deadline, reply caps, and source binding
# ---------------------------------------------------------------------------


def _probe_match_for(xaddrs: str) -> bytes:
    """Return a ProbeMatch datagram advertising ``xaddrs``."""
    return _PROBE_MATCH_XML.replace(
        b"http://192.168.1.50/onvif/device_service", xaddrs.encode()
    )


class _Responder(asyncio.DatagramProtocol):
    """A UDP responder on loopback that answers each probe on a schedule.

    ``replies`` is a list of ``(delay_seconds, datagram)`` sent back to whoever
    probed, in order; ``trickle`` instead sends ``trickle[1]`` every
    ``trickle[0]`` seconds until the test ends. ``probes`` counts what arrived.
    """

    def __init__(
        self,
        replies: list[tuple[float, bytes]] | None = None,
        trickle: tuple[float, bytes] | None = None,
    ) -> None:
        self._replies = replies or []
        self._trickle = trickle
        self._tasks: list[asyncio.Task[None]] = []
        self.transport: asyncio.DatagramTransport | None = None
        self.probes = 0

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = typing.cast(asyncio.DatagramTransport, transport)

    def datagram_received(self, data: bytes, addr: Any) -> None:
        self.probes += 1
        self._tasks.append(asyncio.ensure_future(self._answer(addr)))

    async def _answer(self, addr: Any) -> None:
        assert self.transport is not None
        if self._trickle is not None:
            delay, data = self._trickle
            while not self.transport.is_closing():
                await asyncio.sleep(delay)
                self.transport.sendto(data, addr)
        for delay, data in self._replies:
            await asyncio.sleep(delay)
            if self.transport.is_closing():
                return
            self.transport.sendto(data, addr)

    def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self.transport is not None:
            self.transport.close()

    @property
    def port(self) -> int:
        assert self.transport is not None
        return int(self.transport.get_extra_info("sockname")[1])


@pytest.fixture
async def responder_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Callable[..., Awaitable[_Responder]]]:
    """Start loopback responders and aim discovery's probe port at the last one."""
    started: list[_Responder] = []

    async def start(**kwargs: Any) -> _Responder:
        loop = asyncio.get_running_loop()
        _transport, protocol = await loop.create_datagram_endpoint(
            lambda: _Responder(**kwargs), local_addr=("127.0.0.1", 0)
        )
        started.append(protocol)
        monkeypatch.setattr(discovery, "WS_DISCOVERY_PORT", protocol.port)
        monkeypatch.setattr(discovery, "WS_DISCOVERY_ADDRESS", "127.0.0.1")
        return protocol

    yield start
    for protocol in started:
        protocol.stop()


class TestReplyCollectorBounds:
    """The protocol object enforces the caps and the source binding (WP-69-T)."""

    def test_reply_count_is_capped(self) -> None:
        collector = discovery._ReplyCollector(
            max_replies=_MAX_MULTICAST_REPLIES, max_bytes=_MAX_MULTICAST_BYTES
        )
        for i in range(300):
            collector.datagram_received(b"<x/>", (f"10.0.{i // 250}.{i % 250 + 1}", 1))
        assert len(collector.replies) == _MAX_MULTICAST_REPLIES == 256
        assert collector.capped and collector.done.is_set()

    def test_total_bytes_are_capped(self) -> None:
        collector = discovery._ReplyCollector(
            max_replies=_MAX_MULTICAST_REPLIES, max_bytes=_MAX_MULTICAST_BYTES
        )
        big = b"x" * 60_000
        for _ in range(40):
            collector.datagram_received(big, ("10.0.0.7", 1))
        assert sum(len(data) for data, _ in collector.replies) <= _MAX_MULTICAST_BYTES
        assert len(collector.replies) == _MAX_MULTICAST_BYTES // len(big)
        assert collector.done.is_set()

    def test_keeps_each_reply_source_address(self) -> None:
        collector = discovery._ReplyCollector(max_replies=10, max_bytes=1000)
        collector.datagram_received(b"one", ("10.0.0.7", 3702))
        collector.datagram_received(b"two", ("10.0.0.8", 3702))
        assert collector.replies == [(b"one", "10.0.0.7"), (b"two", "10.0.0.8")]

    def test_a_refused_source_is_ignored_and_does_not_end_the_round(self) -> None:
        collector = discovery._ReplyCollector(
            max_replies=1, max_bytes=1000, accept=lambda source: source == "10.0.0.5"
        )
        collector.datagram_received(b"spoofed", ("10.0.0.66", 3702))
        assert collector.replies == [] and not collector.done.is_set()
        collector.datagram_received(b"genuine", ("10.0.0.5", 3702))
        assert collector.replies == [(b"genuine", "10.0.0.5")]
        assert collector.done.is_set()

    def test_nothing_is_kept_after_the_round_is_over(self) -> None:
        collector = discovery._ReplyCollector(max_replies=1, max_bytes=1000)
        collector.datagram_received(b"first", ("10.0.0.5", 1))
        collector.datagram_received(b"late", ("10.0.0.6", 1))
        assert collector.replies == [(b"first", "10.0.0.5")]


class TestMulticastRound:
    async def test_collects_replies_with_their_source(
        self, responder_factory: Callable[..., Awaitable[_Responder]]
    ) -> None:
        await responder_factory(replies=[(0.0, b"one"), (0.01, b"two")])
        replies = await discovery._collect_multicast_replies(0.4, None)
        assert replies == [(b"one", "127.0.0.1"), (b"two", "127.0.0.1")]

    async def test_steady_trickle_cannot_hold_the_round_open(
        self, responder_factory: Callable[..., Awaitable[_Responder]]
    ) -> None:
        """A reply every 0.1 s under a 0.35 s window used to reset the timer."""
        await responder_factory(trickle=(0.1, _PROBE_MATCH_XML))
        started = time.monotonic()
        replies = await discovery._collect_multicast_replies(0.35, None)
        assert time.monotonic() - started < 1.0
        assert 1 <= len(replies) <= 4

    async def test_a_burst_over_the_cap_ends_the_round_early(
        self, responder_factory: Callable[..., Awaitable[_Responder]]
    ) -> None:
        await responder_factory(replies=[(0.0, b"<x/>")] * 300)
        started = time.monotonic()
        replies = await discovery._collect_multicast_replies(10.0, None)
        assert time.monotonic() - started < 5.0
        assert 0 < len(replies) <= _MAX_MULTICAST_REPLIES

    async def test_an_unusable_interface_returns_nothing(self) -> None:
        # 192.0.2.1 is not an address of this host: bind fails, no raise.
        assert await discovery._collect_multicast_replies(0.1, "192.0.2.1") == []

    async def test_the_socket_is_closed_after_a_round(
        self,
        responder_factory: Callable[..., Awaitable[_Responder]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await responder_factory(replies=[])
        opened: list[socket.socket] = []
        real = discovery._udp_socket

        def spy(family: int = socket.AF_INET) -> socket.socket:
            sock = real(family)
            opened.append(sock)
            return sock

        monkeypatch.setattr(discovery, "_udp_socket", spy)
        await discovery._collect_multicast_replies(0.05, None)
        await asyncio.sleep(0.01)  # a closing transport releases its fd next tick
        assert opened and all(sock.fileno() == -1 for sock in opened)


class TestUnicastProbe:
    async def test_returns_the_probed_hosts_reply(
        self, responder_factory: Callable[..., Awaitable[_Responder]]
    ) -> None:
        responder = await responder_factory(replies=[(0.0, b"genuine")])
        assert await _unicast_probe("127.0.0.1", 1.0) == b"genuine"
        assert responder.probes == 1

    async def test_silence_times_out_at_the_deadline(
        self, responder_factory: Callable[..., Awaitable[_Responder]]
    ) -> None:
        await responder_factory(replies=[])
        started = time.monotonic()
        assert await _unicast_probe("127.0.0.1", 0.2) is None
        elapsed = time.monotonic() - started
        assert 0.15 <= elapsed < 1.5

    async def test_a_trickle_from_the_host_cannot_extend_the_deadline(
        self, responder_factory: Callable[..., Awaitable[_Responder]]
    ) -> None:
        # Replies keep coming but the first one ends the probe at once.
        await responder_factory(trickle=(0.05, b"x"))
        started = time.monotonic()
        assert await _unicast_probe("127.0.0.1", 5.0) == b"x"
        assert time.monotonic() - started < 2.0

    async def test_an_ipv6_host_uses_an_ipv6_socket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop = asyncio.get_running_loop()
        try:
            transport, protocol = await loop.create_datagram_endpoint(
                lambda: _Responder(replies=[(0.0, b"v6")]),
                local_addr=("::1", 0),
                family=socket.AF_INET6,
            )
        except OSError:
            pytest.skip("no IPv6 loopback on this host")
        try:
            monkeypatch.setattr(discovery, "WS_DISCOVERY_PORT", protocol.port)
            assert await _unicast_probe("::1", 1.0) == b"v6"
        finally:
            protocol.stop()
            transport.close()

    async def test_an_unreachable_family_is_none_not_an_error(self) -> None:
        # An IPv6 literal that cannot be routed from here must not raise.
        assert await _unicast_probe("2001:db8::1", 0.05) is None


class TestScanDoesNotUseThreads:
    """CAM-P3: a /24 scan holds no executor thread and does not lag the loop."""

    async def test_a_silent_24_keeps_the_loop_responsive_and_uses_no_executor(
        self,
        private_scan_context: None,
        responder_factory: Callable[..., Awaitable[_Responder]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        silent = await responder_factory(replies=[])
        loop = asyncio.get_running_loop()

        def no_executor(*_args: object, **_kwargs: object) -> typing.NoReturn:
            raise AssertionError("discovery used an executor thread")

        monkeypatch.setattr(loop, "run_in_executor", no_executor)
        monkeypatch.setattr(asyncio, "to_thread", no_executor)
        # 254 targets, all aimed at the silent loopback responder (the guard is
        # bypassed here only so the probes can leave the machine).
        monkeypatch.setattr(discovery, "assert_address_allowed", lambda *_a, **_k: None)
        monkeypatch.setattr(
            discovery, "_hosts_from_spec", lambda _spec: ["127.0.0.1"] * 254
        )

        # Warm up first: the first scan in a process pays one-off costs (lazy
        # imports, socket setup) that a loaded CI runner turned into a single
        # 0.4 s tick (release dry run, 2026-10-30). The bound below is about the
        # scan itself, so measure a scan that runs warm.
        assert (
            await scan_range(
                "10.0.0.1-10.0.0.1", max_concurrent=1, per_host_timeout=0.05
            )
            == []
        )
        warmup_probes = silent.probes

        lags: list[float] = []
        running = True

        async def ticker() -> None:
            while running:
                before = time.monotonic()
                await asyncio.sleep(0.005)
                lags.append(time.monotonic() - before - 0.005)

        tick = asyncio.create_task(ticker())
        try:
            found = await scan_range(
                "10.0.0.1-10.0.0.254", max_concurrent=10, per_host_timeout=0.05
            )
        finally:
            running = False
            await tick
        assert found == []
        assert silent.probes - warmup_probes == 254
        # The 50 ms target, with room for a loaded CI machine.
        assert max(lags) < 0.25, f"event loop lagged {max(lags):.3f}s during the scan"

    async def test_a_responder_per_host_is_found(
        self,
        private_scan_context: None,
        responder_factory: Callable[..., Awaitable[_Responder]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await responder_factory(replies=[(0.0, _PROBE_MATCH_XML)])
        monkeypatch.setattr(discovery, "assert_address_allowed", lambda *_a, **_k: None)
        monkeypatch.setattr(discovery, "_hosts_from_spec", lambda _spec: ["127.0.0.1"])
        found = await scan_range("10.0.0.1", per_host_timeout=1.0)
        assert [camera.address for camera in found] == ["127.0.0.1"]


class TestParseProbeMatchSourceBinding:
    def test_matching_source_is_kept(self) -> None:
        camera = _parse_probe_match(_PROBE_MATCH_XML, "192.168.1.50")
        assert camera is not None
        assert camera.address == "192.168.1.50"

    def test_spoofed_xaddrs_is_dropped(self) -> None:
        assert _parse_probe_match(_PROBE_MATCH_XML, "192.168.1.99") is None

    def test_any_matching_xaddrs_entry_is_used(self) -> None:
        data = _probe_match_for(
            "http://[fe80::1]/onvif/device_service http://10.0.0.7/onvif/device_service"
        )
        camera = _parse_probe_match(data, "10.0.0.7")
        assert camera is not None
        assert camera.address == "10.0.0.7"

    def test_hostname_xaddrs_cannot_be_bound_and_is_dropped(self) -> None:
        data = _probe_match_for("http://camera.example/onvif/device_service")
        assert _parse_probe_match(data, "10.0.0.7") is None


class TestDiscoverOnvifSourceBinding:
    async def test_reply_advertising_another_host_is_dropped(
        self, private_scan_context: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        replies = [
            # 10.0.0.9 claims to be 10.0.0.50: dropped.
            (_probe_match_for("http://10.0.0.50/onvif/device_service"), "10.0.0.9"),
            (_probe_match_for("http://10.0.0.7/onvif/device_service"), "10.0.0.7"),
        ]
        monkeypatch.setattr(
            discovery,
            "_collect_multicast_replies",
            AsyncMock(return_value=replies),
        )
        found = await discovery.discover_onvif(timeout_seconds=0.1)
        assert [camera.address for camera in found] == ["10.0.0.7"]


class TestMalformedXaddrs:
    """A malformed XAddrs entry spoils only itself, never the round."""

    def test_malformed_entry_beside_a_valid_one_without_source(self) -> None:
        data = _probe_match_for(
            "http://10.0.0.7/onvif/device_service http://[bad/onvif"
        )
        camera = _parse_probe_match(data)
        assert camera is not None
        assert camera.address == "10.0.0.7"

    def test_malformed_first_entry_without_source_is_none(self) -> None:
        data = _probe_match_for("http://[bad/onvif http://10.0.0.7/onvif")
        assert _parse_probe_match(data) is None

    def test_malformed_entry_beside_a_matching_one_with_source(self) -> None:
        data = _probe_match_for("http://[x]/onvif http://10.0.0.7/onvif")
        camera = _parse_probe_match(data, "10.0.0.7")
        assert camera is not None
        assert camera.address == "10.0.0.7"

    def test_only_malformed_entry_with_source_is_none(self) -> None:
        assert _parse_probe_match(_probe_match_for("http://[bad/x"), "10.0.0.7") is None

    async def test_malformed_reply_does_not_sink_discover_onvif(
        self, private_scan_context: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        replies = [
            (_probe_match_for("http://[bad/x"), "10.0.0.9"),
            (_probe_match_for("http://10.0.0.7/onvif/device_service"), "10.0.0.7"),
        ]
        monkeypatch.setattr(
            discovery,
            "_collect_multicast_replies",
            AsyncMock(return_value=replies),
        )
        found = await discovery.discover_onvif(timeout_seconds=0.1)
        assert [camera.address for camera in found] == ["10.0.0.7"]

    async def test_malformed_reply_does_not_sink_scan_range(
        self, private_scan_context: None
    ) -> None:
        def _probe(host: str, timeout: float) -> bytes:
            if host == "10.0.0.1":
                return _probe_match_for("http://[bad/x")
            return _PROBE_MATCH_XML

        with patch(
            "timelapse_manager.cameras.discovery._unicast_probe", side_effect=_probe
        ):
            result = await scan_range("10.0.0.1-10.0.0.2", per_host_timeout=0.01)
        assert [camera.address for camera in result] == ["10.0.0.2"]
