"""Camera TLS trust: one context, and ``SSL_CERT_FILE`` keeps working (#69 CAM-S1).

Every camera in the deployment is reached over ``https://`` and verified through
``SSL_CERT_FILE`` (an internal CA bundle). Passing an explicit ``verify=`` context
to httpx bypasses its environment lookup, so these tests pin the invariant
against a real TLS server whose certificate only a generated test CA trusts:

* ``SSL_CERT_FILE`` alone -> all three camera clients and the VAPIX event
  WebSocket verify the server;
* ``capture.tls_ca_bundle`` overrides the environment;
* neither -> certifi, which does not know the test CA, so verification fails;
* a missing or unreadable bundle is a settings error at startup.
"""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from pydantic import ValidationError

from tests.unit.camera_net_support import (
    LocalHttpServer,
    allow_loopback_dial,
    make_test_pki,
)
from timelapse_manager.cameras import vapix
from timelapse_manager.cameras.base import CaptureError
from timelapse_manager.cameras.clients import CameraHttpClients
from timelapse_manager.cameras.tls import camera_ssl_context
from timelapse_manager.config.settings import CaptureSettings, Settings

_PURPOSES = ("capture", "events", "probe")


@pytest.fixture(autouse=True)
def _loopback_dial(monkeypatch: pytest.MonkeyPatch) -> None:
    """Camera clients dial only validated addresses; let them reach 127.0.0.1."""
    allow_loopback_dial(monkeypatch)


