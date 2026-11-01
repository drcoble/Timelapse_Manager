"""The bound default login is contained at the socket (#268, #69 #248).

Credential binding decides, from the shared resolution cache, that a camera's
hosts are inside ``ssrf.allowed_private_subnets``. That answer lives 60 s and a
kept-alive pooled connection is never re-resolved, so the transport must also
refuse to **connect** or **write** the default login to an address outside the
live subnets. These tests drive the real clients with a fake socket backend and
resolver:

* the contained client refuses an out-of-subnet answer even when the earlier
  check passed (the cache expired in between), before any socket opens;
* a pooled connection opened earlier (by a request without the default, or
  while the address was still allowed) is refused at write time once its
  address is outside the live policy, and no byte leaves;
* a camera with its own credentials keeps using the plain client untouched;
* every other path a default login leaves the process by (ffmpeg RTSP, the VAPIX
  event WebSocket, the adapter factory, Detect/Query) is contained.
"""

from __future__ import annotations

import socket
import typing
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpcore
import httpx
import pytest

from tests.unit.camera_pin_support import make_self_signed, pem_of
from timelapse_manager.cameras import rtsp, vapix
from timelapse_manager.cameras.base import CaptureError, UnreachableCaptureError
from timelapse_manager.cameras.clients import (
    CameraHttpClients,
    allow_uncontained_double,
    contained_client,
)
from timelapse_manager.cameras.http_jpeg import HttpJpegAdapter
from timelapse_manager.cameras.registry import build_adapter, uses_default_login
from timelapse_manager.cameras.tls import (
    PinnedCameraClient,
    PinnedSchemeError,
    is_pinned_context,
    pinned_context_of,
)
from timelapse_manager.config.settings import CaptureSettings
from timelapse_manager.security import pinned_http, ssrf
from timelapse_manager.security.credential_binding import default_still_bound
from timelapse_manager.security.pinned_http import address_inside_subnets

_NAME = "cam.lan"
_URL = f"http://{_NAME}/axis-cgi/jpg/image.cgi"
_LAN = "10.0.16.61"
_PUBLIC = "93.184.216.34"
_OK = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"


class _Stream(httpcore.AsyncNetworkStream):
    """A keep-alive HTTP/1.1 peer that answers every request with ``200 ok``."""

    def __init__(self, address: str) -> None:
        self.address = address
        self.written: list[bytes] = []
        self._pending = b""

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        data, self._pending = self._pending, b""
        return data

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.written.append(buffer)
        if buffer.endswith(b"\r\n\r\n"):
            self._pending = _OK

    async def aclose(self) -> None:
        return None

    async def start_tls(
        self,
        ssl_context: Any,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        return self

    def get_extra_info(self, info: str) -> typing.Any:
        return None


class _Backend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.streams: list[_Stream] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Any = None,
    ) -> httpcore.AsyncNetworkStream:
        stream = _Stream(host)
        self.streams.append(stream)
        return stream

    async def sleep(self, seconds: float) -> None:
        return None


class _Dns:
    def __init__(self) -> None:
        self.answers: dict[str, list[str]] = {}

    def __call__(self, host: str, *_a: object, **_k: object) -> list[Any]:
        if host not in self.answers:
            raise socket.gaierror(host)
        return [(2, 1, 6, "", (ip, 0)) for ip in self.answers[host]]


class _Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Env(typing.NamedTuple):
    backend: _Backend
    dns: _Dns
    subnets: list[str]
    clock: _Clock


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Env]:
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    backend = _Backend()
    monkeypatch.setattr(pinned_http, "inner_backend_factory", lambda: backend)
    dns = _Dns()
    monkeypatch.setattr(ssrf.socket, "getaddrinfo", dns)
    subnets = ["10.0.16.0/24"]
    context = MagicMock()
    context.settings.ssrf.allowed_private_subnets = subnets
    monkeypatch.setattr("timelapse_manager.runtime.get_context", lambda: context)
    clock = _Clock()
    monkeypatch.setattr(ssrf._resolution_cache, "clock", clock)
    ssrf.clear_resolution_cache()
    yield _Env(backend, dns, subnets, clock)
    ssrf.clear_resolution_cache()


@pytest.fixture
async def clients(env: _Env) -> typing.AsyncIterator[CameraHttpClients]:
    built = CameraHttpClients(CaptureSettings())
    yield built
    await built.aclose()


