"""An RTSP grab never leaves ffmpeg behind (#69 CAM-R3), and its argv is tight.

A fake ``ffmpeg`` shell script stands in for the real binary, so these run
without ffmpeg installed. What only a real ffmpeg proves -- that the flags are
accepted and a real camera's stream decodes -- is covered by the CI build.
"""

from __future__ import annotations

import asyncio
import os
import stat
import time
from pathlib import Path

import pytest

from tests.conftest import make_jpeg
from timelapse_manager.cameras import rtsp
from timelapse_manager.cameras.base import CameraAdapter, TimeoutCaptureError
from timelapse_manager.cameras.onvif import OnvifAdapter
from timelapse_manager.cameras.rtsp import RtspAdapter, build_ffmpeg_command

_URL = "rtsp://camera.example/stream"

# Starts a background child (as ffmpeg might), records both pids and the
# process group, then waits.
_SLEEPER = """#!/bin/sh
echo $$ > "$PIDFILE"
sleep 60 &
echo $! > "$CHILDFILE"
ps -o pgid= -p $$ | tr -d ' ' > "$PGIDFILE"
wait
"""


@pytest.fixture(autouse=True)
def _no_ssrf_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    async def allow(_url: str) -> None:
        return None

    monkeypatch.setattr(rtsp, "_guard_stream_url", allow)


@pytest.fixture
def sleeper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    script = tmp_path / "ffmpeg"
    script.write_text(_SLEEPER)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    files = {
        "binary": script,
        "PIDFILE": tmp_path / "pid",
        "CHILDFILE": tmp_path / "child",
        "PGIDFILE": tmp_path / "pgid",
    }
    for name in ("PIDFILE", "CHILDFILE", "PGIDFILE"):
        monkeypatch.setenv(name, str(files[name]))
    return files


async def _wait_for_files(files: dict[str, Path]) -> tuple[int, int, int]:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            values = [
                int(files[name].read_text().strip())
                for name in ("PIDFILE", "CHILDFILE", "PGIDFILE")
            ]
        except (FileNotFoundError, ValueError):
            await asyncio.sleep(0.02)
            continue
        return values[0], values[1], values[2]
    raise AssertionError("the fake ffmpeg never started")


def _alive(pid: int) -> bool:
    """Whether ``pid`` is still running; a zombie counts as gone.

    In a container whose PID 1 does not reap orphans (the CI job container), a
    killed grandchild stays a zombie forever: it has exited, but ``kill(pid, 0)``
    still succeeds. On Linux the state letter in ``/proc/<pid>/stat`` tells them
    apart; where there is no ``/proc`` (macOS) the signal check stands.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        proc_stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    state = proc_stat.rsplit(")", 1)[-1].split()[:1]
    return state != ["Z"]


async def _assert_gone(*pids: int) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not any(_alive(pid) for pid in pids):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"processes left behind: {[p for p in pids if _alive(p)]}")


def _adapter(binary: Path, **kwargs: object) -> RtspAdapter:
    return RtspAdapter(_URL, ffmpeg_binary=str(binary), **kwargs)  # type: ignore[arg-type]


class TestNoChildLeftBehind:
    async def test_the_outer_capture_timeout_kills_ffmpeg_and_its_children(
        self, sleeper: dict[str, Path]
    ) -> None:
        adapter = _adapter(sleeper["binary"])

        async def capture_with_deadline_after_start() -> None:
            # The deadline is moved in only once the fake ffmpeg is running, so a
            # loaded host cannot expire it before the script has written its pids.
            async with asyncio.timeout(60.0) as deadline:
                task = asyncio.ensure_future(adapter.capture())
                await _wait_for_files(sleeper)
                deadline.reschedule(asyncio.get_running_loop().time() + 0.2)
                await task

        with pytest.raises(TimeoutError):
            await capture_with_deadline_after_start()
        pid, child, _ = await _wait_for_files(sleeper)
        await _assert_gone(pid, child)

    async def test_cancelling_the_capture_during_a_grab_leaves_no_child(
        self, sleeper: dict[str, Path]
    ) -> None:
        adapter = _adapter(sleeper["binary"])
        task = asyncio.ensure_future(adapter.capture())
        pid, child, _ = await _wait_for_files(sleeper)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _assert_gone(pid, child)

    async def test_the_adapters_own_wall_clock_kills_ffmpeg(
        self, sleeper: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rtsp, "WALL_CLOCK_MARGIN_SECONDS", 0.0)
        # The adapter's deadline runs from the spawn, so it must outlast the fake
        # ffmpeg's start-up on a loaded host; the test waits for the pid files
        # while the capture runs, then for the deadline to fire.
        adapter = _adapter(sleeper["binary"], timeout_seconds=2.0)
        task = asyncio.ensure_future(adapter.capture())
        pid, child, _ = await _wait_for_files(sleeper)
        with pytest.raises(TimeoutCaptureError):
            await task
        await _assert_gone(pid, child)

    async def test_ffmpeg_runs_in_its_own_process_group(
        self, sleeper: dict[str, Path]
    ) -> None:
        """So ``killpg`` can never reach the application's own group."""
        adapter = _adapter(sleeper["binary"])
        task = asyncio.ensure_future(adapter.capture())
        pid, child, pgid = await _wait_for_files(sleeper)
        try:
            assert pgid == pid
            assert pgid != os.getpgrp()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await _assert_gone(pid, child)

    async def test_a_process_that_already_exited_is_not_an_error(
        self, tmp_path: Path
    ) -> None:
        script = tmp_path / "ffmpeg"
        script.write_text("#!/bin/sh\nexit 1\n")
        script.chmod(0o755)
        with pytest.raises(Exception, match="rtsp capture failed"):
            await _adapter(script).capture()

    async def test_a_good_grab_is_returned(self, tmp_path: Path) -> None:
        jpeg = tmp_path / "frame.jpg"
        jpeg.write_bytes(make_jpeg(32, 24))
        script = tmp_path / "ffmpeg"
        script.write_text(f'#!/bin/sh\ncat "{jpeg}"\n')
        script.chmod(0o755)
        frame = await _adapter(script).capture()
        assert (frame.width, frame.height) == (32, 24)
        assert frame.image_bytes == jpeg.read_bytes()

    async def test_stderr_is_still_redacted(self, tmp_path: Path) -> None:
        script = tmp_path / "ffmpeg"
        script.write_text(
            "#!/bin/sh\n"
            "echo 'rtsp://admin:hunter2@10.0.16.5/s: Connection refused' >&2\n"
            "exit 1\n"
        )
        script.chmod(0o755)
        with pytest.raises(Exception) as excinfo:
            await _adapter(script).capture()
        assert "hunter2" not in str(excinfo.value)
        assert "Connection refused" in str(excinfo.value)


