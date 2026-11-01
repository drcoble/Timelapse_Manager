"""Certificate fixtures for the camera-pin tests (#69 CAM-S1).

Throwaway certificates generated with ``cryptography``:

* :func:`make_self_signed` -- a camera-style factory certificate: self-signed,
  naming no host the tests connect to (the pin must not depend on the name);
* :func:`make_ca_with_leaves` -- one CA and several distinct leaves it signed,
  to show a pin on one leaf does not extend to its siblings or to the CA.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from tests.unit.camera_net_support import GeneratedPki

_NOW = datetime.datetime.now(datetime.UTC)


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _write(
    directory: Path,
    label: str,
    cert: x509.Certificate,
    key: ec.EllipticCurvePrivateKey,
    ca: x509.Certificate | None = None,
) -> GeneratedPki:
    directory.mkdir(parents=True, exist_ok=True)
    pki = GeneratedPki(
        ca_cert=directory / f"{label}-ca.pem",
        leaf_cert=directory / f"{label}-leaf.pem",
        leaf_key=directory / f"{label}-leaf.key",
    )
    pki.leaf_cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    pki.ca_cert.write_bytes((ca or cert).public_bytes(serialization.Encoding.PEM))
    pki.leaf_key.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return pki


def make_self_signed(
    directory: Path, label: str = "cam", *, expired: bool = False
) -> GeneratedPki:
    """Write a self-signed certificate that names no host (a factory certificate)."""
    key = ec.generate_private_key(ec.SECP256R1())
    start = _NOW - datetime.timedelta(days=(30 if expired else 0), minutes=5)
    end = (
        _NOW - datetime.timedelta(days=1)
        if expired
        else _NOW + datetime.timedelta(days=1)
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(f"AXIS {label}"))
        .issuer_name(_name(f"AXIS {label}"))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .sign(key, hashes.SHA256())
    )
    return _write(directory, label, cert, key)


def make_ca_with_leaves(directory: Path, count: int = 2) -> list[GeneratedPki]:
    """Write one CA and ``count`` distinct leaves it signed."""
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (
        x509.CertificateBuilder()
        .subject_name(_name("pin CA"))
        .issuer_name(_name("pin CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_NOW - datetime.timedelta(minutes=5))
        .not_valid_after(_NOW + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(ca_key, hashes.SHA256())
    )
    leaves = []
    for index in range(count):
        key = ec.generate_private_key(ec.SECP256R1())
        leaf = (
            x509.CertificateBuilder()
            .subject_name(_name(f"leaf {index}"))
            .issuer_name(ca.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_NOW - datetime.timedelta(minutes=5))
            .not_valid_after(_NOW + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .sign(ca_key, hashes.SHA256())
        )
        leaves.append(_write(directory, f"leaf{index}", leaf, key, ca))
    return leaves


def pem_of(pki: GeneratedPki) -> str:
    """Return the leaf certificate as a PEM string."""
    return pki.leaf_cert.read_text(encoding="ascii")