class TestConnectTimeContainment:
    async def test_an_in_subnet_answer_is_dialled_and_served(
        self, env: _Env, clients: CameraHttpClients
    ) -> None:
        env.dns.answers[_NAME] = [_LAN]
        response = await contained_client(clients.capture).get(_URL)
        assert response.status_code == 200
        assert [s.address for s in env.backend.streams] == [_LAN]

    async def test_a_rebind_after_the_use_time_check_is_refused_before_a_socket(
        self, env: _Env, clients: CameraHttpClients
    ) -> None:
        """The 60 s gap: the use-time check passed, the cached answer then expired."""
        env.dns.answers[_NAME] = [_LAN]
        config = SimpleNamespace(
            protocol="http",
            address=None,
            snapshot_uri=_URL,
            stream_uri=None,
            credentials=None,
            credentials_inherit_default=True,
            default_credentials=("fleet", "pw"),
        )
        assert await default_still_bound(config, env.subnets)  # the use-time check
        env.clock.now += 61.0  # the cache entry expires before the connect
        env.dns.answers[_NAME] = [_PUBLIC]
        adapter = build_adapter(
            config,
            clients.capture,
            default_credentials=config.default_credentials,
            credentials=config.default_credentials,
        )
        with pytest.raises(CaptureError):
            await adapter.capture()
        assert env.backend.streams == []  # no socket was opened, nothing was sent

    async def test_a_mixed_answer_is_refused(
        self, env: _Env, clients: CameraHttpClients
    ) -> None:
        env.dns.answers[_NAME] = [_LAN, _PUBLIC]
        with pytest.raises(httpx.ConnectError):
            await contained_client(clients.capture).get(_URL)
        assert env.backend.streams == []

    async def test_the_plain_client_is_not_affected(
        self, env: _Env, clients: CameraHttpClients
    ) -> None:
        """A camera with its own credentials keeps the camera policy as before."""
        env.dns.answers[_NAME] = [_PUBLIC]
        response = await clients.capture.get(_URL)
        assert response.status_code == 200
        assert [s.address for s in env.backend.streams] == [_PUBLIC]

    async def test_every_purpose_has_a_twin(
        self, env: _Env, clients: CameraHttpClients
    ) -> None:
        for purpose in ("capture", "events", "probe"):
            base = clients.for_purpose(purpose)  # type: ignore[arg-type]
            assert contained_client(base) is not base

    async def test_a_client_without_a_twin_fails_closed(self) -> None:
        client = httpx.AsyncClient()
        try:
            with pytest.raises(TypeError, match="no default-login containment"):
                contained_client(client)
            # ... and the adapter factory refuses it for the default login.
            camera = SimpleNamespace(
                protocol="http",
                snapshot_uri=_URL,
                credentials=None,
                credentials_inherit_default=True,
            )
            with pytest.raises(TypeError):
                build_adapter(camera, client, default_credentials=("a", "b"))
            # A test that needs a double says so.
            assert contained_client(allow_uncontained_double(client)) is client
        finally:
            await client.aclose()

    async def test_a_pinned_camera_keeps_its_pin_and_https_only_on_the_twin(
        self, env: _Env, clients: CameraHttpClients, tmp_path: Path
    ) -> None:
        pem = pem_of(make_self_signed(tmp_path))
        pinned = clients.for_purpose("capture", pem)
        twin = contained_client(pinned)
        assert twin is not pinned
        assert isinstance(twin, PinnedCameraClient)
        # The WebSocket and the TLS handshake read the trust from the client.
        assert pinned_context_of(twin) is not None
        assert is_pinned_context(pinned_context_of(twin))
        with pytest.raises(PinnedSchemeError):
            await twin.get(_URL)  # plain http is refused, default login or not
        assert env.backend.streams == []

    async def test_the_pinned_twin_is_contained_too(
        self, env: _Env, clients: CameraHttpClients, tmp_path: Path
    ) -> None:
        pem = pem_of(make_self_signed(tmp_path))
        env.dns.answers[_NAME] = [_PUBLIC]
        twin = contained_client(clients.for_purpose("capture", pem))
        with pytest.raises(httpx.ConnectError, match="allowed private subnets"):
            await twin.get(f"https://{_NAME}/x")
        assert env.backend.streams == []


