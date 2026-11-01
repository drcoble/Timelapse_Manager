"""The event listener resolves cameras through the camera-session registry (#153).

Before #153 a subscription re-read the camera and decrypted its credentials
every time, and an undecryptable own credential surfaced as Fernet's bare
``InvalidToken``. Wired with the supervisor's registry (as ``app.py`` does) a
subscription:

* takes the camera's configuration and once-decrypted credentials from the
  same cache the captures use, loading on the capture executor only on a miss;
* fails with the registry's named, *local* "key changed?" error when the stored
  credential cannot be decrypted, and does not mistake that for a WebSocket
  failure that falls back to ONVIF PullPoint;
* parks, as before, when the camera no longer exists.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session, sessionmaker

from tests.unit.test_campaign_bounds import _make_settings, _permissive_disk_monitor
from timelapse_manager.cameras.base import OtherCaptureError
from timelapse_manager.capture.event_listener import EventListenerFactory
from timelapse_manager.capture.supervisor import CaptureSupervisor, CaptureTarget
from timelapse_manager.db.models import Camera
from timelapse_manager.db.session import session_scope
from timelapse_manager.security import crypto as decrypt_mod
from timelapse_manager.security.crypto import (
    CREDENTIAL_DECRYPT_MESSAGE,
    CredentialDecryptError,
    encrypt_credentials,
)

_BUILD = "timelapse_manager.capture.event_listener.build_adapter"
_THREAD_PREFIX = "tlm-capture-test"


def _add_camera(
    factory: sessionmaker[Session], *, protocol: str, credentials: Any
) -> int:
    with session_scope(factory) as session:
        camera = Camera(
            name=f"{protocol}-cam",
            address="10.0.0.5",
            protocol=protocol,
            credentials=credentials,
            credentials_inherit_default=False,
        )
        session.add(camera)
        session.flush()
        return camera.id


def _foreign_key_credentials() -> dict[str, object]:
    """A credential document encrypted under a key the app does not hold."""
    token = Fernet(Fernet.generate_key()).encrypt(b"own-pw").decode("ascii")
    return {"username": "own", "password": f"enc:v1:{token}"}


def _target(camera_id: int) -> CaptureTarget:
    return CaptureTarget(
        project_id=1, project_name="p", camera_id=camera_id, interval_seconds=None
    )


async def _parked() -> AsyncIterator[Any]:
    await asyncio.Event().wait()
    yield  # pragma: no cover


def _parked_adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.open_event_source = MagicMock(side_effect=_parked)
    adapter.close = AsyncMock()
    return adapter


@pytest.fixture()
def pool() -> Iterator[ThreadPoolExecutor]:
    executor = ThreadPoolExecutor(thread_name_prefix=_THREAD_PREFIX)
    yield executor
    executor.shutdown(wait=True)


@pytest.fixture()
def supervisor(
    migrated_factory: sessionmaker[Session],
    tmp_path: Path,
    pool: ThreadPoolExecutor,
) -> CaptureSupervisor:
    return CaptureSupervisor(
        _make_settings(tmp_path),
        migrated_factory,
        disk_monitor=_permissive_disk_monitor(),
        executor=pool,
    )


def _factory(
    supervisor: CaptureSupervisor, pool: ThreadPoolExecutor
) -> EventListenerFactory:
    """The factory exactly as ``app.py`` wires it."""
    return EventListenerFactory(
        MagicMock(),
        supervisor._load_camera,
        default_credentials_loader=supervisor._load_default_credentials,
        executor=pool,
        camera_sessions=supervisor.camera_sessions,
    )


async def _subscribe_until_open(source: AsyncIterator[Any], adapter: MagicMock) -> None:
    """Drive ``source`` until its adapter opens the event stream, then stop it."""
    consumer = asyncio.create_task(anext(source))  # type: ignore[arg-type]
    for _ in range(500):
        if adapter.open_event_source.call_count:
            break
        await asyncio.sleep(0.01)
    consumer.cancel()
    await asyncio.gather(consumer, return_exceptions=True)
    await source.aclose()  # type: ignore[attr-defined]


async def test_subscriptions_share_the_cached_session(
    migrated_factory: sessionmaker[Session],
    supervisor: CaptureSupervisor,
    pool: ThreadPoolExecutor,
) -> None:
    """Two subscriptions: one load, on the capture executor; decrypted once."""
    camera_id = _add_camera(
        migrated_factory,
        protocol="onvif",
        credentials=encrypt_credentials({"username": "own", "password": "own-pw"}),
    )
    load_threads: list[str] = []
    real_load = supervisor._load_capture_config

    def recording_load(cid: int) -> Any:
        load_threads.append(threading.current_thread().name)
        return real_load(cid)

    factory = _factory(supervisor, pool)
    adapter = _parked_adapter()
    with (
        patch.object(supervisor, "_load_capture_config", side_effect=recording_load),
        patch(_BUILD, return_value=adapter) as build,
    ):
        for _ in range(2):
            await _subscribe_until_open(factory(_target(camera_id)), adapter)
            adapter.open_event_source.reset_mock()

    assert supervisor.camera_sessions.loads == 1
    assert len(load_threads) == 1
    assert load_threads[0].startswith(_THREAD_PREFIX)
    assert build.call_count == 2
    for call in build.call_args_list:
        # Resolved credentials are passed in, so the adapter decrypts nothing.
        assert call.kwargs["credentials"] == ("own", "own-pw")


@pytest.mark.parametrize("protocol", ["vapix", "onvif"])
async def test_an_undecryptable_credential_is_the_named_local_error(
    migrated_factory: sessionmaker[Session],
    supervisor: CaptureSupervisor,
    pool: ThreadPoolExecutor,
    protocol: str,
) -> None:
    """The listener raises "key changed?" (local), and VAPIX does not fall back."""
    camera_id = _add_camera(
        migrated_factory, protocol=protocol, credentials=_foreign_key_credentials()
    )
    factory = _factory(supervisor, pool)
    with patch(_BUILD) as build:
        source = factory(_target(camera_id))
        with pytest.raises(OtherCaptureError) as raised:
            await asyncio.wait_for(anext(source), 10)  # type: ignore[arg-type]

    assert str(raised.value) == CREDENTIAL_DECRYPT_MESSAGE
    assert "key changed?" in str(raised.value)
    assert raised.value.local is True
    assert isinstance(raised.value.__cause__, CredentialDecryptError)
    # No transport was tried: not the WebSocket, not a PullPoint fallback.
    build.assert_not_called()
    # Nothing is cached, so a fixed key is picked up on the next subscription.
    assert supervisor.camera_sessions.cached(camera_id) is None


async def test_a_deleted_camera_parks(
    supervisor: CaptureSupervisor, pool: ThreadPoolExecutor
) -> None:
    """A missing camera parks the subscription instead of raising (no backoff loop)."""
    factory = _factory(supervisor, pool)
    with patch(_BUILD) as build:
        source = factory(_target(999_999))
        consumer = asyncio.create_task(anext(source))  # type: ignore[arg-type]
        await asyncio.sleep(0.3)
        assert not consumer.done()
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await source.aclose()  # type: ignore[attr-defined]
    build.assert_not_called()


# -- A factory without the registry (#162) -------------------------------------


def _bare_factory(
    supervisor: CaptureSupervisor, pool: ThreadPoolExecutor
) -> EventListenerFactory:
    """The factory as tests and other wiring build it: no session registry."""
    return EventListenerFactory(
        MagicMock(),
        supervisor._load_camera,
        default_credentials_loader=supervisor._load_default_credentials,
        executor=pool,
    )


@pytest.mark.parametrize("protocol", ["vapix", "onvif"])
async def test_without_the_registry_an_undecryptable_credential_is_named_local(
    migrated_factory: sessionmaker[Session],
    supervisor: CaptureSupervisor,
    pool: ThreadPoolExecutor,
    protocol: str,
) -> None:
    camera_id = _add_camera(
        migrated_factory, protocol=protocol, credentials=_foreign_key_credentials()
    )
    factory = _bare_factory(supervisor, pool)
    with patch(_BUILD) as build:
        source = factory(_target(camera_id))
        with pytest.raises(OtherCaptureError) as raised:
            await asyncio.wait_for(anext(source), 10)  # type: ignore[arg-type]

    # Not Fernet's bare, message-less InvalidToken.
    assert str(raised.value) == CREDENTIAL_DECRYPT_MESSAGE
    assert raised.value.local is True
    build.assert_not_called()


async def test_without_the_registry_credentials_are_decrypted_off_the_loop(
    migrated_factory: sessionmaker[Session],
    supervisor: CaptureSupervisor,
    pool: ThreadPoolExecutor,
) -> None:
    camera_id = _add_camera(
        migrated_factory,
        protocol="onvif",
        credentials=encrypt_credentials({"username": "own", "password": "own-pw"}),
    )
    decrypt_threads: list[str] = []
    real = decrypt_mod.decrypt_credentials

    def recording(creds: Any) -> Any:
        decrypt_threads.append(threading.current_thread().name)
        return real(creds)

    factory = _bare_factory(supervisor, pool)
    adapter = _parked_adapter()
    with (
        patch("timelapse_manager.cameras.http_jpeg.decrypt_credentials", recording),
        patch(_BUILD, return_value=adapter) as build,
    ):
        await _subscribe_until_open(factory(_target(camera_id)), adapter)

    # Decrypted once, in the subscription's worker hop; the adapter is handed the
    # result and decrypts nothing on the loop.
    assert decrypt_threads
    assert all(name.startswith(_THREAD_PREFIX) for name in decrypt_threads)
    assert build.call_args.kwargs["credentials"] == ("own", "own-pw")


# -- Session sharing and reports ---------------------------------------------------


async def test_the_listener_shares_the_sessions_caches_and_auth_state(
    migrated_factory: sessionmaker[Session],
    supervisor: CaptureSupervisor,
    pool: ThreadPoolExecutor,
) -> None:
    camera_id = _add_camera(
        migrated_factory,
        protocol="onvif",
        credentials=encrypt_credentials({"username": "own", "password": "own-pw"}),
    )
    factory = _factory(supervisor, pool)
    adapter = _parked_adapter()
    with patch(_BUILD, return_value=adapter) as build:
        await _subscribe_until_open(factory(_target(camera_id)), adapter)

    session = supervisor.camera_sessions.cached(camera_id)
    assert session is not None
    kwargs = build.call_args.kwargs
    # The ONVIF clock offset is measured once per camera and the Digest nonce
    # is negotiated once, whoever asks first: a capture or the listener.
    assert kwargs["uri_cache"] is session.uri_cache
    assert kwargs["knob_cache"] is session.knob_cache
    assert kwargs["auth_state"] is session.auth_state


class _Reporter:
    def __init__(self) -> None:
        self.subscribed: list[tuple[int, str]] = []
        self.downgrades: list[tuple[str, str, str]] = []

    async def listener_subscribed(self, target: CaptureTarget, transport: str) -> None:
        self.subscribed.append((target.project_id, transport))

    async def listener_downgraded(
        self,
        target: CaptureTarget,
        *,
        from_transport: str,
        to_transport: str,
        reason: str,
    ) -> None:
        self.downgrades.append((from_transport, to_transport, reason))


async def _failing_ws() -> AsyncIterator[Any]:
    raise ConnectionError("upgrade refused for http://u:p@10.0.0.5/ws")
    yield  # pragma: no cover


async def test_a_websocket_failure_is_reported_as_one_downgrade(
    migrated_factory: sessionmaker[Session],
    supervisor: CaptureSupervisor,
    pool: ThreadPoolExecutor,
) -> None:
    camera_id = _add_camera(migrated_factory, protocol="vapix", credentials=None)
    factory = _factory(supervisor, pool)
    reporter = _Reporter()
    factory.set_reporter(reporter)
    ws = MagicMock()
    ws.open_event_source = MagicMock(side_effect=_failing_ws)
    ws.close = AsyncMock()
    pullpoint = _parked_adapter()
    with patch(_BUILD, side_effect=[ws, pullpoint]):
        await _subscribe_until_open(factory(_target(camera_id)), pullpoint)

    assert [(a, b) for a, b, _ in reporter.downgrades] == [
        ("vapix-websocket", "onvif-pullpoint")
    ]
    assert "upgrade refused" in reporter.downgrades[0][2]


async def test_the_adapters_subscribe_hook_reports_the_transport(
    migrated_factory: sessionmaker[Session],
    supervisor: CaptureSupervisor,
    pool: ThreadPoolExecutor,
) -> None:
    from timelapse_manager.cameras.onvif import OnvifAdapter

    camera_id = _add_camera(migrated_factory, protocol="onvif", credentials=None)
    factory = _factory(supervisor, pool)
    reporter = _Reporter()
    factory.set_reporter(reporter)
    adapter = OnvifAdapter(MagicMock(), "10.0.0.5")

    async def parked(*_a: Any, **_k: Any) -> AsyncIterator[Any]:
        assert adapter.on_subscribed is not None
        await adapter.on_subscribed()
        await asyncio.Event().wait()
        yield  # pragma: no cover

    adapter.open_event_source = MagicMock(side_effect=parked)  # type: ignore[method-assign]
    with patch(_BUILD, return_value=adapter):
        source = factory(_target(camera_id))
        consumer = asyncio.create_task(anext(source))  # type: ignore[arg-type]
        for _ in range(500):
            if reporter.subscribed:
                break
            await asyncio.sleep(0.01)
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await source.aclose()  # type: ignore[attr-defined]

    assert reporter.subscribed == [(1, "onvif-pullpoint")]


async def test_a_failing_report_does_not_break_the_subscription(
    migrated_factory: sessionmaker[Session],
    supervisor: CaptureSupervisor,
    pool: ThreadPoolExecutor,
) -> None:
    from timelapse_manager.cameras.onvif import OnvifAdapter

    camera_id = _add_camera(migrated_factory, protocol="onvif", credentials=None)
    factory = _factory(supervisor, pool)
    reporter = MagicMock()
    reporter.listener_subscribed = AsyncMock(side_effect=RuntimeError("db down"))
    factory.set_reporter(reporter)
    adapter = OnvifAdapter(MagicMock(), "10.0.0.5")
    with patch(_BUILD, return_value=adapter):
        source = factory(_target(camera_id))
        consumer = asyncio.create_task(anext(source))  # type: ignore[arg-type]
        for _ in range(500):
            if adapter.on_subscribed is not None:
                break
            await asyncio.sleep(0.01)
        assert adapter.on_subscribed is not None
        await adapter.on_subscribed()  # swallowed: reporting never fails a subscribe
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await source.aclose()  # type: ignore[attr-defined]
    reporter.listener_subscribed.assert_awaited()
