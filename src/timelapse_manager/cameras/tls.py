"""One TLS context for every camera connection (#69 CAM-S1).

Every camera connection the server opens over TLS -- the three httpx clients in
:mod:`.clients` and the raw VAPIX event WebSocket -- verifies the camera's
certificate against one trust source, chosen here:

1. ``capture.tls_ca_bundle``, when set: a CA bundle that applies to camera
   connections only;
2. otherwise exactly what httpx does by default: ``SSL_CERT_FILE``, then
   ``SSL_CERT_DIR``, then certifi.

The second rule is delegated to :func:`httpx.create_ssl_context` rather than
re-implemented, so the deployments that verify their cameras through
``SSL_CERT_FILE`` today keep working unchanged. An explicit ``verify=`` context
bypasses httpx's environment lookup, so building one any other way would
silently drop that bundle and fail every capture.

The context is built once per distinct trust source and cached. The cache key
includes the two environment variables, so a changed setting or environment
yields a fresh context; the bundle file itself is read once, as httpx's own
default context is.

A camera can instead be *pinned* to one exact certificate (#69 CAM-S1, the
pinning half; ``camera.tls_pinned_cert_pem``). Precedence, per camera:

1. a pin, when the camera has one: the stored certificate is the **only** trust
   anchor, so neither ``capture.tls_ca_bundle`` nor ``SSL_CERT_FILE`` can vouch
   for a different certificate on that camera, and the host name is not checked
   (a factory certificate seldom names the host it is reached at);
2. otherwise ``capture.tls_ca_bundle``;
3. otherwise the httpx default, as above.

A pin is opt-in and set only by an explicit operator action; nothing here
captures one automatically. A pinned camera is reachable over HTTPS only
(:class:`PinnedCameraClient` refuses ``http://``), so a pin can never be
bypassed by a plain-HTTP address. Errors about a pinned connection are a fixed
sentence: they never quote the presented or stored certificate.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import functools
import hashlib
import hmac
import os
import re
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from cryptography import x509

__all__ = [
    "CertificateSummary",
    "PIN_MISMATCH_MESSAGE",
    "PinMismatchError",
    "PinnedCameraClient",
    "PinnedSchemeError",
    "camera_ssl_context",
    "fetch_peer_certificate",
    "fingerprints_match",
    "is_pinned_context",
    "format_fingerprint",
    "normalize_fingerprint",
    "pem_fingerprint",
    "pem_summary",
    "pin_mismatch_message",
    "pinned_context_of",
    "pinned_ssl_context",
    "resolve_tls_ca_bundle",
    "summarize_certificate",
]

#: The fixed text for a pinned connection whose certificate was not accepted.
#: Deliberately free of any detail from the handshake or the stored pin.
PIN_MISMATCH_MESSAGE = (
    "pinned certificate not accepted (it changed or expired); "
    "re-pin it in the camera form"
)

_FINGERPRINT_SEPARATORS = re.compile(r"[\s:\-]")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_PEM_BEGIN = "-----BEGIN CERTIFICATE-----"
_MAX_PEM_CHARS = 16_384


def camera_ssl_context(ca_bundle: Path | str | None) -> ssl.SSLContext:
    """Return the verifying TLS context for camera connections.

    :param ca_bundle: the configured ``capture.tls_ca_bundle``, or ``None`` to
        trust what httpx trusts by default (``SSL_CERT_FILE``, ``SSL_CERT_DIR``,
        else certifi).
    :raises OSError: when the bundle cannot be read.
    :raises ssl.SSLError: when the bundle holds no usable certificate.
    """
    bundle = str(ca_bundle) if ca_bundle is not None else None
    return _build_context(
        bundle, os.environ.get("SSL_CERT_FILE"), os.environ.get("SSL_CERT_DIR")
    )


@functools.lru_cache(maxsize=8)
def _build_context(
    ca_bundle: str | None, env_cert_file: str | None, env_cert_dir: str | None
) -> ssl.SSLContext:
    """Build (and cache) the context for one trust source.

    ``env_cert_file`` and ``env_cert_dir`` are only part of the cache key:
    :func:`httpx.create_ssl_context` reads the environment itself.
    """
    del env_cert_file, env_cert_dir
    if ca_bundle is not None:
        return ssl.create_default_context(cafile=ca_bundle)
    return httpx.create_ssl_context()


def resolve_tls_ca_bundle() -> Path | None:
    """Return the configured camera CA bundle from the running application.

    For callers that hold no settings object (the VAPIX event WebSocket). With
    no application context installed (an adapter exercised on its own) there is
    no setting, so the httpx default trust applies.
    """
    from ..runtime import get_context

    try:
        bundle = get_context().settings.capture.tls_ca_bundle
    except (RuntimeError, AttributeError):
        return None
    return Path(bundle) if isinstance(bundle, str | Path) else None


# -- pinned certificates ------------------------------------------------------


class PinMismatchError(httpx.ConnectError):
    """A pinned camera presented a certificate other than its pin.

    Raised in place of httpx's TLS error so the adapters' existing
    ``httpx.TransportError`` handling reports it with a fixed message.
    """


class PinnedSchemeError(httpx.UnsupportedProtocol):
    """A pinned camera was asked for over plain ``http://`` (a downgrade)."""