@pytest.fixture
def no_trust_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from no environment trust override."""
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)


async def _fetch_all(clients: CameraHttpClients, url: str) -> None:
    for purpose in _PURPOSES:
        response = await clients.for_purpose(purpose).get(url)
        assert response.status_code == 200, purpose


@pytest.mark.usefixtures("no_trust_env")
class TestTrustSource:
    async def test_ssl_cert_file_alone_verifies_all_three_clients(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pki = make_test_pki(tmp_path)
        monkeypatch.setenv("SSL_CERT_FILE", str(pki.ca_cert))
        clients = CameraHttpClients(CaptureSettings())
        try:
            async with LocalHttpServer(pki) as server:
                await _fetch_all(clients, server.url(host="localhost"))
        finally:
            await clients.aclose()

    async def test_setting_overrides_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server_pki = make_test_pki(tmp_path / "server", "server")
        other_pki = make_test_pki(tmp_path / "other", "other")
        # The environment trusts the server, the setting does not: the setting
        # wins, so verification fails.
        monkeypatch.setenv("SSL_CERT_FILE", str(server_pki.ca_cert))
        clients = CameraHttpClients(CaptureSettings(tls_ca_bundle=other_pki.ca_cert))
        try:
            async with LocalHttpServer(server_pki) as server:
                for purpose in _PURPOSES:
                    with pytest.raises(httpx.ConnectError, match="CERTIFICATE"):
                        await clients.for_purpose(purpose).get(
                            server.url(host="localhost")
                        )
        finally:
            await clients.aclose()

        # And the other way round: the setting trusts the server, the
        # environment does not.
        monkeypatch.setenv("SSL_CERT_FILE", str(other_pki.ca_cert))
        clients = CameraHttpClients(CaptureSettings(tls_ca_bundle=server_pki.ca_cert))
        try:
            async with LocalHttpServer(server_pki) as server:
                await _fetch_all(clients, server.url(host="localhost"))
        finally:
            await clients.aclose()

    async def test_neither_falls_back_to_certifi(self, tmp_path: Path) -> None:
        pki = make_test_pki(tmp_path)
        clients = CameraHttpClients(CaptureSettings())
        try:
            async with LocalHttpServer(pki) as server:
                with pytest.raises(httpx.ConnectError, match="CERTIFICATE"):
                    await clients.capture.get(server.url(host="localhost"))
        finally:
            await clients.aclose()

    def test_context_verifies_certificate_and_hostname(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pki = make_test_pki(tmp_path)
        monkeypatch.setenv("SSL_CERT_FILE", str(pki.ca_cert))
        context = camera_ssl_context(None)
        assert context.check_hostname is True
        assert context.verify_mode is ssl.CERT_REQUIRED

    def test_matches_httpx_default_when_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same CA set as the context httpx builds by default, for each source.
        pki = make_test_pki(tmp_path)

        def ca_subjects(context: ssl.SSLContext) -> list[Any]:
            return sorted(str(c["subject"]) for c in context.get_ca_certs())

        assert ca_subjects(camera_ssl_context(None)) == ca_subjects(
            httpx.create_ssl_context()
        )
        monkeypatch.setenv("SSL_CERT_FILE", str(pki.ca_cert))
        ours = ca_subjects(camera_ssl_context(None))
        assert ours == ca_subjects(httpx.create_ssl_context())
        assert len(ours) == 1

    def test_context_is_cached_per_trust_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pki = make_test_pki(tmp_path)
        default = camera_ssl_context(None)
        assert camera_ssl_context(None) is default
        monkeypatch.setenv("SSL_CERT_FILE", str(pki.ca_cert))
        from_env = camera_ssl_context(None)
        assert from_env is not default
        assert camera_ssl_context(None) is from_env
        assert camera_ssl_context(pki.ca_cert) is not from_env


@pytest.mark.usefixtures("no_trust_env")
class TestWebSocketPath:
    async def test_ws_connect_uses_the_clients_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pki = make_test_pki(tmp_path)
        monkeypatch.setenv("SSL_CERT_FILE", str(pki.ca_cert))
        seen: dict[str, Any] = {}

        class _Stop(Exception):
            pass

        async def fake_open(
            connect_host: str, port: int, ssl_ctx: object | None, **kwargs: Any
        ) -> Any:
            seen["ctx"] = ssl_ctx
            seen["server_hostname"] = kwargs["server_hostname"]
            raise _Stop

        monkeypatch.setattr(vapix, "_ws_open_stream", fake_open)
        clients = CameraHttpClients(CaptureSettings())
        try:
            with pytest.raises(_Stop):
                await vapix._ws_connect(
                    clients.events,
                    "https://cam.example:443/vapix/ws-data-stream",
                    "127.0.0.1",
                    None,
                    1.0,
                    MagicMock(),
                )
        finally:
            await clients.aclose()
        assert seen["ctx"] is camera_ssl_context(None)
        assert seen["server_hostname"] == "cam.example"

    async def test_ws_stream_verifies_with_ssl_cert_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pki = make_test_pki(tmp_path)
        async with LocalHttpServer(pki) as server:
            # Without the env var the test CA is unknown: a capture error.
            with pytest.raises(CaptureError):
                await vapix._ws_open_stream(
                    "127.0.0.1",
                    server.port,
                    camera_ssl_context(None),
                    server_hostname="localhost",
                    url=server.url(host="localhost"),
                    timeout=2.0,
                )
            monkeypatch.setenv("SSL_CERT_FILE", str(pki.ca_cert))
            reader, writer = await vapix._ws_open_stream(
                "127.0.0.1",
                server.port,
                camera_ssl_context(None),
                server_hostname="localhost",
                url=server.url(host="localhost"),
                timeout=2.0,
            )
            writer.close()

    async def test_ws_uses_the_configured_bundle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        from timelapse_manager.cameras import tls

        pki = make_test_pki(tmp_path)
        ctx = SimpleNamespace(
            settings=SimpleNamespace(capture=CaptureSettings(tls_ca_bundle=pki.ca_cert))
        )
        monkeypatch.setattr("timelapse_manager.runtime.get_context", lambda: ctx)
        assert tls.resolve_tls_ca_bundle() == pki.ca_cert


class TestBundleSetting:
    def test_missing_bundle_is_a_settings_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.pem"
        with pytest.raises(ValidationError, match="tls_ca_bundle"):
            CaptureSettings(tls_ca_bundle=missing)

    def test_directory_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="not a readable file"):
            CaptureSettings(tls_ca_bundle=tmp_path)

    def test_environment_variable_binds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pki = make_test_pki(tmp_path)
        monkeypatch.setenv("TLM_CAPTURE__TLS_CA_BUNDLE", str(pki.ca_cert))
        assert Settings().capture.tls_ca_bundle == pki.ca_cert

    def test_unset_by_default(self) -> None:
        assert CaptureSettings().tls_ca_bundle is None
