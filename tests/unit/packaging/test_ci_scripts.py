"""Tests for the release pipeline's helper scripts, with fake ``objdump`` and ``curl``.

Nothing here touches the network or a registry: ``curl`` is a recording stub and
``objdump`` replays text stored in the fake ELF files.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_PKG = _ROOT / "packaging"
_ELF_MAGIC = b"\x7fELF"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _script(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _run(
    argv: list[str], env_extra: dict[str, str], path_dirs: list[Path]
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.pathsep.join([*(str(d) for d in path_dirs), os.environ["PATH"]]),
        "HOME": os.environ.get("HOME", "/tmp"),
        **env_extra,
    }
    return subprocess.run(
        argv, capture_output=True, text=True, env=env, check=False, timeout=60
    )


# ---------------------------------------------------------------------------
# check-glibc-floor.sh
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_objdump(tmp_path: Path) -> Path:
    """An ``objdump -T`` that prints whatever follows the ELF magic in the file."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _script(bin_dir, "objdump", '[ "$1" = "-T" ] || exit 2\ntail -c +5 "$2"')
    return bin_dir


def _elf(path: Path, *versions: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"0000 g DF .text 0000 GLIBC_{v} sym{i}" for i, v in enumerate(versions)]
    path.write_bytes(_ELF_MAGIC + "\n".join(lines).encode())


