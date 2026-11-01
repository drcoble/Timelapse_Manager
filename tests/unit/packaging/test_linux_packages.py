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
        "pkgversion.py",
        "scripts/preinstall.sh",
        "scripts/postinstall.sh",
        "scripts/preremove.sh",
        "scripts/postremove.sh",
    ):
        assert (_PACKAGING / rel).is_file(), f"missing packaging file: {rel}"


def test_maintainer_scripts_are_posix_sh() -> None:
    scripts = (
        "scripts/preinstall.sh",
        "scripts/postinstall.sh",
        "scripts/preremove.sh",
        "scripts/postremove.sh",
    )
    for rel in scripts:
        text = (_PACKAGING / rel).read_text(encoding="utf-8")
        assert text.startswith("#!/bin/sh"), f"{rel} must be a /bin/sh script"
        assert "set -e" in text


def _spec() -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((_PACKAGING / "nfpm.yaml").read_text(encoding="utf-8"))


def test_nfpm_spec_targets_expected_layout() -> None:
    spec = _spec()

    assert spec["name"] == "timelapse-manager"
    # The frozen bundle installs under /opt, the unit under the canonical
    # /usr/lib/systemd (the old /lib path exists only on merged-/usr systems).
    destinations = {entry["dst"] for entry in spec["contents"]}
    assert "/opt/timelapse-manager" in destinations
    assert "/usr/lib/systemd/system/timelapse-manager.service" in destinations
    assert "/lib/systemd/system/timelapse-manager.service" not in destinations
    assert "/var/lib/timelapse-manager" in destinations
    # Maintainer scripts are wired up for both formats.
    assert set(spec["scripts"]) == {
        "preinstall",
        "postinstall",
        "preremove",
        "postremove",
    }
    for rel in spec["scripts"].values():
        assert (_PACKAGING.parent / rel).is_file(), rel


def test_nfpm_metadata_is_not_a_placeholder() -> None:
    spec = _spec()
    assert spec["maintainer"] == "Drew Coble <drcoble@users.noreply.github.com>"
    assert spec["vendor"] == "Drew Coble"
    assert spec["homepage"] == "https://github.com/drcoble/Timelapse_Manager"
    text = (_PACKAGING / "nfpm.yaml").read_text(encoding="utf-8")
    assert "REPLACE_ME" not in text


def test_nfpm_version_fields_are_templated_with_schema_none() -> None:
    spec = _spec()
    assert spec["version_schema"] == "none"
    assert spec["version"] == "${PKG_VERSION}"
    assert spec["prerelease"] == "${PKG_PRERELEASE}"
    assert spec["version_metadata"] == "${PKG_METADATA}"
    assert str(spec["release"]) == "1"


def test_nfpm_puts_the_cli_on_path() -> None:
    (link,) = [
        entry
        for entry in _spec()["contents"]
        if entry["dst"] == "/usr/bin/timelapse-manager"
    ]
    assert link["type"] == "symlink"
    assert link["src"] == "/opt/timelapse-manager/timelapse-manager"


def test_nfpm_ships_the_staged_bundle_without_packaging_tree() -> None:
    spec = _spec()
    (bundle,) = [e for e in spec["contents"] if e["dst"] == "/opt/timelapse-manager"]
    assert bundle["src"].startswith("${BUNDLE_STAGE}")
    script = (_PACKAGING / "build-linux-packages.sh").read_text(encoding="utf-8")
    assert 'rm -rf "${BUNDLE_STAGE}/packaging"' in script


def test_nfpm_dependencies() -> None:
    spec = _spec()
    assert spec["recommends"] == ["curl"]
    deb = spec["overrides"]["deb"]["depends"]
    rpm = spec["overrides"]["rpm"]["depends"]
    assert {"passwd", "systemd"} <= set(deb) and "adduser" not in deb
    assert {"shadow-utils", "util-linux", "systemd"} <= set(rpm)


def test_rpm_signing_is_optional_and_env_driven() -> None:
    spec = _spec()
    assert spec["rpm"]["signature"]["key_file"] == "${RPM_KEY_FILE}"
    script = (_PACKAGING / "build-linux-packages.sh").read_text(encoding="utf-8")
    assert 'RPM_KEY_FILE="${TLM_RPM_SIGNING_KEY_FILE:-}"' in script


def test_build_script_requires_the_version_and_maps_it() -> None:
    script = (_PACKAGING / "build-linux-packages.sh").read_text(encoding="utf-8")
    assert 'VERSION="${1:?' in script
    assert "0.1.0" not in script
    assert "pkgversion.py" in script


def test_unit_documentation_url_is_the_project_home() -> None:
    assert (
        "Documentation",
        "https://github.com/drcoble/Timelapse_Manager",
    ) in _unit_directives()


