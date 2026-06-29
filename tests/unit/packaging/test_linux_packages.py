"""Static checks for the Linux .deb/.rpm packaging artifacts.

These guard the nfpm packaging spec and maintainer scripts against accidental
deletion or corruption. The actual package build (nfpm) and install are validated
out-of-band on the build/test servers; here we only assert the in-repo inputs are
present and structurally coherent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_PACKAGING = Path(__file__).resolve().parents[3] / "packaging"


def test_packaging_artifacts_exist() -> None:
    for rel in (
        "nfpm.yaml",
        "build-linux-packages.sh",
        "scripts/postinstall.sh",
        "scripts/preremove.sh",
        "scripts/postremove.sh",
    ):
        assert (_PACKAGING / rel).is_file(), f"missing packaging file: {rel}"


def test_maintainer_scripts_are_posix_sh() -> None:
    scripts = (
        "scripts/postinstall.sh",
        "scripts/preremove.sh",
        "scripts/postremove.sh",
    )
    for rel in scripts:
        text = (_PACKAGING / rel).read_text(encoding="utf-8")
        assert text.startswith("#!/bin/sh"), f"{rel} must be a /bin/sh script"
        assert "set -e" in text


def test_nfpm_spec_targets_expected_layout() -> None:
    yaml = pytest.importorskip("yaml")
    spec = yaml.safe_load((_PACKAGING / "nfpm.yaml").read_text(encoding="utf-8"))

    assert spec["name"] == "timelapse-manager"
    # The frozen bundle installs under /opt and the unit under /lib/systemd.
    destinations = {entry["dst"] for entry in spec["contents"]}
    assert "/opt/timelapse-manager" in destinations
    assert "/lib/systemd/system/timelapse-manager.service" in destinations
    assert "/var/lib/timelapse-manager" in destinations
    # Maintainer scripts are wired up for both formats.
    assert set(spec["scripts"]) == {"postinstall", "preremove", "postremove"}


def test_postinstall_creates_user_and_enables_service() -> None:
    text = (_PACKAGING / "scripts" / "postinstall.sh").read_text(encoding="utf-8")
    assert "useradd" in text and "timelapse" in text
    assert "migrate" in text  # DB is migrated before first start
    assert "systemctl enable" in text


def test_preremove_stops_service() -> None:
    text = (_PACKAGING / "scripts" / "preremove.sh").read_text(encoding="utf-8")
    assert "systemctl stop" in text
    assert "systemctl disable" in text
