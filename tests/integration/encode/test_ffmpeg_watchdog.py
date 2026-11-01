"""Process-level tests for the ffmpeg render watchdog (#67 R2).

These run without ffmpeg: each test writes a small Python script that stands in
for ffmpeg and passes it as ``ffmpeg_binary``. The script ignores the real
encode, writes a few bytes to the output path (``argv[-1]``) so partial-output
removal can be checked, records its pid, and then behaves as the scenario needs:
progress then hang, no progress at all, identical progress blocks forever,
steady progress past the ceiling, and so on.

The fakes are Python, not shell, on purpose: a shell fake that runs ``sleep``
leaves a grandchild holding the pipes open after the kill.

Only the last test uses the real ffmpeg, and only when one is on ``PATH``: it
checks that the build in use emits the ``-progress`` keys the watchdog parses.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import textwrap
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from timelapse_manager.encode.encoder import (
    FrameRef,
    FrameSequence,
    OutputSettings,
    OverlayConfig,
    RenderSpec,
)
from timelapse_manager.encode.ffmpeg_impl import (
    EncoderTimeoutError,
    FfmpegEncoder,
    _ProgressTracker,
)

pytestmark = pytest.mark.slow

# Common prologue for every fake: write a partial output and record the pid.
_PROLOGUE = """\
import os, sys, time
time.sleep({startup_delay!r})
with open(sys.argv[-1], "wb") as out:
    out.write(b"partial")
with open({pidfile!r}, "w") as pid:
    pid.write(str(os.getpid()))
with open({argvfile!r}, "w") as recorded:
    recorded.write("\\n".join(sys.argv[1:]))

def block(frame, out_time_us="N/A"):
    sys.stdout.write(
        f"frame={{frame}}\\nfps=0.0\\nout_time_us={{out_time_us}}\\n"
        "out_time=N/A\\nspeed=N/A\\nprogress=continue\\n"
    )
    sys.stdout.flush()
"""

_SCENARIOS: dict[str, str] = {
    # One progress block, then wedged.
    "progress_then_hang": """
block(1, 40000)
time.sleep(600)
""",
    # Never writes to the progress pipe at all.
    "silent": """
time.sleep(600)
""",
    # Keeps emitting blocks, but nothing ever advances.
    "repeat_same": """
while True:
    block(5)
    time.sleep(0.02)
""",
    # Real progress every 20 ms, forever: only the wall ceiling can stop it.
    "steady_forever": """
n = 0
while True:
    n += 1
    block(n, n * 40000)
    time.sleep(0.02)
""",
    # Real progress for longer than the stall timeout, then a clean finish.
    "steady_finish": """
for n in range(1, 61):
    block(n, n * 40000)
    time.sleep(0.02)
sys.stdout.write("progress=end\\n")
sys.stdout.flush()
""",
    # Megabytes of stderr with no newline, a long stdout line, then a failure.
    "stderr_flood_fail": """
sys.stdout.write("x" * 300000 + "\\n")
sys.stdout.flush()
chunk = "e" * 65536
for _ in range(64):
    sys.stderr.write(chunk)
sys.stderr.write("\\nError opening output file: disk on fire\\n\\n")
sys.stderr.flush()
sys.exit(1)
""",
    # Plain failure: the last stderr line becomes the error.
    "fail": """
