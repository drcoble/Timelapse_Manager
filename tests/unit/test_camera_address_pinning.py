"""Camera clients dial only the address the SSRF check validated (#69 #222, #234).

Credential binding and the ONVIF host check decide on one DNS answer; an
unpinned client then resolves the name again when it connects. These tests pin
the contract of :func:`timelapse_manager.cameras.clients._build_client`, which
builds every camera client (pinned-certificate ones included):

* the connection goes to a validated IP, never to a re-resolved name, and
  the validated answer is the one the check cached;
* a camera whose DNS answer changes legitimately is followed once the cached
  answer expires (nothing is pinned forever);
* a denied answer (loopback, link-local, a mixed answer, an AAAA record) is
  refused before any socket opens, under the policy read at each connect;
* the TLS server name and the certificate host-name check stay on the camera's
  *name*, and the client keeps the CA bundle or the certificate pin it was built
  with (#234).
"""

from __future__ import annotations

import socket
import ssl
import typing
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpcore
import httpx
import pytest

from tests.unit.camera_net_support import LocalHttpServer, make_test_pki
from tests.unit.camera_pin_support import make_self_signed, pem_of
from timelapse_manager.cameras.clients import CameraHttpClients
from timelapse_manager.cameras.host_resolution import check_camera_url_async
from timelapse_manager.cameras.tls import is_pinned_context
from timelapse_manager.config.settings import CaptureSettings
from timelapse_manager.security import pinned_http, ssrf
from timelapse_manager.security.ssrf import SsrfError

_NAME = "axis-b8a44f17384a"
_URL = f"https://{_NAME}/axis-cgi/jpg/image.cgi"


class _Stream(httpcore.AsyncNetworkStream):
    """A stream that records the TLS handshake it is asked for, then fails it."""

    def __init__(self, backend: _Backend) -> None:
        self._backend = backend

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        raise httpcore.ReadError("not connected")

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        raise httpcore.WriteError("not connected")

    async def aclose(self) -> None:
        return None

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self._backend.tls.append((ssl_context, server_hostname))
        raise httpcore.ConnectError("handshake stopped by the test")

    def get_extra_info(self, info: str) -> typing.Any:
        return None


class _Backend(httpcore.AsyncNetworkBackend):
    """Records each dial; ``failing`` addresses refuse the connection."""

    def __init__(self) -> None:
        self.dials: list[tuple[str, int]] = []
        self.tls: list[tuple[ssl.SSLContext, str | None]] = []
        self.failing: set[str] = set()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Any = None,
    ) -> httpcore.AsyncNetworkStream:
        self.dials.append((host, port))
        if host in self.failing:
            raise httpcore.ConnectError("refused")
        return _Stream(self)

    async def sleep(self, seconds: float) -> None:
        return None


class _Dns:
    """A fake resolver: ``answers`` maps a name to the addresses it returns."""

    def __init__(self) -> None:
        self.answers: dict[str, list[str]] = {}
        self.lookups: list[str] = []

    def __call__(self, host: str, *_args: object, **_kwargs: object) -> list[Any]:
        self.lookups.append(host)
        if host not in self.answers:
            raise socket.gaierror(f"no such host: {host}")
        out: list[Any] = []
        for ip in self.answers[host]:
            family = 10 if ":" in ip else 2
            out.append((family, 1, 6, "", (ip, 0)))
        return out


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Env(typing.NamedTuple):
    backend: _Backend
    dns: _Dns
    subnets: list[str]
    clock: _Clock


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Env]:
    """A recording socket backend, a fake resolver and an editable policy."""
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


async def _get(client: httpx.AsyncClient, url: str = _URL) -> None:
    with pytest.raises(httpx.ConnectError):
        await client.get(url)


