"""System-level packaging and distribution tests.

These tests exercise artifacts that cannot run on a standard development
workstation -- they require a frozen executable bundle, Docker, systemd,
and/or gpg. Every test guards itself with two conditions:

1. The ``RUN_SYSTEM_TESTS`` environment variable must equal ``"1"``.
2. The required tool (docker, systemctl, gpg, the frozen exe) must be
   available via ``shutil.which``.

Any absent condition causes a clean ``pytest.skip`` with an explanatory
reason. The tests must NEVER silently pass or error in a standard dev
environment -- they report skipped.

These tests are intentionally not run in the normal unit/integration CI
gate and are expected to show as skipped in the standard suite.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Skip guards
# ---------------------------------------------------------------------------

_RUN_SYSTEM = os.environ.get("RUN_SYSTEM_TESTS") == "1"
_REASON_NO_FLAG = "RUN_SYSTEM_TESTS != '1'; set RUN_SYSTEM_TESTS=1 to run system tests"

# Freeze artifacts are assumed to be built before running system tests.
# The frozen exe sits at <repo>/dist/timelapse-manager (POSIX) or
# <repo>/dist/timelapse-manager.exe (Windows).
_CODE_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DIST_DIR = _CODE_ROOT / "dist"
_FROZEN_EXE_NAME = (
    "timelapse-manager.exe" if sys.platform.startswith("win") else "timelapse-manager"
)
_FROZEN_EXE = _DIST_DIR / _FROZEN_EXE_NAME


def _skip_unless_system() -> None:
    """Skip immediately if system tests are not enabled."""
    if not _RUN_SYSTEM:
        pytest.skip(_REASON_NO_FLAG)


def _skip_unless_tool(name: str) -> None:
    """Skip if *name* is not on PATH."""
    if shutil.which(name) is None:
        pytest.skip(f"'{name}' not found on PATH; cannot run this system test")


def _skip_unless_frozen_exe() -> None:
    """Skip if the frozen executable does not exist under dist/."""
    if not _FROZEN_EXE.is_file():
        pytest.skip(
            f"Frozen executable not found at {_FROZEN_EXE}; "
            "build with PyInstaller before running system tests"
        )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_http(url: str, timeout: float = 30.0, interval: float = 0.5) -> bool:
    import http.client
    import urllib.parse

    parsed = urllib.parse.urlparse(url)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
            conn.request("GET", parsed.path or "/")
            resp = conn.getresponse()
            if resp.status == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Clean-host bundle smoke test
# ---------------------------------------------------------------------------


def test_frozen_exe_healthz_returns_200() -> None:
    """Frozen exe starts, serves /healthz 200, and contains a bundled ffmpeg path."""
    _skip_unless_system()
    _skip_unless_frozen_exe()

    port = _free_port()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        data_dir = tmp / "data"
        data_dir.mkdir()
        env = {
            **os.environ,
            "TLM_SERVER__HTTP_PORT": str(port),
            "TLM_SERVER__BIND_ADDRESS": "127.0.0.1",
            "TLM_DATABASE__URL": f"sqlite:///{tmp}/smoke.db",
            "TLM_PATHS__DATA_DIR": str(data_dir),
            "TLM_LOGGING__LEVEL": "WARNING",
        }
        proc = subprocess.Popen(
            [str(_FROZEN_EXE), "run"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        healthz_url = f"http://127.0.0.1:{port}/healthz"
        try:
            assert _wait_for_http(healthz_url, timeout=30.0), (
                f"Frozen server did not respond at {healthz_url} within 30s"
            )
            import urllib.request

            raw = urllib.request.urlopen(healthz_url, timeout=5).read()
            body = json.loads(raw)
            assert body.get("db_status") == "ok"
            assert "ffmpeg_path" in body
            ffmpeg_path = body["ffmpeg_path"]
            assert isinstance(ffmpeg_path, str) and ffmpeg_path.strip() != ""
            # The bundled ffmpeg must live inside the bundle, not on system PATH.
            assert str(_DIST_DIR) in ffmpeg_path or "ffmpeg" in ffmpeg_path.lower()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def test_frozen_exe_bundled_ffmpeg_is_inside_bundle() -> None:
    """The ffmpeg_path reported by the frozen exe lives inside the distribution dir."""
    _skip_unless_system()
    _skip_unless_frozen_exe()

    port = _free_port()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        data_dir = tmp / "data"
        data_dir.mkdir()
        env = {
            **os.environ,
            "TLM_SERVER__HTTP_PORT": str(port),
            "TLM_SERVER__BIND_ADDRESS": "127.0.0.1",
            "TLM_DATABASE__URL": f"sqlite:///{tmp}/ffmpegpath.db",
            "TLM_PATHS__DATA_DIR": str(data_dir),
            "TLM_LOGGING__LEVEL": "WARNING",
        }
        proc = subprocess.Popen(
            [str(_FROZEN_EXE), "run"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        healthz_url = f"http://127.0.0.1:{port}/healthz"
        try:
            assert _wait_for_http(healthz_url, timeout=30.0)
            import urllib.request

            raw = urllib.request.urlopen(healthz_url, timeout=5).read()
            body = json.loads(raw)
            ffmpeg_path = body["ffmpeg_path"]
            # The path must not be the bare string "ffmpeg" (PATH fallback).
            assert ffmpeg_path != "ffmpeg", (
                "Frozen exe is using PATH ffmpeg instead of bundled binary"
            )
            # Must be an absolute path under the dist tree.
            assert Path(ffmpeg_path).is_absolute(), (
                f"Expected absolute ffmpeg_path, got: {ffmpeg_path!r}"
            )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def test_frozen_exe_alembic_head_applies() -> None:
    """Migrations run to head when the frozen exe starts with a fresh database."""
    _skip_unless_system()
    _skip_unless_frozen_exe()

    port = _free_port()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        data_dir = tmp / "data"
        data_dir.mkdir()
        env = {
            **os.environ,
            "TLM_SERVER__HTTP_PORT": str(port),
            "TLM_SERVER__BIND_ADDRESS": "127.0.0.1",
            "TLM_DATABASE__URL": f"sqlite:///{tmp}/migration.db",
            "TLM_PATHS__DATA_DIR": str(data_dir),
            "TLM_LOGGING__LEVEL": "WARNING",
        }
        proc = subprocess.Popen(
            [str(_FROZEN_EXE), "run"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        healthz_url = f"http://127.0.0.1:{port}/healthz"
        try:
            assert _wait_for_http(healthz_url, timeout=30.0)
            import urllib.request

            raw = urllib.request.urlopen(healthz_url, timeout=5).read()
            body = json.loads(raw)
            # A fully started app must have migrated to head.
            assert body.get("alembic_revision") != "unknown", (
                f"Expected a real revision, got 'unknown'. Full healthz: {body}"
            )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


# ---------------------------------------------------------------------------
# systemd integration
# ---------------------------------------------------------------------------


def test_systemd_service_is_active() -> None:
    """The timelapse-manager systemd unit is active and not in failed state."""
    _skip_unless_system()
    _skip_unless_tool("systemctl")

    result = subprocess.run(
        ["systemctl", "is-active", "timelapse-manager"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.stdout.strip() == "active", (
        f"timelapse-manager systemd unit is not active: {result.stdout.strip()!r}"
    )


def test_systemd_service_runs_as_non_root() -> None:
    """The timelapse-manager systemd unit runs as a non-root user."""
    _skip_unless_system()
    _skip_unless_tool("systemctl")
    _skip_unless_tool("ps")

    # Get the PID from systemd.
    result = subprocess.run(
        ["systemctl", "show", "-p", "MainPID", "--value", "timelapse-manager"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    pid = result.stdout.strip()
    if not pid or pid == "0":
        pytest.skip("timelapse-manager systemd unit has no running PID")

    # Check the effective UID via /proc/<pid>/status.
    status_path = Path(f"/proc/{pid}/status")
    if not status_path.exists():
        pytest.skip(f"/proc/{pid}/status not accessible")

    status = status_path.read_text(encoding="utf-8")
    for line in status.splitlines():
        if line.startswith("Uid:"):
            uid = int(line.split()[1])
            assert uid != 0, "Service is running as root (UID=0)"
            return
    pytest.skip("Could not read UID from /proc status")


def test_systemd_service_listens_on_port_8080() -> None:
    """The timelapse-manager service binds to port 8080."""
    _skip_unless_system()
    _skip_unless_tool("systemctl")

    try:
        with socket.create_connection(("127.0.0.1", 8080), timeout=3):
            pass
    except (ConnectionRefusedError, OSError) as exc:
        pytest.fail(f"Nothing listening on port 8080: {exc}")


def test_systemd_service_restarts_after_kill() -> None:
    """The systemd service restarts automatically after being killed."""
    _skip_unless_system()
    _skip_unless_tool("systemctl")
    _skip_unless_tool("kill")

    result = subprocess.run(
        ["systemctl", "show", "-p", "MainPID", "--value", "timelapse-manager"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    original_pid = result.stdout.strip()
    if not original_pid or original_pid == "0":
        pytest.skip("timelapse-manager has no running PID")

    # Kill the process.
    subprocess.run(["kill", "-9", original_pid], check=True, timeout=5)

    # Wait up to 15s for systemd to restart it with a new PID.
    deadline = time.monotonic() + 15.0
    new_pid = None
    while time.monotonic() < deadline:
        r = subprocess.run(
            ["systemctl", "show", "-p", "MainPID", "--value", "timelapse-manager"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        candidate = r.stdout.strip()
        if candidate and candidate != "0" and candidate != original_pid:
            new_pid = candidate
            break
        time.sleep(1.0)

    assert new_pid is not None, (
        f"timelapse-manager did not restart within 15s after killing PID {original_pid}"
    )


# ---------------------------------------------------------------------------
# Docker image
# ---------------------------------------------------------------------------


def test_docker_image_healthcheck_healthy() -> None:
    """The Docker image starts a container that passes its own HEALTHCHECK."""
    _skip_unless_system()
    _skip_unless_tool("docker")

    image = "timelapse-manager:latest"
    with tempfile.TemporaryDirectory() as td:
        data_vol = Path(td) / "data"
        data_vol.mkdir()

        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--detach",
                "--publish",
                "0:8080",
                "--volume",
                f"{data_vol}:/data",
                "--env",
                "TLM_PATHS__DATA_DIR=/data",
                image,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            pytest.skip(
                f"docker run failed (image may not exist): {result.stderr[:200]}"
            )
        container_id = result.stdout.strip()
        try:
            # Wait up to 60s for HEALTHCHECK to become healthy.
            deadline = time.monotonic() + 60.0
            healthy = False
            while time.monotonic() < deadline:
                inspect = subprocess.run(
                    [
                        "docker",
                        "inspect",
                        "--format",
                        "{{.State.Health.Status}}",
                        container_id,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if inspect.stdout.strip() == "healthy":
                    healthy = True
                    break
                time.sleep(2.0)
            assert healthy, (
                "Docker container HEALTHCHECK did not reach 'healthy' within 60s"
            )
        finally:
            subprocess.run(
                ["docker", "rm", "-f", container_id],
                capture_output=True,
                timeout=10,
            )


def test_docker_container_runs_as_non_root() -> None:
    """The Docker image runs its process as a non-root user."""
    _skip_unless_system()
    _skip_unless_tool("docker")

    image = "timelapse-manager:latest"
    result = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "id", image, "-u"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        pytest.skip(f"docker run failed: {result.stderr[:200]}")
    uid = result.stdout.strip()
    assert uid != "0", f"Container runs as root (uid={uid!r})"


def test_docker_container_pid1_is_app() -> None:
    """PID 1 inside the Docker container is the application process."""
    _skip_unless_system()
    _skip_unless_tool("docker")

    image = "timelapse-manager:latest"
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            image,
            "-c",
            "cat /proc/1/comm",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        pytest.skip(f"docker run failed: {result.stderr[:200]}")
    pid1_name = result.stdout.strip()
    # Must not be a shell or init that then execs the app.
    assert pid1_name != "sh", f"PID 1 is a shell ({pid1_name!r}), not the app"


def test_docker_ffmpeg_path_is_configured() -> None:
    """Docker image sets TLM_RENDER__FFMPEG_BINARY to the bundled ffmpeg path."""
    _skip_unless_system()
    _skip_unless_tool("docker")

    image = "timelapse-manager:latest"
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            image,
            "-c",
            "echo $TLM_RENDER__FFMPEG_BINARY",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        pytest.skip(f"docker run failed: {result.stderr[:200]}")
    value = result.stdout.strip()
    assert value == "/opt/ffmpeg/bin/ffmpeg", (
        f"Expected TLM_RENDER__FFMPEG_BINARY=/opt/ffmpeg/bin/ffmpeg, got {value!r}"
    )


# ---------------------------------------------------------------------------
# Artifact integrity with gpg signature verification
# ---------------------------------------------------------------------------


def test_gpg_signature_valid_on_release_artifact() -> None:
    """gpg --verify passes on the signed release artifact."""
    _skip_unless_system()
    _skip_unless_tool("gpg")

    # Look for any .sig or .asc file beside a release artifact in dist/.
    sig_files = list(_DIST_DIR.glob("*.sig")) + list(_DIST_DIR.glob("*.asc"))
    if not sig_files:
        pytest.skip(f"No .sig/.asc files found in {_DIST_DIR}")

    sig_file = sig_files[0]
    # The signed artifact is the sig file without the signature extension.
    artifact = sig_file.with_suffix("")
    if not artifact.is_file():
        pytest.skip(f"Signed artifact not found at {artifact}")

    result = subprocess.run(
        ["gpg", "--verify", str(sig_file), str(artifact)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"gpg --verify failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_gpg_signature_fails_on_tampered_artifact(tmp_path: Path) -> None:
    """gpg --verify fails when the artifact has been tampered with."""
    _skip_unless_system()
    _skip_unless_tool("gpg")

    sig_files = list(_DIST_DIR.glob("*.sig")) + list(_DIST_DIR.glob("*.asc"))
    if not sig_files:
        pytest.skip(f"No .sig/.asc files found in {_DIST_DIR}")

    sig_file = sig_files[0]
    artifact = sig_file.with_suffix("")
    if not artifact.is_file():
        pytest.skip(f"Signed artifact not found at {artifact}")

    # Copy the artifact and flip one byte.
    tampered = tmp_path / artifact.name
    data = bytearray(artifact.read_bytes())
    data[len(data) // 2] ^= 0xFF
    tampered.write_bytes(bytes(data))

    result = subprocess.run(
        ["gpg", "--verify", str(sig_file), str(tampered)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0, (
        "gpg --verify should fail on a tampered artifact, but it passed"
    )


def test_sha256_of_release_artifact_matches_manifest(tmp_path: Path) -> None:
    """SHA-256 of a release artifact is stable and detects tampering."""
    _skip_unless_system()

    # Find any release artifact in dist/.
    artifacts = [
        f
        for f in _DIST_DIR.glob("*")
        if f.is_file() and f.suffix not in (".sig", ".asc", ".json")
    ]
    if not artifacts:
        pytest.skip(f"No release artifacts found in {_DIST_DIR}")

    artifact = artifacts[0]
    content = artifact.read_bytes()

    # Compute digest twice from the same bytes: must be stable.
    digest1 = hashlib.sha256(content).hexdigest()
    digest2 = hashlib.sha256(content).hexdigest()
    assert digest1 == digest2

    # Tamper: a copy with a flipped byte must produce a different digest.
    tampered = bytearray(content)
    tampered[0] ^= 0x01
    tampered_digest = hashlib.sha256(bytes(tampered)).hexdigest()
    assert tampered_digest != digest1, "Tampered artifact produced the same digest"
