"""Static checks on ``docker/Dockerfile`` (no Docker needed).

The image is only built on the release runner, so these tests guard the
properties that would otherwise surface as a failed release build: no
placeholder digests, every base pinned, uv matching the CI toolchain, and the
FFmpeg version read from ``ffmpeg-pin.json`` rather than hardcoded.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_DOCKERFILE = (_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
# The maintainer's release workflow is not part of the public export; the checks
# that read it skip when it is absent.
_WORKFLOW_PATH = _ROOT / ".gitea" / "workflows" / "release.yml"
_WORKFLOW = (
    _WORKFLOW_PATH.read_text(encoding="utf-8") if _WORKFLOW_PATH.is_file() else ""
)

_DIGEST = re.compile(r"@sha256:([0-9a-f]{64})\b")
_ARG_IMAGE = re.compile(r"^ARG\s+(\w+_IMAGE)=(\S+)\s*$", re.MULTILINE)
_FROM = re.compile(
    r"^FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?\s*$", re.MULTILINE
)


def _image_args() -> dict[str, str]:
    return dict(_ARG_IMAGE.findall(_DOCKERFILE))


def test_every_base_image_arg_is_pinned_by_a_real_digest() -> None:
    args = _image_args()
    assert set(args) == {"PYTHON_IMAGE", "FFMPEG_BASE_IMAGE", "UV_IMAGE"}
    for name, value in args.items():
        match = _DIGEST.search(value)
        assert match, f"{name} is not pinned by digest: {value}"
        assert set(match.group(1)) != {"0"}, f"{name} uses the all-zeros placeholder"


def test_no_all_zeros_digest_anywhere() -> None:
    assert "0" * 64 not in _DOCKERFILE


def test_every_from_is_pinned_or_a_pinned_arg_or_an_earlier_stage() -> None:
    args = _image_args()
    stages: set[str] = set()
    froms = _FROM.findall(_DOCKERFILE)
    assert froms
    for ref, alias in froms:
        arg = re.fullmatch(r"\$\{(\w+)\}", ref)
        if arg:
            assert arg.group(1) in args, f"FROM {ref} uses an undeclared ARG"
        else:
            assert ref in stages or _DIGEST.search(ref), f"FROM {ref} is not pinned"
        if alias:
            stages.add(alias)


def test_bases_are_debian_13() -> None:
    args = _image_args()
    assert "trixie" in args["PYTHON_IMAGE"]
    assert "trixie" in args["FFMPEG_BASE_IMAGE"]
    assert "bookworm" not in _DOCKERFILE


@pytest.mark.skipif(not _WORKFLOW, reason="release workflow not present")
def test_uv_version_matches_the_release_workflow() -> None:
    workflow = re.search(r'^\s*UV_VERSION:\s*"([^"]+)"', _WORKFLOW, re.MULTILINE)
    assert workflow, "UV_VERSION not found in .gitea/workflows/release.yml"
    image = _image_args()["UV_IMAGE"]
    assert image.startswith(f"ghcr.io/astral-sh/uv:{workflow.group(1)}@sha256:")


def test_builder_copies_uv_from_the_pinned_stage() -> None:
    assert "COPY --from=uv /uv /uvx" in _DOCKERFILE
    assert not re.search(r"COPY --from=ghcr\.io/\S+:\d", _DOCKERFILE)


def test_ffmpeg_version_is_read_from_the_pin_not_hardcoded() -> None:
    assert "COPY ffmpeg-pin.json" in _DOCKERFILE
    assert "/stage/ffmpeg-pin.json" in _DOCKERFILE
    assert not re.search(r"\bn\d+\.\d+(\.\d+)?\b", _DOCKERFILE), (
        "FFmpeg version hardcoded"
    )
    assert not re.search(r"https?://\S*ffmpeg\S*\.tar", _DOCKERFILE)


def test_oci_labels_come_from_build_args() -> None:
    for key, arg in (
        ("version", "VERSION"),
        ("revision", "VCS_REF"),
        ("created", "BUILD_DATE"),
    ):
        assert f'org.opencontainers.image.{key}="${{{arg}}}"' in _DOCKERFILE
        assert re.search(rf"^ARG {arg}=", _DOCKERFILE, re.MULTILINE)
    assert "org.opencontainers.image.source=" in _DOCKERFILE


def test_compose_and_makefile_use_the_public_image() -> None:
    compose = (_ROOT / "docker" / "docker-compose.yml").read_text(encoding="utf-8")
    makefile = (_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "image: ghcr.io/drcoble/timelapse_manager:" in compose
    assert "IMAGE_NAME ?= ghcr.io/drcoble/timelapse_manager" in makefile
    assert "bookworm" not in makefile


def test_from_lines_carry_no_constant_platform_flag() -> None:
    # BuildKit warns (FromPlatformFlagConstDisallowed) about a hard-coded
    # --platform on FROM; the build command passes --platform linux/amd64.
    assert "--platform" not in " ".join(re.findall(r"^FROM .*$", _DOCKERFILE, re.M))
    ct = (_ROOT / "packaging" / "lxc" / "Dockerfile").read_text(encoding="utf-8")
    assert "--platform" not in " ".join(re.findall(r"^FROM .*$", ct, re.M))