class TestDialsTheValidatedAddress:
    async def test_connects_to_the_ip_and_keeps_the_name_for_tls(
        self, env: _Env
    ) -> None:
        env.dns.answers[_NAME] = ["10.0.16.61"]
        clients = CameraHttpClients(CaptureSettings())
        try:
            for purpose in ("capture", "events", "probe"):
                await _get(clients.for_purpose(purpose))
        finally:
            await clients.aclose()
        assert set(env.backend.dials) == {("10.0.16.61", 443)}
        # SNI (and with it certificate host-name verification) is the camera's
        # name, never the dialled IP.
        assert {sni for _ctx, sni in env.backend.tls} == {_NAME}

    async def test_a_rebind_after_the_check_is_not_followed(self, env: _Env) -> None:
        """The credential-binding race: checked as .61, then DNS says loopback."""
        env.dns.answers[_NAME] = ["10.0.16.61"]
        await check_camera_url_async(_URL)
        env.dns.answers[_NAME] = ["127.0.0.1"]
        clients = CameraHttpClients(CaptureSettings())
        try:
            await _get(clients.capture)
        finally:
            await clients.aclose()
        assert env.backend.dials == [("10.0.16.61", 443)]
        assert env.dns.lookups == [_NAME]  # resolved once, by the check

    async def test_a_legitimate_dns_change_is_followed_after_the_ttl(
        self, env: _Env
    ) -> None:
        env.dns.answers[_NAME] = ["10.0.16.61"]
        clients = CameraHttpClients(CaptureSettings())
        try:
            await _get(clients.capture)
            env.dns.answers[_NAME] = ["10.0.16.62"]
            await _get(clients.capture)  # still inside the cached answer
            env.clock.now += 61
            await _get(clients.capture)  # a new connection re-resolves
            await _get(clients.capture)
        finally:
            await clients.aclose()
        assert [ip for ip, _port in env.backend.dials] == [
            "10.0.16.61",
            "10.0.16.61",
            "10.0.16.62",
            "10.0.16.62",
        ]
        assert env.dns.lookups == [_NAME, _NAME]

    async def test_an_ip_literal_address_needs_no_lookup(self, env: _Env) -> None:
        clients = CameraHttpClients(CaptureSettings())
        try:
            await _get(clients.capture, "https://10.0.16.61/x")
        finally:
            await clients.aclose()
        assert env.backend.dials == [("10.0.16.61", 443)]
        assert env.backend.tls[0][1] == "10.0.16.61"
        assert env.dns.lookups == []

    async def test_a_dual_stack_camera_falls_back_to_the_other_family(
        self, env: _Env
    ) -> None:
        env.subnets.append("fd00::/8")
        env.dns.answers[_NAME] = ["fd00::61", "10.0.16.61"]
        env.backend.failing.add("fd00::61")
        clients = CameraHttpClients(CaptureSettings())
        try:
            await _get(clients.capture)
        finally:
            await clients.aclose()
        assert env.backend.dials == [("fd00::61", 443), ("10.0.16.61", 443)]
        assert env.backend.tls[0][1] == _NAME