def pin_mismatch_message() -> str:
    """Return the fixed operator-facing text for a rejected pinned certificate."""
    return PIN_MISMATCH_MESSAGE


def normalize_fingerprint(text: str) -> str:
    """Return ``text`` as 64 lower-case hex digits.

    Accepts the usual spellings of a SHA-256 fingerprint: upper or lower case,
    with or without ``:``, ``-`` or whitespace separators. Anything else (a
    wrong length, a non-hex digit) is rejected rather than guessed at.

    :raises ValueError: when ``text`` is not a SHA-256 fingerprint.
    """
    cleaned = _FINGERPRINT_SEPARATORS.sub("", text).lower()
    if not _HEX64.fullmatch(cleaned):
        raise ValueError("not a SHA-256 fingerprint")
    return cleaned


def format_fingerprint(fingerprint: str) -> str:
    """Return a normalised fingerprint as upper-case, colon-separated pairs."""
    digits = normalize_fingerprint(fingerprint).upper()
    return ":".join(digits[i : i + 2] for i in range(0, len(digits), 2))


def fingerprints_match(left: str, right: str) -> bool:
    """Return whether two fingerprints name the same certificate.

    Both sides are normalised first, so spelling never decides the answer, and
    the comparison is constant-time. Either side being malformed is a mismatch.
    """
    try:
        return hmac.compare_digest(
            normalize_fingerprint(left), normalize_fingerprint(right)
        )
    except ValueError:
        return False


def _der_of_pem(pem: str) -> bytes:
    """Return the DER of the one certificate in ``pem``.

    :raises ValueError: when ``pem`` is not exactly one well-formed certificate
        (a bundle of several would silently widen the trust).
    """
    text = pem.strip()
    if len(text) > _MAX_PEM_CHARS or text.count(_PEM_BEGIN) != 1:
        raise ValueError("expected exactly one PEM certificate")
    try:
        der = ssl.PEM_cert_to_DER_cert(text)
        x509.load_der_x509_certificate(der)
    except Exception as exc:  # noqa: BLE001 - any parse failure is one ValueError
        raise ValueError("not a valid PEM certificate") from exc
    return der


@functools.lru_cache(maxsize=64)
def pem_fingerprint(pem: str) -> str:
    """Return the lower-case hex SHA-256 fingerprint of a PEM certificate.

    :raises ValueError: when ``pem`` is not exactly one valid certificate.
    """
    return hashlib.sha256(_der_of_pem(pem)).hexdigest()


@dataclass(frozen=True)
class CertificateSummary:
    """What an operator needs to confirm a camera certificate.

    :param fingerprint: lower-case hex SHA-256 of the DER certificate.
    :param subject: RFC 4514 subject.
    :param issuer: RFC 4514 issuer (equal to ``subject`` when self-signed).
    :param not_before: start of validity (UTC).
    :param not_after: end of validity (UTC).
    :param pem: the certificate as PEM, for storing once confirmed.
    """

    fingerprint: str
    subject: str
    issuer: str
    not_before: datetime.datetime
    not_after: datetime.datetime
    pem: str

    @property
    def display_fingerprint(self) -> str:
        """The fingerprint as upper-case, colon-separated pairs."""
        return format_fingerprint(self.fingerprint)

    @property
    def self_signed(self) -> bool:
        """Whether the subject names the issuer."""
        return self.subject == self.issuer

    @property
    def valid_now(self) -> bool:
        """Whether today falls inside the validity period.

        OpenSSL rejects a certificate outside it even when pinned, so pinning
        one would stop the camera's captures at once.
        """
        return self.not_before <= datetime.datetime.now(datetime.UTC) <= self.not_after


def pem_summary(pem: str) -> CertificateSummary:
    """Describe a PEM certificate.

    :raises ValueError: when ``pem`` is not exactly one valid certificate.
    """
    return summarize_certificate(_der_of_pem(pem))


