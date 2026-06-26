"""Unit tests for ONVIF/WS-Discovery helpers.

Covers:
- _probe_message builds valid UTF-8 XML with a UUID message ID
- _parse_probe_match extracts host and vendor from canned ProbeMatch XML
- _parse_probe_match returns None for malformed XML
- _parse_probe_match returns None when XAddrs absent
- _hosts_from_spec handles CIDR, dash-range, single IP; raises on invalid
- scan_range with an invalid range returns [] without raising
- scan_range host enumeration (patch _unicast_probe to avoid real sockets)

No real network sockets are opened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree as ET

import pytest

from timelapse_manager.cameras.discovery import (
    InvalidScanRange,
    ScanRangeTooLarge,
    _hosts_from_spec,
    _parse_probe_match,
    _probe_message,
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