class TestArgv:
    def test_video_only_and_jpeg_quality(self) -> None:
        argv = build_ffmpeg_command(_URL)
        for flag in ("-an", "-sn", "-dn"):
            assert flag in argv
            assert argv.index(flag) > argv.index("-i")
        assert argv[argv.index("-q:v") + 1] == "2"
        assert argv[-1] == "-"

    def test_quality_is_configurable(self) -> None:
        argv = build_ffmpeg_command(_URL, jpeg_quality=5)
        assert argv[argv.index("-q:v") + 1] == "5"

    def test_rtsps_adds_tls_and_nothing_more(self) -> None:
        argv = build_ffmpeg_command("rtsps://camera.example/stream")
        assert argv[argv.index("-protocol_whitelist") + 1] == "rtp,rtsp,tcp,udp,tls"

    def test_rtsps_scheme_match_is_case_insensitive(self) -> None:
        argv = build_ffmpeg_command("RTSPS://camera.example/stream")
        assert argv[argv.index("-protocol_whitelist") + 1].endswith(",tls")

    def test_plain_rtsp_gets_no_tls(self) -> None:
        argv = build_ffmpeg_command(_URL)
        assert "tls" not in argv[argv.index("-protocol_whitelist") + 1]

    def test_a_tls_looking_path_does_not_widen_the_whitelist(self) -> None:
        argv = build_ffmpeg_command("rtsp://camera.example/rtsps://x")
        assert argv[argv.index("-protocol_whitelist") + 1] == "rtp,rtsp,tcp,udp"

    def test_keyframe_skip_is_an_input_option_and_off_by_default(self) -> None:
        assert "-skip_frame" not in build_ffmpeg_command(_URL)
        argv = build_ffmpeg_command(_URL, wait_for_keyframe=True)
        assert argv[argv.index("-skip_frame") + 1] == "nokey"
        assert argv.index("-skip_frame") < argv.index("-i")

    def test_the_url_is_still_a_single_argument(self) -> None:
        argv = build_ffmpeg_command("rtsp://h/a b;c")
        assert argv[argv.index("-i") + 1] == "rtsp://h/a b;c"


class TestBudget:
    def test_the_defaults_come_from_the_capture_settings(self) -> None:
        adapter = RtspAdapter(_URL)
        assert adapter.capture_budget_seconds == 25.0
        assert adapter._jpeg_quality == 2  # noqa: SLF001
        assert adapter._wait_for_keyframe is False  # noqa: SLF001

    def test_an_explicit_timeout_wins(self) -> None:
        assert RtspAdapter(_URL, timeout_seconds=3.0).capture_budget_seconds == 8.0

    def test_other_adapters_declare_no_budget(self) -> None:
        assert CameraAdapter.capture_budget_seconds.fget(object()) is None  # type: ignore[attr-defined]

    def test_onvif_declares_the_rtsp_fallbacks_budget(self) -> None:
        assert OnvifAdapter.capture_budget_seconds.fget(object()) == 25.0  # type: ignore[attr-defined]

    def test_the_settings_bounds(self) -> None:
        from pydantic import ValidationError

        from timelapse_manager.config.settings import CaptureSettings

        for bad in (
            {"rtsp_timeout_seconds": 0},
            {"rtsp_timeout_seconds": 46},
            {"rtsp_jpeg_quality": 0},
            {"rtsp_jpeg_quality": 32},
        ):
            with pytest.raises(ValidationError):
                CaptureSettings(**bad)  # type: ignore[arg-type]
        assert CaptureSettings().rtsp_wait_for_keyframe is False
