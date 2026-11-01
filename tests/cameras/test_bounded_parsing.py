"""Hostile-camera tests for bounded reads and defused parsing (#69 WP-69-N).

A camera is untrusted input. These fixtures play a broken or malicious device
against every read and parse path the adapters use:

* oversized bodies on each SOAP and text path (CAM-S3);
* billion laughs, external entities and plain DTDs (#67-S24);
* deep nesting against the topic walkers (CAM-R12);
* markup injected through usernames, profile tokens, subscription addresses and
  camera-returned reference parameters (CAM-S7);
* a WebSocket upgrade with a missing or wrong ``Sec-WebSocket-Accept``, and an
  oversized response head (CAM-S7, CAM-R12).

Everything runs against ``httpx.MockTransport`` or an in-memory
``asyncio.StreamReader``; no network and no real camera.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import httpx
import pytest

from timelapse_manager.cameras import _bounded, vapix
from timelapse_manager.cameras import _onvif_soap as soap
from timelapse_manager.cameras.base import OtherCaptureError, PTZError
from timelapse_manager.cameras.discovery import _parse_probe_match
from timelapse_manager.cameras.http_jpeg import http_get_image
from timelapse_manager.cameras.onvif import (
    OnvifAdapter,
    get_snapshot_uri_body,
    get_stream_uri_body,
)

SOAP_CAP = _bounded.SOAP_RESPONSE_MAX_BYTES
TEXT_CAP = _bounded.TEXT_RESPONSE_MAX_BYTES

# -- Malicious documents ------------------------------------------------------

# Classic entity expansion: ten levels of ten-fold references, about 10^9 "lol"s
# if a parser expanded them.
BILLION_LAUGHS = (
    '<?xml version="1.0"?>'
    "<!DOCTYPE lolz ["
    '<!ENTITY lol "lol">'
    + "".join(
        f'<!ENTITY lol{i} "{("&lol" + (str(i - 1) if i > 1 else "") + ";") * 10}">'
        for i in range(1, 10)
    )
    + "]><lolz>&lol9;</lolz>"
)

# An external entity that would read a local file into the document.
EXTERNAL_ENTITY = (
    '<?xml version="1.0"?>'
    '<!DOCTYPE r [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
    "<r><Location><Lat>&xxe;</Lat><Lng>1</Lng></Location></r>"
)

# A DOCTYPE with no entities at all: still refused, because no camera sends one.
PLAIN_DTD = '<?xml version="1.0"?><!DOCTYPE r SYSTEM "r.dtd"><r/>'

DEEP = 10_000


def _deep_topic_set(depth: int, *, wrapper: str) -> str:
    """A TopicSet whose only topic sits ``depth`` levels down."""
    opening = "".join(f"<n{i}>" for i in range(depth))
    closing = "".join(f"</n{i}>" for i in reversed(range(depth)))
    return (
        f"<Envelope><Body>{wrapper}"
        f'<TopicSet xmlns:wstop="http://docs.oasis-open.org/wsn/t-1">'
        f'{opening}<Leaf wstop:topic="true"><MessageInstance/></Leaf>{closing}'
        "</TopicSet></Body></Envelope>"
    )


# -- Fixtures -------------------------------------------------------------------


@pytest.fixture
def camera_context() -> Iterator[None]:
    """A runtime context with the SSRF guards passing every camera URL."""
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            ssrf=SimpleNamespace(allowed_private_subnets=()),
            capture=SimpleNamespace(max_image_bytes=64 * 1024 * 1024),
        )
    )
    with (
        patch("timelapse_manager.runtime.get_context", return_value=ctx),
        patch(
            "timelapse_manager.cameras.onvif.check_camera_url_async",
            side_effect=lambda url, **_: url,
        ),
        patch(
            "timelapse_manager.cameras.http_jpeg.check_camera_url_async",
            side_effect=lambda url, **_: url,
        ),
    ):
        yield


async def _endless(chunk: bytes) -> AsyncIterator[bytes]:
    """A body that never ends, sent without a Content-Length."""
    while True:
        yield chunk


def _body_of(size: int) -> AsyncIterator[bytes]:
    """A chunked body of exactly ``size`` bytes, sent without a Content-Length."""

    async def gen() -> AsyncIterator[bytes]:
        remaining = size
        while remaining:
            n = min(remaining, 64 * 1024)
            remaining -= n
            yield b"x" * n

    return gen()


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# -- safe_fromstring --------------------------------------------------------------


@pytest.mark.parametrize(
    "document",
    [BILLION_LAUGHS, EXTERNAL_ENTITY, PLAIN_DTD],
    ids=["billion-laughs", "external-entity", "plain-dtd"],
)
def test_safe_fromstring_refuses_dtds(
    document: str, caplog: pytest.LogCaptureFixture
) -> None:
    assert _bounded.safe_fromstring(document) is None
    assert _bounded.safe_fromstring(document.encode()) is None
    # Refused, and logged without echoing the document.
    assert "camera XML refused" in caplog.text
    assert "passwd" not in caplog.text


def test_safe_fromstring_refuses_oversized_input() -> None:
    body = "<r>" + "x" * 100 + "</r>"
    assert _bounded.safe_fromstring(body, max_bytes=len(body)) is not None
    assert _bounded.safe_fromstring(body, max_bytes=len(body) - 1) is None
    assert _bounded.safe_fromstring(body.encode(), max_bytes=len(body) - 1) is None


def test_safe_fromstring_counts_encoded_bytes_for_text() -> None:
    # 40 characters, 120 UTF-8 bytes: within a 100 "character" budget but over
    # a 100-byte one, so it must be refused.
    body = "<r>" + "€" * 40 + "</r>"
    assert len(body) < 100 < len(body.encode())
    assert _bounded.safe_fromstring(body, max_bytes=100) is None


def test_safe_fromstring_malformed_is_none() -> None:
    assert _bounded.safe_fromstring("<r><unclosed></r>") is None
    assert _bounded.safe_fromstring("") is None


def test_safe_fromstring_yields_standard_iterative_elements() -> None:
    # The stdlib C elements iterate deep trees without recursion; the parser
    # must hand those back, not a pure-Python tree with a recursive iter().
    deep = "<a>" * DEEP + "</a>" * DEEP
    root = _bounded.safe_fromstring(deep)
    assert isinstance(root, ET.Element)
    assert sum(1 for _ in root.iter()) == DEEP


# -- walk_named_descendants ------------------------------------------------------


def test_walk_preserves_document_order_and_skips() -> None:
    root = ET.fromstring("<r><a><b/><skip><c/></skip></a><d/><!--x--></r>")
    walked = [
        path
        for _el, path in _bounded.walk_named_descendants(root, skip=frozenset({"skip"}))
    ]
    assert walked == [("a",), ("a", "b"), ("d",)]


def test_walk_stops_at_depth_cap() -> None:
    root = ET.fromstring("<r>" + "<n>" * 5 + "</n>" * 5 + "</r>")
    paths = [p for _e, p in _bounded.walk_named_descendants(root, max_depth=3)]
    assert [len(p) for p in paths] == [1, 2, 3]


# -- Deep nesting against the topic walkers (CAM-R12) ---------------------------


def test_onvif_topic_walk_survives_deep_nesting() -> None:
    xml = _deep_topic_set(DEEP, wrapper="<GetEventPropertiesResponse>")
    xml = xml.replace("<TopicSet", "<TopicSet").replace(
        "</Body>", "</GetEventPropertiesResponse></Body>"
    )
    assert soap.parse_event_topics(xml) == []


def test_onvif_topic_walk_keeps_topics_above_the_cap() -> None:
    shallow = _deep_topic_set(3, wrapper="")
    assert soap.parse_event_topics(shallow) == ["n0/n1/n2/Leaf"]
    at_cap = _deep_topic_set(_bounded.XML_MAX_DEPTH - 1, wrapper="")
    assert len(soap.parse_event_topics(at_cap)) == 1
    past_cap = _deep_topic_set(_bounded.XML_MAX_DEPTH, wrapper="")
    assert soap.parse_event_topics(past_cap) == []


def test_vapix_instance_walk_survives_deep_nesting() -> None:
    assert vapix.parse_event_instances(_deep_topic_set(DEEP, wrapper="")) == []
    [descriptor] = vapix.parse_event_instances(_deep_topic_set(2, wrapper=""))
    assert descriptor.topic_id.endswith("Leaf")


def test_pull_messages_survive_deep_nesting() -> None:
    xml = (
        "<Envelope><Body><PullMessagesResponse>"
        + "<x>" * DEEP
        + "</x>" * DEEP
        + "</PullMessagesResponse></Body></Envelope>"
    )
    assert soap.parse_pull_messages(xml) == []


# -- Every XML parse site refuses entity tricks (#67-S24) -------------------------


@pytest.mark.parametrize("document", [BILLION_LAUGHS, EXTERNAL_ENTITY])
def test_every_parse_site_refuses_entity_documents(document: str) -> None:
    assert soap.parse_xml(document) is None
    assert soap.parse_event_topics(document) == []
    assert soap.parse_pull_messages(document) == []
    assert soap.parse_subscription_reference(document) is None
    assert vapix.parse_event_instances(document) == []
    assert vapix.geolocation_from_position_xml(document) is None
    assert _parse_probe_match(document.encode()) is None


def test_probe_match_over_one_datagram_is_dropped() -> None:
    padding = "<pad>" + "x" * 70_000 + "</pad>"
    match = (
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">'
        "<s:Body><d:ProbeMatches><d:ProbeMatch>"
        "<d:XAddrs>http://10.0.0.5/onvif/device_service</d:XAddrs>"
        "</d:ProbeMatch></d:ProbeMatches>{pad}</s:Body></s:Envelope>"
    )
    assert _parse_probe_match(match.format(pad="").encode()) is not None
    assert _parse_probe_match(match.format(pad=padding).encode()) is None


# -- Escaping (CAM-S7) ---------------------------------------------------------------


def _parse_envelope(xml: str) -> ET.Element:
    root = _bounded.safe_fromstring(xml)
    assert root is not None, xml
    return root


def test_username_markup_is_escaped() -> None:
    username = "a</wsse:Username><x>"
    root = _parse_envelope(soap.envelope("<tds:GetHostname/>", (username, "pw")))
    found = root.findall(".//wsse:Username", soap.NS)
    assert [element.text for element in found] == [username]
    # Nothing was injected: the only new element is the expected Username.
    assert root.find(".//x") is None


@pytest.mark.parametrize("build", [get_snapshot_uri_body, get_stream_uri_body])
def test_profile_token_markup_is_escaped(build: Any) -> None:
    token = "p&1</trt:ProfileToken><evil/>"
    root = _parse_envelope(soap.envelope(build(token), None))
    assert root.findtext(".//trt:ProfileToken", namespaces=soap.NS) == token
    assert root.find(".//evil") is None


def test_subscription_address_is_escaped() -> None:
    to = "http://10.0.0.5/onvif/services?a=1&b=<2>"
    headers = soap.addressing_headers(soap.ACTION_PULL_MESSAGES, to)
    root = _parse_envelope(soap.envelope("<tev:PullMessages/>", None, headers))
    assert root.findtext(".//wsa:To", namespaces=soap.NS) == to


def test_reference_parameters_with_markup_round_trip() -> None:
    # The camera returns a SubscriptionId whose text (escaped on the wire)
    # contains markup, and a namespace with a double quote. Re-serialised into the
    # next request, both must come back as the same values, with nothing injected.
    namespace = 'urn:cam"ns'
    value = "7<Inject/>&amp"
    create_response = (
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2" '
        'xmlns:wsa="http://www.w3.org/2005/08/addressing">'
        "<s:Body><wsnt:SubscriptionReference>"
        "<wsa:Address>http://10.0.0.5/onvif/services</wsa:Address>"
        "<wsa:ReferenceParameters>"
        "<c:SubscriptionId xmlns:c='urn:cam\"ns'>7&lt;Inject/&gt;&amp;amp"
        "</c:SubscriptionId>"
        "</wsa:ReferenceParameters>"
        "</wsnt:SubscriptionReference></s:Body></s:Envelope>"
    )
    parsed = soap.parse_subscription_reference(create_response)
    assert parsed is not None
    address, ref_params = parsed
    headers = soap.addressing_headers(
        soap.ACTION_PULL_MESSAGES, address, reference_parameters=ref_params
    )
    root = _parse_envelope(soap.envelope("<tev:PullMessages/>", None, headers))
    echoed = root.find(f".//{{{namespace}}}SubscriptionId")
    assert echoed is not None
    assert echoed.text == value
    assert root.find(".//Inject") is None


# -- Bounded reads: the shared helper ------------------------------------------------


async def test_read_capped_response_aborts_an_endless_body() -> None:
    async with _client(
        lambda _r: httpx.Response(200, content=_endless(b"x" * 4096))
    ) as client:
        with pytest.raises(OtherCaptureError, match="exceeded the 65536-byte limit"):
            await _bounded.read_capped_response(
                client, "GET", "http://cam/x", max_bytes=64 * 1024
            )


async def test_read_capped_response_rejects_declared_oversize_before_reading() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Length": str(10**12)}, content=_endless(b"x")
        )

    async with _client(handler) as client:
        with pytest.raises(OtherCaptureError, match="declares"):
            await _bounded.read_capped_response(
                client, "GET", "http://cam/x", max_bytes=10
            )


async def test_read_capped_response_accepts_exactly_the_cap() -> None:
    async with _client(
        lambda _r: httpx.Response(200, content=_body_of(1000))
    ) as client:
        response = await _bounded.read_capped_response(
            client, "GET", "http://cam/x", max_bytes=1000
        )
    assert response.status_code == 200
    assert response.text == "x" * 1000


async def test_read_capped_response_skips_the_body_on_error_status() -> None:
    async with _client(
        lambda _r: httpx.Response(500, content=_endless(b"x"))
    ) as client:
        response = await _bounded.read_capped_response(
            client, "GET", "http://cam/x", max_bytes=10
        )
    assert response.status_code == 500
    assert response.text == ""


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("text/xml; charset=iso-8859-1", "café"),
        ("text/xml; charset=no-such-charset", "caf�"),
        ("text/xml", "caf�"),
    ],
)
async def test_read_capped_response_decodes_declared_charset(
    content_type: str, expected: str
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Type": content_type}, content=b"caf\xe9"
        )

    async with _client(handler) as client:
        response = await _bounded.read_capped_response(
            client, "GET", "http://cam/x", max_bytes=10
        )
    assert response.text == expected


# -- Bounded reads: every SOAP path (CAM-S3) ------------------------------------------


async def test_onvif_device_soap_call_is_capped(camera_context: None) -> None:
    client = _client(lambda _r: httpx.Response(200, content=_body_of(SOAP_CAP + 1)))
    adapter = OnvifAdapter(client, "10.0.0.5")
    with pytest.raises(OtherCaptureError, match=f"{SOAP_CAP}-byte limit"):
        await adapter._soap_call("http://10.0.0.5/onvif/device_service", "<x/>")


async def test_onvif_soap_call_accepts_a_body_at_the_cap(camera_context: None) -> None:
    client = _client(lambda _r: httpx.Response(200, content=_body_of(SOAP_CAP)))
    adapter = OnvifAdapter(client, "10.0.0.5")
    text = await adapter._soap_call("http://10.0.0.5/onvif/device_service", "<x/>")
    assert len(text) == SOAP_CAP


async def test_onvif_event_soap_call_is_capped(camera_context: None) -> None:
    client = _client(lambda _r: httpx.Response(200, content=_body_of(SOAP_CAP + 1)))
    adapter = OnvifAdapter(client, "10.0.0.5")
    with pytest.raises(OtherCaptureError, match=f"{SOAP_CAP}-byte limit"):
        await adapter._guarded_event_soap_call(
            "http://10.0.0.5/onvif/services",
            soap.pull_messages_body(),
            action=soap.ACTION_PULL_MESSAGES,
        )


async def test_onvif_soap_error_status_is_still_mapped(camera_context: None) -> None:
    client = _client(lambda _r: httpx.Response(500, content=_endless(b"x")))
    adapter = OnvifAdapter(client, "10.0.0.5")
    with pytest.raises(OtherCaptureError, match="returned 500"):
        await adapter._soap_call("http://10.0.0.5/onvif/device_service", "<x/>")


async def test_vapix_post_soap_is_capped(camera_context: None) -> None:
    client = _client(lambda _r: httpx.Response(200, content=_body_of(SOAP_CAP + 1)))
    adapter = vapix.VapixAdapter(client, "10.0.0.5")
    with pytest.raises(OtherCaptureError, match=f"{SOAP_CAP}-byte limit"):
        await adapter._post_soap("http://10.0.0.5/vapix/services", "<x/>")


async def test_vapix_post_soap_caps_the_authenticated_retry(
    camera_context: None,
) -> None:
    seen: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorised = "authorization" in request.headers
        seen.append(authorised)
        if not authorised:
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": 'Digest realm="axis", nonce="n"'},
            )
        return httpx.Response(200, content=_body_of(SOAP_CAP + 1))

    adapter = vapix.VapixAdapter(_client(handler), "10.0.0.5", credentials=("u", "p"))
    with pytest.raises(OtherCaptureError, match=f"{SOAP_CAP}-byte limit"):
        await adapter._post_soap("http://10.0.0.5/vapix/services", "<x/>")
    assert seen == [False, True]


# -- Bounded reads: VAPIX text CGIs ------------------------------------------------


async def test_http_get_image_honours_an_explicit_cap(camera_context: None) -> None:
    client = _client(lambda _r: httpx.Response(200, content=_body_of(TEXT_CAP + 1)))
    with pytest.raises(OtherCaptureError, match=f"{TEXT_CAP}-byte limit"):
        await http_get_image(client, "http://10.0.0.5/x", None, 5.0, max_bytes=TEXT_CAP)


async def test_vapix_param_read_over_the_cap_reads_as_failure(
    camera_context: None,
) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(200, content=_body_of(TEXT_CAP + 1))

    adapter = vapix.VapixAdapter(_client(handler), "10.0.0.5")
    assert await adapter._read_params("Image") == {}
    assert requested == [vapix.PARAM_PATH]


async def test_vapix_ptz_move_over_the_cap_fails_closed(camera_context: None) -> None:
    client = _client(lambda _r: httpx.Response(200, content=_body_of(TEXT_CAP + 1)))
    adapter = vapix.VapixAdapter(client, "10.0.0.5")
    with pytest.raises(PTZError, match=f"{TEXT_CAP}-byte limit"):
        await adapter.move_to(preset_id="Home")


async def test_vapix_geolocation_over_the_cap_raises(camera_context: None) -> None:
    client = _client(lambda _r: httpx.Response(200, content=_body_of(TEXT_CAP + 1)))
    adapter = vapix.VapixAdapter(client, "10.0.0.5")
    with pytest.raises(OtherCaptureError, match=f"{TEXT_CAP}-byte limit"):
        await adapter.read_geolocation_api()


# -- WebSocket handshake (CAM-S7, CAM-R12) ------------------------------------------


class _Writer:
    """An asyncio.StreamWriter stand-in that records what was sent."""

    def __init__(self) -> None:
        self.written = bytearray()

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


def _reader(data: bytes) -> asyncio.StreamReader:
    """A real StreamReader (default 64 KiB line limit) preloaded with ``data``."""
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


async def _handshake(response_head: bytes) -> tuple[int, str]:
    writer = _Writer()
    return await vapix._ws_handshake(
        _reader(response_head),
        writer,  # type: ignore[arg-type]
        urlsplit("http://10.0.0.5/vapix/ws-data-stream?sources=events"),
        "10.0.0.5",
        80,
        auth=None,
    )


def _upgrade(accept: str | None) -> bytes:
    head = b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
    if accept is not None:
        head += f"Sec-WebSocket-Accept: {accept}\r\n".encode()
    return head + b"\r\n"


def test_expected_accept_matches_rfc_6455_sample() -> None:
    # RFC 6455 section 1.3's worked example.
    assert (
        vapix._ws_expected_accept("dGhlIHNhbXBsZSBub25jZQ==")
        == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
    )


async def test_handshake_accepts_the_matching_accept_key() -> None:
    key = "x3JJHMbDL1EzLkh9GBhXDw=="
    with patch.object(vapix, "_new_ws_key", return_value=key):
        status, _ = await _handshake(_upgrade(vapix._ws_expected_accept(key)))
    assert status == 101


async def test_handshake_sends_a_fresh_key_and_checks_against_it() -> None:
    writer = _Writer()
    # Answer with the accept for some other key: the real fresh key never matches.
    with pytest.raises(OtherCaptureError, match="Sec-WebSocket-Accept"):
        await vapix._ws_handshake(
            _reader(_upgrade(vapix._ws_expected_accept("dGhlIHNhbXBsZSBub25jZQ=="))),
            writer,  # type: ignore[arg-type]
            urlsplit("http://10.0.0.5/x"),
            "10.0.0.5",
            80,
            auth=None,
        )
    assert b"Sec-WebSocket-Key: " in writer.written


@pytest.mark.parametrize("accept", [None, "", "bad", "s3pPLMBiTxaQ9kYGzzhZRbK+xOo"])
async def test_handshake_refuses_a_missing_or_wrong_accept_key(
    accept: str | None,
) -> None:
    with (
        patch.object(vapix, "_new_ws_key", return_value="dGhlIHNhbXBsZSBub25jZQ=="),
        pytest.raises(OtherCaptureError, match="Sec-WebSocket-Accept"),
    ):
        await _handshake(_upgrade(accept))


async def test_handshake_does_not_demand_an_accept_key_on_401() -> None:
    head = b'HTTP/1.1 401 Unauthorized\r\nWWW-Authenticate: Basic realm="a"\r\n\r\n'
    status, challenges = await _handshake(head)
    assert (status, challenges) == (401, ['Basic realm="a"'])


async def test_handshake_maps_a_70_kib_header_line() -> None:
    head = (
        b"HTTP/1.1 101 Switching Protocols\r\nX-Junk: "
        + b"a" * (70 * 1024)
        + b"\r\n\r\n"
    )
    with pytest.raises(OtherCaptureError, match="line too long"):
        await _handshake(head)


async def test_handshake_maps_a_70_kib_status_line() -> None:
    with pytest.raises(OtherCaptureError, match="line too long"):
        await _handshake(b"HTTP/1.1 101 " + b"a" * (70 * 1024) + b"\r\n\r\n")


async def test_handshake_refuses_endless_header_lines() -> None:
    head = b"HTTP/1.1 101 Switching Protocols\r\n" + b"X-A: b\r\n" * 1000 + b"\r\n"
    with pytest.raises(OtherCaptureError, match="header lines"):
        await _handshake(head)


async def test_event_source_fails_on_a_bad_accept_key(
    camera_context: None,
) -> None:
    # End to end through the adapter: a 101 with the wrong accept never becomes a
    # connection, and the socket is closed.
    writer = _Writer()
    closed: list[bool] = []
    writer.close = lambda: closed.append(True)  # type: ignore[method-assign]

    async def fake_open_connection(*_a: Any, **_k: Any) -> Any:
        return _reader(_upgrade("bad") + bytes([0x88, 0x00])), writer

    async def pin(host: str, **_: Any) -> str:
        return host

    adapter = vapix.VapixAdapter(httpx.AsyncClient(), "10.0.0.5")
    with (
        patch.object(vapix, "resolve_and_check_pinned_async", new=pin),
        patch.object(
            vapix.asyncio, "open_connection", side_effect=fake_open_connection
        ),
        patch.object(
            vapix,
            "_resolve_ws_limits",
            return_value=vapix._WsLimits(30.0, 60.0, 1024),
        ),
        pytest.raises(OtherCaptureError, match="Sec-WebSocket-Accept"),
    ):
        async for _event in adapter.open_event_source():
            pass  # pragma: no cover - the handshake must fail first
    assert closed