def test_postinstall_creates_user_and_enables_service() -> None:
    text = (_PACKAGING / "scripts" / "postinstall.sh").read_text(encoding="utf-8")
    assert "useradd" in text and "timelapse" in text
    assert "migrate" in text  # DB is migrated before first start
    assert "systemctl enable" in text


def test_preremove_stops_service() -> None:
    text = (_PACKAGING / "scripts" / "preremove.sh").read_text(encoding="utf-8")
    assert "systemctl stop" in text
    assert "systemctl disable" in text


def _unit_directives() -> list[tuple[str, str]]:
    """Return the systemd unit's ``(key, value)`` lines, comments skipped."""
    text = (_PACKAGING / "systemd" / "timelapse-manager.service").read_text(
        encoding="utf-8"
    )
    directives = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        key, _, value = line.partition("=")
        directives.append((key.strip(), value.strip()))
    return directives


def test_unit_stop_timeout_covers_the_shutdown_budget() -> None:
    """A stop drains both listeners, then tears down, inside 45 s (#67 R4)."""
    assert ("TimeoutStopSec", "45s") in _unit_directives()
    assert ("KillSignal", "SIGTERM") in _unit_directives()


def test_unit_keeps_devices_private_by_default() -> None:
    """GPU access stays an opt-in drop-in: the unit grants no device (#67 R15)."""
    directives = _unit_directives()
    keys = {key for key, _ in directives}

    assert ("PrivateDevices", "true") in directives
    assert "DeviceAllow" not in keys
    assert "SupplementaryGroups" not in keys
    # Every other hardening directive is still there.
    for expected in (
        ("NoNewPrivileges", "true"),
        ("ProtectSystem", "strict"),
        ("ProtectHome", "true"),
        ("PrivateTmp", "true"),
        ("ProtectKernelTunables", "true"),
        ("ProtectKernelModules", "true"),
        ("ProtectControlGroups", "true"),
        ("RestrictSUIDSGID", "true"),
        ("RestrictRealtime", "true"),
        ("LockPersonality", "true"),
        ("RestrictAddressFamilies", "AF_INET AF_INET6 AF_UNIX"),
    ):
        assert expected in directives, expected


def test_nfpm_ships_config_yml_as_a_noreplace_conffile() -> None:
    (entry,) = [
        e
        for e in _spec()["contents"]
        if e["dst"] == "/etc/timelapse-manager/config.yml"
    ]
    assert entry["type"] == "config|noreplace"
    assert entry["src"] == "./packaging/config.yml"
    info = entry["file_info"]
    assert (info["owner"], info["group"], info["mode"]) == ("root", "timelapse", 0o640)
    assert (_PACKAGING / "config.yml").is_file()


def test_nfpm_config_dir_is_root_owned_and_group_readable() -> None:
    (entry,) = [e for e in _spec()["contents"] if e["dst"] == "/etc/timelapse-manager"]
    info = entry["file_info"]
    assert (info["owner"], info["group"], info["mode"]) == ("root", "timelapse", 0o750)


def test_install_sh_makes_config_root_owned_group_readable() -> None:
    text = (_PACKAGING / "install.sh").read_text(encoding="utf-8")
    assert '-o root -g "${SERVICE_GROUP}" -m 0750 "${CONFIG_DIR}"' in text
    assert "-execdir chown -h root:" in text
    assert "chown -R" not in text.split("INSTALL_DIR")[0]


def test_nfpm_unit_is_world_readable() -> None:
    (entry,) = [
        e
        for e in _spec()["contents"]
        if e["dst"] == "/usr/lib/systemd/system/timelapse-manager.service"
    ]
    assert entry["file_info"]["mode"] == 0o644
    script = (_PACKAGING / "build-linux-packages.sh").read_text(encoding="utf-8")
    assert "chmod 0644 packaging/build/timelapse-manager.service" in script


def test_shipped_config_matches_the_unit_defaults() -> None:
    """The conffile carries what the unit's Environment= lines set (#338)."""
    yaml = pytest.importorskip("yaml")
    config = yaml.safe_load((_PACKAGING / "config.yml").read_text(encoding="utf-8"))
    assert config == {
        "paths": {"data_dir": "/var/lib/timelapse-manager"},
        "database": {"url": "sqlite:////var/lib/timelapse-manager/timelapse.db"},
    }
    unit = dict(_unit_directives_with_env())
    assert unit["TLM_PATHS__DATA_DIR"] == "@STATE_DIR@"
    assert unit["TLM_DATABASE__URL"] == "sqlite:///@STATE_DIR@/timelapse.db"
    assert unit["TLM_CONFIG"] == "@CONFIG_DIR@/config.yml"


def _unit_directives_with_env() -> list[tuple[str, str]]:
    """``(name, value)`` for each ``Environment=NAME=value`` line of the unit."""
    pairs = []
    for key, value in _unit_directives():
        if key == "Environment":
            name, _, rest = value.partition("=")
            pairs.append((name, rest))
    return pairs
