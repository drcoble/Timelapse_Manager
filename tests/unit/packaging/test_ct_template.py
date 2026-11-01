"""Tests for the Proxmox CT template builder (``packaging/lxc``).

Docker is not needed: the rootfs listing checks run against synthetic tars,
and the rest are static checks on the committed files.
"""

from __future__ import annotations

import importlib.util
import io
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

_LXC = Path(__file__).resolve().parents[3] / "packaging" / "lxc"

_spec = importlib.util.spec_from_file_location("check_rootfs", _LXC / "check_rootfs.py")
assert _spec is not None and _spec.loader is not None
check_rootfs = importlib.util.module_from_spec(_spec)
sys.modules["check_rootfs"] = check_rootfs
_spec.loader.exec_module(check_rootfs)

_GOOD_ENV = b"# comment\nTLM_SERVER__BIND_ADDRESS=127.0.0.1\n"


def _build_tar(
    path: Path,
    *,
    omit: frozenset[str] = frozenset(),
    extra: dict[str, bytes] | None = None,
    debian_version: bytes = b"13.1\n",
    machine_id: bytes = b"",
    env: bytes = _GOOD_ENV,
    prefix: str = "./",
    symlinks: dict[str, str] | None = None,
) -> Path:
    """Write a minimal passing rootfs tar, with knobs to break one property."""
    files: dict[str, bytes] = {
        "etc/debian_version": debian_version,
        "etc/machine-id": machine_id,
        "opt/timelapse-manager/_internal/ffmpeg/ffmpeg": b"\x7fELF",
        "opt/timelapse-manager/_internal/ffmpeg/ffprobe": b"\x7fELF",
        "etc/timelapse-manager/timelapse-manager.env": env,
        "etc/timelapse-manager/config.yml": b"paths:\n  data_dir: /x\n",
    }
    files.update(extra or {})
    with tarfile.open(path, "w") as tf:
        directory = tarfile.TarInfo(prefix + "var/lib/timelapse-manager")
        directory.type = tarfile.DIRTYPE
        if "var/lib/timelapse-manager" not in omit:
            tf.addfile(directory)
        link = tarfile.TarInfo(
            prefix
            + "etc/systemd/system/multi-user.target.wants/timelapse-manager.service"
        )
        link.type = tarfile.SYMTYPE
        link.linkname = "/lib/systemd/system/timelapse-manager.service"
        if "wants" not in omit:
            tf.addfile(link)
        all_links = {
            f"etc/systemd/system/{unit}": "/dev/null"
            for unit in check_rootfs.NETWORKD_MASKED
            if f"mask:{unit}" not in omit
        }
        all_links.update(symlinks or {})
        for name, target in all_links.items():
            sym = tarfile.TarInfo(prefix + name)
            sym.type = tarfile.SYMTYPE
            sym.linkname = target
            tf.addfile(sym)
        for name, data in files.items():
            if name in omit:
                continue
            info = tarfile.TarInfo(prefix + name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


def _failures(path: Path) -> list[str]:
    return check_rootfs.check_tar(str(path))


def test_good_rootfs_passes(tmp_path: Path) -> None:
    assert _failures(_build_tar(tmp_path / "r.tar")) == []


def test_names_without_dot_slash_prefix_pass(tmp_path: Path) -> None:
    assert _failures(_build_tar(tmp_path / "r.tar", prefix="")) == []


def test_stream_mode_from_stdin_equivalent(tmp_path: Path) -> None:
    tar = _build_tar(tmp_path / "r.tar")
    script = _LXC / "check_rootfs.py"
    with tar.open("rb") as handle:
        result = subprocess.run(
            [sys.executable, "-I", str(script), "-"],
            stdin=handle,
            capture_output=True,
            check=False,
        )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"debian_version": b"12.9\n"}, "not Debian 13"),
        ({"omit": frozenset({"etc/debian_version"})}, "debian_version is missing"),
        ({"omit": frozenset({"wants"})}, "service not enabled"),
        (
            {"omit": frozenset({"opt/timelapse-manager/_internal/ffmpeg/ffmpeg"})},
            "_internal/ffmpeg/ffmpeg is missing",
        ),
        (
            {"omit": frozenset({"opt/timelapse-manager/_internal/ffmpeg/ffprobe"})},
            "_internal/ffmpeg/ffprobe is missing",
        ),
        ({"extra": {"var/lib/timelapse-manager/.secret-key": b"k"}}, "is not empty"),
        ({"omit": frozenset({"var/lib/timelapse-manager"})}, "directory is missing"),
        ({"extra": {".dockerenv": b""}}, ".dockerenv"),
        ({"machine_id": b"abc123\n"}, "machine-id is not empty"),
        ({"omit": frozenset({"etc/machine-id"})}, "machine-id is missing"),
        ({"extra": {"var/lib/dbus/machine-id": b""}}, "dbus/machine-id"),
        ({"extra": {"etc/ssh/ssh_host_ed25519_key": b"k"}}, "SSH host keys"),
        (
            {
                "symlinks": {
                    "etc/systemd/system/multi-user.target.wants/"
                    "systemd-networkd.service": (
                        "/usr/lib/systemd/system/systemd-networkd.service"
                    )
                }
            },
            "systemd-networkd is enabled",
        ),
        (
            {
                "symlinks": {
                    "etc/systemd/system/network-online.target.wants/"
                    "systemd-networkd-wait-online.service": (
                        "/usr/lib/systemd/system/systemd-networkd-wait-online.service"
                    )
                }
            },
            "systemd-networkd is enabled",
        ),
        (
            {
                "symlinks": {
                    "etc/systemd/system/sockets.target.wants/systemd-networkd.socket": (
                        "/usr/lib/systemd/system/systemd-networkd.socket"
                    )
                }
            },
            "systemd-networkd is enabled",
        ),
        (
            {
                "symlinks": {
                    "etc/systemd/system/dbus-org.freedesktop.network1.service": (
                        "/usr/lib/systemd/system/systemd-networkd.service"
                    )
                }
            },
            "systemd-networkd is enabled",
        ),
        (
            {"omit": frozenset({"mask:systemd-networkd.service"})},
            "not masked: systemd-networkd.service",
        ),
        (
            {"omit": frozenset({"mask:systemd-networkd.socket"})},
            "not masked: systemd-networkd.socket",
        ),
        (
            {"omit": frozenset({"mask:systemd-networkd-wait-online.service"})},
            "not masked: systemd-networkd-wait-online.service",
        ),
        (
            {
                "symlinks": {
                    "etc/systemd/system/systemd-networkd.service": (
                        "/usr/lib/systemd/system/systemd-networkd.service"
                    )
                }
            },
            "not masked: systemd-networkd.service",
        ),
        ({"env": b"TLM_SERVER__BIND_ADDRESS=0.0.0.0\n"}, "does not pin"),
        ({"env": b"# TLM_SERVER__BIND_ADDRESS=127.0.0.1\n"}, "does not pin"),
        (
            {"omit": frozenset({"etc/timelapse-manager/timelapse-manager.env"})},
            "env is missing",
        ),
        (
            {"omit": frozenset({"etc/timelapse-manager/config.yml"})},
            "config.yml is missing",
        ),
    ],
)
def test_each_violation_is_reported(tmp_path: Path, kwargs: dict, needle: str) -> None:
    failures = _failures(_build_tar(tmp_path / "r.tar", **kwargs))
    assert any(needle in f for f in failures), failures


