"""``gen_sbom.py --augment``: add FFmpeg and vendored assets to a scanner's SBOM.

The image and CT template SBOMs come from Trivy, which sees OS packages and
Python distributions but not the bundled static FFmpeg or the vendored web
assets. These tests run the real script against a tiny CycloneDX document; no
scanner and no environment scan is involved.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from timelapse_manager.ffmpeg_pin import load_ffmpeg_pin

_ROOT = Path(__file__).resolve().parents[3]

_spec = importlib.util.spec_from_file_location(
    "gen_sbom_augment_under_test", _ROOT / "dev" / "gen_sbom.py"
)
assert _spec is not None and _spec.loader is not None
gen_sbom = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen_sbom)

_SCANNER_SBOM: dict[str, Any] = {
    "bomFormat": "CycloneDX",
    "specVersion": "1.6",
    "components": [
        {"type": "library", "bom-ref": "pkg:deb/debian/curl@8.14", "name": "curl"},
        {"type": "library", "bom-ref": "pkg:pypi/fastapi@0.1", "name": "fastapi"},
    ],
}


def _write(path: Path, doc: Any) -> Path:
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _names(sbom: dict[str, Any]) -> list[str]:
    return [c["name"] for c in sbom["components"]]


def test_adds_ffmpeg_and_vendored_assets_and_keeps_the_scanner_components(
    tmp_path: Path,
) -> None:
    source = _write(tmp_path / "trivy.json", _SCANNER_SBOM)
    out = tmp_path / "sbom.cdx.json"
    assert gen_sbom.main(["--augment", str(source), "--output", str(out)]) == 0

    sbom = json.loads(out.read_text())
    assert sbom["bomFormat"] == "CycloneDX"
    assert {"curl", "fastapi"} <= set(_names(sbom))
    ffmpeg = [c for c in sbom["components"] if c["name"] == "ffmpeg"]
    assert len(ffmpeg) == 1
    assert ffmpeg[0]["version"] == load_ffmpeg_pin().version
    vendored = [c for c in sbom["components"] if c["bom-ref"].startswith("vendored:")]
    assert vendored, "no vendored web-asset components were added"
    # The input file is not touched when --output names another path.
    assert json.loads(source.read_text()) == _SCANNER_SBOM


def test_rewrites_the_input_in_place_by_default(tmp_path: Path) -> None:
    source = _write(tmp_path / "trivy.json", _SCANNER_SBOM)
    assert gen_sbom.main(["--augment", str(source)]) == 0
    assert "ffmpeg" in _names(json.loads(source.read_text()))


def test_augmenting_twice_does_not_duplicate_components(tmp_path: Path) -> None:
    source = _write(tmp_path / "trivy.json", _SCANNER_SBOM)
    gen_sbom.main(["--augment", str(source)])
    first = json.loads(source.read_text())
    gen_sbom.main(["--augment", str(source)])
    assert json.loads(source.read_text()) == first
    assert _names(first).count("ffmpeg") == 1


def test_does_not_scan_an_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("cyclonedx-py must not run in --augment mode")

    monkeypatch.setattr(gen_sbom, "_run_cyclonedx", boom)
    source = _write(tmp_path / "trivy.json", _SCANNER_SBOM)
    assert gen_sbom.main(["--augment", str(source)]) == 0


@pytest.mark.parametrize(
    "content",
    [
        "not json at all",
        json.dumps(["a", "list"]),
        json.dumps({"bomFormat": "SPDX", "components": []}),
    ],
)
def test_rejects_input_that_is_not_cyclonedx(tmp_path: Path, content: str) -> None:
    source = tmp_path / "bad.json"
    source.write_text(content)
    with pytest.raises(SystemExit) as exc:
        gen_sbom.main(["--augment", str(source)])
    assert exc.value.code == 1


def test_a_missing_input_is_a_hard_failure(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        gen_sbom.main(["--augment", str(tmp_path / "absent.json")])
    assert exc.value.code == 1


def test_a_tampered_vendored_asset_still_fails_the_augment(tmp_path: Path) -> None:
    manifest = json.loads(
        (
            _ROOT / "src" / "timelapse_manager" / "web" / "static" / "vendor.json"
        ).read_text()
    )
    first = manifest["assets"][0]
    root = tmp_path / "static"
    target = root / first["path"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b"tampered")
    (root / "vendor.json").write_text(json.dumps({"assets": [first]}))
    source = _write(tmp_path / "trivy.json", _SCANNER_SBOM)
    with pytest.raises(SystemExit):
        gen_sbom.main(["--augment", str(source), "--vendor", str(root / "vendor.json")])