class TestDeniedAnswersNeverConnect:
    @pytest.mark.parametrize(
        "answer",
        [
            ["127.0.0.1"],
            ["::1"],
            ["169.254.169.254"],
            ["fe80::1"],
            ["10.0.16.61", "127.0.0.1"],  # one bad record rejects the name
            ["10.9.9.9"],  # private, but not in the administrator's opt-in
            ["fd00::61"],  # AAAA outside the opt-in (only 10.0.16.0/24)
        ],
    )
    async def test_refused_before_a_socket_opens(
        self, env: _Env, answer: list[str]
    ) -> None:
        env.dns.answers[_NAME] = answer
        clients = CameraHttpClients(CaptureSettings())
        try:
            with pytest.raises(httpx.ConnectError, match="SSRF guard"):
                await clients.capture.get(_URL)
        finally:
            await clients.aclose()
        assert env.backend.dials == []

    async def test_an_unresolvable_name_is_a_connect_error(self, env: _Env) -> None:
        clients = CameraHttpClients(CaptureSettings())
        try:
            with pytest.raises(httpx.ConnectError, match="did not resolve"):
                await clients.capture.get(_URL)
        finally:
            await clients.aclose()
        assert env.backend.dials == []

    async def test_the_opt_in_is_read_at_each_connect(self, env: _Env) -> None:
        env.dns.answers[_NAME] = ["10.0.16.61"]
        env.subnets.clear()
        clients = CameraHttpClients(CaptureSettings())
        try:
            with pytest.raises(httpx.ConnectError, match="SSRF guard"):
                await clients.capture.get(_URL)
            env.subnets.append("10.0.16.0/24")  # the administrator opts in
            await _get(clients.capture)
        finally:
            await clients.aclose()
        assert env.backend.dials == [("10.0.16.61", 443)]

    async def test_without_an_application_context_private_space_is_denied(
        self, env: _Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_context() -> typing.NoReturn:
            raise RuntimeError("no context")

        monkeypatch.setattr("timelapse_manager.runtime.get_context", no_context)
        env.dns.answers[_NAME] = ["10.0.16.61"]
        clients = CameraHttpClients(CaptureSettings())
        try:
            with pytest.raises(httpx.ConnectError, match="SSRF guard"):
                await clients.capture.get(_URL)
        finally:
            await clients.aclose()
        assert env.backend.dials == []

    async def test_the_check_and_the_connection_agree_on_a_denied_name(
        self, env: _Env
    ) -> None:
        env.dns.answers[_NAME] = ["127.0.0.1"]
        with pytest.raises(SsrfError):
            await check_camera_url_async(_URL)


class TestTlsContextComposes:
    """#234: address pinning must not replace the CA bundle or the cert pin."""

    async def test_an_unpinned_client_verifies_with_the_shared_context(
        self, env: _Env, tmp_path: Path
    ) -> None:
        pki = make_test_pki(tmp_path)
        env.dns.answers[_NAME] = ["10.0.16.61"]
        clients = CameraHttpClients(CaptureSettings(tls_ca_bundle=pki.ca_cert))
        try:
            await _get(clients.capture)
        finally:
            await clients.aclose()
        (context, _sni) = env.backend.tls[0]
        assert context.check_hostname is True
        assert context.verify_mode is ssl.CERT_REQUIRED
        # Exactly the configured bundle: the generated CA, nothing from certifi.
        subjects = [str(c["subject"]) for c in context.get_ca_certs()]
        assert subjects == [str(c["subject"]) for c in _load_ca(pki.ca_cert)]

    async def test_a_pinned_client_keeps_its_pin_as_the_only_trust_anchor(
        self, env: _Env, tmp_path: Path
    ) -> None:
        pinned = make_self_signed(tmp_path / "cam", "cam")
        env.dns.answers[_NAME] = ["10.0.16.61"]
        clients = CameraHttpClients(CaptureSettings())
        try:
            client = clients.for_purpose("capture", pem_of(pinned))
            await _get(client)
        finally:
            await clients.aclose()
        (context, sni) = env.backend.tls[0]
        assert is_pinned_context(context)
        assert context.verify_mode is ssl.CERT_REQUIRED
        # Not the shared bundle context: it holds no CA from the system store.
        assert context.get_ca_certs() == []
        assert sni == _NAME
        assert env.backend.dials == [("10.0.16.61", 443)]

    async def test_the_pool_uses_the_context_and_limits_it_was_given(
        self, env: _Env
    ) -> None:
        clients = CameraHttpClients(
            CaptureSettings(http_max_connections=7, http_keepalive_connections=5)
        )
        try:
            transport = clients.capture._transport
            pool = typing.cast(httpcore.AsyncConnectionPool, transport._pool)  # type: ignore[attr-defined]
            assert pool._ssl_context.check_hostname is True
            assert pool._max_connections == 7
        finally:
            await clients.aclose()


def _load_ca(path: Path) -> list[dict[str, typing.Any]]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=str(path))
    return context.get_ca_certs()


class TestRealHandshakeVerifiesTheName:
    """A real TLS server, dialled by IP: the name is what is verified."""

    @pytest.fixture(autouse=True)
    def _dial_loopback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Loopback is always denied by the guard; dial it for these tests only.
        async def loopback(host: str, **_policy: object) -> tuple[str, ...]:
            return ("127.0.0.1",)

        monkeypatch.setattr(pinned_http, "pinned_addresses_async", loopback)
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("SSL_CERT_DIR", raising=False)

    async def test_the_certificate_must_match_the_name_not_the_ip(
        self, tmp_path: Path
    ) -> None:
        pki = make_test_pki(tmp_path)  # SAN: localhost and 127.0.0.1
        clients = CameraHttpClients(CaptureSettings(tls_ca_bundle=pki.ca_cert))
        try:
            async with LocalHttpServer(pki) as server:
                # The right name verifies (and the configured bundle is used).
                ok = await clients.capture.get(server.url(host="localhost"))
                assert ok.status_code == 200
                # Another name dialled to the very same IP must fail, although
                # the certificate does cover that IP: verification is by name.
                with pytest.raises(httpx.ConnectError, match="CERTIFICATE|hostname"):
                    await clients.capture.get(server.url(host="wrong.example"))
        finally:
            await clients.aclose()
