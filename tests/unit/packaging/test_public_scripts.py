"""The public-export and GitHub-publish scripts, exercised without a network.

``dev/export-public.sh`` runs against a throwaway git repository. The publish
script only ever runs with ``--dry-run`` (or fails argument checks) here, with
stub ``curl``/``crane``/``docker`` on PATH that fail the test if they are called.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path

import pytest

from tests.unit.test_public_tree import carchive_bytes, pyz_bytes

_ROOT = Path(__file__).resolve().parents[3]
_EXPORT = _ROOT / "dev" / "export-public.sh"
_PUBLISH = _ROOT / "dev" / "publish-github-release.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="needs bash and git",
)

# The scripts need a private deny-list; these tests use a synthetic one.
_PRIVATE_HOST = "secretcorp.example"
_LIST_DIR = Path(tempfile.mkdtemp(prefix="tlm-guard-list-"))
_LIST = _LIST_DIR / "deny.txt"
_LIST.write_text(
    "deny-bin\tcorp name\tsecretcorp\n"
    "deny\ttest net\t(?<![\\d.])198\\.18\\.\\d{1,3}\\.\\d{1,3}\n",
    encoding="utf-8",
)
# Stand-in for gitleaks, so the export tests do not depend on the real tool.
_GITLEAKS_OK = _LIST_DIR / "gitleaks-ok"
_GITLEAKS_OK.write_text(
    '#!/bin/sh\necho "gitleaks $*" >> "${GITLEAKS_LOG:-/dev/null}"\nexit 0\n',
    encoding="utf-8",
)
_GITLEAKS_OK.chmod(0o755)


def _run(
    argv: list[str],
    env_extra: dict[str, str] | None = None,
    path_dirs: list[Path] | None = None,
) -> subprocess.CompletedProcess[str]:
    path = os.pathsep.join([*(str(d) for d in path_dirs or []), os.environ["PATH"]])
    env = {
        "PATH": path,
        "HOME": os.environ.get("HOME", "/tmp"),
        "TLM_PUBLIC_GUARD_LIST": str(_LIST),
        "TLM_GITLEAKS": str(_GITLEAKS_OK),
        **(env_extra or {}),
    }
    return subprocess.run(
        argv, capture_output=True, text=True, env=env, check=False, timeout=120
    )


def _stub(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def no_network(tmp_path: Path) -> tuple[Path, Path]:
    """Stub network tools that record a call and fail it."""
    bin_dir = tmp_path / "netstubs"
    bin_dir.mkdir()
    log = tmp_path / "network.log"
    for tool in ("curl", "crane", "docker", "gh"):
        _stub(bin_dir, tool, f'echo "{tool} $*" >> "{log}"\nexit 99')
    return bin_dir, log


# ---------------------------------------------------------------------------
# export-public.sh
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            *args,
        ],
        check=True,
        capture_output=True,
    )


def _make_repo(repo: Path, extra: dict[str, str] | None = None) -> None:
    files = {
        "README.md": "# Demo\n",
        "src/app.py": "print('hi')\n",
        ".gitea/workflows/release.yml": "name: private\n",
        "docs/security-audit.md": "internal\n",
        "docs/releases/0.9.0.md": "# Release 0.9.0\n\n## Highlights\n\nnine\n",
        "docs/releases/0.10.0.md": "# Release 0.10.0\n\n## Highlights\n\nten\n",
        "CHANGELOG.md": "internal changelog\n",
        **(extra or {}),
    }
    repo.mkdir()
    _git(repo, "init", "-q")
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "tag", "v0.10.0")


class TestExport:
    def test_a_clean_repo_exports_without_the_private_paths(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        _make_repo(repo)
        out = tmp_path / "out"
        result = _run([str(_EXPORT), "--repo", str(repo), "v0.10.0", str(out)])
        assert result.returncode == 0, result.stdout + result.stderr
        assert (out / "README.md").is_file()
        assert (out / "src" / "app.py").is_file()
        assert not (out / ".gitea").exists()
        assert not (out / "docs" / "security-audit.md").exists()

    def test_the_changelog_is_built_from_the_release_notes_newest_first(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        _make_repo(repo)
        out = tmp_path / "out"
        assert (
            _run([str(_EXPORT), "--repo", str(repo), "v0.10.0", str(out)]).returncode
            == 0
        )
        text = (out / "CHANGELOG.md").read_text()
        assert "internal changelog" not in text
        assert text.index("## 0.10.0") < text.index("## 0.9.0")  # numeric, not lexical
        assert "### Highlights" in text  # headings nest under the version
        assert "# Release 0.10.0" not in text

    def test_the_manifest_is_the_sorted_file_list_beside_the_export(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        _make_repo(repo)
        out = tmp_path / "out"
        assert (
            _run([str(_EXPORT), "--repo", str(repo), "v0.10.0", str(out)]).returncode
            == 0
        )
        manifest = (tmp_path / "out.MANIFEST").read_text().splitlines()
        assert manifest == sorted(manifest)
        assert "CHANGELOG.md" in manifest
        assert "src/app.py" in manifest
        assert ".gitea/workflows/release.yml" not in manifest
        assert not (out / "MANIFEST").exists()

    def test_a_private_detail_fails_the_export(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _make_repo(repo, {"src/cfg.py": f"HOST = '{_PRIVATE_HOST}'\n"})
        result = _run(
            [str(_EXPORT), "--repo", str(repo), "v0.10.0", str(tmp_path / "out")]
        )
        assert result.returncode == 1
        assert "src/cfg.py:1:" in result.stdout

    def test_private_paths_are_not_scanned_because_they_are_dropped(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        _make_repo(
            repo,
            {
                ".gitea/workflows/x.yml": f"host: {_PRIVATE_HOST}\n",
                "docs/security-audit.md": f"{_PRIVATE_HOST}\n",
            },
        )
        result = _run(
            [str(_EXPORT), "--repo", str(repo), "v0.10.0", str(tmp_path / "out")]
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_missing_release_notes_fail_clearly(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _make_repo(repo)
        _git(repo, "rm", "-q", "-r", "docs/releases")
        _git(repo, "commit", "-q", "-m", "drop notes")
        _git(repo, "tag", "v0.11.0")
        result = _run(
            [str(_EXPORT), "--repo", str(repo), "v0.11.0", str(tmp_path / "out")]
        )
        assert result.returncode != 0
        assert "docs/releases" in result.stderr

    def test_gitleaks_findings_fail_the_export(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _make_repo(repo)
        failing = _stub(tmp_path, "gitleaks-fail", "echo leak found\nexit 1")
        result = _run(
            [str(_EXPORT), "--repo", str(repo), "v0.10.0", str(tmp_path / "out")],
            {"TLM_GITLEAKS": str(failing)},
        )
        assert result.returncode == 1
        assert "leak found" in result.stdout

    def test_gitleaks_runs_inside_the_export_with_its_ignore_file(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        _make_repo(repo, {".gitleaksignore": "a/b.py:generic-api-key:3\n"})
        log = tmp_path / "gitleaks.log"
        result = _run(
            [str(_EXPORT), "--repo", str(repo), "v0.10.0", str(tmp_path / "out")],
            {"GITLEAKS_LOG": str(log)},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        line = log.read_text()
        assert "--no-git" in line and "--source ." in line
        assert "--gitleaks-ignore-path .gitleaksignore" in line
        assert (tmp_path / "out" / ".gitleaksignore").is_file()

    def test_a_missing_gitleaks_fails_unless_allowed(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _make_repo(repo)
        env = {"TLM_GITLEAKS": str(tmp_path / "no-such-gitleaks")}
        result = _run(
            [str(_EXPORT), "--repo", str(repo), "v0.10.0", str(tmp_path / "out")], env
        )
        assert result.returncode == 1
        assert "gitleaks is not installed" in result.stderr
        result = _run(
            [
                str(_EXPORT),
                "--repo",
                str(repo),
                "--allow-no-gitleaks",
                "v0.10.0",
                str(tmp_path / "out2"),
            ],
            env,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "--allow-no-gitleaks" in result.stderr

    def test_the_private_list_is_required(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _make_repo(repo)
        result = _run(
            [str(_EXPORT), "--repo", str(repo), "v0.10.0", str(tmp_path / "out")],
            {"TLM_PUBLIC_GUARD_LIST": str(tmp_path / "none.txt")},
        )
        assert result.returncode == 2
        assert "private deny-list is missing" in result.stderr
        assert not (tmp_path / "out").exists()

    def test_bad_arguments(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _make_repo(repo)
        assert _run([str(_EXPORT)]).returncode == 2
        assert (
            _run(
                [str(_EXPORT), "--repo", str(repo), "nope", str(tmp_path / "o")]
            ).returncode
            == 2
        )
        busy = tmp_path / "busy"
        busy.mkdir()
        (busy / "x").write_text("x")
        assert (
            _run([str(_EXPORT), "--repo", str(repo), "v0.10.0", str(busy)]).returncode
            == 2
        )


# ---------------------------------------------------------------------------
# publish-github-release.sh (dry run only)
# ---------------------------------------------------------------------------

_VERSION = "0.2.0"
_DIGEST = "sha256:" + "ab" * 32


def _targz(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def _make_assets(directory: Path, version: str = _VERSION, **extra: bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    _targz(
        directory / f"timelapse-manager-{version}-linux-amd64.tar.gz",
        {
            "timelapse-manager-x/readme.txt": b"hello\n",
            **{k: v for k, v in extra.items() if k.startswith("timelapse-manager-x/")},
        },
    )
    (directory / f"timelapse-manager_{version}-1_amd64.deb").write_bytes(
        b"!<arch>\nnot a real deb"
    )
    (directory / f"timelapse-manager-{version}-1.x86_64.rpm").write_bytes(
        b"not a real rpm"
    )
    ct = f"debian-13-timelapse-manager_{version}_amd64.tar.zst"
    src = directory.parent / "ct-src"
    src.mkdir(exist_ok=True)
    (src / "etc").mkdir(exist_ok=True)
    (src / "etc" / "hostname").write_text("ct\n")
    subprocess.run(
        ["tar", "--zstd", "-cf", str(directory / ct), "-C", str(src), "etc"], check=True
    )
    (directory / f"{ct}.sha512").write_text("0" * 128 + f"  {ct}\n")
    for name in ("cyclonedx-bom.json", "sbom-image.cdx.json", "sbom-lxc.cdx.json"):
        (directory / name).write_text(
            extra.get(name, b"{}").decode()
            if isinstance(extra.get(name), bytes)
            else "{}"
        )
    (directory / "image-digest.txt").write_text(f"{_DIGEST}\n")
    (directory / "timelapse-manager-rpm-signing-key.asc").write_text("key\n")
    sums = [
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n"
        for p in sorted(directory.iterdir())
        if p.name != "SHA256SUMS"
    ]
    (directory / "SHA256SUMS").write_text("".join(sums))
    return directory


def _resum(assets: Path) -> None:
    sums = [
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n"
        for p in sorted(assets.iterdir())
        if p.name != "SHA256SUMS"
    ]
    (assets / "SHA256SUMS").write_text("".join(sums))


def _notes(tmp_path: Path, text: str = "# Notes\n\nA release.\n") -> Path:
    path = tmp_path / "notes.md"
    path.write_text(text, encoding="utf-8")
    return path


needs_zstd = pytest.mark.skipif(
    subprocess.run(
        ["tar", "--zstd", "-cf", os.devnull, "-T", os.devnull],
        capture_output=True,
        check=False,
    ).returncode
    != 0,
    reason="tar cannot write zstd",
)


class TestPublishArguments:
    def test_a_tag_is_required(self) -> None:
        result = _run([str(_PUBLISH), "--dry-run"])
        assert result.returncode == 2
        assert "usage" in result.stderr

    @pytest.mark.parametrize("tag", ["0.2.0", "main", "v1..2", "v0.2.0;rm"])
    def test_a_malformed_tag_is_refused(self, tag: str, tmp_path: Path) -> None:
        result = _run(
            [str(_PUBLISH), "--dry-run", "--notes", str(_notes(tmp_path)), tag]
        )
        assert result.returncode == 2
        assert "tag must look like" in result.stderr

    def test_unknown_options_and_missing_notes_are_refused(
        self, tmp_path: Path
    ) -> None:
        assert _run([str(_PUBLISH), "--frobnicate", "v0.2.0"]).returncode == 2
        result = _run(
            [str(_PUBLISH), "--dry-run", "--notes", str(tmp_path / "none.md"), "v0.2.0"]
        )
        assert result.returncode == 2
        assert "release notes not found" in result.stderr

    def test_the_private_list_is_required(
        self, tmp_path: Path, no_network: tuple[Path, Path]
    ) -> None:
        result = _run(
            [str(_PUBLISH), "--dry-run", "--notes", str(_notes(tmp_path)), "v0.2.0"],
            {"TLM_PUBLIC_GUARD_LIST": str(tmp_path / "none.txt")},
            [no_network[0]],
        )
        assert result.returncode == 2
        assert "private deny-list is missing" in result.stderr
        assert not no_network[1].exists()

    def test_a_real_run_needs_both_token_files(
        self, tmp_path: Path, no_network: tuple[Path, Path]
    ) -> None:
        assets = _make_assets(tmp_path / "assets") if shutil.which("zstd") else tmp_path
        result = _run(
            [
                str(_PUBLISH),
                "--assets-dir",
                str(assets),
                "--notes",
                str(_notes(tmp_path)),
                "v0.2.0",
            ],
            path_dirs=[no_network[0]],
        )
        assert result.returncode == 2
        assert "GITHUB_TOKEN_FILE" in result.stderr
        assert not no_network[1].exists()

    def test_a_token_file_must_exist(
        self, tmp_path: Path, no_network: tuple[Path, Path]
    ) -> None:
        result = _run(
            [str(_PUBLISH), "--notes", str(_notes(tmp_path)), "v0.2.0"],
            {
                "GITHUB_TOKEN_FILE": str(tmp_path / "missing"),
                "GHCR_TOKEN_FILE": str(tmp_path / "missing"),
            },
            [no_network[0]],
        )
        assert result.returncode == 2
        assert "does not point at a file" in result.stderr

    def test_a_download_run_needs_the_registry_settings(
        self, tmp_path: Path, no_network: tuple[Path, Path]
    ) -> None:
        result = _run(
            [str(_PUBLISH), "--dry-run", "--notes", str(_notes(tmp_path)), "v0.2.0"],
            path_dirs=[no_network[0]],
        )
        assert result.returncode == 2
        assert "TLM_REGISTRY_URL" in result.stderr
        assert not no_network[1].exists()


@needs_zstd
class TestPublishDryRun:
    def _dry(
        self,
        tmp_path: Path,
        assets: Path,
        net: tuple[Path, Path],
        *extra: str,
        tag: str = "v0.2.0",
    ) -> subprocess.CompletedProcess[str]:
        return _run(
            [
                str(_PUBLISH),
                "--dry-run",
                "--no-package-contents",
                "--assets-dir",
                str(assets),
                "--notes",
                str(_notes(tmp_path)),
                *extra,
                tag,
            ],
            path_dirs=[net[0]],
        )

    def test_the_plan_lists_every_write_without_making_any(
        self, tmp_path: Path, no_network: tuple[Path, Path]
    ) -> None:
        assets = _make_assets(tmp_path / "assets")
        result = self._dry(tmp_path, assets, no_network)
        assert result.returncode == 0, result.stdout + result.stderr
        plan = [ln for ln in result.stdout.splitlines() if ln.startswith("PLAN:")]
        uploads = [
            ln.removeprefix("PLAN: upload ")
            for ln in plan
            if ln.startswith("PLAN: upload ")
        ]
        assert uploads[-1] == "SHA256SUMS"  # the sums go up last
        assert f"timelapse-manager-{_VERSION}-linux-amd64.tar.gz" in uploads
        assert "image-digest.txt" in uploads
        assert any("create a draft release" in ln for ln in plan)
        assert "PLAN: publish the release" in plan
        copies = [ln for ln in plan if "copy " in ln]
        assert any(ln.endswith("timelapse_manager:0.2.0") for ln in copies)
        assert any(
            ln.endswith("timelapse_manager:latest") for ln in copies
        )  # a final version
        assert f"@{_DIGEST} ->" in copies[0]
        assert "nothing was written to GitHub or ghcr" in result.stdout
        assert not no_network[1].exists(), "a network tool was called"

    def test_a_prerelease_does_not_move_latest(
        self, tmp_path: Path, no_network: tuple[Path, Path]
    ) -> None:
        assets = _make_assets(tmp_path / "assets", version="0.2.0.dev14")
        result = self._dry(tmp_path, assets, no_network, tag="v0.2.0.dev14")
        assert result.returncode == 0, result.stdout + result.stderr
        assert ":latest" not in result.stdout
        assert "prerelease: true" in result.stdout

    def test_an_image_reference_in_the_digest_file_is_refused(
        self, tmp_path: Path, no_network: tuple[Path, Path]
    ) -> None:
        assets = _make_assets(tmp_path / "assets")
        (assets / "image-digest.txt").write_text(
            f"registry.example.com/o/i@{_DIGEST}\n"
        )
        sums = [
            f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n"
            for p in sorted(assets.iterdir())
            if p.name != "SHA256SUMS"
        ]
        (assets / "SHA256SUMS").write_text("".join(sums))
        result = self._dry(tmp_path, assets, no_network)
        assert result.returncode == 2
        assert "only a sha256:<digest>" in result.stderr

    def test_a_checksum_mismatch_stops_everything(
        self, tmp_path: Path, no_network: tuple[Path, Path]
    ) -> None:
        assets = _make_assets(tmp_path / "assets")
        (assets / "cyclonedx-bom.json").write_text('{"tampered": true}')
        result = self._dry(tmp_path, assets, no_network)
        assert result.returncode == 2
        assert "checksum mismatch" in result.stderr
        assert "PLAN:" not in result.stdout

    def test_an_unlisted_file_stops_everything(
        self, tmp_path: Path, no_network: tuple[Path, Path]
    ) -> None:
        assets = _make_assets(tmp_path / "assets")
        (assets / "extra.bin").write_text("x")
        result = self._dry(tmp_path, assets, no_network)
        assert result.returncode == 2
        assert "not covered by SHA256SUMS" in result.stderr

    def test_a_missing_asset_stops_everything(
        self, tmp_path: Path, no_network: tuple[Path, Path]
    ) -> None:
        assets = _make_assets(tmp_path / "assets")
        name = "sbom-lxc.cdx.json"
        (assets / name).unlink()
        lines = [
            ln
            for ln in (assets / "SHA256SUMS").read_text().splitlines()
            if not ln.endswith(name)
        ]
        (assets / "SHA256SUMS").write_text("\n".join(lines) + "\n")
        result = self._dry(tmp_path, assets, no_network)
        assert result.returncode == 2
        assert "missing asset" in result.stderr

    def test_a_private_detail_in_the_release_notes_stops_everything(
        self, tmp_path: Path, no_network: tuple[Path, Path]
    ) -> None:
        assets = _make_assets(tmp_path / "assets")
        notes = _notes(tmp_path, f"see {_PRIVATE_HOST}\n")
        result = _run(
            [
                str(_PUBLISH),
                "--dry-run",
                "--no-package-contents",
                "--assets-dir",
                str(assets),
                "--notes",
                str(notes),
                "v0.2.0",
            ],
            path_dirs=[no_network[0]],
        )
        assert result.returncode == 2
        assert "private details found" in result.stderr
        assert "PLAN:" not in result.stdout

    def test_a_private_detail_inside_the_tarball_stops_everything(
        self, tmp_path: Path, no_network: tuple[Path, Path]
    ) -> None:
        assets = _make_assets(tmp_path / "assets")
        _targz(
            assets / f"timelapse-manager-{_VERSION}-linux-amd64.tar.gz",
            {"timelapse-manager-x/conf.txt": f"host {_PRIVATE_HOST}\n".encode()},
        )
        sums = [
            f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n"
            for p in sorted(assets.iterdir())
            if p.name != "SHA256SUMS"
        ]
        (assets / "SHA256SUMS").write_text("".join(sums))
        result = self._dry(tmp_path, assets, no_network)
        assert result.returncode == 2
        assert "conf.txt:1:" in result.stdout

    @staticmethod
    def _bundle_assets(tmp_path: Path, members: dict[str, bytes]) -> Path:
        assets = _make_assets(tmp_path / "assets")
        _targz(
            assets / f"timelapse-manager-{_VERSION}-linux-amd64.tar.gz",
            {f"timelapse-manager-x/{k}": v for k, v in members.items()},
        )
        _resum(assets)
        return assets

    @staticmethod
    def _exe(own: bytes, third_party: bytes = b"\xe3 nothing") -> bytes:
        modules = {"timelapse_manager.mod": own, "somelib.mod": third_party}
        return carchive_bytes(
            [("PYZ.pyz", b"z", pyz_bytes(modules))], lead=b"\x7fELF\0\0"
        )

    def test_a_private_name_in_the_compressed_pyinstaller_code_is_a_finding(
        self, tmp_path: Path, no_network: tuple[Path, Path]
    ) -> None:
        exe = self._exe(f"\xe3 host {_PRIVATE_HOST} 198.18.4.5".encode())
        assert _PRIVATE_HOST.encode() not in exe  # a raw byte scan cannot see it
        assets = self._bundle_assets(tmp_path, {"timelapse-manager": exe})
        result = self._dry(tmp_path, assets, no_network)
        assert result.returncode == 2
        assert "timelapse-manager!PYZ.pyz/timelapse_manager.mod: [corp name]" in (
            result.stdout
        )
        assert "[test net]" in result.stdout

    def test_short_patterns_stay_off_third_party_binaries_and_libraries(
        self, tmp_path: Path, no_network: tuple[Path, Path]
    ) -> None:
        exe = self._exe(b"\xe3 fine", third_party=b"\xe3 version 198.18.4.5")
        assets = self._bundle_assets(
            tmp_path,
            {
                "timelapse-manager": exe,
                "tzdata/Sydney": b"TZif2\0\0 198.18.4.5 \0",
                "lib/native.so": b"\x7fELF\0 198.18.9.9 \0",
            },
        )
        result = self._dry(tmp_path, assets, no_network)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "scans clean" in result.stdout

    def test_a_short_pattern_in_our_own_binary_file_is_a_finding(
        self, tmp_path: Path, no_network: tuple[Path, Path]
    ) -> None:
        blob = b"\x7fELF\0 198.18.4.5 \0"
        assets = self._bundle_assets(
            tmp_path,
            {
                "_internal/timelapse_manager/data.bin": blob,
                "_internal/tzdata/zone": blob,
            },
        )
        result = self._dry(tmp_path, assets, no_network)
        assert result.returncode == 2
        assert "timelapse_manager/data.bin: [test net] at byte" in result.stdout
        assert "tzdata" not in result.stdout

    def test_a_long_identifier_in_a_third_party_binary_is_a_finding(
        self, tmp_path: Path, no_network: tuple[Path, Path]
    ) -> None:
        blob = b"\x7fELF\0 " + _PRIVATE_HOST.encode() + b" \0"
        assets = self._bundle_assets(tmp_path, {"lib/native.so": blob})
        result = self._dry(tmp_path, assets, no_network)
        assert result.returncode == 2
        assert "lib/native.so: [corp name] at byte" in result.stdout

    def test_a_pem_body_is_not_a_finding(
        self, tmp_path: Path, no_network: tuple[Path, Path]
    ) -> None:
        pem = (
            b"-----BEGIN CERTIFICATE-----\n"
            b"xx198.18.4.5xxAAAAAAAAAAAAAAAAAAAA\n"
            b"-----END CERTIFICATE-----\n"
        )
        assets = self._bundle_assets(tmp_path, {"certifi/cacert.pem": pem})
        result = self._dry(tmp_path, assets, no_network)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_private_name_in_the_ct_template_stops_everything(
        self, tmp_path: Path, no_network: tuple[Path, Path]
    ) -> None:
        assets = _make_assets(tmp_path / "assets")
        src = tmp_path / "ct-src2"
        (src / "etc").mkdir(parents=True)
        (src / "etc" / f"{_PRIVATE_HOST}.conf").write_text("x\n")
        ct = assets / f"debian-13-timelapse-manager_{_VERSION}_amd64.tar.zst"
        subprocess.run(
            ["tar", "--zstd", "-cf", str(ct), "-C", str(src), "etc"], check=True
        )
        sums = [
            f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n"
            for p in sorted(assets.iterdir())
            if p.name != "SHA256SUMS"
        ]
        (assets / "SHA256SUMS").write_text("".join(sums))
        result = self._dry(tmp_path, assets, no_network)
        assert result.returncode == 2
        assert "[corp name]" in result.stdout

    def test_tokens_are_never_printed(
        self, tmp_path: Path, no_network: tuple[Path, Path]
    ) -> None:
        assets = _make_assets(tmp_path / "assets")
        gh = tmp_path / "gh.token"
        gh.write_text("ghp_SECRETVALUE123\n")
        result = _run(
            [
                str(_PUBLISH),
                "--dry-run",
                "--no-package-contents",
                "--assets-dir",
                str(assets),
                "--notes",
                str(_notes(tmp_path)),
                "v0.2.0",
            ],
            {"GITHUB_TOKEN_FILE": str(gh), "GHCR_TOKEN_FILE": str(gh)},
            [no_network[0]],
        )
        assert result.returncode == 0, result.stderr
        assert "SECRETVALUE" not in result.stdout + result.stderr


def _ar(path: Path, members: dict[str, bytes]) -> None:
    """Write a minimal ``ar`` archive (the container of a .deb)."""
    out = bytearray(b"!<arch>\n")
    for name, data in members.items():
        header = f"{name:<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n"
        out += header.encode() + data + (b"\n" if len(data) % 2 else b"")
    path.write_bytes(bytes(out))


def _make_deb(path: Path, files: dict[str, bytes]) -> None:
    """A genuine minimal .deb: debian-binary, control.tar.gz and data.tar.gz."""

    def tgz(members: dict[str, bytes]) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return buf.getvalue()

    control = b"Package: timelapse-manager\nVersion: 0\nArchitecture: amd64\n"
    control += b"Maintainer: x <x@example.com>\nDescription: t\n"
    _ar(
        path,
        {
            "debian-binary": b"2.0\n",
            "control.tar.gz": tgz({"./control": control}),
            "data.tar.gz": tgz({f"./{k}": v for k, v in files.items()}),
        },
    )


def _reader_stubs(tmp_path: Path, **bodies: str) -> Path:
    """Stub package readers; each keyword is a tool name and its shell body."""
    bin_dir = tmp_path / "readers"
    bin_dir.mkdir(exist_ok=True)
    defaults = {
        "dpkg-deb": 'echo "-rw-r--r-- root/root 1 2026-01-01 00:00 ./usr/bin/tool"',
        "rpm": 'echo "/usr/bin/tool"',
        "rpm2cpio": "cat /dev/null",
        "cpio": "cat > /dev/null",
    }
    for name, body in {**defaults, **bodies}.items():
        _stub(bin_dir, name, body)
    return bin_dir


@needs_zstd
class TestPublishPackageReaders:
    def _run_publish(
        self,
        tmp_path: Path,
        assets: Path,
        net: tuple[Path, Path],
        readers: Path,
        *extra: str,
    ) -> subprocess.CompletedProcess[str]:
        return _run(
            [
                str(_PUBLISH),
                "--dry-run",
                "--assets-dir",
                str(assets),
                "--notes",
                str(_notes(tmp_path)),
                *extra,
                "v0.2.0",
            ],
            path_dirs=[net[0], readers],
        )

    def _assets(self, tmp_path: Path, files: dict[str, bytes] | None = None) -> Path:
        assets = _make_assets(tmp_path / "assets")
        _make_deb(
            assets / f"timelapse-manager_{_VERSION}-1_amd64.deb",
            files or {"usr/bin/tool": b"x"},
        )
        _resum(assets)
        return assets

    def test_no_package_contents_skips_every_package_tool(
        self, tmp_path: Path, no_network: tuple[Path, Path]
    ) -> None:
        assets = self._assets(tmp_path)
        # Corrupt packages and readers that always fail: nothing may be called.
        (assets / f"timelapse-manager_{_VERSION}-1_amd64.deb").write_bytes(b"junk")
        _resum(assets)
        readers = _reader_stubs(
            tmp_path,
            **dict.fromkeys(
                ("dpkg-deb", "rpm", "rpm2cpio", "cpio"),
                "echo reader called >&2\nexit 9",
            ),
        )
        result = self._run_publish(
            tmp_path, assets, no_network, readers, "--no-package-contents"
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "reader called" not in result.stderr
        assert result.stderr.count("not scanned (--no-package-contents)") == 2
        assert "timelapse-manager_0.2.0-1_amd64.deb not scanned" in result.stderr
        assert "timelapse-manager-0.2.0-1.x86_64.rpm not scanned" in result.stderr

    def test_a_genuine_clean_package_scans_clean(
        self, tmp_path: Path, no_network: tuple[Path, Path]
    ) -> None:
        assets = self._assets(tmp_path)
        result = self._run_publish(
            tmp_path, assets, no_network, _reader_stubs(tmp_path)
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "scans clean" in result.stdout

    def test_a_private_name_inside_a_package_is_a_finding(
        self, tmp_path: Path, no_network: tuple[Path, Path]
    ) -> None:
        assets = self._assets(
            tmp_path, {"etc/app.conf": f"host {_PRIVATE_HOST}\n".encode()}
        )
        result = self._run_publish(
            tmp_path, assets, no_network, _reader_stubs(tmp_path)
        )
        assert result.returncode == 2
        assert "private details found" in result.stderr
        assert "[corp name]" in result.stdout

    def test_a_corrupt_deb_is_a_read_error_not_a_finding(
        self, tmp_path: Path, no_network: tuple[Path, Path]
    ) -> None:
        assets = self._assets(tmp_path)
        (assets / f"timelapse-manager_{_VERSION}-1_amd64.deb").write_bytes(b"junk")
        _resum(assets)
        readers = _reader_stubs(
            tmp_path,
            **{"dpkg-deb": "echo 'unexpected end of file in archive' >&2\nexit 2"},
        )
        result = self._run_publish(tmp_path, assets, no_network, readers)
        assert result.returncode == 3
        assert "could not read timelapse-manager_0.2.0-1_amd64.deb" in result.stderr
        assert "unexpected end of file" in result.stderr
        assert "private details found" not in result.stderr

    def test_a_corrupt_deb_without_dpkg_deb_is_still_a_read_error(
        self, tmp_path: Path, no_network: tuple[Path, Path]
    ) -> None:
        assets = self._assets(tmp_path)
        (assets / f"timelapse-manager_{_VERSION}-1_amd64.deb").write_bytes(b"junk")
        _resum(assets)
        readers = _reader_stubs(tmp_path)
        (readers / "dpkg-deb").unlink()
        result = self._run_publish(tmp_path, assets, no_network, readers)
        assert result.returncode == 3
        assert "could not read timelapse-manager_0.2.0-1_amd64.deb" in result.stderr

    def test_a_failing_rpm_reader_names_the_rpm(
        self, tmp_path: Path, no_network: tuple[Path, Path]
    ) -> None:
        assets = self._assets(tmp_path)
        readers = _reader_stubs(
            tmp_path,
            rpm2cpio="echo 'not an rpm' >&2\nexit 1",
            rpm="echo 'bad rpm' >&2\nexit 1",
        )
        result = self._run_publish(tmp_path, assets, no_network, readers)
        assert result.returncode == 3
        assert "could not read timelapse-manager-0.2.0-1.x86_64.rpm" in result.stderr
        assert "scanning timelapse-manager-0.2.0-1.x86_64.rpm" in result.stdout
        assert ".deb" not in result.stderr.replace(
            "timelapse-manager_0.2.0-1_amd64.deb not", ""
        )


def test_the_publish_script_has_no_unconditional_write_before_the_dry_run_exit() -> (
    None
):
    text = _PUBLISH.read_text()
    exit_at = text.index('if [ "${dry_run}" -eq 1 ]; then')
    for needle in (
        "-X POST",
        "-X PATCH",
        "crane copy",
        "imagetools create",
        "docker login",
    ):
        assert needle not in text[:exit_at], needle
    assert "set -x" not in text


@pytest.mark.parametrize("script", [_EXPORT, _PUBLISH], ids=lambda p: p.name)
def test_scripts_are_executable_strict_and_parse(script: Path) -> None:
    assert os.access(script, os.X_OK)
    text = script.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text
    assert subprocess.run(["bash", "-n", str(script)], check=False).returncode == 0


def test_no_gh_curl_call_reads_its_body_from_stdin() -> None:
    """gh_curl feeds the auth header to curl on stdin, so a body on stdin is lost."""
    script = (
        Path(__file__).resolve().parents[3] / "dev" / "publish-github-release.sh"
    ).read_text(encoding="utf-8")
    code = [ln for ln in script.splitlines() if not ln.lstrip().startswith("#")]
    for line in code:
        if "gh_curl" in line:
            assert "@-" not in line, line
            assert "| gh_curl" not in line.replace("|  gh_curl", "| gh_curl"), line
