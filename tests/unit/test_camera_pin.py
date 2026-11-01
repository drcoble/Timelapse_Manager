"""Per-camera certificate pinning: TLS behaviour (#69 CAM-S1, the pinning half).

Run against real TLS servers with generated certificates:

* a pinned camera trusts its pinned certificate and nothing else -- not a
  sibling certificate from the same CA, not the CA, not ``SSL_CERT_FILE``, not
  ``capture.tls_ca_bundle`` -- and the host name is not checked;
* a camera without a pin is untouched: the bundle and ``SSL_CERT_FILE`` still
  decide, exactly as before;
* a pinned camera is never reached over plain HTTP, on the HTTP clients or the
  event WebSocket;
* errors about a pinned connection are a fixed sentence with no certificate or
  handshake detail;
* fingerprints are compared by value, not by spelling.
"""

from __future__ import annotations

import asyncio
import hashlib
import ssl
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from tests.unit.camera_net_support import LocalHttpServer, allow_loopback_dial
from tests.unit.camera_pin_support import (
    make_ca_with_leaves,
    make_self_signed,
    pem_of,
)
from timelapse_manager.cameras import vapix
from timelapse_manager.cameras.base import OtherCaptureError, UnreachableCaptureError
from timelapse_manager.cameras.clients import CameraHttpClients
from timelapse_manager.cameras.tls import (
    PIN_MISMATCH_MESSAGE,
    PinMismatchError,
    PinnedCameraClient,
    PinnedSchemeError,
    camera_ssl_context,
    fetch_peer_certificate,
    fingerprints_match,
    format_fingerprint,
    is_pinned_context,
    normalize_fingerprint,
    pem_fingerprint,
    pinned_context_of,
    pinned_ssl_context,
    summarize_certificate,
)
from timelapse_manager.config.settings import CaptureSettings

_PURPOSES = ("capture", "events", "probe")


