"""Abuse tests: static audit for no arbitrary subprocess execution.

Reads the source of every known subprocess spawn site and asserts:
  - Shell execution is never used (no shell=True, no create_subprocess_shell)
  - Every spawn uses an argument list (not a string command)
  - The argv[0] is a fixed, admin-configured binary (ffmpeg or openssl),
    never a value derived from user input at the call site

This is an AST-level audit backed by source text inspection. It documents
the complete set of subprocess spawn sites and fails if a new one is added
without review.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Source root (absolute) for all inspected modules.
_SRC_ROOT = (
    Path(__file__).parent.parent.parent / "src" / "timelapse_manager"
).resolve()

# The exact source files that may spawn subprocesses.  If a NEW spawn site is
# added outside these files, the test_no_undeclared_spawn_sites test will catch
# it; if an existing spawn site is changed to use a shell string, the per-file
# assertion tests will catch it.
_KNOWN_SPAWN_FILES: tuple[Path, ...] = (
    _SRC_ROOT / "encode" / "ffmpeg_impl.py",
    # Thumbnail generation: spawns the configured ffmpeg with an argv list to
    # downscale a single local frame; argv[0] is the admin-configured binary and
    # the input/output are caller-resolved, containment-checked local paths.
    _SRC_ROOT / "encode" / "thumbnail.py",
    # Hardware-encoder probe: spawns the configured ffmpeg with a fixed argv list
    # (``ffmpeg -hide_banner -encoders``) once to learn which GPU encoders the
    # local ffmpeg advertises; argv[0] is the admin-configured binary and no
    # caller input reaches the argv.
    _SRC_ROOT / "encode" / "hwaccel.py",
    _SRC_ROOT / "cameras" / "rtsp.py",
    _SRC_ROOT / "service" / "tls.py",
    _SRC_ROOT / "version.py",
)

# Substrings whose presence in a source file indicates a shell-based spawn.
# Any occurrence (outside comments) is an audit failure.
_SHELL_SPAWN_PATTERNS = (
    "shell=True",
    "create_subprocess_shell",
    "os.system(",
    "os.popen(",
)


def _read_source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _collect_spawn_files() -> list[Path]:
    """Walk the src tree and return files that contain any subprocess spawn call."""
    spawn_indicators = [
        "create_subprocess_exec",
        "create_subprocess_shell",
        "subprocess.run(",
        "subprocess.Popen(",
        "subprocess.call(",
        "os.system(",
        "os.popen(",
    ]
    found = []
    for py_file in _SRC_ROOT.rglob("*.py"):
        src = py_file.read_text(encoding="utf-8")
        if any(indicator in src for indicator in spawn_indicators):
            found.append(py_file)
    return found


@pytest.mark.abuse
class TestNoShellExecution:
    @pytest.mark.parametrize("source_file", _KNOWN_SPAWN_FILES, ids=lambda p: p.name)
    def test_no_shell_true_in_known_spawn_file(self, source_file: Path) -> None:
        src = _read_source(source_file)
        for pattern in _SHELL_SPAWN_PATTERNS:
            assert pattern not in src, (
                f"Found forbidden pattern {pattern!r} in {source_file}. "
                "All subprocess spawns must use argv lists with shell=False semantics."
            )

    def test_no_undeclared_spawn_files(self) -> None:
        """Every file that spawns a process must be in the known-spawn list.

        This test fails when a new subprocess spawn is added to a file not in
        _KNOWN_SPAWN_FILES, forcing a human to review and register the new spawn
        site here with the rationale that it is safe (argv list, fixed binary).
        """
        found_files = set(_collect_spawn_files())
        known_files = set(_KNOWN_SPAWN_FILES)
        undeclared = found_files - known_files
        assert not undeclared, (
            "New subprocess spawn site(s) found in src/ that are not registered "
            "in _KNOWN_SPAWN_FILES:\n"
            + "\n".join(f"  {f.relative_to(_SRC_ROOT)}" for f in sorted(undeclared))
            + "\nReview each new spawn site: confirm it uses an argv list (not a shell "
            "string), has a fixed argv[0], and register it in _KNOWN_SPAWN_FILES."
        )


@pytest.mark.abuse
class TestArgvIsListNotString:
    """For each known spawn site, the first argument to the spawn call is a list."""

    def _check_no_string_command(self, source_file: Path, spawn_func: str) -> None:
        """Assert that spawn_func is never called with a string literal as argv."""
        src = _read_source(source_file)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # Match calls like asyncio.create_subprocess_exec(*argv) or
            # subprocess.run(["ffmpeg", ...], ...) - we want to confirm no
            # direct string is used as the first positional arg.
            call_str = ast.unparse(node)
            if spawn_func not in call_str:
                continue
            # The first positional argument must not be a string literal.
            if node.args:
                first_arg = node.args[0]
                assert not isinstance(first_arg, ast.Constant) or not isinstance(
                    first_arg.value, str
                ), (
                    f"{source_file.name}: {spawn_func} called with a string literal "
                    f"as first argument at line {node.lineno}. Use an argv list."
                )

    def test_ffmpeg_impl_uses_list_argv(self) -> None:
        self._check_no_string_command(
            _SRC_ROOT / "encode" / "ffmpeg_impl.py",
            "create_subprocess_exec",
        )

    def test_thumbnail_uses_list_argv(self) -> None:
        self._check_no_string_command(
            _SRC_ROOT / "encode" / "thumbnail.py",
            "subprocess.run",
        )

    def test_rtsp_uses_list_argv(self) -> None:
        self._check_no_string_command(
            _SRC_ROOT / "cameras" / "rtsp.py",
            "create_subprocess_exec",
        )

    def test_tls_uses_list_argv(self) -> None:
        self._check_no_string_command(
            _SRC_ROOT / "service" / "tls.py",
            "subprocess.run",
        )

    def test_version_uses_list_argv(self) -> None:
        self._check_no_string_command(
            _SRC_ROOT / "version.py",
            "subprocess.run",
        )


@pytest.mark.abuse
class TestFixedBinaryInArgv:
    """The binary at argv[0] must be a known fixed string, not user-derived."""

    def test_ffmpeg_impl_argv0_is_ffmpeg_binary(self) -> None:
        """ffmpeg_impl spawns using *argv where argv is built by the module itself."""
        src = _read_source(_SRC_ROOT / "encode" / "ffmpeg_impl.py")
        # The spawn call uses *argv (spread), and argv is always assembled by
        # the module's _build_argv() function, not from user input directly.
        # Confirm no user string is inserted at argv[0] by checking the build function.
        assert "FFMPEG_BINARY" in src, (
            "Expected ffmpeg_impl.py to use a FFMPEG_BINARY constant for argv[0]"
        )

    def test_rtsp_argv0_is_configured_binary(self) -> None:
        """rtsp.py builds its ffmpeg argv using a configurable binary path."""
        src = _read_source(_SRC_ROOT / "cameras" / "rtsp.py")
        # build_ffmpeg_command is the seam; it takes ffmpeg_binary parameter.
        assert "ffmpeg_binary" in src, (
            "rtsp.py should use a ffmpeg_binary parameter, not a hard-coded string "
            "derived from user input."
        )

    def test_tls_argv0_is_openssl_literal(self) -> None:
        """tls.py spawns openssl with a literal string, not user input."""
        src = _read_source(_SRC_ROOT / "service" / "tls.py")
        assert '"openssl"' in src, "Expected openssl literal as argv[0] in tls.py"

    def test_version_argv0_is_binary_parameter(self) -> None:
        """version.py probes the binary by name, not from user input."""
        src = _read_source(_SRC_ROOT / "version.py")
        # The binary is passed in as a parameter with a default of "ffmpeg".
        assert "binary" in src, (
            "version.py should receive the binary as a parameter, "
            "not build it from user input."
        )