def _glibc(tree: Path, bin_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run(
        ["bash", str(_PKG / "check-glibc-floor.sh"), str(tree), *args], {}, [bin_dir]
    )


class TestGlibcFloor:
    def test_passes_at_or_below_the_floor(
        self, tmp_path: Path, fake_objdump: Path
    ) -> None:
        tree = tmp_path / "bundle"
        _elf(tree / "timelapse-manager", "2.2.5", "2.17", "2.34")
        _elf(tree / "_internal" / "libz.so.1", "2.4", "2.28")
        result = _glibc(tree, fake_objdump)
        assert result.returncode == 0, result.stderr
        assert "highest GLIBC symbol version required: 2.34" in result.stdout
        assert "OK: the bundle needs no glibc newer than 2.34" in result.stdout

    def test_fails_above_the_floor_and_names_the_file(
        self, tmp_path: Path, fake_objdump: Path
    ) -> None:
        tree = tmp_path / "bundle"
        _elf(tree / "timelapse-manager", "2.17")
        _elf(tree / "_internal" / "ffmpeg" / "ffmpeg", "2.28", "2.35")
        result = _glibc(tree, fake_objdump)
        assert result.returncode == 1
        assert "_internal/ffmpeg/ffmpeg needs GLIBC_2.35" in result.stderr
        assert "highest GLIBC symbol version required: 2.35" in result.stdout

    def test_lists_every_file_above_the_floor(
        self, tmp_path: Path, fake_objdump: Path
    ) -> None:
        tree = tmp_path / "bundle"
        _elf(tree / "_internal" / "libgcc_s.so.1", "2.35")
        _elf(tree / "_internal" / "libother.so", "2.36")
        _elf(tree / "fine", "2.17")
        result = _glibc(tree, fake_objdump)
        assert result.returncode == 1
        assert "_internal/libgcc_s.so.1 needs GLIBC_2.35" in result.stderr
        assert "_internal/libother.so needs GLIBC_2.36" in result.stderr
        assert "fine needs" not in result.stderr

    def test_versions_compare_numerically_not_as_strings(
        self, tmp_path: Path, fake_objdump: Path
    ) -> None:
        tree = tmp_path / "bundle"
        # As text "2.9" > "2.34"; as versions 2.9 is far below the floor.
        _elf(tree / "a", "2.9")
        result = _glibc(tree, fake_objdump)
        assert result.returncode == 0, result.stderr
        assert "required: 2.9 " in result.stdout
        # And 2.100 is above 2.34 even though "2.100" < "2.34" as text.
        _elf(tree / "b", "2.100")
        assert _glibc(tree, fake_objdump).returncode == 1

    def test_glibc_private_and_libstdcxx_are_ignored(
        self, tmp_path: Path, fake_objdump: Path
    ) -> None:
        tree = tmp_path / "bundle"
        _elf(tree / "a", "2.17")
        (tree / "a").write_bytes(
            (tree / "a").read_bytes()
            + b"\n0 g DF GLIBC_PRIVATE x\n0 g DF GLIBCXX_3.4.30 y"
        )
        result = _glibc(tree, fake_objdump)
        assert result.returncode == 0, result.stderr
        assert "required: 2.17 " in result.stdout

    def test_non_elf_files_are_skipped(
        self, tmp_path: Path, fake_objdump: Path
    ) -> None:
        tree = tmp_path / "bundle"
        _elf(tree / "ok", "2.17")
        (tree / "notes.txt").write_text("GLIBC_2.99 appears in prose only")
        assert _glibc(tree, fake_objdump).returncode == 0

    def test_a_scan_that_finds_nothing_fails(
        self, tmp_path: Path, fake_objdump: Path
    ) -> None:
        tree = tmp_path / "empty"
        tree.mkdir()
        (tree / "readme").write_text("no binaries here")
        result = _glibc(tree, fake_objdump)
        assert result.returncode == 1
        assert "no GLIBC symbol versions found" in result.stderr

    def test_needs_nothing_beyond_coreutils_to_recognise_elf_files(
        self, tmp_path: Path, fake_objdump: Path
    ) -> None:
        """The EL9 verify image has no cmp (diffutils): a PATH without it must work."""
        tree = tmp_path / "bundle"
        _elf(tree / "a", "2.17")
        bare = tmp_path / "bare"
        bare.mkdir()
        for tool in (
            "bash",
            "head",
            "od",
            "tr",
            "find",
            "grep",
            "sed",
            "sort",
            "tail",
            "printf",
            "cat",
        ):
            found = shutil.which(tool)
            if found:
                (bare / tool).symlink_to(found)
        (bare / "objdump").symlink_to(fake_objdump / "objdump")
        for tool in ("sh", "env"):
            found = shutil.which(tool)
            if found:
                (bare / tool).symlink_to(found)
        result = subprocess.run(
            ["bash", str(_PKG / "check-glibc-floor.sh"), str(tree)],
            capture_output=True,
            text=True,
            env={"PATH": str(bare)},
            check=False,
        )
        assert result.returncode == 0, result.stderr

    def test_a_custom_floor(self, tmp_path: Path, fake_objdump: Path) -> None:
        tree = tmp_path / "bundle"
        _elf(tree / "a", "2.28")
        assert _glibc(tree, fake_objdump, "2.28").returncode == 0
        assert _glibc(tree, fake_objdump, "2.17").returncode == 1

    def test_a_missing_directory_is_a_usage_error(
        self, tmp_path: Path, fake_objdump: Path
    ) -> None:
        assert _glibc(tmp_path / "nope", fake_objdump).returncode == 2


# ---------------------------------------------------------------------------
# is-final-version.sh
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "final"),
    [
        ("0.2.0", True),
        ("1.0", True),
        ("10.20.30", True),
        ("0.2.0.dev10", False),
        ("0.2.0rc6", False),
        ("0.2.0rc1.dev3", False),
        ("0.2.0.post1", False),
        ("v0.2.0", False),
        ("", False),
    ],
)
def test_is_final_version(version: str, final: bool) -> None:
    result = subprocess.run(
        ["bash", str(_PKG / "ci" / "is-final-version.sh"), version],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is final


# ---------------------------------------------------------------------------
# registry.sh and unpublish.sh, with a recording curl
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_curl(tmp_path: Path) -> tuple[Path, Path]:
    """A ``curl`` that logs its arguments and answers from FAKE_HTTP_CODE."""
    bin_dir = tmp_path / "curlbin"
    bin_dir.mkdir()
    log = tmp_path / "curl.log"
    _script(
        bin_dir,
        "curl",
        'echo "curl $*" >> "$CURL_LOG"\n'
        'case "$*" in\n'
        '  *"-X DELETE"*) printf "%s" "${FAKE_DELETE_CODE:-204}" ;;\n'
        '  *--write-out*) printf "%s" "${FAKE_HTTP_CODE:-200}" ;;\n'
        "esac\n"
        'exit "${FAKE_CURL_RC:-0}"',
    )
    return bin_dir, log


_REGISTRY_ENV = {
    "TLM_REGISTRY_URL": "https://registry.example.com",
    "TLM_REGISTRY_OWNER": "owner",
}


def _registry(
    fake: tuple[Path, Path], *args: str, code: str = "200", token: str | None = "tok"
) -> subprocess.CompletedProcess[str]:
    bin_dir, log = fake
    env = {
        "CURL_LOG": str(log),
        "FAKE_HTTP_CODE": code,
        "FAKE_DELETE_CODE": code,
        **_REGISTRY_ENV,
    }
    if token is not None:
        env["GITEA_TOKEN"] = token
    return _run(["bash", str(_PKG / "ci" / "registry.sh"), *args], env, [bin_dir])


class TestRegistry:
    def test_put_uploads_to_the_generic_registry_with_the_token(
        self, tmp_path: Path, fake_curl: tuple[Path, Path]
    ) -> None:
        payload = tmp_path / "pkg~1.tar.gz"
        payload.write_bytes(b"x")
        result = _registry(
            fake_curl, "put", "timelapse-manager", "0.2.0.dev10", str(payload)
        )
        assert result.returncode == 0, result.stderr
        line = fake_curl[1].read_text()
        assert "-H Authorization: token tok" in line
        assert f"--upload-file {payload}" in line
        assert line.strip().endswith(
            "https://registry.example.com/api/packages/owner/generic/"
            "timelapse-manager/0.2.0.dev10/pkg~1.tar.gz"
        )
        assert "--location" not in line  # a PUT is never followed across redirects

    def test_put_can_rename_the_remote_file(
        self, tmp_path: Path, fake_curl: tuple[Path, Path]
    ) -> None:
        payload = tmp_path / "local.bin"
        payload.write_bytes(b"x")
        _registry(fake_curl, "put", "p", "1", str(payload), "remote.bin")
        assert fake_curl[1].read_text().strip().endswith("/generic/p/1/remote.bin")

    def test_get_downloads_to_the_destination(
        self, tmp_path: Path, fake_curl: tuple[Path, Path]
    ) -> None:
        dest = tmp_path / "out.bin"
        result = _registry(fake_curl, "get", "p", "1.0", "f.bin", str(dest))
        assert result.returncode == 0, result.stderr
        line = fake_curl[1].read_text()
        assert f"--output {dest}" in line
        assert line.strip().endswith("/generic/p/1.0/f.bin")

    @pytest.mark.parametrize(
        ("code", "rc"), [("200", 0), ("404", 1), ("500", 2), ("401", 2)]
    )
    def test_exists_maps_the_status_code(
        self, fake_curl: tuple[Path, Path], code: str, rc: int
    ) -> None:
        result = _registry(
            fake_curl,
            "exists",
            "debian",
            "timelapse-manager",
            "0.2.0~dev10-1",
            code=code,
        )
        assert result.returncode == rc
        assert "/api/v1/packages/owner/debian/timelapse-manager/0.2.0~dev10-1" in (
            fake_curl[1].read_text()
        )

    def test_delete_encodes_plus_and_keeps_tilde(
        self, fake_curl: tuple[Path, Path]
    ) -> None:
        result = _registry(
            fake_curl,
            "delete",
            "rpm",
            "timelapse-manager",
            "0.2.0+post1~x-1",
            code="204",
        )
        assert result.returncode == 0, result.stderr
        line = fake_curl[1].read_text()
        assert "-X DELETE" in line
        assert line.strip().endswith("/rpm/timelapse-manager/0.2.0%2Bpost1~x-1")
        assert "deleted rpm/timelapse-manager" in result.stdout

    def test_delete_of_a_missing_version_is_not_an_error(
        self, fake_curl: tuple[Path, Path]
    ) -> None:
        result = _registry(fake_curl, "delete", "generic", "p", "9", code="404")
        assert result.returncode == 0
        assert "not present" in result.stdout

    def test_a_token_is_required(self, fake_curl: tuple[Path, Path]) -> None:
        result = _registry(fake_curl, "exists", "generic", "p", "1", token=None)
        assert result.returncode == 2
        assert "GITEA_TOKEN" in result.stderr
        assert not fake_curl[1].exists()

    @pytest.mark.parametrize("missing", ["TLM_REGISTRY_URL", "TLM_REGISTRY_OWNER"])
    def test_there_is_no_built_in_registry(
        self, fake_curl: tuple[Path, Path], missing: str
    ) -> None:
        bin_dir, log = fake_curl
        env = {"CURL_LOG": str(log), "GITEA_TOKEN": "t", **_REGISTRY_ENV}
        del env[missing]
        result = _run(
            ["bash", str(_PKG / "ci" / "registry.sh"), "exists", "generic", "p", "1"],
            env,
            [bin_dir],
        )
        assert result.returncode == 2
        assert missing in result.stderr
        assert not log.exists()

    def test_the_base_url_and_owner_are_overridable(
        self, fake_curl: tuple[Path, Path]
    ) -> None:
        bin_dir, log = fake_curl
        env = {
            "CURL_LOG": str(log),
            "GITEA_TOKEN": "t",
            "TLM_REGISTRY_URL": "https://example.test/",
            "TLM_REGISTRY_OWNER": "someone",
            "FAKE_HTTP_CODE": "200",
        }
        _run(
            ["bash", str(_PKG / "ci" / "registry.sh"), "exists", "generic", "p", "1"],
            env,
            [bin_dir],
        )
        assert (
            "https://example.test/api/v1/packages/someone/generic/p/1"
            in log.read_text()
        )


class TestUnpublish:
    def _run(
        self, fake: tuple[Path, Path], *args: str, code: str = "200"
    ) -> subprocess.CompletedProcess[str]:
        bin_dir, log = fake
        env = {
            "CURL_LOG": str(log),
            "GITEA_TOKEN": "tok",
            "FAKE_HTTP_CODE": code,
            **_REGISTRY_ENV,
        }
        return _run(["bash", str(_PKG / "unpublish.sh"), *args], env, [bin_dir])

    def test_without_yes_it_only_reports(self, fake_curl: tuple[Path, Path]) -> None:
        result = self._run(fake_curl, "0.2.0.dev10")
        assert result.returncode == 0, result.stderr
        assert "Dry run" in result.stdout
        assert "present" in result.stdout
        assert "DELETE" not in fake_curl[1].read_text()

    def test_with_yes_it_deletes_every_registry_version(
        self, fake_curl: tuple[Path, Path]
    ) -> None:
        result = self._run(fake_curl, "0.2.0.dev10", "--yes")
        assert result.returncode == 0, result.stderr
        deletes = [
            ln for ln in fake_curl[1].read_text().splitlines() if "-X DELETE" in ln
        ]
        urls = [ln.split()[-1] for ln in deletes]
        base = "https://registry.example.com/api/v1/packages/owner"
        assert urls == [
            f"{base}/container/timelapse-manager/0.2.0.dev10",
            f"{base}/rpm/timelapse-manager/0.2.0~dev10-1",
            f"{base}/debian/timelapse-manager/0.2.0~dev10-1",
            f"{base}/generic/timelapse-manager/0.2.0.dev10",
        ]

    def test_latest_is_left_alone_unless_asked(
        self, fake_curl: tuple[Path, Path]
    ) -> None:
        self._run(fake_curl, "0.2.0", "--yes")
        assert "/container/timelapse-manager/latest" not in fake_curl[1].read_text()
        fake_curl[1].unlink()
        self._run(fake_curl, "0.2.0", "--yes", "--latest")
        assert "/container/timelapse-manager/latest" in fake_curl[1].read_text()

    def test_a_failed_probe_stops_before_deleting(
        self, fake_curl: tuple[Path, Path]
    ) -> None:
        result = self._run(fake_curl, "0.2.0", "--yes", code="500")
        assert result.returncode == 2
        assert "-X DELETE" not in fake_curl[1].read_text()

    def test_a_version_and_a_token_are_required(
        self, fake_curl: tuple[Path, Path]
    ) -> None:
        assert self._run(fake_curl).returncode == 2
        result = _run(["bash", str(_PKG / "unpublish.sh"), "0.2.0"], {}, [fake_curl[0]])
        assert result.returncode == 2
        env = {"GITEA_TOKEN": "tok"}  # no registry settings: no built-in default
        result = _run(
            ["bash", str(_PKG / "unpublish.sh"), "0.2.0"], env, [fake_curl[0]]
        )
        assert result.returncode == 2
        assert "TLM_REGISTRY_URL" in result.stderr


# ---------------------------------------------------------------------------
# fetch-tool.sh
# ---------------------------------------------------------------------------


@pytest.fixture
def tool_fixture(tmp_path: Path) -> dict[str, Path]:
    """A fake mirror payload, a pins file describing it and a curl serving it."""
    payload_dir = tmp_path / "payload"
    payload_dir.mkdir()
    binary = payload_dir / "faketool"
    binary.write_text("#!/bin/sh\necho faketool 1.0\n")
    tarball = tmp_path / "faketool.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        tar.add(binary, arcname="faketool_1.0/faketool")
    sha = hashlib.sha256(tarball.read_bytes()).hexdigest()
    pins = tmp_path / "pins.json"
    pins.write_text(
        json.dumps(
            {
                "faketool": {
                    "version": "1.0",
                    "sha256": sha,
                    "binary": "faketool",
                    "mirror_path": "ci-tools/faketool-1.0/faketool.tar.gz",
                }
            }
        )
    )
    bin_dir = tmp_path / "curlbin"
    bin_dir.mkdir()
    _script(
        bin_dir,
        "curl",
        'out=""\n'
        "while [ $# -gt 0 ]; do\n"
        '  [ "$1" = "--output" ] && out="$2"\n'
        "  shift\n"
        "done\n"
        '[ "${FAKE_CURL_RC:-0}" -eq 0 ] || exit "$FAKE_CURL_RC"\n'
        'cp "$FAKE_PAYLOAD" "$out"',
    )
    return {"bin": bin_dir, "pins": pins, "tarball": tarball}


class TestFetchTool:
    def _fetch(
        self, fx: dict[str, Path], dest: Path, **env: str
    ) -> subprocess.CompletedProcess[str]:
        return _run(
            ["bash", str(_PKG / "ci" / "fetch-tool.sh"), "faketool", str(dest)],
            {
                "GITEA_TOKEN": "tok",
                **_REGISTRY_ENV,
                "TOOL_PINS_FILE": str(fx["pins"]),
                "FAKE_PAYLOAD": str(fx["tarball"]),
                **env,
            },
            [fx["bin"]],
        )

    def test_installs_a_verified_binary(
        self, tmp_path: Path, tool_fixture: dict[str, Path]
    ) -> None:
        dest = tmp_path / "tools"
        result = self._fetch(tool_fixture, dest)
        assert result.returncode == 0, result.stderr
        assert os.access(dest / "faketool", os.X_OK)
        assert "verified" in result.stdout

    def test_refuses_a_sha256_mismatch(
        self, tmp_path: Path, tool_fixture: dict[str, Path]
    ) -> None:
        evil = tmp_path / "evil.tar.gz"
        evil.write_bytes(b"not the pinned tarball")
        result = self._fetch(tool_fixture, tmp_path / "tools", FAKE_PAYLOAD=str(evil))
        assert result.returncode == 1
        assert "sha256 mismatch" in result.stderr
        assert not (tmp_path / "tools" / "faketool").exists()

    def test_a_missing_mirror_points_at_the_seeding_script(
        self, tmp_path: Path, tool_fixture: dict[str, Path]
    ) -> None:
        result = self._fetch(tool_fixture, tmp_path / "tools", FAKE_CURL_RC="22")
        assert result.returncode == 1
        assert "dev/mirror-tools.sh" in result.stderr

    def test_a_token_is_required(
        self, tmp_path: Path, tool_fixture: dict[str, Path]
    ) -> None:
        result = _run(
            [
                "bash",
                str(_PKG / "ci" / "fetch-tool.sh"),
                "faketool",
                str(tmp_path / "t"),
            ],
            {"TOOL_PINS_FILE": str(tool_fixture["pins"]), **_REGISTRY_ENV},
            [tool_fixture["bin"]],
        )
        assert result.returncode == 2

    def test_the_registry_settings_are_required(
        self, tmp_path: Path, tool_fixture: dict[str, Path]
    ) -> None:
        result = _run(
            [
                "bash",
                str(_PKG / "ci" / "fetch-tool.sh"),
                "faketool",
                str(tmp_path / "t"),
            ],
            {"GITEA_TOKEN": "tok", "TOOL_PINS_FILE": str(tool_fixture["pins"])},
            [tool_fixture["bin"]],
        )
        assert result.returncode == 2
        assert "TLM_REGISTRY_URL" in result.stderr

    def test_the_mirror_url_is_built_from_the_registry_settings(
        self, tmp_path: Path, tool_fixture: dict[str, Path]
    ) -> None:
        log = tmp_path / "curl.log"
        bin_dir = tmp_path / "logbin"
        bin_dir.mkdir()
        _script(
            bin_dir,
            "curl",
            'echo "$@" >> "$CURL_LOG"\n'
            'out=""\n'
            'while [ $# -gt 0 ]; do [ "$1" = "--output" ] && out="$2"; shift; done\n'
            'cp "$FAKE_PAYLOAD" "$out"',
        )
        result = _run(
            [
                "bash",
                str(_PKG / "ci" / "fetch-tool.sh"),
                "faketool",
                str(tmp_path / "t"),
            ],
            {
                "GITEA_TOKEN": "tok",
                **_REGISTRY_ENV,
                "TOOL_PINS_FILE": str(tool_fixture["pins"]),
                "FAKE_PAYLOAD": str(tool_fixture["tarball"]),
                "CURL_LOG": str(log),
            },
            [bin_dir],
        )
        assert result.returncode == 0, result.stderr
        assert (
            log.read_text()
            .strip()
            .endswith(
                "https://registry.example.com/api/packages/owner/generic/"
                "ci-tools/faketool-1.0/faketool.tar.gz"
            )
        )

    def test_only_the_mirror_is_ever_contacted(self) -> None:
        text = (_PKG / "ci" / "fetch-tool.sh").read_text()
        assert "upstream_url" not in text
        assert "github.com" not in text


# ---------------------------------------------------------------------------
# Static checks on every new script
# ---------------------------------------------------------------------------

_SCRIPTS = [
    _PKG / "check-glibc-floor.sh",
    _PKG / "unpublish.sh",
    _PKG / "ci" / "registry.sh",
    _PKG / "ci" / "fetch-tool.sh",
    _PKG / "ci" / "inspect-packages.sh",
    _PKG / "ci" / "is-final-version.sh",
    _ROOT / "dev" / "mirror-tools.sh",
]


@pytest.mark.parametrize("script", _SCRIPTS, ids=lambda p: p.name)
def test_script_is_executable_with_strict_mode_and_parses(script: Path) -> None:
    assert os.access(script, os.X_OK), f"{script.name} is not executable"
    text = script.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text
    assert subprocess.run(["bash", "-n", str(script)], check=False).returncode == 0


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="needs shellcheck")
@pytest.mark.parametrize("script", _SCRIPTS, ids=lambda p: p.name)
def test_script_is_shellcheck_clean(script: Path) -> None:
    result = subprocess.run(
        ["shellcheck", "-x", str(script)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout
