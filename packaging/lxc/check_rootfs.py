#!/usr/bin/env python3
"""Listing checks for the Timelapse Manager Proxmox CT template rootfs.

Usage: check_rootfs.py <rootfs.tar | ->    (``-`` reads an uncompressed tar on stdin)

Stdlib only, so the CI step needs nothing installed. Exit status 0 when every
check passes; otherwise each failure is printed to stderr and the status is 1.
``check_rootfs(members, read)`` is the testable core.
"""

from __future__ import annotations

import sys
import tarfile
from collections.abc import Callable, Iterable

STATE_DIR = "var/lib/timelapse-manager"
WANTS_LINK = "etc/systemd/system/multi-user.target.wants/timelapse-manager.service"
# PyInstaller 6 onedir puts the spec's bundle-root ``ffmpeg/`` under
# ``_internal/`` (timelapse-manager.spec; /healthz on 0.2.0.dev10 reports
# /opt/timelapse-manager/_internal/ffmpeg/ffmpeg). release.sh stages ffprobe
# from the same pin, so both are expected.
BUNDLE_BINARIES = (
    "opt/timelapse-manager/_internal/ffmpeg/ffmpeg",
    "opt/timelapse-manager/_internal/ffmpeg/ffprobe",
)
ENV_FILE = "etc/timelapse-manager/timelapse-manager.env"
# Shipped by the .deb as a conffile; without it the CLI cannot find the data dir.
CONFIG_FILE = "etc/timelapse-manager/config.yml"
LOOPBACK_LINE = "TLM_SERVER__BIND_ADDRESS=127.0.0.1"
# networkd units that must be masked, so first-boot presets cannot re-enable them.
NETWORKD_MASKED = (
    "systemd-networkd.service",
    "systemd-networkd.socket",
    "systemd-networkd-wait-online.service",
)


def _norm(name: str) -> str:
    """Strip leading ``./`` and ``/`` and trailing ``/`` so names compare equal."""
    while name.startswith("./"):
        name = name[2:]
    return name.strip("/").removeprefix("./") if name != "." else ""


def check_rootfs(
    members: Iterable[tarfile.TarInfo], read: Callable[[str], bytes | None]
) -> list[str]:
    """Return a list of failure messages (empty when the rootfs is acceptable).

    ``read(name)`` returns the content of a regular file by normalised name, or
    None when it is absent.
    """
    failures: list[str] = []
    by_name: dict[str, tarfile.TarInfo] = {}
    for member in members:
        by_name[_norm(member.name)] = member

    version = read("etc/debian_version")
    if version is None:
        failures.append("etc/debian_version is missing")
    elif not version.decode("utf-8", "replace").strip().startswith("13"):
        failures.append(f"etc/debian_version is not Debian 13: {version!r}")

    link = by_name.get(WANTS_LINK)
    if link is None or not (link.issym() or link.islnk()):
        failures.append(f"{WANTS_LINK} link is missing (service not enabled)")

    for binary in BUNDLE_BINARIES:
        found = by_name.get(binary)
        if found is None or not found.isfile():
            failures.append(f"{binary} is missing (pinned FFmpeg not bundled)")

    leaked = sorted(n for n in by_name if n.startswith(STATE_DIR + "/"))
    if leaked:
        failures.append(f"{STATE_DIR} is not empty: {leaked[:5]}")
    if STATE_DIR not in by_name:
        failures.append(f"{STATE_DIR} directory is missing")

    if ".dockerenv" in by_name:
        failures.append(".dockerenv present (Docker artefact)")

    machine_id = by_name.get("etc/machine-id")
    if machine_id is None:
        failures.append("etc/machine-id is missing (must exist and be empty)")
    elif machine_id.size != 0:
        failures.append("etc/machine-id is not empty")
    if "var/lib/dbus/machine-id" in by_name:
        failures.append("var/lib/dbus/machine-id present")

    keys = sorted(n for n in by_name if n.startswith("etc/ssh/ssh_host_"))
    if keys:
        failures.append(f"SSH host keys present: {keys}")

    config = by_name.get(CONFIG_FILE)
    if config is None or not config.isfile():
        failures.append(f"{CONFIG_FILE} is missing (package conffile not installed)")

    # Proxmox configures networking through ifupdown. An enabled systemd-networkd
    # manages no link, so its wait-online unit times out after 2 minutes and holds
    # up network-online.target, and with it the service, on every boot. The units
    # must be masked (a link to /dev/null): with an empty machine-id the first boot
    # applies the vendor presets, which re-enable a unit that is only disabled.
    for unit in NETWORKD_MASKED:
        mask = by_name.get(f"etc/systemd/system/{unit}")
        if mask is None or not (mask.issym() and mask.linkname == "/dev/null"):
            failures.append(f"systemd-networkd unit is not masked: {unit}")
    for name, member in sorted(by_name.items()):
        if not name.startswith("etc/systemd/system/"):
            continue
        base = name.rsplit("/", 1)[-1]
        if not (
            base.startswith("systemd-networkd")
            or base == "dbus-org.freedesktop.network1.service"
        ):
            continue
        if member.issym() and member.linkname == "/dev/null":
            continue
        if member.isdir():
            continue
        failures.append(f"systemd-networkd is enabled: {name}")

    env = read(ENV_FILE)
    if env is None:
        failures.append(f"{ENV_FILE} is missing")
    else:
        lines = [ln.strip() for ln in env.decode("utf-8", "replace").splitlines()]
        if LOOPBACK_LINE not in lines:
            failures.append(f"{ENV_FILE} does not pin {LOOPBACK_LINE}")
    return failures


def check_tar(path: str) -> list[str]:
    """Run :func:`check_rootfs` over a tar file (or ``-`` for stdin)."""
    if path == "-":
        archive = tarfile.open(fileobj=sys.stdin.buffer, mode="r|")  # noqa: SIM115
    else:
        archive = tarfile.open(path, mode="r:")  # noqa: SIM115
    wanted = {"etc/debian_version", ENV_FILE}
    contents: dict[str, bytes] = {}
    members: list[tarfile.TarInfo] = []
    with archive:
        for member in archive:
            members.append(member)
            if _norm(member.name) in wanted and member.isfile():
                extracted = archive.extractfile(member)
                if extracted is not None:
                    contents[_norm(member.name)] = extracted.read()
    return check_rootfs(members, contents.get)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    failures = check_tar(argv[1])
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    if failures:
        return 1
    print("rootfs checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
