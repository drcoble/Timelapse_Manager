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
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from timelapse_manager.ffmpeg_pin import load_ffmpeg_pin

# Repository root: navigate up from this file's location to the code root.
_CODE_ROOT = Path(__file__).resolve().parent.parent.parent.parent


# ---------------------------------------------------------------------------
# SBOM generation
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def generated_sbom(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Run ``gen_sbom.py`` once for this module and return the parsed SBOM.

    One generation is shared by the SBOM tests (each used to run its own, four
    concurrent ``cyclonedx-py`` scans under ``-n auto``). A failure shows the
    script's stdout and stderr instead of a bare ``CalledProcessError``.
    """
    output = tmp_path_factory.mktemp("sbom") / "sbom.json"
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
    sbom: dict[str, Any] = json.loads(output.read_text(encoding="utf-8"))
    return sbom


@pytest.mark.slow
@pytest.mark.packaging
def test_gen_sbom_produces_cyclonedx_output(generated_sbom: dict[str, Any]) -> None:
    """gen_sbom.py writes a file that self-identifies as CycloneDX."""
    assert generated_sbom.get("bomFormat") == "CycloneDX"


@pytest.mark.slow
@pytest.mark.packaging
def test_gen_sbom_lists_locked_python_deps(generated_sbom: dict[str, Any]) -> None:
    """SBOM includes at least one expected locked Python dependency (fastapi)."""
    component_names = {
        c.get("name", "").lower() for c in generated_sbom.get("components", [])
    }
    assert "fastapi" in component_names, (
        f"Expected 'fastapi' in SBOM components; found: {sorted(component_names)[:20]}"
    )


@pytest.mark.slow
@pytest.mark.packaging
def test_gen_sbom_contains_ffmpeg_component_with_pinned_version(
    generated_sbom: dict[str, Any],
) -> None:
    """SBOM includes an ffmpeg component whose version matches ffmpeg-pin.json."""
    pin = load_ffmpeg_pin()
    ffmpeg_components = [
        c for c in generated_sbom.get("components", []) if c.get("name") == "ffmpeg"
    ]
    assert ffmpeg_components, "No component named 'ffmpeg' found in SBOM"
    ffmpeg_comp = ffmpeg_components[0]
    sbom_ver = ffmpeg_comp.get("version")
    assert sbom_ver == pin.version, (
        f"SBOM ffmpeg version {sbom_ver!r} != pin version {pin.version!r}"
    )


@pytest.mark.slow
@pytest.mark.packaging
def test_gen_sbom_ffmpeg_component_has_required_fields(
    generated_sbom: dict[str, Any],
) -> None:
    """The ffmpeg SBOM component includes type, name, version, and purl."""
    ffmpeg_comp = next(
        (c for c in generated_sbom.get("components", []) if c.get("name") == "ffmpeg"),
        None,
    )
    assert ffmpeg_comp is not None
    for field in ("type", "name", "version", "purl"):
        assert field in ffmpeg_comp, f"SBOM ffmpeg component missing field {field!r}"


@pytest.mark.packaging
def test_gen_sbom_runs_cyclonedx_without_a_nested_uv_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The scanner runs on this interpreter; ``uv run`` would re-sync the venv.

    A nested ``uv run`` syncs (or, with another ``UV_PYTHON``, recreates) the
    shared ``.venv`` while other test workers import from it (#34).
    """
    spec = importlib.util.spec_from_file_location(
        "gen_sbom_under_test", _CODE_ROOT / "dev" / "gen_sbom.py"
    )
    assert spec is not None and spec.loader is not None
    gen_sbom = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen_sbom)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(gen_sbom.subprocess, "run", fake_run)
    gen_sbom._run_cyclonedx(tmp_path / "venv", tmp_path / "sbom.json")

    assert len(calls) == 1
    assert calls[0][:3] == [sys.executable, "-m", "cyclonedx_py"]
    assert "uv" not in calls[0]


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


# ---------------------------------------------------------------------------
# Vendored web assets: web/static/vendor.json (#67 S25, #70 F-S7)
# ---------------------------------------------------------------------------

_STATIC = _CODE_ROOT / "src" / "timelapse_manager" / "web" / "static"
_VENDOR = _STATIC / "vendor.json"


def _vendor_assets() -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = json.loads(_VENDOR.read_text(encoding="utf-8"))[
        "assets"
    ]
    return assets


