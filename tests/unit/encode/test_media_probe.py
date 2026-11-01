"""Unit tests for the encode-time ffprobe call (#25).

No real ffprobe is needed: a stand-in script prints recorded n9 output, sleeps
past the timeout, or fails, so the subprocess handling is exercised for real.
"""

from __future__ import annotations

import json
import stat
import sys
import time
from pathlib import Path

import pytest

from timelapse_manager.encode.media_probe import (
    MediaInfo,
    parse_probe_output,
    probe_argv,
    probe_media,
)

_FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures" / "ffprobe" / "n9_streams.json"
)


# Timeout for the fakes that should finish: well above a cold interpreter start.
_SLOW_START = 60.0


def _fake_ffprobe(tmp_path: Path, body: str) -> str:
    """Write an executable stand-in for ffprobe running ``body`` (Python)."""
    script = tmp_path / "ffprobe"
    script.write_text(f"#!{sys.executable}\nimport sys, time\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


def _av1_output() -> str:
    streams = json.loads(_FIXTURE.read_text(encoding="utf-8"))["streams"]
    return json.dumps({"streams": [streams["libsvtav1/mp4"]]})


class TestArgvAndParse:
    def test_argv_selects_first_video_stream_as_json(self) -> None:
        argv = probe_argv("/opt/ffprobe", Path("/r/render-1.mp4"))
        assert argv[0] == "/opt/ffprobe"
        assert argv[argv.index("-select_streams") + 1] == "v:0"
        assert argv[argv.index("-of") + 1] == "json"
        assert argv[-1] == "/r/render-1.mp4"

    def test_argv_allows_only_the_file_protocol(self) -> None:
        argv = probe_argv("/opt/ffprobe", Path("/r/render-1.mp4"))
        assert argv[argv.index("-protocol_whitelist") + 1] == "file"
        # The whitelist is an input option: it precedes the input path.
        assert argv.index("-protocol_whitelist") < len(argv) - 1

    def test_argv_path_is_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        argv = probe_argv("ffprobe", Path("-render.mp4"))
        assert argv[-1] == str(tmp_path.resolve() / "-render.mp4")
        assert Path(argv[-1]).is_absolute()

    def test_parse_recorded_output(self) -> None:
        assert parse_probe_output(_av1_output()) == "av01.0.05M.08"

    @pytest.mark.parametrize(
        "raw", [b"", b"not json", b"[]", b'{"streams": []}', b'{"streams": [1]}']
    )
    def test_parse_garbage_is_none(self, raw: bytes) -> None:
        assert parse_probe_output(raw) is None


class TestProbeMedia:
    async def test_success_returns_codec_and_mime(self, tmp_path: Path) -> None:
        payload = _av1_output()
        binary = _fake_ffprobe(tmp_path, f"sys.stdout.write({payload!r})")
        # A generous timeout: the fake is a cold Python start, which takes
        # seconds under a loaded parallel suite (the default 10 s flaked).
        info = await probe_media(binary, tmp_path / "render-1.mp4", timeout=_SLOW_START)
        assert info == MediaInfo(codec_string="av01.0.05M.08", mime_type="video/mp4")

    async def test_unknown_codec_keeps_the_mime_type(self, tmp_path: Path) -> None:
        payload = json.dumps({"streams": [{"codec_name": "mpeg4"}]})
        binary = _fake_ffprobe(tmp_path, f"sys.stdout.write({payload!r})")
        info = await probe_media(
            binary, tmp_path / "render-1.webm", timeout=_SLOW_START
        )
        assert info == MediaInfo(codec_string=None, mime_type="video/webm")

    async def test_non_zero_exit_is_none(self, tmp_path: Path) -> None:
        binary = _fake_ffprobe(tmp_path, "sys.stderr.write('bad file\\n'); sys.exit(1)")
        assert (
            await probe_media(binary, tmp_path / "render-1.mp4", timeout=_SLOW_START)
            is None
        )

    async def test_timeout_kills_and_is_none(self, tmp_path: Path) -> None:
        binary = _fake_ffprobe(tmp_path, "time.sleep(30)")
        started = time.monotonic()
        assert await probe_media(binary, tmp_path / "x.mp4", timeout=0.5) is None
        # The child was killed at the timeout, not waited out.
        assert time.monotonic() - started < 10

    async def test_missing_binary_is_none(self, tmp_path: Path) -> None:
        missing = str(tmp_path / "no-such-ffprobe")
        assert await probe_media(missing, tmp_path / "render-1.mp4") is None
