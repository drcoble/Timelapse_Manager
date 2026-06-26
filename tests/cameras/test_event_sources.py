"""Tests for the live event-source iterators and the listener factory.

These drive the ONVIF PullPoint loop and the VAPIX WebSocket source with mocked
transports (no real camera): the ONVIF source against a fake httpx client that
returns the spike's CreatePullPoint / PullMessages / Unsubscribe responses, and
the factory's VAPIX->ONVIF fallback against fake adapters. The point is the
lifecycle (subscribe -> yield -> unsubscribe on teardown) and the canonical
CameraEvent contract, not the wire bytes (those are covered elsewhere).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from timelapse_manager.cameras.base import (
    CameraEvent,
    EventNotSupportedError,
    OtherCaptureError,
)
from timelapse_manager.cameras.onvif import OnvifAdapter
from timelapse_manager.capture.event_listener import EventListenerFactory

_CREATE_RESPONSE = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
  xmlns:tev="http://www.onvif.org/ver10/events/wsdl"
  xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"
  xmlns:wsa5="http://www.w3.org/2005/08/addressing">
  <s:Body><tev:CreatePullPointSubscriptionResponse>
    <tev:SubscriptionReference>
      <wsa5:Address>http://10.0.0.5/onvif/services</wsa5:Address>
      <wsa5:ReferenceParameters>
        <dom0:SubscriptionId xmlns:dom0="http://www.axis.com/2009/event">7</dom0:SubscriptionId>
      </wsa5:ReferenceParameters>
    </tev:SubscriptionReference>
    <wsnt:TerminationTime>2026-06-24T00:25:00Z</wsnt:TerminationTime>
  </tev:CreatePullPointSubscriptionResponse></s:Body></s:Envelope>"""

_PULL_RESPONSE = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
  xmlns:tev="http://www.onvif.org/ver10/events/wsdl"
  xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"
  xmlns:tt="http://www.onvif.org/ver10/schema">
  <s:Body><tev:PullMessagesResponse>
    <wsnt:NotificationMessage>
      <wsnt:Topic>tns1:Device/tnsaxis:IO/VirtualInput</wsnt:Topic>
      <wsnt:Message>
        <tt:Message UtcTime="2026-06-24T00:23:05Z" PropertyOperation="Changed">
          <tt:Source><tt:SimpleItem Name="port" Value="1"/></tt:Source>
          <tt:Data><tt:SimpleItem Name="active" Value="1"/></tt:Data>
        </tt:Message>
      </wsnt:Message>
    </wsnt:NotificationMessage>
  </tev:PullMessagesResponse></s:Body></s:Envelope>"""

_UNSUBSCRIBE_RESPONSE = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
  xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2">
  <s:Body><wsnt:UnsubscribeResponse/></s:Body></s:Envelope>"""