def _load_gen_sbom() -> Any:
    spec = importlib.util.spec_from_file_location(
        "gen_sbom_vendor_under_test", _CODE_ROOT / "dev" / "gen_sbom.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.packaging
def test_vendored_files_match_their_recorded_sha256() -> None:
    """Every vendored file's bytes match vendor.json, the one hash record."""
    assets = _vendor_assets()
    assert {entry["path"] for entry in assets} >= {
        "js/htmx.min.js",
        "fonts/Geist-Variable.woff2",
        "fonts/GeistMono-Variable.woff2",
    }
    for entry in assets:
        data = (_STATIC / entry["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"], entry["path"]


@pytest.mark.packaging
def test_every_third_party_static_file_is_recorded() -> None:
    """Minified scripts and fonts are vendored; each must have a manifest entry."""
    recorded = {entry["path"] for entry in _vendor_assets()}
    vendored = {
        path.relative_to(_STATIC).as_posix()
        for pattern in ("**/*.min.js", "**/*.woff2", "**/*.woff", "**/*.ttf")
        for path in _STATIC.glob(pattern)
    }
    assert vendored <= recorded, f"unrecorded vendored files: {vendored - recorded}"


@pytest.mark.packaging
def test_vendored_licences_ship_and_notice_names_each() -> None:
    """Licence files ship beside the assets, and NOTICE credits every one."""
    notice = (_CODE_ROOT / "NOTICE").read_text(encoding="utf-8")
    for entry in _vendor_assets():
        assert entry["license"], entry["path"]
        if entry.get("license_file"):
            licence = (_STATIC / entry["license_file"]).read_text(encoding="utf-8")
            assert "SIL OPEN FONT LICENSE Version 1.1" in licence
        assert entry["name"] in notice, f"NOTICE does not mention {entry['name']}"
        assert entry["license"] in notice or "Open Font License" in notice


@pytest.mark.packaging
def test_gen_sbom_vendor_components_carry_the_recorded_hash() -> None:
    """The SBOM components come from vendor.json with its hashes and licences."""
    gen_sbom = _load_gen_sbom()
    components = [
        gen_sbom._vendor_component(entry)
        for entry in gen_sbom._load_vendor_assets(_VENDOR)
    ]
    by_name = {component["name"]: component for component in components}
    recorded = {entry["name"]: entry for entry in _vendor_assets()}
    assert set(by_name) == set(recorded)
    for name, component in by_name.items():
        assert component["hashes"] == [
            {"alg": "SHA-256", "content": recorded[name]["sha256"]}
        ]
        assert component["licenses"] == [{"license": {"id": recorded[name]["license"]}}]
    assert by_name["htmx"]["version"] == "1.9.12"
    # The fonts are byte-identical to the official npm geist@1.7.2 files (#105).
    for font in ("Geist", "Geist Mono"):
        assert by_name[font]["version"] == "1.7.2"
        assert by_name[font]["purl"] == "pkg:npm/geist@1.7.2"
        assert "byte-identical" in by_name[font]["description"]


def _write_manifest(folder: Path, entry: dict[str, Any]) -> Path:
    manifest = folder / "vendor.json"
    manifest.write_text(json.dumps({"schema": 1, "assets": [entry]}), encoding="utf-8")
    return manifest


@pytest.mark.packaging
def test_gen_sbom_refuses_a_tampered_vendored_file(tmp_path: Path) -> None:
    gen_sbom = _load_gen_sbom()
    asset = tmp_path / "js" / "lib.min.js"
    asset.parent.mkdir()
    asset.write_bytes(b"console.log(1)")
    entry = {
        "path": "js/lib.min.js",
        "name": "lib",
        "type": "library",
        "license": "MIT",
        "sha256": hashlib.sha256(b"console.log(1)").hexdigest(),
    }
    manifest = _write_manifest(tmp_path, entry)
    assert gen_sbom._load_vendor_assets(manifest) == [entry]
    asset.write_bytes(b"console.log(2)")
    with pytest.raises(SystemExit):
        gen_sbom._load_vendor_assets(manifest)


@pytest.mark.packaging
@pytest.mark.parametrize("path", ["../outside.js", "missing.js"])
def test_gen_sbom_refuses_a_path_outside_or_missing(tmp_path: Path, path: str) -> None:
    gen_sbom = _load_gen_sbom()
    inner = tmp_path / "static"
    inner.mkdir()
    (tmp_path / "outside.js").write_bytes(b"x")
    entry = {
        "path": path,
        "name": "lib",
        "type": "library",
        "license": "MIT",
        "sha256": hashlib.sha256(b"x").hexdigest(),
    }
    with pytest.raises(SystemExit):
        gen_sbom._load_vendor_assets(_write_manifest(inner, entry))


@pytest.mark.slow
@pytest.mark.packaging
def test_gen_sbom_lists_htmx_with_its_hash(generated_sbom: dict[str, Any]) -> None:
    """The generated SBOM carries the vendored htmx component and its SHA-256."""
    [entry] = [e for e in _vendor_assets() if e["name"] == "htmx"]
    [htmx] = [
        c for c in generated_sbom.get("components", []) if c.get("name") == "htmx"
    ]
    assert htmx["hashes"] == [{"alg": "SHA-256", "content": entry["sha256"]}]