class TestPooledConnections:
    async def test_a_pooled_connection_outside_the_narrowed_policy_gets_no_bytes(
        self, env: _Env, clients: CameraHttpClients
    ) -> None:
        env.dns.answers[_NAME] = [_LAN]
        contained = contained_client(clients.capture)
        assert (await contained.get(_URL)).status_code == 200  # opens + pools
        (stream,) = env.backend.streams
        sent = len(stream.written)

        env.subnets[:] = ["10.200.0.0/16"]  # the administrator narrows the policy
        with pytest.raises(httpx.ConnectError, match="allowed private subnets"):
            await contained.get(_URL)
        assert len(stream.written) == sent  # nothing more left the process

    async def test_a_connection_a_plain_request_opened_is_checked_for_the_default(
        self, env: _Env, clients: CameraHttpClients
    ) -> None:
        env.dns.answers[_NAME] = [_PUBLIC]
        assert (await clients.capture.get(_URL)).status_code == 200  # plain, public
        (stream,) = env.backend.streams
        sent = len(stream.written)
        with pytest.raises(httpx.ConnectError):
            await contained_client(clients.capture).get(_URL)  # reuses that socket
        assert len(stream.written) == sent
        # ... and the plain client keeps using it.
        assert (await clients.capture.get(_URL)).status_code == 200


class TestMixedAnswerPooledConnection:
    async def test_a_plain_connection_to_a_mixed_answer_never_carries_the_default(
        self, env: _Env, clients: CameraHttpClients
    ) -> None:
        """One address inside, one outside: the dialled (inside) one is not enough."""
        env.dns.answers[_NAME] = [_LAN, _PUBLIC]
        assert (await clients.capture.get(_URL)).status_code == 200  # plain request
        (stream,) = env.backend.streams
        assert stream.address == _LAN  # the first answer was in the subnet
        sent = len(stream.written)
        with pytest.raises(httpx.ConnectError, match="allowed private subnets"):
            await contained_client(clients.capture).get(_URL)
        assert len(stream.written) == sent


class TestSubnetHelper:
    @pytest.mark.parametrize(
        ("address", "subnets", "expected"),
        [
            ("10.0.16.5", ["10.0.16.0/24"], True),
            ("10.0.17.5", ["10.0.16.0/24"], False),
            ("::ffff:10.0.16.5", ["10.0.16.0/24"], True),
            ("cam.lan", ["10.0.16.0/24"], False),
            ("10.0.16.5", [], False),
            ("10.0.16.5", ["not-a-cidr"], False),
        ],
    )
    def test_inside(self, address: str, subnets: list[str], expected: bool) -> None:
        assert address_inside_subnets(address, subnets) is expected


class TestAdapterFactorySelectsTheContainedClient:
    def _camera(self, **extra: Any) -> SimpleNamespace:
        values: dict[str, Any] = {
            "protocol": "http",
            "snapshot_uri": _URL,
            "credentials": None,
            "credentials_inherit_default": True,
        }
        values.update(extra)
        return SimpleNamespace(**values)

    def test_the_default_login_gets_the_twin(self, clients: CameraHttpClients) -> None:
        default = ("fleet", "pw")
        adapter = build_adapter(
            self._camera(), clients.capture, default_credentials=default
        )
        assert isinstance(adapter, HttpJpegAdapter)
        assert adapter._client is contained_client(clients.capture)

    def test_own_credentials_keep_the_plain_client(
        self, clients: CameraHttpClients
    ) -> None:
        camera = self._camera(credentials={"username": "own", "password": "x"})
        adapter = build_adapter(camera, clients.capture, default_credentials=("a", "b"))
        assert isinstance(adapter, HttpJpegAdapter)
        assert adapter._client is clients.capture

    def test_an_explicit_credential_pair_equal_to_the_default_is_still_own(
        self,
    ) -> None:
        camera = self._camera(credentials={"username": "fleet", "password": "pw"})
        assert not uses_default_login(camera, ("fleet", "pw"), ("fleet", "pw"))

    def test_no_default_in_play_is_not_contained(self) -> None:
        assert not uses_default_login(self._camera(), None, ("a", "b"))
        assert not uses_default_login(self._camera(), ("a", "b"), None)


