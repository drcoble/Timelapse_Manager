"""TLS certificate material for the built-in HTTPS listener.

The service binds HTTPS directly (no external terminator required), so it needs
a certificate and key on disk. This module resolves that material in one place:

* If an explicit ``cert_path``/``key_path`` pair is configured and both files
  exist, that pair is used as-is.
* Otherwise, if auto-generation is enabled, a self-signed certificate valid for
  ``localhost`` and the loopback addresses is generated into the data directory
  so a fresh install serves HTTPS out of the box. The private key is written
  with owner-only permissions.
* If neither a usable pair nor auto-generation is available, a clear error is
  raised rather than silently falling back to plaintext.

The self-signed certificate is for local/single-host use; a public deployment
should supply a real certificate via the explicit paths.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import ipaddress
from pathlib import Path

from ..config import Settings

# Names the auto-generated certificate is valid for. Loopback only -- this is a
# local-trust certificate, not one intended for a public hostname.
_DNS_NAMES = ["localhost"]
_IP_ADDRESSES = ["127.0.0.1", "::1"]
# Under the 825-day cap several TLS clients enforce on leaf certificates.
_VALID_DAYS = 825

# Filenames for the auto-generated pair inside the data directory.
_CERT_FILENAME = "tls-cert.pem"
_KEY_FILENAME = "tls-key.pem"


class TlsConfigurationError(RuntimeError):
    """Raised when no usable TLS certificate can be resolved or generated."""


def ensure_tls_cert(settings: Settings) -> tuple[Path, Path]:
    """Resolve (or generate) the TLS certificate/key pair and return their paths.

    Resolution order:

    1. An explicit ``tls.cert_path``/``tls.key_path`` pair, when both files
       exist, is returned unchanged.
    2. Otherwise, when ``tls.auto_generate`` is set, a self-signed certificate is
       generated into the data directory (reusing it on subsequent starts) and
       its paths returned.
    3. Otherwise a :class:`TlsConfigurationError` is raised.

    :raises TlsConfigurationError: if explicit paths are set but missing and
        auto-generation is disabled, or if no certificate backend is available.
    """
    tls = settings.tls
    if tls.cert_path is not None and tls.key_path is not None:
        cert = Path(tls.cert_path)
        key = Path(tls.key_path)
        if cert.exists() and key.exists():
            return cert, key
        if not tls.auto_generate:
            raise TlsConfigurationError(
                "TLS certificate or key file is missing and auto-generation is "
                f"disabled: cert={tls.cert_path!r}, key={tls.key_path!r}."
            )

    if not tls.auto_generate:
        raise TlsConfigurationError(
            "No TLS certificate configured and auto-generation is disabled. "
            "Set tls.cert_path and tls.key_path, or enable tls.auto_generate."
        )

    data_dir = Path(settings.paths.data_dir)
    cert = data_dir / _CERT_FILENAME
    key = data_dir / _KEY_FILENAME
    if cert.exists() and key.exists():
        return cert, key

    _generate_self_signed(cert, key)
    return cert, key


def _generate_self_signed(cert_path: Path, key_path: Path) -> None:
    """Generate a self-signed cert/key pair into the given paths.

    Writes a SAN-bearing leaf certificate (modern clients ignore the Common Name)
    and restricts the private key to owner-only permissions. Two backends are
    tried in order so generation works without adding a runtime dependency: the
    ``cryptography`` library if importable, then the system ``openssl`` binary.

    :raises TlsConfigurationError: if neither backend is available.
    """
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    if _generate_with_cryptography(cert_path, key_path):
        _restrict_key(key_path)
        return
    if _generate_with_openssl(cert_path, key_path):
        _restrict_key(key_path)
        return
    raise TlsConfigurationError(
        "Cannot generate a TLS certificate: neither the 'cryptography' package "
        "nor the 'openssl' CLI is available. Install one, or provide "
        "tls.cert_path/tls.key_path."
    )


def _generate_with_cryptography(cert_path: Path, key_path: Path) -> bool:
    """Generate the pair with the ``cryptography`` library; ``False`` if absent."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        return False

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Timelapse Manager"),
        ]
    )
    san = x509.SubjectAlternativeName(
        [x509.DNSName(n) for n in _DNS_NAMES]
        + [x509.IPAddress(ipaddress.ip_address(a)) for a in _IP_ADDRESSES]
    )
    now = _dt.datetime.now(_dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=1))
        .not_valid_after(now + _dt.timedelta(days=_VALID_DAYS))
        .add_extension(san, critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return True


def _generate_with_openssl(cert_path: Path, key_path: Path) -> bool:
    """Generate the pair by shelling out to ``openssl``; ``False`` if absent.

    :raises subprocess.CalledProcessError: if ``openssl`` is present but fails.
    """
    import shutil
    import subprocess

    if shutil.which("openssl") is None:
        return False

    san_entries = [f"DNS:{n}" for n in _DNS_NAMES]
    san_entries += [f"IP:{a}" for a in _IP_ADDRESSES]
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
            "-days",
            str(_VALID_DAYS),
            "-subj",
            "/O=Timelapse Manager/CN=localhost",
            "-addext",
            "subjectAltName=" + ",".join(san_entries),
        ],
        check=True,
        capture_output=True,
    )
    return True


def _restrict_key(key_path: Path) -> None:
    """Tighten the private-key file to owner-only where supported."""
    with contextlib.suppress(OSError, NotImplementedError):
        key_path.chmod(0o600)
