"""The registry settings, the FFmpeg mirror hand-off and the GitHub workflows.

The packaging scripts have no built-in registry: the maintainer's workflow
(``.gitea/workflows``, not part of the public export) must supply it, and must
hand the private FFmpeg mirror to the builds. These tests pin that wiring, and
check that the public workflows only verify and never build or publish.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[3]
_GITEA = _ROOT / ".gitea" / "workflows"
_GITHUB = _ROOT / ".github" / "workflows"
_PACKAGING = _ROOT / "packaging"

_needs_gitea = pytest.mark.skipif(not _GITEA.is_dir(), reason="no maintainer workflows")


def _load(path: Path) -> dict[Any, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The maintainer's workflows set the registry
# ---------------------------------------------------------------------------


@_needs_gitea
class TestGiteaWorkflowsSetTheRegistry:
    def test_release_sets_the_registry_url_and_owner(self) -> None:
        env = _load(_GITEA / "release.yml")["env"]
        assert str(env["TLM_REGISTRY_URL"]).startswith("https://")
        assert str(env["TLM_REGISTRY_OWNER"])
        # The image reference and the registry host agree with the registry URL.
        host = str(env["TLM_REGISTRY_URL"]).removeprefix("https://")
        assert env["REGISTRY_HOST"] == host
        assert str(env["IMAGE_NAME"]).startswith(f"{host}/{env['TLM_REGISTRY_OWNER']}/")

    def test_the_old_variable_names_are_gone(self) -> None:
        for path in _GITEA.glob("*.yml"):
            text = path.read_text(encoding="utf-8")
            assert "GITEA_BASE_URL" not in text, path.name
            assert "PACKAGE_OWNER" not in text, path.name
            assert "GITEA_OWNER" not in text, path.name

    def test_the_ffmpeg_refresh_sets_the_registry_for_apply(self) -> None:
        doc = _load(_GITEA / "ffmpeg-refresh.yml")
        steps = doc["jobs"]["refresh"]["steps"]
        apply_step = next(s for s in steps if s.get("name") == "Apply FFmpeg update")
        assert str(apply_step["env"]["TLM_REGISTRY_URL"]).startswith("https://")
        assert apply_step["env"]["TLM_REGISTRY_OWNER"]
        assert "GITEA_TOKEN" in apply_step["env"]

    def test_every_script_run_by_the_release_has_its_registry_env(self) -> None:
        # registry.sh, fetch-tool.sh and the ffmpeg mirror helper read the
        # workflow-level env, so it must not be shadowed per job.
        doc = _load(_GITEA / "release.yml")
        for name, job in doc["jobs"].items():
            for key in ("TLM_REGISTRY_URL", "TLM_REGISTRY_OWNER"):
                assert key not in job.get("env", {}), f"{name} overrides {key}"

    def test_published_metadata_is_neutral(self) -> None:
        text = (_GITEA / "release.yml").read_text(encoding="utf-8")
        # image-digest.txt is published as built: the bare digest only.
        assert 'echo "${digest_ref##*@}" > dist/image-digest.txt' in text
        assert 'echo "${digest_ref}" > dist/image-digest.txt' not in text
        env = _load(_GITEA / "release.yml")["env"]
        assert str(env["PUBLIC_IMAGE_NAME"]).startswith("ghcr.io/")
        assert '--rename "${IMAGE_NAME}=${PUBLIC_IMAGE_NAME}"' in text
        assert "--component-name timelapse-manager-ct-template" in text
        # The text assets are scanned before SHA256SUMS is written.
        scan = text.index("Check the text assets for private details")
        assert scan < text.index("- name: Write SHA256SUMS")
        assert "tests/unit/test_public_tree.py --scan" in text

    def test_the_builds_get_the_private_ffmpeg_mirror(self) -> None:
        text = (_GITEA / "release.yml").read_text(encoding="utf-8")
        # Derived from the pin, computed into a variable first so a failure stops
        # the step instead of silently building without the mirror.
        assert (
            text.count(
                'mirror_url="$(python3 packaging/refresh-ffmpeg-pin.py --mirror-url)"'
            )
            == 2
        )
        assert text.count('--build-arg "TLM_FFMPEG_MIRROR_URL=${mirror_url}"') == 2
        assert 'url="$(python3 packaging/refresh-ffmpeg-pin.py --mirror-url)"' in text
        assert 'mirror_urls"][0]' not in text


# ---------------------------------------------------------------------------
# No built-in registry anywhere in the shipped scripts
# ---------------------------------------------------------------------------

_SCRIPT_FILES = [
    *sorted(_PACKAGING.rglob("*.sh")),
    _PACKAGING / "refresh-ffmpeg-pin.py",
    _PACKAGING / "tool-pins.json",
    _PACKAGING / "nfpm.yaml",
    _ROOT / "dev" / "mirror-tools.sh",
    _ROOT / "dev" / "export-public.sh",
    _ROOT / "dev" / "publish-github-release.sh",
    _ROOT / "Makefile",
    _ROOT / "ffmpeg-pin.json",
]


@pytest.mark.parametrize("path", _SCRIPT_FILES, ids=lambda p: p.name)
def test_no_private_registry_default(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "GITEA_BASE_URL" not in text
    assert "PACKAGE_OWNER" not in text
    assert "GITEA_OWNER" not in text
    assert not re.search(r"\$\{TLM_REGISTRY_(URL|OWNER):-[^}]", text), (
        "the registry settings must not have defaults"
    )


# ---------------------------------------------------------------------------
# FFmpeg pin
# ---------------------------------------------------------------------------


class TestFfmpegPin:
    def test_the_committed_pin_lists_only_public_builds(self) -> None:
        pin = json.loads((_ROOT / "ffmpeg-pin.json").read_text(encoding="utf-8"))
        # The public dependency release comes first (BtbN prunes dated
        # autobuilds); the upstream URL stays as a fallback. No private mirror.
        assert pin["mirror_urls"][0].startswith(
            "https://github.com/drcoble/Timelapse_Manager/releases/download/ffmpeg-"
        )
        assert pin["url"] in pin["mirror_urls"]
        assert pin["url"].startswith("https://github.com/BtbN/FFmpeg-Builds/releases/")
        assert all(u.startswith("https://github.com/") for u in pin["mirror_urls"])

    def _mirror_url(self, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-I",
                str(_PACKAGING / "refresh-ffmpeg-pin.py"),
                "--mirror-url",
            ],
            capture_output=True,
            text=True,
            env={"PATH": os.environ["PATH"], **env},
            check=False,
            timeout=60,
        )

    def test_the_mirror_url_is_derived_from_the_pin_and_the_registry(self) -> None:
        pin = json.loads((_ROOT / "ffmpeg-pin.json").read_text(encoding="utf-8"))
        result = self._mirror_url(
            TLM_REGISTRY_URL="https://registry.example.com/", TLM_REGISTRY_OWNER="owner"
        )
        assert result.returncode == 0, result.stderr
        filename = pin["url"].rsplit("/", 1)[-1]
        assert result.stdout.strip() == (
            f"https://registry.example.com/api/packages/owner/generic/ffmpeg-static/"
            f"{pin['version']}-{pin['sha256'][:8]}/{filename}"
        )

    def test_the_mirror_url_needs_the_registry_settings(self) -> None:
        result = self._mirror_url()
        assert result.returncode == 1
        assert "TLM_REGISTRY_URL" in result.stderr

    def test_release_sh_and_the_dockerfile_try_the_injected_mirror_first(self) -> None:
        release = (_PACKAGING / "release.sh").read_text(encoding="utf-8")
        docker = (_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
        bundle = (_PACKAGING / "bundle" / "Dockerfile").read_text(encoding="utf-8")
        assert 'os.environ.get("TLM_FFMPEG_MIRROR_URL", "")' in release
        assert '"${TLM_FFMPEG_MIRROR_URL:-}")' in release  # the authenticated branch
        assert 'os.environ.get("TLM_FFMPEG_MIRROR_URL", "")' in docker
        assert '"${TLM_FFMPEG_MIRROR_URL:-}")' in docker
        assert re.search(r"^ARG TLM_FFMPEG_MIRROR_URL=", docker, re.M)
        assert re.search(r"^ARG TLM_FFMPEG_MIRROR_URL=", bundle, re.M)

    def test_the_token_only_goes_to_the_mirror_url(self) -> None:
        for rel in ("packaging/release.sh", "docker/Dockerfile"):
            text = (_ROOT / rel).read_text(encoding="utf-8")
            auth_lines = [
                ln for ln in text.splitlines() if "Authorization: token" in ln
            ]
            assert auth_lines, rel
        release = (_PACKAGING / "release.sh").read_text(encoding="utf-8")
        branch = release.index('"${TLM_FFMPEG_MIRROR_URL:-}")')
        assert release.index("Authorization: token") > branch
        assert release.index("Authorization: token") < release.index(
            "            *)\n", branch
        )


# ---------------------------------------------------------------------------
# The public GitHub workflows
# ---------------------------------------------------------------------------

_PINNED = re.compile(r"uses:\s*\S+@[0-9a-f]{40}\b")


class TestGithubWorkflows:
    def test_actions_are_pinned_by_sha(self) -> None:
        for path in _GITHUB.glob("*.yml"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if re.search(r"^\s*-?\s*uses:", line):
                    assert _PINNED.search(line), f"{path.name}: {line.strip()}"

    def test_release_workflow_only_verifies(self) -> None:
        path = _GITHUB / "release.yml"
        text = path.read_text(encoding="utf-8")
        doc = _load(path)
        triggers = doc.get("on", doc.get(True))
        assert triggers["release"]["types"] == ["published"]
        assert "push" not in triggers
        perms = doc["permissions"]
        assert perms == {"contents": "read", "packages": "read"}
        assert "GITEA_TOKEN" not in text
        for forbidden in (
            "docker build",
            "buildx build",
            "docker push",
            "release.sh",
            "cosign",
        ):
            assert forbidden not in text, forbidden
        for required in (
            "sha256sum --check SHA256SUMS",
            "rpm --dbpath",
            "-K",
            "dpkg-deb --info",
            "image-digest.txt",
            "/healthz",
        ):
            assert required in text, required

    def test_the_verify_job_pulls_the_image_by_digest(self) -> None:
        text = (_GITHUB / "release.yml").read_text(encoding="utf-8")
        assert "${IMAGE_REPO}@${digest}" in text
        assert "ghcr.io/drcoble/timelapse_manager" in text

    def test_ci_caps_fit_the_current_suite(self) -> None:
        jobs = _load(_GITHUB / "ci.yml")["jobs"]
        assert jobs["quality"]["timeout-minutes"] >= 90
        assert jobs["browser-e2e"]["timeout-minutes"] >= 45
        text = (_GITHUB / "ci.yml").read_text(encoding="utf-8")
        assert "~5 min" not in text
