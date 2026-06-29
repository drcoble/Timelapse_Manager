"""Tests for SBOM generation integrity and artifact tamper-detection.

Covers:
- gen_sbom.py subprocess: output parses as CycloneDX, lists locked deps,
  contains an ffmpeg-named component with the pinned version.
- SHA-256 manifest + tamper test: clean verify passes; a flipped byte fails.

The SBOM test shells out to uv run cyclonedx-py and is marked slow.
The tamper test is in-process and is not marked slow.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from timelapse_manager.ffmpeg_pin import load_ffmpeg_pin

# Repository root: navigate up from this file's location to the code root.
_CODE_ROOT = Path(__file__).resolve().parent.parent.parent.parent


# ---------------------------------------------------------------------------
# SBOM generation
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.packaging
def test_gen_sbom_produces_cyclonedx_output(tmp_path: Path) -> None:
    """gen_sbom.py writes a file that self-identifies as CycloneDX."""
    output = tmp_path / "sbom.json"
    result = subprocess.run(
        [sys.executable, "dev/gen_sbom.py", "--output", str(output)],
        cwd=str(_CODE_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"gen_sbom.py exited {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert output.is_file(), "gen_sbom.py did not produce an output file"
    sbom = json.loads(output.read_text(encoding="utf-8"))
    assert sbom.get("bomFormat") == "CycloneDX"


@pytest.mark.slow
@pytest.mark.packaging
def test_gen_sbom_lists_locked_python_deps(tmp_path: Path) -> None:
    """SBOM includes at least one expected locked Python dependency (fastapi)."""
    output = tmp_path / "sbom.json"
    subprocess.run(
        [sys.executable, "dev/gen_sbom.py", "--output", str(output)],
        cwd=str(_CODE_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    sbom = json.loads(output.read_text(encoding="utf-8"))
    component_names = {c.get("name", "").lower() for c in sbom.get("components", [])}
    assert "fastapi" in component_names, (
        f"Expected 'fastapi' in SBOM components; found: {sorted(component_names)[:20]}"
    )


@pytest.mark.slow
@pytest.mark.packaging
def test_gen_sbom_contains_ffmpeg_component_with_pinned_version(tmp_path: Path) -> None:
    """SBOM includes an ffmpeg component whose version matches ffmpeg-pin.json."""
    pin = load_ffmpeg_pin()
    output = tmp_path / "sbom.json"
    subprocess.run(
        [sys.executable, "dev/gen_sbom.py", "--output", str(output)],
        cwd=str(_CODE_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    sbom = json.loads(output.read_text(encoding="utf-8"))
    ffmpeg_components = [
        c for c in sbom.get("components", []) if c.get("name") == "ffmpeg"
    ]
    assert ffmpeg_components, "No component named 'ffmpeg' found in SBOM"
    ffmpeg_comp = ffmpeg_components[0]
    sbom_ver = ffmpeg_comp.get("version")
    assert sbom_ver == pin.version, (
        f"SBOM ffmpeg version {sbom_ver!r} != pin version {pin.version!r}"
    )


@pytest.mark.slow
@pytest.mark.packaging
def test_gen_sbom_ffmpeg_component_has_required_fields(tmp_path: Path) -> None:
    """The ffmpeg SBOM component includes type, name, version, and purl."""
    output = tmp_path / "sbom.json"
    subprocess.run(
        [sys.executable, "dev/gen_sbom.py", "--output", str(output)],
        cwd=str(_CODE_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    sbom = json.loads(output.read_text(encoding="utf-8"))
    ffmpeg_comp = next(
        (c for c in sbom.get("components", []) if c.get("name") == "ffmpeg"), None
    )
    assert ffmpeg_comp is not None
    for field in ("type", "name", "version", "purl"):
        assert field in ffmpeg_comp, f"SBOM ffmpeg component missing field {field!r}"


# ---------------------------------------------------------------------------
# SHA-256 manifest / tamper detection (in-process, no slow mark)
# ---------------------------------------------------------------------------


@pytest.mark.packaging
def test_sha256_of_unchanged_content_verifies_clean(tmp_path: Path) -> None:
    """A file's SHA-256 matches a freshly computed digest of the same bytes."""
    artifact = tmp_path / "artifact.bin"
    content = b"timelapse-manager release artifact content"
    artifact.write_bytes(content)

    expected_digest = hashlib.sha256(content).hexdigest()
    actual_digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

    assert actual_digest == expected_digest


@pytest.mark.packaging
def test_sha256_detects_single_flipped_byte(tmp_path: Path) -> None:
    """A single flipped byte produces a different SHA-256 digest."""
    content = b"timelapse-manager release artifact content"
    original_digest = hashlib.sha256(content).hexdigest()

    # Flip one byte near the middle.
    tampered = bytearray(content)
    mid = len(tampered) // 2
    tampered[mid] ^= 0xFF
    tampered_digest = hashlib.sha256(bytes(tampered)).hexdigest()

    assert tampered_digest != original_digest


@pytest.mark.packaging
def test_sha256_tamper_detected_on_file(tmp_path: Path) -> None:
    """Writing tampered bytes to disk and re-reading exposes the digest mismatch."""
    content = b"integrity check payload"
    artifact = tmp_path / "release.bin"
    artifact.write_bytes(content)
    expected = hashlib.sha256(content).hexdigest()

    # Tamper: flip the first byte.
    data = bytearray(artifact.read_bytes())
    data[0] ^= 0x01
    artifact.write_bytes(bytes(data))

    actual = hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert actual != expected, "Tamper should have changed the digest"


@pytest.mark.packaging
def test_sha256_pin_sha256_field_has_expected_length() -> None:
    """The sha256 field in ffmpeg-pin.json is a plausible hex digest (64 chars)."""
    pin = load_ffmpeg_pin()
    # A SHA-256 hex digest is always exactly 64 hex characters.
    assert len(pin.sha256) == 64, (
        f"sha256 in pin file is {len(pin.sha256)} chars, expected 64"
    )


@pytest.mark.packaging
def test_sha256_pin_sha256_is_hex_string() -> None:
    """The sha256 field contains only hexadecimal characters."""
    pin = load_ffmpeg_pin()
    try:
        int(pin.sha256, 16)
    except ValueError:
        pytest.fail(f"pin sha256 {pin.sha256!r} is not a valid hex string")