class TestRtspDefaultLogin:
    @pytest.fixture(autouse=True)
    def _policy(self, env: _Env) -> None:
        return None

    async def test_a_name_inside_the_subnets_is_dialled_by_its_literal(
        self, env: _Env
    ) -> None:
        env.dns.answers[_NAME] = [_LAN]
        url = await rtsp._contain_default_url("rtsp://u:p%40w@cam.lan:8554/s?x=1")
        assert url == f"rtsp://u:p%40w@{_LAN}:8554/s?x=1"

    async def test_a_name_resolving_outside_is_refused(self, env: _Env) -> None:
        env.dns.answers[_NAME] = [_PUBLIC]
        with pytest.raises(UnreachableCaptureError):
            await rtsp._contain_default_url("rtsp://u:p@cam.lan/s")

    async def test_a_literal_inside_is_rebuilt_and_one_outside_is_refused(
        self, env: _Env
    ) -> None:
        inside = f"rtsp://u:p@{_LAN}/s"
        assert await rtsp._contain_default_url(inside) == inside
        # Rebuilt from the parsed parts: the host is the validated address.
        assert (
            await rtsp._contain_default_url(f"rtsp://u:p@{_LAN.upper()}:554/s?a=1")
            == f"rtsp://u:p@{_LAN}:554/s?a=1"
        )
        with pytest.raises(UnreachableCaptureError):
            await rtsp._contain_default_url(f"rtsp://u:p@{_PUBLIC}/s")

    async def test_a_name_behind_rtsps_cannot_be_pinned(self, env: _Env) -> None:
        env.dns.answers[_NAME] = [_LAN]
        with pytest.raises(UnreachableCaptureError, match="cannot be pinned"):
            await rtsp._contain_default_url("rtsps://u:p@cam.lan/s")

    async def test_an_unresolvable_name_is_refused(self, env: _Env) -> None:
        with pytest.raises(UnreachableCaptureError):
            await rtsp._contain_default_url("rtsp://u:p@cam.lan/s")

    @pytest.mark.parametrize(
        "url",
        [
            f"rtsp://u:p@{_LAN}\t.evil.example/s",
            "rtsp://u:p@192.0.2.\n61/s",
            "rtsp://u:p@cam\r.lan/s",
            f"rtsp://u:p@{_LAN}\\@evil.example/s",
            f"rtsp://u:p@{_LAN} /s",
        ],
    )
    async def test_control_characters_in_the_url_are_refused(
        self, env: _Env, url: str
    ) -> None:
        """``urlsplit`` drops tab/CR/LF silently; ffmpeg may not (a differential)."""
        env.dns.answers[_NAME] = [_LAN]
        with pytest.raises(UnreachableCaptureError, match="control character"):
            await rtsp._contain_default_url(url)

    async def test_a_malformed_port_is_a_capture_error_not_a_crash(
        self, env: _Env
    ) -> None:
        env.dns.answers[_NAME] = [_LAN]
        with pytest.raises(UnreachableCaptureError):
            await rtsp._contain_default_url("rtsp://u:p@cam.lan:99999x/s")


class TestVapixWebSocketDefaultLogin:
    async def test_an_out_of_subnet_host_is_refused_before_any_socket(
        self, env: _Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opened: list[str] = []

        async def fake_open(host: str, *args: Any, **kwargs: Any) -> Any:
            opened.append(host)
            raise AssertionError("must not open a socket")

        monkeypatch.setattr(vapix, "_ws_open_stream", fake_open)
        limits = vapix._resolve_ws_limits()
        with pytest.raises(UnreachableCaptureError, match="allowed private subnets"):
            await vapix._ws_connect(
                httpx.AsyncClient(),
                f"http://{_NAME}/vapix/ws-data-stream",
                _PUBLIC,
                ("fleet", "pw"),
                5.0,
                limits,
                contain_default=True,
            )
        assert opened == []

    async def test_a_mixed_answer_is_refused_though_the_dialled_ip_is_inside(
        self, env: _Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env.dns.answers[_NAME] = [_LAN, _PUBLIC]

        async def fake_open(host: str, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("must not open a socket")

        monkeypatch.setattr(vapix, "_ws_open_stream", fake_open)
        with pytest.raises(UnreachableCaptureError, match="allowed private subnets"):
            await vapix._ws_connect(
                httpx.AsyncClient(),
                f"http://{_NAME}/vapix/ws-data-stream",
                _LAN,
                ("fleet", "pw"),
                5.0,
                vapix._resolve_ws_limits(),
                contain_default=True,
            )

    async def test_an_in_subnet_host_proceeds_to_the_socket(
        self, env: _Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env.dns.answers[_NAME] = [_LAN]
        opened: list[str] = []

        async def fake_open(host: str, *args: Any, **kwargs: Any) -> Any:
            opened.append(host)
            raise UnreachableCaptureError("stop here")

        monkeypatch.setattr(vapix, "_ws_open_stream", fake_open)
        with pytest.raises(UnreachableCaptureError, match="stop here"):
            await vapix._ws_connect(
                httpx.AsyncClient(),
                f"http://{_NAME}/vapix/ws-data-stream",
                _LAN,
                ("fleet", "pw"),
                5.0,
                vapix._resolve_ws_limits(),
                contain_default=True,
            )
        assert opened == [_LAN]