def summarize_certificate(der: bytes) -> CertificateSummary:
    """Describe a DER certificate.

    :raises ValueError: when ``der`` is not a certificate.
    """
    try:
        cert = x509.load_der_x509_certificate(der)
    except Exception as exc:  # noqa: BLE001 - any parse failure is one ValueError
        raise ValueError("not a valid certificate") from exc
    return CertificateSummary(
        fingerprint=hashlib.sha256(der).hexdigest(),
        subject=cert.subject.rfc4514_string(),
        issuer=cert.issuer.rfc4514_string(),
        not_before=cert.not_valid_before_utc,
        not_after=cert.not_valid_after_utc,
        pem=ssl.DER_cert_to_PEM_cert(der),
    )


@functools.lru_cache(maxsize=32)
def pinned_ssl_context(pem: str) -> ssl.SSLContext:
    """Return a context whose only trust anchor is the certificate in ``pem``.

    ``CERT_REQUIRED`` stays on: a different certificate, an expired one, or one
    the pin does not vouch for fails the handshake. The host name is not
    checked (``check_hostname`` off): the exact-certificate match is the
    control, and a factory certificate rarely names the address it is reached
    at. ``VERIFY_X509_PARTIAL_CHAIN`` lets a non-self-signed leaf (for example
    one an internal CA issued) be the anchor itself, so the pin holds for any
    certificate and never widens to the CA behind it.

    Cached per PEM; the PEM is public data.

    :raises ValueError: when ``pem`` is not exactly one valid certificate.
    :raises ssl.SSLError: when OpenSSL rejects the certificate as an anchor.
    """
    _der_of_pem(pem)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    context.load_verify_locations(cadata=pem.strip())
    return context


def _is_certificate_failure(exc: BaseException | None) -> bool:
    """Return whether ``exc``'s cause chain holds a certificate verify failure."""
    for _ in range(6):
        if exc is None:
            return False
        if isinstance(exc, ssl.SSLCertVerificationError):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


class PinnedCameraClient(httpx.AsyncClient):
    """An httpx client that trusts exactly one camera certificate.

    Built with :func:`pinned_ssl_context`. It also refuses ``http://`` outright
    (:class:`PinnedSchemeError`), so a pin cannot be sidestepped by an address
    or URI that downgrades to plain HTTP, and it turns a certificate
    verification failure into :class:`PinMismatchError` with a fixed message
    that carries no handshake or certificate detail.
    """

    def __init__(self, pem: str, **kwargs: Any) -> None:
        """Create the client; ``kwargs`` are :class:`httpx.AsyncClient`'s.

        :param pem: the one certificate to trust.
        :raises ValueError: when ``pem`` is not exactly one valid certificate.
        """
        self.pinned_ssl_context = pinned_ssl_context(pem)
        super().__init__(verify=self.pinned_ssl_context, **kwargs)

    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        """Send ``request`` if it is HTTPS, mapping a rejected pin to a fixed error."""
        if request.url.scheme != "https":
            raise PinnedSchemeError(
                "this camera is pinned to a certificate and must use https",
                request=request,
            )
        try:
            return await super().send(request, **kwargs)
        except httpx.ConnectError as exc:
            if _is_certificate_failure(exc):
                raise PinMismatchError(PIN_MISMATCH_MESSAGE, request=request) from None
            raise


def pinned_context_of(client: object) -> ssl.SSLContext | None:
    """Return the pin context a camera client trusts, or ``None`` if not pinned."""
    if isinstance(client, PinnedCameraClient):
        return client.pinned_ssl_context
    return None


def is_pinned_context(context: object) -> bool:
    """Return whether ``context`` is a pin context (not the shared camera one).

    The shared camera contexts always check the host name; a pin context never
    does, and that is the only context here that does not.
    """
    return isinstance(context, ssl.SSLContext) and not context.check_hostname


async def fetch_peer_certificate(
    connect_host: str,
    port: int,
    *,
    server_hostname: str | None,
    timeout: float,
) -> bytes:
    """Return the DER certificate a camera presents, without trusting it.

    **Display only.** The handshake is deliberately unverified -- there is
    nothing to verify against yet -- so it must carry no traffic: no HTTP
    request, no credentials. The connection is closed as soon as the certificate
    is read, and the result is shown to an operator to confirm; it is never
    used to talk to the camera.

    :param connect_host: the (SSRF-checked) address to dial.
    :param server_hostname: the name to send as SNI, or ``None``.
    :raises TimeoutError: when the handshake does not finish in ``timeout``.
    :raises OSError: when the camera cannot be reached or does not speak TLS.
    :raises ValueError: when the peer presented no certificate.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # noqa: S503 - display-only, sends nothing
    async with asyncio.timeout(timeout):
        _reader, writer = await asyncio.open_connection(
            connect_host, port, ssl=context, server_hostname=server_hostname
        )
        try:
            ssl_object = writer.get_extra_info("ssl_object")
            der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
    if not der:
        raise ValueError("the camera presented no certificate")
    return bytes(der)
