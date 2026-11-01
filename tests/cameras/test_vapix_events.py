"""Tests for the VAPIX event discovery and WebSocket frame parsing.

Fixtures mirror the shapes captured on real Axis hardware during the spike: the
GetEventInstances tree (with its Source/Data field names and isProperty marker)
and the events:notify WebSocket JSON frame (whose topic carries the same
namespace-prefix dialect as ONVIF, exercised through the shared canonicaliser).
"""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from timelapse_manager.cameras import vapix
from timelapse_manager.cameras.base import AuthCaptureError
from timelapse_manager.cameras.http_auth import BasicPolicy, CameraAuthState

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

    async def wait_closed(self) -> None:
        return None


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
# The RFC 6455 section 1.3 sample key and the accept value it requires. The
# autouse fixture below makes every handshake in this module send this key, so
# the canned 101 carries the correct ``Sec-WebSocket-Accept``.
_WS_TEST_KEY = "dGhlIHNhbXBsZSBub25jZQ=="
_WS_TEST_ACCEPT = b"s3pPLMBiTxaQ9kYGzzhZRbK+xOo="

_WS_101 = (
    b"HTTP/1.1 101 Switching Protocols\r\n"
    b"Upgrade: websocket\r\n"
    b"Connection: Upgrade\r\n"
    b"Sec-WebSocket-Accept: " + _WS_TEST_ACCEPT + b"\r\n"
    b"\r\n"
)


@pytest.fixture(autouse=True)
def _fixed_ws_key() -> Any:
    """Send the RFC sample ``Sec-WebSocket-Key`` so ``_WS_101`` validates."""
    with patch.object(vapix, "_new_ws_key", return_value=_WS_TEST_KEY):
        yield


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
    return vapix.VapixAdapter(client, "10.0.20.111", credentials=("root", "pass"))