def _response(text: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.text = text
    return resp


@pytest.fixture(autouse=True)
def _stub_ssrf_context() -> Any:
    """Stub the runtime context so the event SSRF guard resolves without an app.

    The guard reads ``get_context().settings.ssrf.allowed_private_subnets`` and
    calls ``assert_allowed_url``; both are patched so the source logic is tested
    without standing up the application context or doing DNS.
    """
    ctx = SimpleNamespace(
        settings=SimpleNamespace(ssrf=SimpleNamespace(allowed_private_subnets=()))
    )
    with (
        patch("timelapse_manager.runtime.get_context", return_value=ctx),
        patch(
            "timelapse_manager.cameras.onvif.assert_allowed_url",
            side_effect=lambda url, **_: url,
        ),
        patch(
            "timelapse_manager.cameras.vapix.assert_allowed_url",
            side_effect=lambda url, **_: url,
        ),
    ):
        yield


async def test_onvif_pullpoint_yields_and_unsubscribes() -> None:
    # The client returns Create, then Pull (one event), then any further call
    # (the next Pull, or the teardown Unsubscribe) succeeds.
    posts: list[str] = []

    async def fake_post(url: str, **kwargs: Any) -> MagicMock:
        body = kwargs.get("content", "")
        if "CreatePullPointSubscription" in body:
            return _response(_CREATE_RESPONSE)
        if "PullMessages" in body:
            posts.append("pull")
            return _response(_PULL_RESPONSE)
        if "Unsubscribe" in body:
            posts.append("unsubscribe")
            return _response(_UNSUBSCRIBE_RESPONSE)
        return _response("<s:Envelope/>")

    client = MagicMock()
    client.post = AsyncMock(side_effect=fake_post)
    adapter = OnvifAdapter(client, "10.0.0.5", credentials=("u", "p"))

    source = adapter.open_event_source()
    first = await source.__anext__()
    assert isinstance(first, CameraEvent)
    assert first.topic_id == "Device/IO/VirtualInput"
    assert first.active is True

    # Tearing the generator down must Unsubscribe (best-effort, in the finally).
    await source.aclose()
    assert "unsubscribe" in posts


async def test_onvif_pullpoint_subscribe_pull_yield_echoes_reference() -> None:
    # The PullPoint contract: Create precedes Pull; the Pull (and the teardown
    # Unsubscribe) echo the SubscriptionId reference parameter from the Create
    # response; and the yielded event carries the canonical topic/active state.
    sent: list[str] = []

    pull_bodies: list[str] = []

    async def fake_post(url: str, **kwargs: Any) -> MagicMock:
        body = kwargs.get("content", "")
        if "GetServices" in body:
            return _response("<s:Envelope/>")  # resolve events URL -> fallback
        if "CreatePullPointSubscription" in body:
            sent.append("create")
            return _response(_CREATE_RESPONSE)
        if "PullMessages" in body:
            sent.append("pull")
            pull_bodies.append(body)
            return _response(_PULL_RESPONSE)
        sent.append("unsubscribe")
        return _response(_UNSUBSCRIBE_RESPONSE)

    client = MagicMock()
    client.post = AsyncMock(side_effect=fake_post)
    adapter = OnvifAdapter(client, "10.0.0.5", credentials=("u", "p"))

    source = adapter.open_event_source()
    event = await source.__anext__()
    # Create happened before the first Pull.
    assert sent[0] == "create"
    assert "pull" in sent
    assert isinstance(event, CameraEvent)
    assert event.topic_id == "Device/IO/VirtualInput"  # canonicalised
    assert event.active is True
    # The echoed SubscriptionId ref-param (7) must ride on the Pull, or the
    # firmware rejects it with ter:InvalidArgs.
    assert "SubscriptionId" in pull_bodies[0]
    assert "7" in pull_bodies[0]
    await source.aclose()
    assert "unsubscribe" in sent


async def test_onvif_pullpoint_unsubscribes_on_task_cancellation() -> None:
    # Production teardown is task cancellation thrown into the PullMessages await,
    # not aclose(); the finally must still fire Unsubscribe.
    posts: list[str] = []
    pulled = asyncio.Event()

    async def fake_post(url: str, **kwargs: Any) -> MagicMock:
        body = kwargs.get("content", "")
        if "CreatePullPointSubscription" in body:
            return _response(_CREATE_RESPONSE)
        if "PullMessages" in body:
            pulled.set()
            await asyncio.sleep(60)  # long-poll: cancellation lands here
            return _response(_PULL_RESPONSE)
        if "Unsubscribe" in body:
            posts.append("unsubscribe")
            return _response(_UNSUBSCRIBE_RESPONSE)
        return _response("<s:Envelope/>")

    client = MagicMock()
    client.post = AsyncMock(side_effect=fake_post)
    adapter = OnvifAdapter(client, "10.0.0.5", credentials=("u", "p"))

    async def consume() -> None:
        async for _event in adapter.open_event_source():
            pass

    task = asyncio.ensure_future(consume())
    await pulled.wait()  # subscription is up and blocked in PullMessages
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "unsubscribe" in posts  # teardown released the subscription


async def test_onvif_pullpoint_raises_without_subscription_reference() -> None:
    client = MagicMock()
    client.post = AsyncMock(return_value=_response("<s:Envelope/>"))
    adapter = OnvifAdapter(client, "10.0.0.5", credentials=("u", "p"))
    source = adapter.open_event_source()
    with pytest.raises(OtherCaptureError):  # no subscription reference returned
        await source.__anext__()


# -- Factory ----------------------------------------------------------------


def _camera(protocol: str) -> SimpleNamespace:
    return SimpleNamespace(
        protocol=protocol,
        address="10.0.0.5",
        credentials=None,
        credentials_inherit_default=False,
        snapshot_uri=None,
        stream_uri=None,
        default_resolution=None,
    )


def _target(camera_id: int = 1, project_id: int = 1) -> Any:
    return SimpleNamespace(camera_id=camera_id, project_id=project_id)


async def test_factory_parks_for_unknown_camera() -> None:
    # An unresolvable camera yields a source that parks (waits for cancellation)
    # rather than ending, so the supervisor does not busy-respawn it.
    factory = EventListenerFactory(MagicMock(), camera_loader=lambda cid: None)
    source = factory(_target())
    assert source is not None
    task = asyncio.ensure_future(source.__anext__())
    await asyncio.sleep(0.05)
    assert not task.done()  # parked, not ended
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_factory_parks_for_non_event_protocol() -> None:
    factory = EventListenerFactory(
        MagicMock(), camera_loader=lambda cid: _camera("rtsp")
    )
    source = factory(_target())
    assert source is not None
    task = asyncio.ensure_future(source.__anext__())
    await asyncio.sleep(0.05)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_factory_call_is_io_free_and_returns_iterator() -> None:
    # Calling the factory must not touch the camera: the loader is invoked only on
    # the iterator's first iteration (off-thread), never in __call__.
    loaded: list[int] = []

    def _loader(cid: int) -> Any:
        loaded.append(cid)
        return _camera("onvif")

    factory = EventListenerFactory(MagicMock(), camera_loader=_loader)
    source = factory(_target())
    assert source is not None
    assert hasattr(source, "__anext__")
    # __call__ did no lookup -- the loader has not run yet.
    assert loaded == []


async def test_factory_vapix_falls_back_to_onvif_on_unsupported_ws() -> None:
    # An adapter whose WS source raises EventNotSupportedError must transparently
    # drive the ONVIF PullPoint source instead.
    onvif_event = CameraEvent(
        topic_id="Device/IO/VirtualInput",
        category="io",
        source={"port": "1"},
        data={"active": "1"},
        active=True,
        occurred_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )

    async def ws_source() -> Any:
        raise EventNotSupportedError("404")
        yield  # pragma: no cover - makes this an async generator

    async def onvif_source() -> Any:
        yield onvif_event

    vapix_adapter = MagicMock()
    vapix_adapter.open_event_source = MagicMock(return_value=ws_source())
    vapix_adapter.close = AsyncMock()
    onvif_adapter = MagicMock()
    onvif_adapter.open_event_source = MagicMock(return_value=onvif_source())
    onvif_adapter.close = AsyncMock()

    # build_adapter is called twice: first for the vapix view, then the onvif view.
    adapters = iter([vapix_adapter, onvif_adapter])

    factory = EventListenerFactory(
        MagicMock(), camera_loader=lambda cid: _camera("vapix")
    )
    with patch(
        "timelapse_manager.capture.event_listener.build_adapter",
        side_effect=lambda *a, **k: next(adapters),
    ):
        source = factory(_target())
        assert source is not None
        events = [event async for event in source]

    assert len(events) == 1
    assert events[0].topic_id == "Device/IO/VirtualInput"
    vapix_adapter.close.assert_awaited()
    onvif_adapter.close.assert_awaited()


def _onvif_event() -> CameraEvent:
    return CameraEvent(
        topic_id="Device/IO/VirtualInput",
        category="io",
        source={"port": "1"},
        data={"active": "1"},
        active=True,
        occurred_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )


async def test_factory_vapix_falls_back_on_any_pre_event_ws_failure() -> None:
    # The crux of the fix: a WebSocket failure that is NOT EventNotSupportedError
    # (e.g. a broken upgrade/subscribe surfacing as OtherCaptureError) and that
    # happens before any event has been delivered must STILL fall back to ONVIF
    # PullPoint, rather than propagating and being backoff-looped forever.
    async def ws_source() -> Any:
        raise OtherCaptureError("ws upgrade returned 0")
        yield  # pragma: no cover - makes this an async generator

    async def onvif_source() -> Any:
        yield _onvif_event()

    vapix_adapter = MagicMock()
    vapix_adapter.open_event_source = MagicMock(return_value=ws_source())
    vapix_adapter.close = AsyncMock()
    onvif_adapter = MagicMock()
    onvif_adapter.open_event_source = MagicMock(return_value=onvif_source())
    onvif_adapter.close = AsyncMock()
    adapters = iter([vapix_adapter, onvif_adapter])

    factory = EventListenerFactory(
        MagicMock(), camera_loader=lambda cid: _camera("vapix")
    )
    with patch(
        "timelapse_manager.capture.event_listener.build_adapter",
        side_effect=lambda *a, **k: next(adapters),
    ):
        events = [event async for event in factory(_target())]

    assert len(events) == 1  # the ONVIF fallback delivered the event
    onvif_adapter.open_event_source.assert_called_once()


async def test_factory_vapix_falls_back_when_ws_ends_without_events() -> None:
    # A WebSocket that connects, subscribes, and then closes with zero events
    # (the live symptom: 101 then immediate FIN) must fall back to PullPoint, not
    # be treated as a clean end that resets backoff and re-opens the same WS.
    async def ws_source() -> Any:
        return
        yield  # pragma: no cover - makes this an async generator

    async def onvif_source() -> Any:
        yield _onvif_event()

    vapix_adapter = MagicMock()
    vapix_adapter.open_event_source = MagicMock(return_value=ws_source())
    vapix_adapter.close = AsyncMock()
    onvif_adapter = MagicMock()
    onvif_adapter.open_event_source = MagicMock(return_value=onvif_source())
    onvif_adapter.close = AsyncMock()
    adapters = iter([vapix_adapter, onvif_adapter])

    factory = EventListenerFactory(
        MagicMock(), camera_loader=lambda cid: _camera("vapix")
    )
    with patch(
        "timelapse_manager.capture.event_listener.build_adapter",
        side_effect=lambda *a, **k: next(adapters),
    ):
        events = [event async for event in factory(_target())]

    assert len(events) == 1
    onvif_adapter.open_event_source.assert_called_once()


async def test_factory_vapix_does_not_fall_back_after_first_event() -> None:
    # Once events flow on the WebSocket, a later failure must propagate (so the
    # supervisor backs off and re-subscribes over the same fast-path) rather than
    # silently downgrading a healthy camera to PullPoint.
    ws_event = CameraEvent(
        topic_id="Device/IO/VirtualInput",
        category="io",
        source={"port": "1"},
        data={"active": "1"},
        active=True,
        occurred_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )

    async def ws_source() -> Any:
        yield ws_event
        raise OtherCaptureError("ws dropped mid-stream")

    onvif_adapter = MagicMock()
    onvif_adapter.open_event_source = MagicMock()
    onvif_adapter.close = AsyncMock()
    vapix_adapter = MagicMock()
    vapix_adapter.open_event_source = MagicMock(return_value=ws_source())
    vapix_adapter.close = AsyncMock()
    adapters = iter([vapix_adapter, onvif_adapter])

    factory = EventListenerFactory(
        MagicMock(), camera_loader=lambda cid: _camera("vapix")
    )
    collected: list[CameraEvent] = []
    with (
        patch(
            "timelapse_manager.capture.event_listener.build_adapter",
            side_effect=lambda *a, **k: next(adapters),
        ),
        pytest.raises(OtherCaptureError),
    ):
        async for event in factory(_target()):
            collected.append(event)

    assert len(collected) == 1  # the one event before the drop was delivered
    onvif_adapter.open_event_source.assert_not_called()  # no downgrade to PullPoint
