"""Published SBOMs carry no build-environment names or paths."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

_ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "gen_sbom_neutral_under_test", _ROOT / "dev" / "gen_sbom.py"
)
assert _spec is not None and _spec.loader is not None
gen_sbom = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen_sbom)

_BUILD_IMAGE = "registry.build.invalid/owner/timelapse-manager"
_PUBLIC_IMAGE = "ghcr.io/example/timelapse_manager"
_DIGEST = "sha256:" + "cd" * 32


def _trivy_like() -> dict[str, Any]:
    """The shape of ``trivy image --format cyclonedx`` output."""
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "metadata": {
            "component": {
                "bom-ref": "root",
                "type": "container",
                "name": f"{_BUILD_IMAGE}:0.2.0",
                "purl": (
                    f"pkg:oci/timelapse-manager@{quote(_DIGEST, safe='')}"
                    f"?repository_url={quote(_BUILD_IMAGE, safe='')}&arch=amd64"
                ),
                "properties": [
                    {
                        "name": "aquasecurity:trivy:RepoTag",
                        "value": f"{_BUILD_IMAGE}:0.2.0",
                    },
                    {
                        "name": "aquasecurity:trivy:RepoDigest",
                        "value": f"{_BUILD_IMAGE}@{_DIGEST}",
                    },
                ],
            }
        },
        "components": [
            {
                "type": "library",
                "bom-ref": "pkg:pypi/x@1",
                "name": "x",
                "externalReferences": [
                    {"type": "distribution", "url": "file:///home/runner/work/src"},
                    {"type": "website", "url": "https://pypi.org/project/x/"},
                ],
            }
        ],
    }


def test_the_build_image_name_is_replaced_everywhere_including_encoded_forms() -> None:
    out = gen_sbom._neutralise(_trivy_like(), [f"{_BUILD_IMAGE}={_PUBLIC_IMAGE}"], None)
    text = json.dumps(out)
    assert "registry.build.invalid" not in text
    assert quote(_BUILD_IMAGE, safe="") not in text
    assert f"{_PUBLIC_IMAGE}@{_DIGEST}" in text  # the digest is untouched
    assert quote(_PUBLIC_IMAGE, safe="") in text


def test_local_file_references_are_dropped_and_others_kept() -> None:
    out = gen_sbom._neutralise(_trivy_like(), [], None)
    refs = out["components"][0]["externalReferences"]
    assert refs == [{"type": "website", "url": "https://pypi.org/project/x/"}]


def test_the_component_name_can_be_set() -> None:
    out = gen_sbom._neutralise(_trivy_like(), [], "timelapse-manager-ct-template")
    assert out["metadata"]["component"]["name"] == "timelapse-manager-ct-template"


def test_a_malformed_rename_is_refused() -> None:
    import pytest

    with pytest.raises(SystemExit):
        gen_sbom._neutralise(_trivy_like(), ["no-equals-sign"], None)


def test_main_applies_the_neutralisation(tmp_path: Path) -> None:
    source = tmp_path / "raw.json"
    source.write_text(json.dumps(_trivy_like()), encoding="utf-8")
    out = tmp_path / "out.json"
    code = gen_sbom.main(
        [
            "--augment",
            str(source),
            "--output",
            str(out),
            "--rename",
            f"{_BUILD_IMAGE}={_PUBLIC_IMAGE}",
            "--component-name",
            "neutral",
        ]
    )
    assert code == 0
    text = out.read_text()
    assert "registry.build.invalid" not in text
    assert "file:///" not in text
    assert json.loads(text)["metadata"]["component"]["name"] == "neutral"
