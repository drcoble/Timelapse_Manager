"""Local test servers and a test CA for the camera client and TLS tests (#69).

Not a test module: :mod:`tests.unit.test_camera_tls` and
:mod:`tests.unit.test_camera_clients` import from it.

* :func:`make_test_pki` writes a throwaway CA and a ``localhost`` leaf it
  signed, generated with ``cryptography`` (no network, no fixtures on disk).
* :class:`LocalHttpServer` is a minimal asyncio HTTP/1.1 server (optionally
  TLS-wrapped). ``/hang`` holds the request open until :meth:`release` is
  called; any other path answers ``200`` with a tiny JPEG at once. That is
  enough to fill a real httpx connection pool, which ``httpx.MockTransport``
  (which has no pool) cannot do.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import ipaddress
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

if TYPE_CHECKING:
    import pytest

# A complete (SOI ... EOI) JPEG header-only body; parsing is not exercised.
TINY_JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
)


@dataclass(frozen=True)
class GeneratedPki:
    """Paths of a generated CA certificate and the leaf it signed."""

    ca_cert: Path
    leaf_cert: Path
    leaf_key: Path


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def make_test_pki(directory: Path, label: str = "test") -> GeneratedPki:
    """Write a CA and a ``localhost``/``127.0.0.1`` leaf under ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(_name(f"tlm {label} CA"))
        .issuer_name(_name(f"tlm {label} CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False
        )
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("localhost"))
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    pki = GeneratedPki(
        ca_cert=directory / f"{label}-ca.pem",
        leaf_cert=directory / f"{label}-leaf.pem",
        leaf_key=directory / f"{label}-leaf.key",
    )
    pki.ca_cert.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    pki.leaf_cert.write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
    pki.leaf_key.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return pki


class LocalHttpServer:
    """A minimal asyncio HTTP/1.1 server on ``127.0.0.1`` (see module doc)."""

    def __init__(self, pki: GeneratedPki | None = None) -> None:
        self._ssl: ssl.SSLContext | None = None
        if pki is not None:
            self._ssl = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            self._ssl.load_cert_chain(pki.leaf_cert, pki.leaf_key)
        self._server: asyncio.Server | None = None
        self._release = asyncio.Event()
        self.hanging = 0
        self.port = 0

    @property
    def scheme(self) -> str:
        return "https" if self._ssl is not None else "http"

    def url(self, path: str = "/", host: str = "127.0.0.1") -> str:
        return f"{self.scheme}://{host}:{self.port}{path}"

    def release(self) -> None:
        """Let every held ``/hang`` request answer."""
        self._release.set()

    async def __aenter__(self) -> LocalHttpServer:
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0, ssl=self._ssl
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.release()
        assert self._server is not None
        self._server.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._server.wait_closed(), 2)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                request_line = await reader.readline()
                if not request_line:
                    return
                while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                    pass
                path = request_line.split(b" ")[1]
                if path == b"/hang":
                    self.hanging += 1
                    await self._release.wait()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(TINY_JPEG)}\r\n\r\n".encode()
                    + TINY_JPEG
                )
                await writer.drain()
        except (ConnectionError, ssl.SSLError, IndexError):
            return
        finally:
            with contextlib.suppress(Exception):
                writer.close()


def allow_loopback_dial(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let a camera client reach the local test servers on ``127.0.0.1``.

    Camera clients dial only SSRF-validated addresses (#69 #222), and the guard
    never relaxes loopback. These tests exercise TLS and pooling against a real
    local server, so the validation step is replaced by one that returns the
    literal host (``localhost`` -> ``127.0.0.1``, which is where the server
    binds). The pinned transport still connects, and TLS still verifies, exactly
    as in production; the pinning itself is tested in
    :mod:`tests.unit.test_camera_address_pinning`.
    """
    from timelapse_manager.security import pinned_http

    async def _loopback(host: str, **_policy: object) -> tuple[str, ...]:
        return ("127.0.0.1",) if host == "localhost" else (host,)

    monkeypatch.setattr(pinned_http, "pinned_addresses_async", _loopback)
