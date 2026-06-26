#!/usr/bin/env python3
"""Generate a self-signed TLS certificate for local HTTPS development.

Writes a cert/key pair valid for ``localhost`` and ``127.0.0.1`` into the
repository root as ``.dev-cert.pem`` and ``.dev-cert-key.pem`` (both
git-ignored). The certificate carries a Subject Alternative Name (SAN) so
modern TLS clients accept it for those names -- a Common Name alone is not
honoured by current clients.

Two backends are supported, preferring the dependency-light path:

1. The ``cryptography`` library, if importable.
2. The system ``openssl`` binary, as a fallback.

This is for development only. Never use the resulting certificate in
production.
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import subprocess
import sys
from pathlib import Path

# Output paths, resolved relative to the repository root (this file lives in
# ``<root>/dev/``).
ROOT = Path(__file__).resolve().parent.parent
CERT_PATH = ROOT / ".dev-cert.pem"
KEY_PATH = ROOT / ".dev-cert-key.pem"

# Names the certificate must be valid for during local development.
DNS_NAMES = ["localhost"]
IP_ADDRESSES = ["127.0.0.1", "::1"]
VALID_DAYS = 825  # Under the 825-day cap enforced by many TLS clients.


def _generate_with_cryptography() -> bool:
    """Generate the cert/key using the ``cryptography`` library.

    Returns ``True`` on success, ``False`` if the library is unavailable.
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        return False

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
            x509.NameAttribute(
                NameOID.ORGANIZATION_NAME, "Timelapse Manager (dev)"
            ),
        ]
    )

    san = x509.SubjectAlternativeName(
        [x509.DNSName(name) for name in DNS_NAMES]
        + [
            x509.IPAddress(ipaddress.ip_address(addr))
            for addr in IP_ADDRESSES
        ]
    )

    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=1))
        .not_valid_after(now + _dt.timedelta(days=VALID_DAYS))
        .add_extension(san, critical=False)
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
        .sign(key, hashes.SHA256())
    )

    KEY_PATH.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    CERT_PATH.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    _restrict_key_permissions()
    return True


def _generate_with_openssl() -> bool:
    """Generate the cert/key by shelling out to ``openssl``.

    Returns ``True`` on success, ``False`` if ``openssl`` is not on PATH.
    Raises ``subprocess.CalledProcessError`` if ``openssl`` runs but fails.
    """
    import shutil

    if shutil.which("openssl") is None:
        return False

    san_entries = [f"DNS:{name}" for name in DNS_NAMES]
    san_entries += [f"IP:{addr}" for addr in IP_ADDRESSES]
    san = "subjectAltName=" + ",".join(san_entries)

    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",  # do not encrypt the private key
            "-keyout",
            str(KEY_PATH),
            "-out",
            str(CERT_PATH),
            "-days",
            str(VALID_DAYS),
            "-subj",
            "/O=Timelapse Manager (dev)/CN=localhost",
            "-addext",
            san,
        ],
        check=True,
    )
    _restrict_key_permissions()
    return True


def _restrict_key_permissions() -> None:
    """Best-effort tightening of the private-key file mode (POSIX only)."""
    try:
        KEY_PATH.chmod(0o600)
    except (OSError, NotImplementedError):
        pass


def main() -> int:
    if CERT_PATH.exists() and KEY_PATH.exists():
        print(f"Dev cert already present: {CERT_PATH.name}, {KEY_PATH.name}")
        return 0

    if _generate_with_cryptography():
        backend = "cryptography"
    elif _generate_with_openssl():
        backend = "openssl"
    else:
        print(
            "ERROR: no certificate backend available. Install the "
            "'cryptography' package or the 'openssl' CLI.",
            file=sys.stderr,
        )
        return 1

    print(f"Generated dev cert via {backend}:")
    print(f"  cert: {CERT_PATH}")
    print(f"  key:  {KEY_PATH}")
    print(f"  valid for: {', '.join(DNS_NAMES + IP_ADDRESSES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
