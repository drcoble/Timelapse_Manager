"""Tests for the VAPIX event discovery and WebSocket frame parsing.

Fixtures mirror the shapes captured on real Axis hardware during the spike: the
GetEventInstances tree (with its Source/Data field names and isProperty marker)
and the events:notify WebSocket JSON frame (whose topic carries the same
namespace-prefix dialect as ONVIF, exercised through the shared canonicaliser).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from timelapse_manager.cameras import vapix

# -- GetEventInstances discovery ---------------------------------------------

_EVENT_INSTANCES = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
  xmlns:tns1="http://www.onvif.org/ver10/topics"
  xmlns:tnsaxis="http://www.axis.com/2009/event/topics"
  xmlns:aev="http://www.axis.com/vapix/ws/event1"
  xmlns:wstop="http://docs.oasis-open.org/wsn/t-1">
  <soap:Body>
    <aev:GetEventInstancesResponse>
      <wstop:TopicSet>
        <tns1:Device>
          <tns1:IO>
            <tns1:VirtualInput aev:isProperty="true">
              <aev:MessageInstance isProperty="true">
                <aev:SourceInstance>
                  <aev:SimpleItemInstance Name="port"/>
                </aev:SourceInstance>
                <aev:DataInstance>
                  <aev:SimpleItemInstance Name="active"/>
                </aev:DataInstance>
              </aev:MessageInstance>
            </tns1:VirtualInput>
          </tns1:IO>
        </tns1:Device>
        <tns1:VideoSource>
          <tns1:MotionAlarm>
            <aev:MessageInstance isProperty="true">
              <aev:SourceInstance>
                <aev:SimpleItemInstance Name="Source"/>
              </aev:SourceInstance>
              <aev:DataInstance>
                <aev:SimpleItemInstance Name="State"/>
              </aev:DataInstance>
            </aev:MessageInstance>
          </tns1:MotionAlarm>
        </tns1:VideoSource>
      </wstop:TopicSet>
    </aev:GetEventInstancesResponse>
  </soap:Body>
</soap:Envelope>"""


def test_parse_event_instances_builds_descriptors() -> None:
    descriptors = vapix.parse_event_instances(_EVENT_INSTANCES)
    by_topic = {d.topic_id: d for d in descriptors}

    vinput = by_topic["Device/IO/VirtualInput"]
    assert vinput.category == "io"
    assert vinput.stateful is True
    assert vinput.protocol == "vapix"
    assert {f["name"] for f in vinput.data_fields} == {"port", "active"}
    # active is a boolean-state field; port is a plain string.
    types = {f["name"]: f["type"] for f in vinput.data_fields}
    assert types["active"] == "boolean"
    assert types["port"] == "string"

    motion = by_topic["VideoSource/MotionAlarm"]
    assert motion.category == "motion"
    assert motion.stateful is True


def test_parse_event_instances_empty_on_garbage() -> None:
    assert vapix.parse_event_instances("not xml") == []
    assert vapix.parse_event_instances("<soap:Envelope/>") == []


# -- WebSocket events:notify frame parsing -----------------------------------

_WS_FRAME = """{"apiVersion":"1.0","method":"events:notify",
 "params":{"notification":{
   "topic":"tns1:Device/tnsaxis:IO/VirtualInput",
   "timestamp":1782260923190,
   "message":{"source":{"port":"1"},"key":{},"data":{"active":"1"}}}}}"""


def test_parse_ws_event_rising_edge() -> None:
    event = vapix.parse_ws_event(_WS_FRAME)
    assert event is not None
    # The WS topic carries the same dialect difference as ONVIF and canonicalises
    # to the shared key.
    assert event.topic_id == "Device/IO/VirtualInput"
    assert event.category == "io"
    assert event.source == {"port": "1"}
    assert event.data == {"active": "1"}
    assert event.active is True
    # The millisecond epoch is converted to aware-UTC.
    assert event.occurred_at.tzinfo is not None
    assert event.occurred_at.year == 2026


def test_parse_ws_event_falling_edge() -> None:
    falling = _WS_FRAME.replace('"active":"1"', '"active":"0"')
    event = vapix.parse_ws_event(falling)
    assert event is not None
    assert event.active is False


def test_parse_ws_event_ignores_non_notify_frames() -> None:
    # An ack/configure reply or a malformed frame is not an event.
    assert vapix.parse_ws_event('{"method":"events:configure","data":{}}') is None
    assert vapix.parse_ws_event("not json") is None
    assert vapix.parse_ws_event('{"method":"events:notify"}') is None


def test_ws_subscribe_message_all_topics() -> None:
    import json

    message = json.loads(vapix.ws_subscribe_message())
    assert message["method"] == "events:configure"
    assert message["params"]["eventFilterList"][0]["topicFilter"] == "//."


# -- WebSocket framing helpers ----------------------------------------------


def test_digest_authorization_round_trips_challenge() -> None:
    challenge = (
        'Digest realm="AXIS_ACCC8E000000", nonce="abc123", qop="auth", opaque="xyz"'
    )
    header = vapix._digest_authorization(
        challenge, ("root", "pass"), method="GET", path="/vapix/ws-data-stream"
    )
    assert header is not None
    assert header.startswith("Digest ")
    assert 'username="root"' in header
    assert 'realm="AXIS_ACCC8E000000"' in header
    assert "response=" in header
    assert "qop=auth" in header


def test_digest_authorization_none_for_basic_challenge() -> None:
    assert (
        vapix._digest_authorization(
            'Basic realm="x"', ("u", "p"), method="GET", path="/"
        )
        is None
    )