sys.stderr.write("first line\\nUnknown encoder 'nope'\\n")
sys.exit(1)
""",
}


def _write_fake(
    tmp_path: Path, scenario: str, *, startup_delay: float = 0.0
) -> tuple[Path, Path, Path]:
    """Write the fake ffmpeg for ``scenario``; return (binary, pidfile, argvfile).

    ``startup_delay`` makes the fake sleep before it records its pid, standing in
    for a slow interpreter start on a loaded host.
    """
    pidfile = tmp_path / f"{scenario}.pid"
    argvfile = tmp_path / f"{scenario}.argv"
    script = tmp_path / f"fake-ffmpeg-{scenario}"
    body = _PROLOGUE.format(
        pidfile=str(pidfile), argvfile=str(argvfile), startup_delay=startup_delay
    )
    body += textwrap.dedent(_SCENARIOS[scenario])
    script.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    script.chmod(0o755)
    return script, pidfile, argvfile


def _spec(render_root: Path, *, frame_count: int = 3) -> RenderSpec:
    base = datetime(2024, 3, 1, tzinfo=UTC)
    frames = FrameSequence(
        project_id=1,
        frames=[
            FrameRef(
                sequence_index=i,
                capture_timestamp=base + timedelta(hours=i),
                absolute_path=Path("/dev/null"),
                width=64,
                height=48,
            )
            for i in range(frame_count)
        ],
    )
    return RenderSpec(
        project_id=1,
        frames=frames,
        output_settings=OutputSettings(
            fps=1.0, width=64, height=48, codec="h264", container="mp4"
        ),
        overlay=OverlayConfig(),
        chapters=[],
        deflicker=False,
        output_path=render_root / "render-1.mp4",
        project_render_root=render_root,
    )


class _ReadyClock:
    """A watchdog clock that starts when the fake has recorded its pid (#129, #189).

    The encoder measures the stall timeout and the ceiling from the spawn. A fake
    ffmpeg is a Python interpreter, and under a loaded host it can take longer
    than the 0.5 s stall timeout just to start, so the watchdog killed it before
    it wrote its pid file and the test could not prove the process was reaped.
    This clock holds at 0 until the pid file is complete, then runs in real time,
    so the limits count only the time the fake has really been running.
    """

    def __init__(self, pidfile: Path) -> None:
        self._pidfile = pidfile
        self._ready_at: float | None = None

    def __call__(self) -> float:
        if self._ready_at is None:
            try:
                ready = bool(self._pidfile.read_text())
            except OSError:
                ready = False
            if not ready:
                return 0.0
            self._ready_at = time.monotonic()
        return time.monotonic() - self._ready_at


def _encoder(binary: Path, pidfile: Path, **kwargs: float) -> FfmpegEncoder:
    """An encoder whose watchdog clock starts once the fake is running."""
    return FfmpegEncoder(
        ffmpeg_binary=str(binary), watchdog_clock=_ReadyClock(pidfile), **kwargs
    )


@pytest.fixture
def render_root(tmp_path: Path) -> Path:
    root = tmp_path / "renders"
    root.mkdir()
    return root


def _assert_dead(pidfile: Path) -> None:
    """The fake recorded in ``pidfile`` must no longer exist (killed and reaped)."""
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def _assert_no_leaked_tasks() -> None:
    current = asyncio.current_task()
    leftover = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    assert leftover == []


class TestStallWatchdog:
    async def test_progress_then_hang_is_killed_and_partial_removed(
        self, tmp_path: Path, render_root: Path
    ) -> None:
        binary, pidfile, _ = _write_fake(tmp_path, "progress_then_hang")
        encoder = _encoder(binary, pidfile, stall_timeout_seconds=0.5)
        spec = _spec(render_root)

        started = time.monotonic()
        with pytest.raises(EncoderTimeoutError) as excinfo:
            await encoder.render(spec)
        elapsed = time.monotonic() - started

        assert str(excinfo.value) == "ffmpeg stalled: no progress for 0.5s"
        assert not spec.output_path.exists()
        _assert_dead(pidfile)
        _assert_no_leaked_tasks()
        # Killed promptly after the stall timeout, not after the 600 s sleep.
        assert elapsed < 10

    async def test_never_printing_progress_is_killed(
        self, tmp_path: Path, render_root: Path
    ) -> None:
        binary, pidfile, _ = _write_fake(tmp_path, "silent")
        encoder = _encoder(binary, pidfile, stall_timeout_seconds=0.5)
        spec = _spec(render_root)

        with pytest.raises(EncoderTimeoutError, match="stalled"):
            await encoder.render(spec)

        assert not spec.output_path.exists()
        _assert_dead(pidfile)
        _assert_no_leaked_tasks()

    async def test_identical_blocks_forever_count_as_stalled(
        self, tmp_path: Path, render_root: Path
    ) -> None:
        binary, pidfile, _ = _write_fake(tmp_path, "repeat_same")
        encoder = _encoder(binary, pidfile, stall_timeout_seconds=0.5)
        spec = _spec(render_root)

        with pytest.raises(EncoderTimeoutError, match="stalled"):
            await encoder.render(spec)

        assert not spec.output_path.exists()
        _assert_dead(pidfile)

    async def test_slow_interpreter_start_is_not_counted_as_a_stall(
        self, tmp_path: Path, render_root: Path
    ) -> None:
        """The fake takes 1.5 s to start (3x the stall timeout) yet is reaped.

        Without the ready-gated clock the watchdog killed it at 0.5 s, before it
        wrote its pid file: the failure the loaded suite hit (#129).
        """
        binary, pidfile, _ = _write_fake(tmp_path, "silent", startup_delay=1.5)
        encoder = _encoder(binary, pidfile, stall_timeout_seconds=0.5)
        spec = _spec(render_root)

        with pytest.raises(EncoderTimeoutError, match="stalled"):
            await encoder.render(spec)

        assert pidfile.read_text()
        _assert_dead(pidfile)

    async def test_steady_progress_is_not_killed(
        self, tmp_path: Path, render_root: Path
    ) -> None:
        # The fake runs ~1.2 s, more than twice the stall timeout, but reports
        # a new frame every 20 ms.
        binary, pidfile, _ = _write_fake(tmp_path, "steady_finish")
        encoder = _encoder(binary, pidfile, stall_timeout_seconds=0.5)
        spec = _spec(render_root)

        result = await encoder.render(spec)

        assert result.success, result.error
        assert result.output_path == spec.output_path.resolve()
        assert spec.output_path.read_bytes() == b"partial"
        _assert_no_leaked_tasks()


class TestWallClockCeiling:
    async def test_ceiling_fires_despite_steady_progress(
        self, tmp_path: Path, render_root: Path
    ) -> None:
        binary, pidfile, _ = _write_fake(tmp_path, "steady_forever")
        encoder = _encoder(
            binary, pidfile, stall_timeout_seconds=30.0, max_job_seconds=1.0
        )
        spec = _spec(render_root)

        started = time.monotonic()
        with pytest.raises(EncoderTimeoutError) as excinfo:
            await encoder.render(spec)
        elapsed = time.monotonic() - started

        assert str(excinfo.value) == "ffmpeg exceeded max_job_seconds (1s)"
        assert not spec.output_path.exists()
        _assert_dead(pidfile)
        _assert_no_leaked_tasks()
        assert 1.0 <= elapsed < 10


class TestOutputHandling:
    async def test_argv_requests_progress_on_stdout(
        self, tmp_path: Path, render_root: Path
    ) -> None:
        binary, _, argvfile = _write_fake(tmp_path, "steady_finish")
        encoder = FfmpegEncoder(ffmpeg_binary=str(binary))

        await encoder.render(_spec(render_root))

        args = argvfile.read_text().split("\n")
        assert args[:4] == ["-hide_banner", "-progress", "pipe:1", "-nostats"]
        assert args.count("-progress") == 1

    async def test_stderr_flood_does_not_deadlock(
        self, tmp_path: Path, render_root: Path
    ) -> None:
        # 4 MiB of stderr without a newline would fill the pipe and wedge the
        # child if stderr were not drained concurrently.
        binary, pidfile, _ = _write_fake(tmp_path, "stderr_flood_fail")
        encoder = FfmpegEncoder(ffmpeg_binary=str(binary), stall_timeout_seconds=30.0)
        spec = _spec(render_root)

        result = await asyncio.wait_for(encoder.render(spec), timeout=30)

        assert not result.success
        assert result.error == "ffmpeg failed: Error opening output file: disk on fire"
        assert not spec.output_path.exists()
        _assert_dead(pidfile)
        _assert_no_leaked_tasks()

    async def test_nonzero_exit_reports_last_stderr_line(
        self, tmp_path: Path, render_root: Path
    ) -> None:
        binary, _, _ = _write_fake(tmp_path, "fail")
        encoder = FfmpegEncoder(ffmpeg_binary=str(binary))
        spec = _spec(render_root)

        result = await encoder.render(spec)

        assert not result.success
        assert result.output_path is None
        assert result.error == "ffmpeg failed: Unknown encoder 'nope'"
        assert not spec.output_path.exists()


class TestCancellation:
    async def test_cancel_kills_child_and_removes_partial(
        self, tmp_path: Path, render_root: Path
    ) -> None:
        binary, pidfile, _ = _write_fake(tmp_path, "steady_forever")
        encoder = FfmpegEncoder(ffmpeg_binary=str(binary))
        spec = _spec(render_root)

        task = asyncio.create_task(encoder.render(spec))
        deadline = time.monotonic() + 10
        while not (pidfile.exists() and pidfile.read_text()):
            assert time.monotonic() < deadline, "fake ffmpeg never started"
            await asyncio.sleep(0.02)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert not spec.output_path.exists()
        _assert_dead(pidfile)
        _assert_no_leaked_tasks()


_REAL_FFMPEG = shutil.which("ffmpeg")


@pytest.mark.skipif(_REAL_FFMPEG is None, reason="real ffmpeg not on PATH")
def test_real_ffmpeg_progress_output_is_parsed() -> None:
    """The ffmpeg in use emits ``frame``/``out_time_us`` on ``-progress pipe:1``.

    Uses the same global flags as a render (``-progress pipe:1 -nostats
    -loglevel error``) on a synthetic source, then feeds stdout to the parser.
    This is the check that the bundled build (n9 in CI) speaks the format the
    watchdog relies on.
    """
    assert _REAL_FFMPEG is not None
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            _REAL_FFMPEG,
            "-hide_banner",
            "-progress",
            "pipe:1",
            "-nostats",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=3:size=64x48:rate=25",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        check=True,
        timeout=60,
    )
    tracker = _ProgressTracker()
    lines = completed.stdout.splitlines()
    advances = sum(1 for line in lines if tracker.feed(line))
    keys = {line.partition(b"=")[0] for line in lines}

    assert {b"frame", b"out_time_us", b"progress"} <= keys
    assert lines[-1].strip() == b"progress=end"
    assert advances >= 2
    # -nostats: no human-readable stats line on stderr.
    with contextlib.suppress(UnicodeDecodeError):
        assert "frame=" not in completed.stderr.decode()
