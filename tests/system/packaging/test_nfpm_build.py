"""Build the .deb and .rpm from a fake bundle with a real nfpm and inspect them (#303).

Skipped without ``nfpm`` (and ``python3``) on PATH, so it never runs in the
standard suite. It copies the packaging inputs into a scratch tree, so nothing
is written inside the repository, and uses a small fake bundle: the aim is the
package metadata, layout, scriptlets and version, not the real PyInstaller output.
The real bundle is inspected by the release workflow.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_PEP440 = "0.2.0.dev10"

pytestmark = pytest.mark.skipif(
    shutil.which("nfpm") is None or shutil.which("bash") is None,
    reason="needs nfpm and bash on PATH",
)


def _stage_repo(root: Path) -> None:
    """Copy the packaging inputs and lay down a fake frozen bundle."""
    shutil.copytree(
        _REPO / "packaging",
        root / "packaging",
        ignore=shutil.ignore_patterns("build", "__pycache__", "lxc"),
    )
    bundle = root / "dist" / f"timelapse-manager-{_PEP440}-linux-amd64"
    (bundle / "_internal" / "ffmpeg").mkdir(parents=True)
    (bundle / "packaging").mkdir()
    (bundle / "timelapse-manager").write_text("#!/bin/sh\nexit 0\n")
    (bundle / "timelapse-manager").chmod(0o755)
    (bundle / "_internal" / "ffmpeg" / "ffmpeg").write_text("#!/bin/sh\nexit 0\n")
    (bundle / "_internal" / "ffmpeg" / "ffmpeg").chmod(0o755)
    (bundle / "packaging" / "install.sh").write_text("#!/bin/sh\n")


def _build(
    root: Path, version: str = _PEP440, **env: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        # A strict umask, as some CI shells have, must not leak into file modes.
        [
            "bash",
            "-c",
            'umask 077; exec bash "$0" "$@"',
            str(root / "packaging" / "build-linux-packages.sh"),
            version,
            "amd64",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        check=False,
        cwd=root,
        timeout=300,
    )


def _ar_members(path: Path) -> dict[str, bytes]:
    """The members of a ``.deb`` (an ``ar`` archive)."""
    data = path.read_bytes()
    assert data.startswith(b"!<arch>\n")
    members: dict[str, bytes] = {}
    pos = 8
    while pos < len(data):
        name = data[pos : pos + 16].decode().strip().rstrip("/")
        size = int(data[pos + 48 : pos + 58].decode().strip())
        members[name] = data[pos + 60 : pos + 60 + size]
        pos += 60 + size + (size % 2)
    return members


def _tar(member: bytes) -> tarfile.TarFile:
    return tarfile.open(fileobj=io.BytesIO(member), mode="r:*")


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("repo")
    _stage_repo(root)
    result = _build(root)
    assert result.returncode == 0, result.stdout + result.stderr
    return root


class TestDeb:
    def _deb(self, root: Path) -> Path:
        (deb,) = (root / "dist").glob("*.deb")
        return deb

    def test_file_name_carries_the_sortable_version(self, built: Path) -> None:
        assert self._deb(built).name == "timelapse-manager_0.2.0~dev10-1_amd64.deb"

    def test_control_metadata(self, built: Path) -> None:
        control = _tar(_ar_members(self._deb(built))["control.tar.gz"])
        text = control.extractfile("./control").read().decode()  # type: ignore[union-attr]
        assert "Version: 0.2.0~dev10-1" in text
        assert "Maintainer: Drew Coble <drcoble@users.noreply.github.com>" in text
        assert "Homepage: https://github.com/drcoble/Timelapse_Manager" in text
        assert "Depends: passwd, systemd" in text
        assert "Recommends: curl" in text
        names = set(control.getnames())
        assert {"./preinst", "./postinst", "./prerm", "./postrm"} <= names

    def test_payload_layout(self, built: Path) -> None:
        data = _tar(_ar_members(self._deb(built))["data.tar.gz"])
        names = set(data.getnames())
        assert "./usr/lib/systemd/system/timelapse-manager.service" in names
        assert "./opt/timelapse-manager/_internal/ffmpeg/ffmpeg" in names
        assert "./opt/timelapse-manager/timelapse-manager" in names
        # The tarball's installer tree is not shipped in the package.
        assert not [
            n for n in names if n.startswith("./opt/timelapse-manager/packaging")
        ]
        link = data.getmember("./usr/bin/timelapse-manager")
        assert (
            link.issym() and link.linkname == "/opt/timelapse-manager/timelapse-manager"
        )
        state = data.getmember("./var/lib/timelapse-manager")
        assert (state.uname, state.mode & 0o777) == ("timelapse", 0o750)

    def test_unit_is_world_readable_even_under_a_strict_umask(
        self, built: Path
    ) -> None:
        data = _tar(_ar_members(self._deb(built))["data.tar.gz"])
        unit = data.getmember("./usr/lib/systemd/system/timelapse-manager.service")
        assert (unit.uname, unit.gname, unit.mode & 0o777) == ("root", "root", 0o644)

    def test_config_yml_is_a_conffile_with_service_group_access(
        self, built: Path
    ) -> None:
        deb = self._deb(built)
        control = _tar(_ar_members(deb)["control.tar.gz"])
        conffiles = control.extractfile("./conffiles").read().decode().split()  # type: ignore[union-attr]
        assert "/etc/timelapse-manager/config.yml" in conffiles
        data = _tar(_ar_members(deb)["data.tar.gz"])
        config = data.getmember("./etc/timelapse-manager/config.yml")
        assert (config.uname, config.gname, config.mode & 0o777) == (
            "root",
            "timelapse",
            0o640,
        )
        text = data.extractfile(config).read().decode()  # type: ignore[union-attr]
        assert "data_dir: /var/lib/timelapse-manager" in text

    def test_unit_is_rendered_with_the_fixed_layout(self, built: Path) -> None:
        data = _tar(_ar_members(self._deb(built))["data.tar.gz"])
        unit = (
            data.extractfile("./usr/lib/systemd/system/timelapse-manager.service")
            .read()
            .decode()
        )  # type: ignore[union-attr]
        assert not re.search(r"@[A-Z_]+@", unit), "unrendered token left in the unit"
        assert "ExecStart=/opt/timelapse-manager/timelapse-manager run" in unit
        assert "Documentation=https://github.com/drcoble/Timelapse_Manager" in unit

    def test_scriptlets_are_the_repo_scripts(self, built: Path) -> None:
        control = _tar(_ar_members(self._deb(built))["control.tar.gz"])
        preinst = control.extractfile("./preinst").read().decode()  # type: ignore[union-attr]
        assert "useradd" in preinst and "groupadd" in preinst
        postinst = control.extractfile("./postinst").read().decode()  # type: ignore[union-attr]
        assert "/run/systemd/system" in postinst


class TestRpm:
    def _rpm(self, root: Path) -> Path:
        (rpm,) = (root / "dist").glob("*.rpm")
        return rpm

    def test_file_name_carries_the_sortable_version(self, built: Path) -> None:
        assert self._rpm(built).name == "timelapse-manager-0.2.0~dev10-1.x86_64.rpm"

    def test_is_an_rpm_and_unsigned_without_a_key(self, built: Path) -> None:
        yaml = pytest.importorskip("yaml")
        assert self._rpm(built).read_bytes()[:4] == bytes.fromhex("edabeedb")
        rendered = yaml.safe_load((built / "packaging/build/nfpm.yaml").read_text())
        assert rendered["rpm"]["signature"]["key_file"] in (None, "")

    @pytest.mark.skipif(shutil.which("rpm") is None, reason="needs rpm")
    def test_rpm_metadata(self, built: Path) -> None:
        def query(fmt: str) -> str:
            return subprocess.run(
                ["rpm", "-qp", "--qf", fmt, str(self._rpm(built))],
                capture_output=True,
                text=True,
                check=True,
            ).stdout

        assert query("%{VERSION}-%{RELEASE}") == "0.2.0~dev10-1"
        assert query("%{VENDOR}") == "Drew Coble"
        assert query("%{URL}") == "https://github.com/drcoble/Timelapse_Manager"
        requires = subprocess.run(
            ["rpm", "-qp", "--requires", str(self._rpm(built))],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for name in ("shadow-utils", "util-linux", "systemd"):
            assert name in requires
        listing = subprocess.run(
            ["rpm", "-qpl", str(self._rpm(built))],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "/usr/lib/systemd/system/timelapse-manager.service" in listing
        assert "/usr/bin/timelapse-manager" in listing
        assert "/opt/timelapse-manager/packaging" not in listing
        assert "/etc/timelapse-manager/config.yml" in listing
        configs = subprocess.run(
            ["rpm", "-qpc", str(self._rpm(built))],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "/etc/timelapse-manager/config.yml" in configs
        flags = subprocess.run(
            [
                "rpm",
                "-qp",
                "--qf",
                "[%{FILENAMES} %{FILEFLAGS:fflags}\\n]",
                str(self._rpm(built)),
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        (line,) = [
            ln
            for ln in flags.splitlines()
            if ln.startswith("/etc/timelapse-manager/config.yml")
        ]
        assert "n" in line.split()[-1]  # noreplace


class TestBuildScript:
    def test_the_version_argument_is_required(self, built: Path) -> None:
        result = subprocess.run(
            ["bash", str(built / "packaging" / "build-linux-packages.sh")],
            capture_output=True,
            text=True,
            check=False,
            cwd=built,
        )
        assert result.returncode != 0
        assert "usage" in result.stderr

    def test_an_unsupported_version_is_refused(self, built: Path) -> None:
        result = _build(built, "0.2.0a1")
        assert result.returncode != 0
        assert "unsupported version" in result.stderr

    def test_an_unreadable_signing_key_stops_the_build(self, built: Path) -> None:
        result = _build(built, TLM_RPM_SIGNING_KEY_FILE=str(built / "missing.key"))
        assert result.returncode != 0
        assert "TLM_RPM_SIGNING_KEY_FILE" in result.stderr

    def test_a_final_release_has_no_tilde(self, tmp_path: Path) -> None:
        _stage_repo(tmp_path)
        final = tmp_path / "dist" / "timelapse-manager-0.2.0-linux-amd64"
        (tmp_path / "dist" / f"timelapse-manager-{_PEP440}-linux-amd64").rename(final)
        result = _build(tmp_path, "0.2.0")
        assert result.returncode == 0, result.stdout + result.stderr
        names = sorted(
            p.name
            for p in (tmp_path / "dist").glob("*.*")
            if p.suffix in {".deb", ".rpm"}
        )
        assert names == [
            "timelapse-manager-0.2.0-1.x86_64.rpm",
            "timelapse-manager_0.2.0-1_amd64.deb",
        ]