@pytest.fixture
def _stub_ssrf() -> Any:
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            ssrf=SimpleNamespace(allowed_private_subnets=()),
            capture=SimpleNamespace(
                ws_ping_interval_seconds=30.0,
                ws_read_timeout_seconds=60.0,
                max_ws_frame_bytes=2 * 1024 * 1024,
            ),
        )
    )

    async def _pin_passthrough(host: str, **_: Any) -> str:
        # The event path resolves the host to a validated IP before connecting;
        # in tests the address is already a literal IP, so echo it back.
        return host

    with (
        patch("timelapse_manager.runtime.get_context", return_value=ctx),
        patch(
            "timelapse_manager.cameras.vapix.check_camera_url_async",
            side_effect=lambda url, **_: url,
        ),
        patch(
            "timelapse_manager.cameras.vapix.resolve_and_check_pinned_async",
            new=_pin_passthrough,
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


# -- WebSocket read/idle timeout and frame-size cap (untrusted-socket guards) --


def _ws_text_frame(text: str) -> bytes:
    """Build an unmasked server text frame carrying ``text`` (short payloads)."""
    payload = text.encode("utf-8")
    assert len(payload) < 126
    return bytes([0x80 | vapix._WS_OP_TEXT, len(payload)]) + payload


class _SilentReader:
    """A reader that never produces a frame: every read blocks forever.

    Stands in for a half-open/wedged camera socket so the connection's idle
    timeout (ping-then-reconnect) can be exercised deterministically.
    """

    async def readexactly(self, n: int) -> bytes:
        await asyncio.Event().wait()  # block until cancelled by the read timeout
        raise AssertionError("unreachable")  # pragma: no cover


def _connection(
    reader: Any, *, ping_interval: float, read_timeout: float, max_frame_bytes: int
) -> Any:
    return vapix._WebSocketConnection(
        reader,
        _FakeWriter(),
        ping_interval=ping_interval,
        read_timeout=read_timeout,
        max_frame_bytes=max_frame_bytes,
    )


async def test_ws_receive_oversize_frame_rejected() -> None:
    # A frame declaring a length above the cap must be rejected before its
    # payload is read, and must raise a CaptureError (not an OSError) so it
    # routes to the listener's backoff loop rather than a tight reconnect.
    # 0x82 = FIN|binary; 0x7F = 127 (8-byte extended length follows).
    declared = (8 * 1024 * 1024).to_bytes(8, "big")
    frame = bytes([0x82, 0x7F]) + declared + b"never-read"
    conn = _connection(
        _FakeReader(frame),
        ping_interval=30.0,
        read_timeout=60.0,
        max_frame_bytes=2 * 1024 * 1024,
    )
    with pytest.raises(vapix.CaptureError):
        await conn.receive()


async def test_ws_receive_undersize_frame_passes() -> None:
    # A frame at or under the cap is read normally and returns its text.
    conn = _connection(
        _FakeReader(_ws_text_frame("hello")),
        ping_interval=30.0,
        read_timeout=60.0,
        max_frame_bytes=2 * 1024 * 1024,
    )
    assert await conn.receive() == "hello"


async def test_ws_receive_idle_timeout_pings_then_reconnects() -> None:
    # A silent socket: the first idle window sends a keepalive ping, the second
    # consecutive idle window declares the socket dead (receive returns None) so
    # the existing listener loop reconnects. Tiny intervals keep the test fast.
    writer = _FakeWriter()
    conn = vapix._WebSocketConnection(
        _SilentReader(),
        writer,
        ping_interval=0.01,
        read_timeout=0.02,
        max_frame_bytes=2 * 1024 * 1024,
    )
    result = await asyncio.wait_for(conn.receive(), timeout=2.0)
    assert result is None
    # Exactly one keepalive ping (opcode 0x9) was written before giving up.
    assert (writer.written[0] & 0x0F) == vapix._WS_OP_PING


async def test_ws_receive_read_timeout_is_independent_of_ping_interval() -> None:
    # The two knobs are independent: the first wait is ping_interval, the second
    # (post-ping) wait is the remainder of read_timeout -- not another full
    # ping_interval. Capture the delays handed to asyncio.timeout to prove it.
    delays: list[float | None] = []
    real_timeout = asyncio.timeout

    def recording_timeout(delay: float | None) -> asyncio.Timeout:
        delays.append(delay)
        return real_timeout(delay)

    conn = vapix._WebSocketConnection(
        _SilentReader(),
        _FakeWriter(),
        ping_interval=0.01,
        read_timeout=0.05,
        max_frame_bytes=2 * 1024 * 1024,
    )
    with patch(
        "timelapse_manager.cameras.vapix.asyncio.timeout",
        side_effect=recording_timeout,
    ):
        assert await conn.receive() is None
    # Read window (ping interval), the bounded ping write, then the post-ping
    # read window (read_timeout - ping_interval).
    assert delays == [
        pytest.approx(0.01),
        pytest.approx(0.01),
        pytest.approx(0.04),
    ]


async def test_ws_receive_live_frame_resets_idle_counter() -> None:
    # A reader that is silent for one window, then delivers a frame, must not be
    # dropped: the delivered frame resets the idle counter so the live (if quiet)
    # camera keeps streaming.
    delivered = asyncio.Event()

    class _SlowThenFrameReader:
        def __init__(self) -> None:
            self._frame = _ws_text_frame("late")
            self._pos = 0
            self._first = True

        async def readexactly(self, n: int) -> bytes:
            if self._first and self._pos == 0:
                self._first = False
                await asyncio.sleep(0.02)  # one idle window elapses -> a ping
            chunk = self._frame[self._pos : self._pos + n]
            self._pos += n
            delivered.set()
            return chunk

    writer = _FakeWriter()
    conn = vapix._WebSocketConnection(
        _SlowThenFrameReader(),
        writer,
        ping_interval=0.01,
        read_timeout=10.0,
        max_frame_bytes=2 * 1024 * 1024,
    )
    assert await asyncio.wait_for(conn.receive(), timeout=2.0) == "late"


async def test_ws_handshake_read_is_bounded(_stub_ssrf: Any) -> None:
    # A camera that completes the TCP connect but never sends a status line must
    # not hang the handshake forever: the bounded read raises a timeout error.
    class _HangingHandshakeReader:
        async def readline(self) -> bytes:
            await asyncio.Event().wait()  # never returns a status line
            raise AssertionError("unreachable")  # pragma: no cover

        async def readexactly(self, n: int) -> bytes:  # pragma: no cover
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    reader = _HangingHandshakeReader()
    writer = _FakeWriter()

    async def fake_open_connection(*_a: Any, **_k: Any) -> Any:
        return (reader, writer)

    # A tiny per-request timeout so the bounded handshake trips quickly.
    adapter = vapix.VapixAdapter(
        MagicMock(), "10.0.20.111", credentials=("root", "pass"), timeout=0.05
    )
    from timelapse_manager.cameras.base import TimeoutCaptureError

    with (
        patch(
            "timelapse_manager.cameras.vapix.asyncio.open_connection",
            side_effect=fake_open_connection,
        ),
        pytest.raises(TimeoutCaptureError),
    ):
        await asyncio.wait_for(adapter.open_event_source().__anext__(), timeout=2.0)
    assert writer.closed is True


# -- WS auth-scheme dispatch (Basic upgrade, not just Digest) -----------------

_WS_401_BASIC = (
    b"HTTP/1.1 401 Unauthorized\r\n"
    b'WWW-Authenticate: Basic realm="AXIS_TEST"\r\n'
    b"Content-Type: text/html\r\n"
    b"Content-Length: 26\r\n"
    b"\r\n"
    b"<html>unauthorized</html>\n"
)


async def test_ws_handshake_basic_auth_carries_basic_header(_stub_ssrf: Any) -> None:
    # A VAPIX camera advertising Basic on the upgrade must complete the WS event
    # path (carrying an Authorization: Basic header on the authenticated retry)
    # rather than silently falling back to ONVIF PullPoint -- when the camera
    # allows Basic over plain HTTP (#69 CAM-S1, OD-3).
    reader1 = _FakeReader(_WS_401_BASIC)
    writer1 = _FakeWriter()
    reader2 = _FakeReader(_WS_101 + bytes([0x88, 0x00]))
    writer2 = _FakeWriter()
    connections = iter([(reader1, writer1), (reader2, writer2)])

    async def fake_open_connection(*_a: Any, **_k: Any) -> Any:
        return next(connections)

    adapter = vapix.VapixAdapter(
        MagicMock(),
        "10.0.20.111",
        credentials=("root", "pass"),
        auth_state=CameraAuthState(BasicPolicy.for_camera(True)),
    )
    with patch(
        "timelapse_manager.cameras.vapix.asyncio.open_connection",
        side_effect=fake_open_connection,
    ):
        events = [event async for event in adapter.open_event_source()]

    # The unauthenticated socket was dropped; the authenticated retry carried the
    # Basic credentials and the upgrade completed (subscribe frame was written).
    assert writer1.closed is True
    assert b"Authorization:" not in bytes(writer1.written)
    expected = b"Authorization: Basic " + base64.b64encode(b"root:pass")
    assert expected in bytes(writer2.written)
    assert events == []


async def test_ws_unsupported_auth_scheme_raises_unsupported(_stub_ssrf: Any) -> None:
    # A 401 whose only scheme we cannot satisfy (e.g. Negotiate) must raise the
    # unsupported-event error so the listener cleanly falls back to PullPoint,
    # not loop on an upgrade that can never reach 101.
    reader = _FakeReader(
        b"HTTP/1.1 401 Unauthorized\r\n"
        b"WWW-Authenticate: Negotiate\r\n"
        b"Content-Length: 0\r\n\r\n"
    )
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


# -- WS connect pins the validated IP (DNS-rebind TOCTOU) ---------------------


def _ws_context() -> Any:
    return SimpleNamespace(
        settings=SimpleNamespace(
            ssrf=SimpleNamespace(allowed_private_subnets=()),
            capture=SimpleNamespace(
                ws_ping_interval_seconds=30.0,
                ws_read_timeout_seconds=60.0,
                max_ws_frame_bytes=2 * 1024 * 1024,
            ),
        )
    )


async def test_ws_connect_targets_validated_ip_preserving_sni() -> None:
    # The event socket must connect to the *pre-validated IP*, not re-resolve the
    # hostname, while still presenting the original hostname as TLS SNI and in the
    # Host header. Capture the args open_connection receives to prove the pin.
    captured: dict[str, Any] = {}
    reader = _FakeReader(_WS_101 + bytes([0x88, 0x00]))
    writer = _FakeWriter()

    async def fake_open_connection(host: Any, port: Any, **kwargs: Any) -> Any:
        captured["host"] = host
        captured["port"] = port
        captured["server_hostname"] = kwargs.get("server_hostname")
        return (reader, writer)

    async def fake_pin(host: str, **_: Any) -> str:
        # The hostname resolves+validates to this public IP; the socket must dial
        # it directly rather than re-resolving the name at connect time.
        return "203.0.113.5"

    adapter = vapix.VapixAdapter(
        MagicMock(), "https://camera.example.test", credentials=("root", "pass")
    )
    with (
        patch("timelapse_manager.runtime.get_context", return_value=_ws_context()),
        patch(
            "timelapse_manager.cameras.vapix.resolve_and_check_pinned_async",
            new=fake_pin,
        ),
        patch(
            "timelapse_manager.cameras.vapix.asyncio.open_connection",
            side_effect=fake_open_connection,
        ),
    ):
        events = [event async for event in adapter.open_event_source()]

    assert events == []
    # Connected to the validated IP literal, not the hostname.
    assert captured["host"] == "203.0.113.5"
    assert captured["port"] == 443
    # TLS verification / SNI still target the original hostname.
    assert captured["server_hostname"] == "camera.example.test"
    # The Host header carries the original hostname, never the dialled IP.
    written = bytes(writer.written)
    assert b"Host: camera.example.test:443" in written
    assert b"203.0.113.5" not in written


async def test_ws_connect_plaintext_passes_no_sni() -> None:
    # For a plaintext (ws://) endpoint, server_hostname must be None -- asyncio
    # rejects an SNI without TLS -- while the connect still targets the pinned IP.
    captured: dict[str, Any] = {}
    reader = _FakeReader(_WS_101 + bytes([0x88, 0x00]))
    writer = _FakeWriter()

    async def fake_open_connection(host: Any, port: Any, **kwargs: Any) -> Any:
        captured["host"] = host
        captured["server_hostname"] = kwargs.get("server_hostname")
        return (reader, writer)

    async def fake_pin(host: str, **_: Any) -> str:
        return "203.0.113.9"

    adapter = vapix.VapixAdapter(
        MagicMock(), "camera.example.test", credentials=("root", "pass")
    )
    with (
        patch("timelapse_manager.runtime.get_context", return_value=_ws_context()),
        patch(
            "timelapse_manager.cameras.vapix.resolve_and_check_pinned_async",
            new=fake_pin,
        ),
        patch(
            "timelapse_manager.cameras.vapix.asyncio.open_connection",
            side_effect=fake_open_connection,
        ),
    ):
        events = [event async for event in adapter.open_event_source()]

    assert events == []
    assert captured["host"] == "203.0.113.9"
    assert captured["server_hostname"] is None


async def test_ws_connect_rejects_denied_resolved_address() -> None:
    # When the host resolves to a denied address the pin raises SsrfError, which
    # the event path surfaces as UnreachableCaptureError -- and no socket is ever
    # opened (the deny happens before any connect).
    open_connection = MagicMock()

    async def deny(host: str, **_: Any) -> str:
        raise vapix.SsrfError(f"target for {host!r} is denied")

    adapter = vapix.VapixAdapter(
        MagicMock(), "rebind.example.test", credentials=("root", "pass")
    )
    with (
        patch("timelapse_manager.runtime.get_context", return_value=_ws_context()),
        patch(
            "timelapse_manager.cameras.vapix.resolve_and_check_pinned_async",
            new=deny,
        ),
        patch(
            "timelapse_manager.cameras.vapix.asyncio.open_connection",
            open_connection,
        ),
        pytest.raises(vapix.UnreachableCaptureError),
    ):
        await adapter.open_event_source().__anext__()
    open_connection.assert_not_called()


# -- WS challenge choice and failure classification (#69 CAM-S1, CAM-R9) -------

_WS_401_BASIC_AND_DIGEST = (
    b"HTTP/1.1 401 Unauthorized\r\n"
    b'WWW-Authenticate: Basic realm="AXIS_TEST"\r\n'
    b'WWW-Authenticate: Digest realm="AXIS_TEST", nonce="abc123", qop="auth"\r\n'
    b"Content-Length: 0\r\n"
    b"\r\n"
)


def _ws_connections(*heads: bytes) -> tuple[list[_FakeWriter], Any]:
    """Serve one canned response head per connection; return the writers."""
    writers = [_FakeWriter() for _ in heads]
    connections = iter(zip((_FakeReader(h) for h in heads), writers, strict=True))

    async def fake_open_connection(*_a: Any, **_k: Any) -> Any:
        return next(connections)

    return writers, fake_open_connection


async def test_ws_two_challenge_lines_pick_digest(_stub_ssrf: Any) -> None:
    """Both lines are read (the old reader kept only the last); Digest wins."""
    writers, opener = _ws_connections(
        _WS_401_BASIC_AND_DIGEST, _WS_101 + bytes([0x88, 0x00])
    )
    adapter = _vapix_adapter()
    with patch(
        "timelapse_manager.cameras.vapix.asyncio.open_connection", side_effect=opener
    ):
        assert [event async for event in adapter.open_event_source()] == []

    retry = bytes(writers[1].written)
    assert b"Authorization: Digest " in retry
    assert b"Basic" not in retry


async def test_ws_basic_only_over_plain_http_is_refused_before_sending(
    _stub_ssrf: Any,
) -> None:
    """OD-3: without the camera's opt-in, the password never leaves the server."""
    writers, opener = _ws_connections(_WS_401_BASIC)
    adapter = _vapix_adapter()
    with (
        patch(
            "timelapse_manager.cameras.vapix.asyncio.open_connection",
            side_effect=opener,
        ),
        pytest.raises(AuthCaptureError, match="https://"),
    ):
        await adapter.open_event_source().__anext__()
    assert writers[0].closed is True
    assert b"Authorization" not in bytes(writers[0].written)


async def test_ws_second_401_is_an_auth_failure(_stub_ssrf: Any) -> None:
    """A rejected answer is AUTH (camera health), not a generic upgrade error."""
    writers, opener = _ws_connections(_WS_401, _WS_401)
    adapter = _vapix_adapter()
    with (
        patch(
            "timelapse_manager.cameras.vapix.asyncio.open_connection",
            side_effect=opener,
        ),
        pytest.raises(AuthCaptureError, match="401"),
    ):
        await adapter.open_event_source().__anext__()
    assert writers[1].closed is True
    assert b"Authorization: Digest " in bytes(writers[1].written)


async def test_ws_reconnect_reuses_the_negotiated_digest(_stub_ssrf: Any) -> None:
    """The adapter's auth state answers the next upgrade up front (one socket)."""
    writers, opener = _ws_connections(
        _WS_401,
        _WS_101 + bytes([0x88, 0x00]),
        _WS_101 + bytes([0x88, 0x00]),
    )
    adapter = _vapix_adapter()
    with patch(
        "timelapse_manager.cameras.vapix.asyncio.open_connection", side_effect=opener
    ):
        assert [event async for event in adapter.open_event_source()] == []
        assert [event async for event in adapter.open_event_source()] == []

    reconnect = bytes(writers[2].written)
    assert b"Authorization: Digest " in reconnect
    assert b"nc=00000002" in reconnect


# -- Bounded close (#69 CAM-R14) -------------------------------------------------


class _WedgedWriter(_FakeWriter):
    """A writer whose peer stopped reading: drain and wait_closed never return."""

    def __init__(self) -> None:
        super().__init__()
        self.aborted = False
        self.transport = SimpleNamespace(abort=self._abort)

    def _abort(self) -> None:
        self.aborted = True

    async def drain(self) -> None:
        await asyncio.Event().wait()

    async def wait_closed(self) -> None:
        await asyncio.Event().wait()


async def test_ws_close_against_a_wedged_peer_returns_and_aborts() -> None:
    writer = _WedgedWriter()
    conn = vapix._WebSocketConnection(
        _SilentReader(),
        writer,
        ping_interval=1.0,
        read_timeout=2.0,
        max_frame_bytes=1024,
    )
    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(conn.close(), timeout=4.0)
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 2.8  # the close frame and the transport close, 1 s each
    assert writer.closed
    assert writer.aborted


async def test_ws_close_of_a_healthy_peer_does_not_abort() -> None:
    writer = _FakeWriter()
    writer.transport = SimpleNamespace(abort=MagicMock())
    conn = vapix._WebSocketConnection(
        _SilentReader(),
        writer,
        ping_interval=1.0,
        read_timeout=2.0,
        max_frame_bytes=1024,
    )
    await conn.close()
    await conn.close()  # idempotent
    assert (writer.written[0] & 0x0F) == vapix._WS_OP_CLOSE
    writer.transport.abort.assert_not_called()
