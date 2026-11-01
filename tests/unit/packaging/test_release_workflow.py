"""Static checks on the Gitea release workflow (``.gitea/workflows/release.yml``).

The workflow only runs on tags and by hand, so a mistake in it would otherwise
surface hours into a release. These tests parse the YAML and pin the properties
that matter: the job graph, a time cap on every job and step, publishing guarded
to tag refs, the image gate ordering, SHA256SUMS uploaded last, and the pinned
inputs (build image, uv, nfpm and trivy) staying consistent with the files that
repeat them.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW_PATH = _ROOT / ".gitea" / "workflows" / "release.yml"
if not _WORKFLOW_PATH.is_file():
    # The maintainer's release workflow is not part of the public export.
    pytest.skip("release workflow not present", allow_module_level=True)
_TEXT = _WORKFLOW_PATH.read_text(encoding="utf-8")
_DOC: dict[Any, Any] = yaml.safe_load(_TEXT)
_JOBS: dict[str, dict[str, Any]] = _DOC["jobs"]

_PUBLISH_GUARD = "needs.release.outputs.publish == 'true'"


def _steps(job: str) -> list[dict[str, Any]]:
    return list(_JOBS[job]["steps"])


def _step(job: str, name_prefix: str) -> dict[str, Any]:
    steps = _steps(job)
    matches = [s for s in steps if s.get("name") == name_prefix]
    matches = matches or [
        s for s in steps if str(s.get("name", "")).startswith(name_prefix)
    ]
    assert len(matches) == 1, f"{job}: expected one step named {name_prefix!r}"
    return matches[0]


def _index(job: str, name_prefix: str) -> int:
    step = _step(job, name_prefix)
    return _steps(job).index(step)


def _needs(job: str) -> set[str]:
    needs = _JOBS[job].get("needs", [])
    return {needs} if isinstance(needs, str) else set(needs)


class TestTriggers:
    def test_runs_on_version_tags_and_by_hand(self) -> None:
        # PyYAML reads the bare key `on` as the boolean True.
        triggers = _DOC.get("on", _DOC.get(True))
        assert triggers["push"]["tags"] == ["v*"]
        assert "workflow_dispatch" in triggers

    def test_dispatch_has_a_publish_input_that_defaults_to_a_dry_run(self) -> None:
        triggers = _DOC.get("on", _DOC.get(True))
        publish = triggers["workflow_dispatch"]["inputs"]["publish"]
        assert publish["type"] == "boolean"
        assert publish["default"] is False


class TestJobGraph:
    def test_jobs(self) -> None:
        assert set(_JOBS) == {
            "release",
            "bundle",
            "packages",
            "image",
            "publish-sums",
            "cleanup",
        }

    def test_dependencies(self) -> None:
        assert _needs("release") == set()
        assert _needs("bundle") == {"release"}
        assert _needs("packages") == {"release", "bundle"}
        assert _needs("image") == {"release", "packages"}
        assert _needs("publish-sums") == {"release", "bundle", "packages", "image"}
        assert _needs("cleanup") == {
            "release",
            "bundle",
            "packages",
            "image",
            "publish-sums",
        }

    def test_cleanup_runs_even_when_an_earlier_job_fails(self) -> None:
        assert "always()" in str(_JOBS["cleanup"]["if"])

    def test_image_building_jobs_use_the_docker_label(self) -> None:
        for job in ("bundle", "packages", "image"):
            assert _JOBS[job]["runs-on"] == "docker"
        assert _JOBS["release"]["runs-on"] == "ubuntu-latest"

    def test_existing_test_steps_are_kept(self) -> None:
        runs = " ".join(str(s.get("run", "")) for s in _steps("release"))
        assert "make lint fmt-check typecheck" in runs
        assert "uv run pytest -n auto -rfE --timeout=300" in runs
        assert 'UV_PYTHON: "3.11"' in _TEXT


class TestTimeouts:
    @pytest.mark.parametrize("job", sorted(_JOBS))
    def test_every_job_has_a_cap(self, job: str) -> None:
        assert isinstance(_JOBS[job]["timeout-minutes"], int)

    @pytest.mark.parametrize("job", sorted(_JOBS))
    def test_every_step_has_a_cap(self, job: str) -> None:
        for step in _steps(job):
            assert isinstance(step.get("timeout-minutes"), int), (
                f"{job}: step {step.get('name')!r} has no timeout-minutes"
            )

    @pytest.mark.parametrize("job", sorted(_JOBS))
    def test_step_caps_fit_inside_the_job_cap(self, job: str) -> None:
        # Not a sum (steps rarely all run long), but no single step may outlast
        # its job, or the step cap would be decoration.
        cap = _JOBS[job]["timeout-minutes"]
        for step in _steps(job):
            assert step["timeout-minutes"] <= cap


class TestPublishingIsGuarded:
    def test_publish_output_is_true_only_for_tag_refs(self) -> None:
        run = str(_step("release", "Resolve version")["run"])
        assert "publish=false" in run
        # `publish=true` appears once, inside the refs/tags/v* branch.
        assignments = [m.start() for m in re.finditer(r"^\s*publish=true$", run, re.M)]
        assert len(assignments) == 1
        tag_branch = run.index("refs/tags/v*)")
        other_branch = re.search(r"\n\s*\*\)", run[tag_branch:])
        assert other_branch is not None
        assert assignments[0] < tag_branch + other_branch.start()
        assert tag_branch < assignments[0]
        # A dispatch asking to publish from anywhere but a tag is an error.
        assert "publish=true needs a v* tag ref" in run

    def test_every_registry_mutation_is_behind_the_guard(self) -> None:
        mutating = re.compile(
            r"docker push|docker login|--upload-file"
            r"|registry\.sh put \"\$\{PACKAGE_NAME\}\""
        )
        found = 0
        for job in _JOBS:
            for step in _steps(job):
                if mutating.search(str(step.get("run", ""))):
                    found += 1
                    assert _PUBLISH_GUARD in str(step.get("if", "")), (
                        f"{job}: {step.get('name')!r} publishes without the guard"
                    )
        assert found >= 5

    def test_scratch_uploads_use_the_staging_package_only(self) -> None:
        for job in _JOBS:
            for step in _steps(job):
                for line in str(step.get("run", "")).splitlines():
                    if "registry.sh put" in line and "PACKAGE_NAME" not in line:
                        assert "STAGE_PACKAGE" in line, line

    def test_a_rerun_starts_from_an_empty_scratch_version(self) -> None:
        run = str(_step("release", "SBOM")["run"])
        assert run.index("registry.sh delete generic") < run.index("registry.sh put")

    def test_the_scratch_package_is_always_deleted(self) -> None:
        run = str(_step("cleanup", "Delete the scratch package")["run"])
        assert "registry.sh delete generic" in run

    def test_a_published_version_is_refused_up_front(self) -> None:
        run = str(_step("release", "Check secrets and registry state")["run"])
        for kind in ("generic", "debian", "rpm", "container"):
            assert f'"{kind} ' in run
        assert "packaging/unpublish.sh" in run
        # Before the tests, so it fails in the first minute.
        assert _index("release", "Check secrets") < _index("release", "Tests")


class TestSha256SumsLast:
    def test_only_publish_sums_touches_sha256sums_in_the_registry(self) -> None:
        for job in _JOBS:
            if job == "publish-sums":
                continue
            for step in _steps(job):
                assert "SHA256SUMS" not in str(step.get("run", ""))

    def test_sha256sums_is_the_last_registry_write(self) -> None:
        steps = _steps("publish-sums")
        writes = [
            i
            for i, s in enumerate(steps)
            if re.search(r"--upload-file|registry\.sh put", str(s.get("run", "")))
        ]
        last = steps[writes[-1]]
        assert last["name"] == "Publish SHA256SUMS (last)"
        assert "SHA256SUMS" in last["run"]
        assert len(writes) == 4  # Debian, RPM, generic files, SHA256SUMS

    def test_the_generic_loop_skips_sha256sums(self) -> None:
        run = str(_step("publish-sums", "Publish the files to the generic")["run"])
        assert 'if [ "${f}" = "SHA256SUMS" ]' in run

    def test_it_is_written_over_everything_after_the_gather(self) -> None:
        assert _index("publish-sums", "Gather") < _index(
            "publish-sums", "Write SHA256SUMS"
        )
        run = str(_step("publish-sums", "Write SHA256SUMS")["run"])
        assert "sha256sum --check SHA256SUMS" in run

    @pytest.mark.skipif(shutil.which("sha256sum") is None, reason="needs sha256sum")
    def test_the_step_script_runs_and_lists_each_file_once(
        self, tmp_path: Path
    ) -> None:
        """Run the real script (a loop redirect once made the file list itself)."""
        stage = tmp_path / "stage"
        stage.mkdir()
        (stage / "pkg_1.0~dev1-1_amd64.deb").write_bytes(b"deb")
        (stage / "bundle.tar.gz").write_bytes(b"tar")
        (stage / "SHA256SUMS").write_text("stale leftovers\n")
        run = str(_step("publish-sums", "Write SHA256SUMS")["run"])
        result = subprocess.run(
            ["bash", "-c", run],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        entries = (stage / "SHA256SUMS").read_text().splitlines()
        assert sorted(line.split("  ", 1)[1] for line in entries) == [
            "bundle.tar.gz",
            "pkg_1.0~dev1-1_amd64.deb",
        ]
        assert not (tmp_path / "SHA256SUMS.tmp").exists()

    def test_the_published_set_is_verified_after_the_upload(self) -> None:
        assert _index("publish-sums", "Publish SHA256SUMS") < _index(
            "publish-sums", "Verify the published release"
        )
        run = str(_step("publish-sums", "Verify the published release")["run"])
        assert "sha256sum --check SHA256SUMS" in run


class TestImageGate:
    def test_order_is_build_check_scan_push(self) -> None:
        order = [
            _index("image", "Build the image"),
            _index("image", "Check the image"),
            _index("image", "Scan the image"),
            _index("image", "Push the image"),
        ]
        assert order == sorted(order)

    def test_trivy_blocks_on_fixable_high_and_critical(self) -> None:
        run = str(_step("image", "Scan the image")["run"])
        assert "--severity HIGH,CRITICAL" in run
        assert "--ignore-unfixed" in run
        assert "--exit-code 1" in run
        assert "|| true" not in run
        assert ".trivyignore" in run
        assert (_ROOT / ".trivyignore").is_file()

    def test_the_ct_template_gets_the_same_blocking_scan(self) -> None:
        run = str(_step("packages", "CT template SBOM and scan")["run"])
        assert "--severity HIGH,CRITICAL" in run
        assert "--ignore-unfixed" in run
        assert "--exit-code 1" in run
        assert "|| true" not in run
        assert ".trivyignore" in run

    def test_the_application_sbom_is_scanned_and_blocks(self) -> None:
        # The rootfs scan cannot see Python dependencies (no dist-info in a
        # frozen bundle), so the SBOM is scanned with the same policy.
        run = str(_step("packages", "Scan the application SBOM")["run"])
        assert "trivy sbom" in run
        assert "cyclonedx-bom.json" in run
        assert "--severity HIGH,CRITICAL" in run
        assert "--ignore-unfixed" in run
        assert "--exit-code 1" in run
        assert "|| true" not in run
        assert ".trivyignore" in run
        assert _index("packages", "Scan the application SBOM") < _index(
            "packages", "Stage the packages"
        )

    def test_latest_follows_final_versions_only(self) -> None:
        run = str(_step("image", "Push the image")["run"])
        guard = run.index('if [ "${FINAL}" = "true" ]')
        assert guard < run.index(':latest"')
        assert run.count(":latest") == 2  # the tag and its push, both inside the guard

    def test_the_digest_is_recorded_from_the_registry(self) -> None:
        run = str(_step("image", "Push the image")["run"])
        assert "RepoDigests" in run
        assert "image-digest.txt" in run

    def test_the_pushed_image_is_the_one_that_was_scanned(self) -> None:
        build = str(_step("image", "Build the image")["run"])
        assert "--load" in build
        assert "--push" not in build

    def test_the_build_images_are_cleaned_up_even_on_failure(self) -> None:
        step = _step("image", "Remove the build's images")
        assert step["if"] == "always()"


class TestEl9Bundle:
    _DOCKERFILE = (_ROOT / "packaging" / "bundle" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    def test_build_image_matches_the_dockerfile_default(self) -> None:
        env = _DOC["env"]["EL9_IMAGE"]
        default = re.search(r"^ARG BUILD_IMAGE=(\S+)$", self._DOCKERFILE, re.MULTILINE)
        assert default is not None
        assert default.group(1) == env

    def test_build_image_is_el9_and_pinned_by_digest(self) -> None:
        image = _DOC["env"]["EL9_IMAGE"]
        assert image.startswith("rockylinux/rockylinux:9@sha256:")
        assert re.search(r"@sha256:[0-9a-f]{64}$", image)

    def test_uv_matches_the_application_image_and_the_workflow(self) -> None:
        bundle = re.search(r"^ARG UV_IMAGE=(\S+)$", self._DOCKERFILE, re.MULTILINE)
        app = re.search(
            r"^ARG UV_IMAGE=(\S+)$",
            (_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        assert bundle is not None and app is not None
        assert bundle.group(1) == app.group(1)
        assert bundle.group(1).startswith(
            f"ghcr.io/astral-sh/uv:{_DOC['env']['UV_VERSION']}@"
        )

    def test_the_glibc_floor_is_the_el9_one_and_the_build_enforces_it(self) -> None:
        assert "check-glibc-floor.sh" in self._DOCKERFILE
        assert '"${bundle}" 2.34' in self._DOCKERFILE
        # The verify stage's shell has no pipefail: the gate must not be piped.
        assert not re.search(r"check-glibc-floor\.sh .*\|", self._DOCKERFILE)
        assert (
            "exit 1"
            in self._DOCKERFILE[self._DOCKERFILE.index('check-glibc-floor.sh "') :]
        )
        run = str(_step("bundle", "Build the bundle on EL9")["run"])
        assert "OK: the bundle needs no glibc newer than 2.34" in run
        assert "BUILD_IMAGE=${EL9_IMAGE}" in run

    def test_the_bundle_context_is_an_allowlist(self) -> None:
        ignore = (_ROOT / "packaging" / "bundle" / "Dockerfile.dockerignore").read_text(
            encoding="utf-8"
        )
        lines = [ln for ln in ignore.splitlines() if ln and not ln.startswith("#")]
        assert lines[0] == "*"
        assert "!src" in lines and "!packaging" in lines
        assert not any(ln in ("!.git", "!.env") for ln in lines)


class TestPinnedTools:
    _PINS = json.loads((_ROOT / "packaging" / "tool-pins.json").read_text())

    @pytest.mark.parametrize("tool", ["nfpm", "trivy"])
    def test_pin_is_complete(self, tool: str) -> None:
        pin = self._PINS[tool]
        assert re.fullmatch(r"[0-9a-f]{64}", pin["sha256"])
        assert re.fullmatch(r"\d+\.\d+\.\d+", pin["version"])
        assert pin["version"] in pin["file"]
        assert "mirror_url" not in pin, "mirror_path is relative to the registry"
        assert pin["mirror_path"] == f"ci-tools/{tool}-{pin['version']}/{pin['file']}"
        assert pin["upstream_url"].endswith(pin["file"])

    def test_the_workflow_fetches_only_from_the_mirror(self) -> None:
        assert "fetch-tool.sh nfpm" in _TEXT
        assert "fetch-tool.sh trivy" in _TEXT
        assert "goreleaser" not in _TEXT
        assert "aquasecurity" not in _TEXT
        assert "latest/download" not in _TEXT

    def test_images_and_actions_are_not_floating_in_the_workflow(self) -> None:
        assert "@latest" not in _TEXT
        assert re.search(r"actions/checkout@v4", _TEXT)


class TestSecretsHandling:
    def test_signing_is_required_and_the_key_stays_out_of_the_log(self) -> None:
        assert not re.search(r"\bset\s+-[a-z]*x", _TEXT)
        assert "xtrace" not in _TEXT
        step = _step("packages", "Build and inspect")
        run = str(step["run"])
        assert "umask 077" in run
        assert "trap" in run and "rm -rf" in run
        assert "TLM_RPM_SIGNING_KEY is not set" in run
        assert "NFPM_RPM_PASSPHRASE" in run
        for line in run.splitlines():
            if "echo" in line:
                assert "${RPM_SIGNING" not in line and "$RPM_SIGNING" not in line

    def test_secrets_are_only_the_three_named_ones(self) -> None:
        names = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", _TEXT))
        assert names == {
            "TLM_PACKAGE_TOKEN",
            "TLM_RPM_SIGNING_KEY",
            "TLM_RPM_SIGNING_PASSPHRASE",
        }

    def test_the_docker_login_is_scoped_to_a_scratch_config(self) -> None:
        run = str(_step("image", "Push the image")["run"])
        assert "DOCKER_CONFIG" in run
        assert "--password-stdin" in run


class TestNothingIsPublishedToGithub:
    def test_no_github_destinations(self) -> None:
        # The public image name appears only as metadata for the image SBOM;
        # nothing logs in to, tags or pushes to it.
        assert _TEXT.count("ghcr.io/drcoble") == 1
        assert "${PUBLIC_IMAGE_NAME}" in _TEXT
        for line in _TEXT.splitlines():
            if "PUBLIC_IMAGE_NAME" in line and "ghcr.io" not in line:
                assert "push" not in line and "tag " not in line and "login" not in line
        assert "api.github.com" not in _TEXT
        assert "gh release" not in _TEXT