@pytest.fixture(autouse=True)
def no_trust_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from no environment trust override.

    Also lets the camera clients reach the local servers on 127.0.0.1, which
    the address-pinning step would otherwise always refuse.
    """
    allow_loopback_dial(monkeypatch)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)


class TestFingerprints:
    _HEX = "ab" * 32

    @pytest.mark.parametrize(
        "spelling",
        [
            "ab" * 32,
            "AB" * 32,
            ":".join(["AB"] * 32),
            ":".join(["ab"] * 32),
            "-".join(["Ab"] * 32),
            " ".join(["ab"] * 32),
            "  " + "ab" * 32 + "\n",
        ],
    )
    def test_every_spelling_normalises_to_lower_case_hex(self, spelling: str) -> None:
        assert normalize_fingerprint(spelling) == self._HEX

    @pytest.mark.parametrize(
        "bad",
        ["", "ab" * 31, "ab" * 33, "zz" * 32, "ab" * 32 + "g", "sha256:" + "ab" * 32],
    )
    def test_malformed_fingerprints_are_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError, match="fingerprint"):
            normalize_fingerprint(bad)

    def test_format_is_upper_case_colon_pairs(self) -> None:
        assert format_fingerprint(self._HEX) == ":".join(["AB"] * 32)

    def test_match_ignores_spelling_but_not_value(self) -> None:
        other = "cd" * 32
        assert fingerprints_match(":".join(["AB"] * 32), self._HEX)
        assert not fingerprints_match(other, self._HEX)

    @pytest.mark.parametrize("bad", ["", "not hex", "ab" * 31])
    def test_a_malformed_side_never_matches(self, bad: str) -> None:
        assert not fingerprints_match(bad, self._HEX)
        assert not fingerprints_match(self._HEX, bad)
        assert not fingerprints_match(bad, bad)


class TestPemParsing:
    def test_fingerprint_is_sha256_of_the_der(self, tmp_path: Path) -> None:
        pem = pem_of(make_self_signed(tmp_path))
        der = ssl.PEM_cert_to_DER_cert(pem)
        assert pem_fingerprint(pem) == hashlib.sha256(der).hexdigest()

    def test_a_bundle_of_two_certificates_is_rejected(self, tmp_path: Path) -> None:
        one = pem_of(make_self_signed(tmp_path / "a", "a"))
        two = pem_of(make_self_signed(tmp_path / "b", "b"))
        with pytest.raises(ValueError, match="exactly one"):
            pem_fingerprint(one + two)
        with pytest.raises(ValueError, match="exactly one"):
            pinned_ssl_context(one + two)

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "garbage",
            "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n",
        ],
    )
    def test_garbage_is_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError):
            pem_fingerprint(bad)

    def test_an_oversized_pem_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            pem_fingerprint("-----BEGIN CERTIFICATE-----\n" + "A" * 20_000)

    def test_summary_describes_the_certificate(self, tmp_path: Path) -> None:
        pem = pem_of(make_self_signed(tmp_path, "pano"))
        summary = summarize_certificate(ssl.PEM_cert_to_DER_cert(pem))
        assert summary.fingerprint == pem_fingerprint(pem)
        assert "AXIS pano" in summary.subject
        assert summary.self_signed
        assert summary.not_before < summary.not_after
        assert summary.pem.strip() == pem.strip()
        assert summary.display_fingerprint == format_fingerprint(summary.fingerprint)

    def test_summary_rejects_a_non_certificate(self) -> None:
        with pytest.raises(ValueError):
            summarize_certificate(b"not der")


class TestPinContext:
    def test_requires_a_certificate_and_not_the_host_name(self, tmp_path: Path) -> None:
        context = pinned_ssl_context(pem_of(make_self_signed(tmp_path)))
        assert context.verify_mode is ssl.CERT_REQUIRED
        assert context.check_hostname is False
        assert is_pinned_context(context)

    def test_the_shared_context_is_not_a_pin_context(self) -> None:
        assert not is_pinned_context(camera_ssl_context(None))
        assert not is_pinned_context(None)

    def test_only_the_pinned_certificate_is_an_anchor(self, tmp_path: Path) -> None:
        pem = pem_of(make_self_signed(tmp_path))
        # ``get_ca_certs`` lists only CA-flagged certificates; the store count
        # includes a leaf anchor.
        assert pinned_ssl_context(pem).cert_store_stats()["x509"] == 1


async def _get(client: httpx.AsyncClient, server: LocalHttpServer) -> int:
    # 127.0.0.1 is not named by a factory certificate: only a pin may accept it.
    return (await client.get(server.url())).status_code


class TestPinnedClient:
    async def test_the_pinned_self_signed_certificate_is_accepted(
        self, tmp_path: Path
    ) -> None:
        pki = make_self_signed(tmp_path)
        async with (
            LocalHttpServer(pki) as server,
            PinnedCameraClient(pem_of(pki)) as client,
        ):
            assert await _get(client, server) == 200

    async def test_a_different_self_signed_certificate_is_a_mismatch(
        self, tmp_path: Path
    ) -> None:
        pinned = make_self_signed(tmp_path / "pinned", "pinned")
        presented = make_self_signed(tmp_path / "presented", "presented")
        async with (
            LocalHttpServer(presented) as server,
            PinnedCameraClient(pem_of(pinned)) as client,
        ):
            with pytest.raises(PinMismatchError) as caught:
                await _get(client, server)
        message = str(caught.value)
        assert message == PIN_MISMATCH_MESSAGE
        # No handshake or certificate detail in the message or its chain.
        assert caught.value.__cause__ is None
        for leaked in ("CERTIFICATE_VERIFY", "self-signed", "AXIS", "SSL"):
            assert leaked not in message

    async def test_an_expired_pinned_certificate_is_not_accepted(
        self, tmp_path: Path
    ) -> None:
        pki = make_self_signed(tmp_path, expired=True)
        async with (
            LocalHttpServer(pki) as server,
            PinnedCameraClient(pem_of(pki)) as client,
        ):
            with pytest.raises(PinMismatchError):
                await _get(client, server)

    async def test_pinning_a_leaf_does_not_trust_its_sibling_or_the_ca(
        self, tmp_path: Path
    ) -> None:
        first, second = make_ca_with_leaves(tmp_path)
        async with (
            LocalHttpServer(first) as server,
            PinnedCameraClient(pem_of(first)) as client,
        ):
            assert await _get(client, server) == 200
        # A sibling leaf from the same CA is a different certificate.
        async with (
            LocalHttpServer(second) as server,
            PinnedCameraClient(pem_of(first)) as client,
        ):
            with pytest.raises(PinMismatchError):
                await _get(client, server)

    async def test_the_pin_beats_ssl_cert_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The environment trusts the CA that signed the served leaf, but the
        # camera is pinned to a different certificate: the pin decides.
        served, _ = make_ca_with_leaves(tmp_path / "ca")
        other = make_self_signed(tmp_path / "other")
        monkeypatch.setenv("SSL_CERT_FILE", str(served.ca_cert))
        async with (
            LocalHttpServer(served) as server,
            PinnedCameraClient(pem_of(other)) as client,
        ):
            with pytest.raises(PinMismatchError):
                await _get(client, server)

    async def test_the_pin_beats_the_configured_bundle(self, tmp_path: Path) -> None:
        served, _ = make_ca_with_leaves(tmp_path / "ca")
        other = make_self_signed(tmp_path / "other")
        clients = CameraHttpClients(CaptureSettings(tls_ca_bundle=served.ca_cert))
        try:
            async with LocalHttpServer(served) as server:
                for purpose in _PURPOSES:
                    pinned = clients.for_purpose(purpose, pem_of(other))
                    with pytest.raises(PinMismatchError):
                        await _get(pinned, server)
        finally:
            await clients.aclose()

    async def test_plain_http_is_refused_before_any_connection(
        self, tmp_path: Path
    ) -> None:
        pki = make_self_signed(tmp_path)
        async with (
            LocalHttpServer() as plain,
            PinnedCameraClient(pem_of(pki)) as client,
        ):
            with pytest.raises(PinnedSchemeError) as caught:
                await client.get(plain.url())
        assert isinstance(caught.value, httpx.TransportError)
        assert "https" in str(caught.value)

    async def test_a_redirect_is_not_followed_to_http(self, tmp_path: Path) -> None:
        # Redirects are off for camera clients; the pinned one keeps that.
        pki = make_self_signed(tmp_path)
        async with PinnedCameraClient(pem_of(pki)) as client:
            assert client.follow_redirects is False


class TestClientsFactory:
    async def test_unpinned_cameras_keep_the_shared_clients(
        self, tmp_path: Path
    ) -> None:
        clients = CameraHttpClients(CaptureSettings())
        try:
            for purpose in _PURPOSES:
                shared = clients.for_purpose(purpose)
                assert clients.for_purpose(purpose, None) is shared
                assert clients.for_purpose(purpose, "") is shared
                assert not isinstance(shared, PinnedCameraClient)
        finally:
            await clients.aclose()

    async def test_unpinned_camera_still_verifies_through_ssl_cert_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The CA-bundle behaviour for cameras without a pin is unchanged.
        from tests.unit.camera_net_support import make_test_pki

        pki = make_test_pki(tmp_path)
        monkeypatch.setenv("SSL_CERT_FILE", str(pki.ca_cert))
        clients = CameraHttpClients(CaptureSettings())
        try:
            async with LocalHttpServer(pki) as server:
                response = await clients.for_purpose("capture", None).get(
                    server.url(host="localhost")
                )
                assert response.status_code == 200
        finally:
            await clients.aclose()

    async def test_every_purpose_accepts_its_pin_through_the_factory(
        self, tmp_path: Path
    ) -> None:
        # The positive path: a mismatch test would also pass if the pin context
        # were dropped, so show the pinned self-signed certificate is accepted
        # (at 127.0.0.1, which it does not name) through each purpose's client.
        pki = make_self_signed(tmp_path)
        clients = CameraHttpClients(CaptureSettings())
        try:
            async with LocalHttpServer(pki) as server:
                for purpose in _PURPOSES:
                    pinned = clients.for_purpose(purpose, pem_of(pki))
                    assert await _get(pinned, server) == 200, purpose
                    # The shared client for the same purpose still refuses it.
                    with pytest.raises(httpx.ConnectError):
                        await _get(clients.for_purpose(purpose), server)
        finally:
            await clients.aclose()

    async def test_a_pin_gets_one_client_per_purpose_and_pin(
        self, tmp_path: Path
    ) -> None:
        pem_a = pem_of(make_self_signed(tmp_path / "a", "a"))
        pem_b = pem_of(make_self_signed(tmp_path / "b", "b"))
        clients = CameraHttpClients(CaptureSettings())
        try:
            seen: dict[tuple[str, str], Any] = {}
            for purpose in _PURPOSES:
                for pem in (pem_a, pem_b):
                    client = clients.for_purpose(purpose, pem)
                    assert isinstance(client, PinnedCameraClient)
                    assert clients.for_purpose(purpose, pem) is client
                    seen[(purpose, pem)] = client
            assert len({id(c) for c in seen.values()}) == 6
            # A re-spelled but identical PEM is the same pin.
            assert (
                clients.for_purpose("capture", pem_a + "\n\n")
                is seen[("capture", pem_a)]
            )
        finally:
            await clients.aclose()
        assert all(c.is_closed for c in seen.values())

    async def test_pinned_clients_keep_the_purposes_limits(
        self, tmp_path: Path
    ) -> None:
        capture = CaptureSettings(
            http_max_connections=3,
            http_keepalive_connections=2,
            probe_http_max_connections=2,
        )
        clients = CameraHttpClients(capture)
        pem = pem_of(make_self_signed(tmp_path))
        try:
            pool = clients.for_purpose("probe", pem)._transport._pool  # type: ignore[attr-defined]
            assert pool._max_connections == 2
            pool = clients.for_purpose("capture", pem)._transport._pool  # type: ignore[attr-defined]
            assert pool._max_connections == 3
        finally:
            await clients.aclose()

    async def test_an_unusable_stored_pin_refuses_the_camera(self) -> None:
        clients = CameraHttpClients(CaptureSettings())
        try:
            with pytest.raises(OtherCaptureError, match="unusable") as caught:
                clients.for_purpose("capture", "not a certificate")
            # Also a ValueError, so the web and API sites that turn a bad
            # camera configuration into a message catch it instead of a 500.
            assert isinstance(caught.value, ValueError)
            assert caught.value.local is True
            with pytest.raises(ValueError, match="purpose"):
                clients.for_purpose("nope", "x")  # type: ignore[arg-type]
        finally:
            await clients.aclose()


class TestWebSocketPath:
    async def test_ws_connect_uses_the_pinned_context(self, tmp_path: Path) -> None:
        pem = pem_of(make_self_signed(tmp_path))
        seen: dict[str, Any] = {}

        class _Stop(Exception):
            pass

        async def fake_open(
            connect_host: str, port: int, ssl_ctx: object | None, **kwargs: Any
        ) -> Any:
            seen["ctx"] = ssl_ctx
            raise _Stop

        clients = CameraHttpClients(CaptureSettings())
        original = vapix._ws_open_stream
        vapix._ws_open_stream = fake_open  # type: ignore[assignment]
        try:
            client = clients.for_purpose("events", pem)
            with pytest.raises(_Stop):
                await vapix._ws_connect(
                    client,
                    "https://cam.example:443/vapix/ws-data-stream",
                    "127.0.0.1",
                    None,
                    1.0,
                    MagicMock(),
                )
        finally:
            vapix._ws_open_stream = original  # type: ignore[assignment]
            await clients.aclose()
        assert seen["ctx"] is pinned_context_of(client)
        assert seen["ctx"] is pinned_ssl_context(pem)

    async def test_ws_over_plain_http_is_refused_for_a_pinned_camera(
        self, tmp_path: Path
    ) -> None:
        clients = CameraHttpClients(CaptureSettings())
        try:
            client = clients.for_purpose("events", pem_of(make_self_signed(tmp_path)))
            with pytest.raises(UnreachableCaptureError, match="must use https"):
                await vapix._ws_connect(
                    client,
                    "http://cam.example/vapix/ws-data-stream",
                    "127.0.0.1",
                    None,
                    1.0,
                    MagicMock(),
                )
        finally:
            await clients.aclose()

    async def test_ws_stream_accepts_the_pin_and_rejects_another_with_a_fixed_text(
        self, tmp_path: Path
    ) -> None:
        pinned = make_self_signed(tmp_path / "p", "p")
        other = make_self_signed(tmp_path / "o", "o")
        async with LocalHttpServer(pinned) as server:
            _reader, writer = await vapix._ws_open_stream(
                "127.0.0.1",
                server.port,
                pinned_ssl_context(pem_of(pinned)),
                server_hostname="localhost",
                url=server.url(),
                timeout=2.0,
            )
            writer.close()
        async with LocalHttpServer(other) as server:
            with pytest.raises(UnreachableCaptureError) as caught:
                await vapix._ws_open_stream(
                    "127.0.0.1",
                    server.port,
                    pinned_ssl_context(pem_of(pinned)),
                    server_hostname="localhost",
                    url=server.url(),
                    timeout=2.0,
                )
        assert PIN_MISMATCH_MESSAGE in str(caught.value)
        for leaked in ("CERTIFICATE_VERIFY", "self-signed", "AXIS"):
            assert leaked not in str(caught.value)


class TestFetchPeerCertificate:
    async def test_returns_the_presented_certificate(self, tmp_path: Path) -> None:
        pki = make_self_signed(tmp_path)
        async with LocalHttpServer(pki) as server:
            der = await fetch_peer_certificate(
                "127.0.0.1", server.port, server_hostname=None, timeout=3.0
            )
        assert hashlib.sha256(der).hexdigest() == pem_fingerprint(pem_of(pki))

    async def test_the_handshake_sends_no_request_bytes(self, tmp_path: Path) -> None:
        # Display only: after the handshake the connection is closed without a
        # single byte of HTTP (no request line, no credentials).
        pki = make_self_signed(tmp_path)
        received: list[bytes] = []
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(pki.leaf_cert, pki.leaf_key)

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            received.append(await reader.read())
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
        port = server.sockets[0].getsockname()[1]
        try:
            await fetch_peer_certificate(
                "127.0.0.1", port, server_hostname=None, timeout=3.0
            )
            await asyncio.sleep(0.2)
        finally:
            server.close()
            await server.wait_closed()
        assert received == [b""]

    async def test_a_silent_peer_times_out(self) -> None:
        release = asyncio.Event()

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await release.wait()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            with pytest.raises(TimeoutError):
                await fetch_peer_certificate(
                    "127.0.0.1", port, server_hostname=None, timeout=0.3
                )
        finally:
            release.set()
            server.close()
            await server.wait_closed()

    async def test_a_non_tls_peer_is_an_oserror(self) -> None:
        async with LocalHttpServer() as plain:
            with pytest.raises(OSError):
                await fetch_peer_certificate(
                    "127.0.0.1", plain.port, server_hostname=None, timeout=3.0
                )