def test_masked_networkd_is_accepted(tmp_path: Path) -> None:
    tar = _build_tar(
        tmp_path / "r.tar",
        symlinks={
            "etc/systemd/system/systemd-networkd-wait-online.service": "/dev/null"
        },
    )
    assert _failures(tar) == []


def test_dockerfile_masks_networkd_and_verifies_it() -> None:
    text = (_LXC / "Dockerfile").read_text(encoding="utf-8")
    for unit in (
        "systemd-networkd.service",
        "systemd-networkd.socket",
        "systemd-networkd-wait-online.service",
    ):
        assert unit in text, unit
    # Masked, not only disabled: first-boot presets re-enable a disabled unit.
    assert "systemctl mask" in text
    # The build fails if any networkd unit is still enabled afterwards.
    assert "find /etc/systemd/system -name 'systemd-networkd*'" in text


def test_env_file_pins_loopback() -> None:
    lines = (_LXC / "timelapse-manager.env").read_text(encoding="utf-8").splitlines()
    active = [ln for ln in lines if ln and not ln.startswith("#")]
    assert "TLM_SERVER__BIND_ADDRESS=127.0.0.1" in active


def test_dockerfile_scrubs_instance_state_and_is_unpinned_by_default() -> None:
    text = (_LXC / "Dockerfile").read_text(encoding="utf-8")
    for needle in (
        "/var/lib/timelapse-manager/*",
        ": > /etc/machine-id",
        "/var/lib/dbus/machine-id",
        "/etc/ssh/ssh_host_",
        "apt-get clean",
        "chown=root:timelapse",
        "chmod=0640",
    ):
        assert needle in text, needle
    # The base must come from the script, never a floating default.
    assert not re.search(r"^ARG BASE_IMAGE=", text, re.MULTILINE)


def test_script_default_base_is_digest_pinned_trixie() -> None:
    text = (_LXC / "build-ct-template.sh").read_text(encoding="utf-8")
    match = re.search(
        r'^DEFAULT_BASE="(debian:trixie@sha256:[0-9a-f]{64})"', text, re.MULTILINE
    )
    assert match
    assert set(match.group(1).split(":")[-1]) != {"0"}
    assert "sha512sum" in text
    assert "type=tar" in text
    assert "check_rootfs.py" in text


def test_script_syntax_is_valid_bash() -> None:
    bash = shutil.which("bash")
    assert bash
    subprocess.run([bash, "-n", str(_LXC / "build-ct-template.sh")], check=True)


def test_script_is_shellcheck_clean() -> None:
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck not installed")
    subprocess.run([shellcheck, str(_LXC / "build-ct-template.sh")], check=True)


def test_script_rejects_bad_arguments(tmp_path: Path) -> None:
    script = str(_LXC / "build-ct-template.sh")
    result = subprocess.run(
        ["bash", script, "--deb", str(tmp_path / "missing.deb"), "--version", "1.0"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "--deb" in result.stderr


def test_ct_env_and_shipped_config_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CT's loopback env line still wins next to the shipped config.yml (#338)."""
    from timelapse_manager.config import load_settings

    shipped = _LXC.parent / "config.yml"
    for line in (
        (_LXC / "timelapse-manager.env").read_text(encoding="utf-8").splitlines()
    ):
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            monkeypatch.setenv(key, value)
    settings = load_settings(str(shipped))
    assert settings.server.bind_address == "127.0.0.1"
    assert str(settings.paths.data_dir) == "/var/lib/timelapse-manager"