def test_status_code_parsing() -> None:
    assert vapix._parse_status_code(b"HTTP/1.1 101 Switching Protocols\r\n") == 101
    assert vapix._parse_status_code(b"HTTP/1.1 404 Not Found\r\n") == 404
    assert vapix._parse_status_code(b"garbage") == 0


# -- WebSocket handshake digest-retry (the immediate-FIN regression) ---------


class _FakeReader:
    """A minimal asyncio.StreamReader stand-in serving canned bytes.

    ``readline`` returns up to and including the next ``\\n``; ``readexactly``
    returns the requested count. Together these cover the handshake's status +
    header reads and the WebSocket frame reads.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    async def readline(self) -> bytes:
        idx = self._data.find(b"\n", self._pos)
        end = len(self._data) if idx == -1 else idx + 1
        chunk = self._data[self._pos : end]
        self._pos = end
        return chunk

    async def readexactly(self, n: int) -> bytes:
        chunk = self._data[self._pos : self._pos + n]
        if len(chunk) < n:
            raise asyncio.IncompleteReadError(chunk, n)
        self._pos += n
        return chunk


class _FakeWriter:
    """An asyncio.StreamWriter stand-in capturing every byte written."""

    def __init__(self) -> None:
        self.written = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


# A 401 challenge *with a response body*: the broken single-socket retry would
# read these body bytes as the next request's status line and parse a non-101.
# The fix opens a fresh connection, so the leftover body never poisons the retry.
_WS_401 = (
    b"HTTP/1.1 401 Unauthorized\r\n"
    b'WWW-Authenticate: Digest realm="AXIS_TEST", nonce="abc123", qop="auth"\r\n'
    b"Content-Type: text/html\r\n"
    b"Content-Length: 26\r\n"
    b"\r\n"
    b"<html>unauthorized</html>\n"
)
_WS_101 = (
    b"HTTP/1.1 101 Switching Protocols\r\n"
    b"Upgrade: websocket\r\n"
    b"Connection: Upgrade\r\n"
    b"Sec-WebSocket-Accept: irrelevant\r\n"
    b"\r\n"
)


def _decode_first_client_text_frame(frame_bytes: bytes) -> str:
    """Decode the first masked client WebSocket text frame's payload to text."""
    length = frame_bytes[1] & 0x7F
    offset = 2
    if length == 126:
        length = int.from_bytes(frame_bytes[2:4], "big")
        offset = 4
    elif length == 127:
        length = int.from_bytes(frame_bytes[2:10], "big")
        offset = 10
    mask = frame_bytes[offset : offset + 4]
    payload = frame_bytes[offset + 4 : offset + 4 + length]
    unmasked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return unmasked.decode("utf-8")


def _vapix_adapter() -> Any:
    client = MagicMock()
    return vapix.VapixAdapter(client, "10.1.20.111", credentials=("root", "pass"))


@pytest.fixture
def _stub_ssrf() -> Any:
    ctx = SimpleNamespace(
        settings=SimpleNamespace(ssrf=SimpleNamespace(allowed_private_subnets=()))
    )
    with (
        patch("timelapse_manager.runtime.get_context", return_value=ctx),
        patch(
            "timelapse_manager.cameras.vapix.assert_allowed_url",
            side_effect=lambda url, **_: url,
        ),
    ):
        yield


async def test_ws_handshake_401_then_101_subscribes(_stub_ssrf: Any) -> None:
    # Regression: a 401-with-body followed by an authenticated 101 must complete
    # the upgrade and then send the subscribe frame. The fresh-connection retry
    # means the first connection's undrained 401 body never reaches the retry's
    # status line. Drive the adapter end-to-end so we also assert the subscribe.
    reader1 = _FakeReader(_WS_401)
    writer1 = _FakeWriter()
    # The 101 connection serves the upgrade then a server close frame so the
    # event loop ends cleanly (opcode 0x8, unmasked, zero-length payload).
    reader2 = _FakeReader(_WS_101 + bytes([0x88, 0x00]))
    writer2 = _FakeWriter()
    connections = iter([(reader1, writer1), (reader2, writer2)])

    async def fake_open_connection(*_a: Any, **_k: Any) -> Any:
        return next(connections)

    adapter = _vapix_adapter()
    with patch(
        "timelapse_manager.cameras.vapix.asyncio.open_connection",
        side_effect=fake_open_connection,
    ):
        events = [event async for event in adapter.open_event_source()]

    # The unauthenticated socket was closed before the retry; the authenticated
    # GET carried the Digest header and the subscribe frame was sent on it.
    assert writer1.closed is True
    assert b"Authorization: Digest" not in bytes(writer1.written)
    assert b"Authorization: Digest" in bytes(writer2.written)
    # The subscribe control frame (events:configure) was written after the 101.
    # Client frames are masked per RFC6455, so decode the payload to confirm.
    written = bytes(writer2.written)
    payload = _decode_first_client_text_frame(written[written.index(b"\r\n\r\n") + 4 :])
    assert "events:configure" in payload
    # No events arrived in this fixture (the server closed immediately).
    assert events == []


async def test_ws_handshake_404_raises_unsupported(_stub_ssrf: Any) -> None:
    # An older firmware answering 404 to the upgrade must raise so the listener
    # factory can fall back to ONVIF PullPoint.
    reader = _FakeReader(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
    writer = _FakeWriter()

    async def fake_open_connection(*_a: Any, **_k: Any) -> Any:
        return (reader, writer)

    adapter = _vapix_adapter()
    with (
        patch(
            "timelapse_manager.cameras.vapix.asyncio.open_connection",
            side_effect=fake_open_connection,
        ),
        pytest.raises(vapix.EventNotSupportedError),
    ):
        await adapter.open_event_source().__anext__()
    assert writer.closed is True
