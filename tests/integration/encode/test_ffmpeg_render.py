"""Integration tests for FfmpegEncoder: real encoding, ffprobe validation, and security.

Tests marked @slow spawn real ffmpeg processes. The fixture frames are real 64x48
JPEGs committed under tests/fixtures/frames/ — they are decodable by ffmpeg.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from timelapse_manager.encode.encoder import (
    EncoderCapabilityError,
    FrameRef,
    FrameSequence,
    OutputSettings,
    OverlayConfig,
    RenderSpec,
)
from timelapse_manager.encode.ffmpeg_impl import FfmpegEncoder

# Path to the committed fixture frames — real decodable 64x48 JPEGs.
_FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "frames"
_FFPROBE = shutil.which("ffprobe") or str(Path.home() / ".local" / "bin" / "ffprobe")

pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _frame_refs(count: int = 8) -> list[FrameRef]:
    """Return FrameRef objects for the first ``count`` fixture frames."""
    base = datetime(2024, 3, 1, 0, tzinfo=UTC)
    refs = []
    for i in range(count):
        path = _FIXTURES / f"frame_{i:03d}.jpg"
        refs.append(
            FrameRef(
                sequence_index=i,
                capture_timestamp=base + timedelta(hours=i),
                absolute_path=path,
                width=64,
                height=48,
            )
        )
    return refs


def _minimal_spec(
    output_path: Path,
    render_root: Path,
    *,
    fps: float = 1.0,
    codec: str = "h264",
    container: str = "mp4",
    deflicker: bool = False,
    overlay: OverlayConfig | None = None,
    chapters: list | None = None,
    frame_count: int = 8,
) -> RenderSpec:
    frames = FrameSequence(project_id=1, frames=_frame_refs(frame_count))
    return RenderSpec(
        project_id=1,
        frames=frames,
        output_settings=OutputSettings(
            fps=fps,
            width=64,
            height=48,
            codec=codec,
            container=container,
        ),
        overlay=overlay or OverlayConfig(),
        chapters=chapters or [],
        deflicker=deflicker,
        output_path=output_path,
        project_render_root=render_root,
    )


def _ffprobe_info(path: Path) -> dict:
    """Run ffprobe and return parsed JSON streams + format info."""
    result = subprocess.run(
        [
            str(_FFPROBE),
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Core encode: H.264 / MP4
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_real_render_produces_h264_mp4(tmp_path: Path) -> None:
    render_root = tmp_path / "renders"
    render_root.mkdir()
    output = render_root / "out.mp4"
    encoder = FfmpegEncoder()
    spec = _minimal_spec(output, render_root)

    result = await encoder.render(spec)

    assert result.success, f"render failed: {result.error}"
    assert output.is_file()
    assert output.stat().st_size > 0


class _MessageCapture(logging.Handler):
    """Collect emitted log messages, independent of root/propagation config."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.mark.slow
async def test_render_logs_exact_ffmpeg_command(tmp_path: Path) -> None:
    """The exact ffmpeg command is logged for diagnosis (a real argv, not a stub)."""
    render_root = tmp_path / "renders"
    render_root.mkdir()
    output = render_root / "out.mp4"
    encoder = FfmpegEncoder()
    spec = _minimal_spec(output, render_root)

    # Attach directly to the encoder's logger so capture does not depend on the
    # application's root-logger configuration or propagation settings. Also clear
    # any global ``logging.disable`` another test may have left set (which would
    # otherwise suppress INFO records before they reach any handler).
    logger = logging.getLogger("timelapse_manager.encode.ffmpeg_impl")
    capture = _MessageCapture()
    prev_level = logger.level
    prev_disabled = logger.disabled
    prev_disable = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    logger.disabled = False  # a prior dictConfig may have disabled this logger
    logger.addHandler(capture)
    logger.setLevel(logging.INFO)
    try:
        result = await encoder.render(spec)
    finally:
        logger.removeHandler(capture)
        logger.setLevel(prev_level)
        logger.disabled = prev_disabled
        logging.disable(prev_disable)

    assert result.success, f"render failed: {result.error}"
    command_lines = [m for m in capture.messages if "ffmpeg command:" in m]
    assert len(command_lines) == 1, "the exact ffmpeg command must be logged once"
    logged = command_lines[0]
    # The logged command is the real argv: the binary plus the output path.
    assert "ffmpeg" in logged
    assert str(output) in logged


@pytest.mark.slow
async def test_ffprobe_confirms_h264_codec(tmp_path: Path) -> None:
    render_root = tmp_path / "renders"
    render_root.mkdir()
    output = render_root / "out.mp4"
    encoder = FfmpegEncoder()
    spec = _minimal_spec(output, render_root)

    result = await encoder.render(spec)

    assert result.success
    info = _ffprobe_info(output)
    video_streams = [s for s in info["streams"] if s["codec_type"] == "video"]
    assert len(video_streams) == 1
    assert video_streams[0]["codec_name"] == "h264"


@pytest.mark.slow
async def test_ffprobe_confirms_mp4_container(tmp_path: Path) -> None:
    render_root = tmp_path / "renders"
    render_root.mkdir()
    output = render_root / "out.mp4"
    encoder = FfmpegEncoder()
    spec = _minimal_spec(output, render_root)

    result = await encoder.render(spec)

    assert result.success
    info = _ffprobe_info(output)
    assert "mp4" in info["format"]["format_name"]


@pytest.mark.slow
async def test_ffprobe_duration_matches_frame_count_over_fps(tmp_path: Path) -> None:
    render_root = tmp_path / "renders"
    render_root.mkdir()
    output = render_root / "out.mp4"
    encoder = FfmpegEncoder()
    # 8 frames at 2 fps -> expected duration ~4 seconds.
    spec = _minimal_spec(output, render_root, fps=2.0, frame_count=8)

    result = await encoder.render(spec)

    assert result.success
    info = _ffprobe_info(output)
    duration = float(info["format"]["duration"])
    # Allow ±2 seconds tolerance for encoder frame padding.
    assert abs(duration - 4.0) < 2.0


@pytest.mark.slow
async def test_output_resolution_honoured(tmp_path: Path) -> None:
    render_root = tmp_path / "renders"
    render_root.mkdir()
    output = render_root / "out.mp4"
    encoder = FfmpegEncoder()
    # Scale up: source is 64x48, request 128x96 (both even).
    frames = FrameSequence(project_id=1, frames=_frame_refs())
    spec = RenderSpec(
        project_id=1,
        frames=frames,
        output_settings=OutputSettings(
            fps=1.0, width=128, height=96, codec="h264", container="mp4"
        ),
        overlay=OverlayConfig(),
        chapters=[],
        deflicker=False,
        output_path=output,
        project_render_root=render_root,
    )

    result = await encoder.render(spec)

    assert result.success
    info = _ffprobe_info(output)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert video["width"] == 128
    assert video["height"] == 96


@pytest.mark.slow
async def test_result_browser_streamable_for_h264_mp4(tmp_path: Path) -> None:
    render_root = tmp_path / "renders"
    render_root.mkdir()
    output = render_root / "out.mp4"
    encoder = FfmpegEncoder()
    spec = _minimal_spec(output, render_root)

    result = await encoder.render(spec)

    assert result.success
    assert result.browser_streamable is True


@pytest.mark.slow
async def test_result_not_browser_streamable_for_vp9_webm(tmp_path: Path) -> None:
    render_root = tmp_path / "renders"
    render_root.mkdir()
    output = render_root / "out.webm"
    encoder = FfmpegEncoder()
    spec = _minimal_spec(output, render_root, codec="vp9", container="webm")

    result = await encoder.render(spec)

    assert result.success
    assert result.browser_streamable is False


# ---------------------------------------------------------------------------
# AV1 (libsvtav1): download-only, MP4/MKV only, never WebM
# ---------------------------------------------------------------------------


class _StubProcess:
    """Stand-in for an ffmpeg child that reports success without encoding.

    AV1 software encoding is slow, so tests that only need to inspect the built
    argv replace the real subprocess with this stub instead of invoking ffmpeg.
    """

    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""


@pytest.mark.parametrize("container", ["mp4", "mkv"])
async def test_av1_argv_uses_libsvtav1_encoder(tmp_path: Path, container: str) -> None:
    """An AV1 spec builds argv with ``-c:v libsvtav1`` and no quality override.

    The real encoder is stubbed out: we assert on the assembled command only,
    so libsvtav1 (slow) never runs. Schedule renders inject no crf/bitrate, so
    no ``-crf``/``-b:v`` should appear and libsvtav1's own default applies.
    """
    argv_calls: list[list[str]] = []

    async def stub_exec(*args: object, **kwargs: object) -> object:
        argv_calls.append([str(a) for a in args])
        return _StubProcess()

    render_root = tmp_path / "renders"
    render_root.mkdir()
    output = render_root / f"out.{container}"
    encoder = FfmpegEncoder()
    spec = _minimal_spec(output, render_root, codec="av1", container=container)

    import timelapse_manager.encode.ffmpeg_impl as _mod

    original = _mod.asyncio.create_subprocess_exec  # type: ignore[attr-defined]
    _mod.asyncio.create_subprocess_exec = stub_exec  # type: ignore[attr-defined]
    try:
        result = await encoder.render(spec)
    finally:
        _mod.asyncio.create_subprocess_exec = original  # type: ignore[attr-defined]

    assert result.success, f"render failed: {result.error}"
    assert argv_calls, "expected the encoder to spawn a (stubbed) process"
    argv = argv_calls[-1]
    # The codec must resolve to libsvtav1 via the -c:v flag.
    assert "-c:v" in argv
    assert argv[argv.index("-c:v") + 1] == "libsvtav1"
    # No quality override is emitted when neither crf nor bitrate is set.
    assert "-crf" not in argv
    assert "-b:v" not in argv


def test_av1_is_not_browser_streamable() -> None:
    """AV1 is download-only regardless of container; never inline-streamable."""
    from timelapse_manager.encode.browser_streamable import is_browser_streamable

    assert is_browser_streamable("av1", "mp4") is False
    assert is_browser_streamable("av1", "mkv") is False
    assert is_browser_streamable("libsvtav1", "mp4") is False


async def test_av1_webm_rejected_by_validate() -> None:
    """AV1 cannot be muxed into WebM; validate() rejects the pairing."""
    encoder = FfmpegEncoder()
    output = OutputSettings(
        fps=24.0, width=640, height=480, codec="av1", container="webm"
    )
    with pytest.raises(EncoderCapabilityError) as exc_info:
        await encoder.validate(output, has_chapters=False)
    assert exc_info.value.option == "container"


async def test_av1_webm_rejected_before_spawn(tmp_path: Path) -> None:
    """A render of AV1 into WebM raises before any ffmpeg process is spawned."""
    spawn_count = [0]

    async def count_exec(*args: object, **kwargs: object) -> object:
        spawn_count[0] += 1
        return _StubProcess()

    render_root = tmp_path / "renders"
    render_root.mkdir()
    output = render_root / "out.webm"
    encoder = FfmpegEncoder()
    spec = _minimal_spec(output, render_root, codec="av1", container="webm")

    import timelapse_manager.encode.ffmpeg_impl as _mod

    original = _mod.asyncio.create_subprocess_exec  # type: ignore[attr-defined]
    _mod.asyncio.create_subprocess_exec = count_exec  # type: ignore[attr-defined]
    try:
        with pytest.raises(EncoderCapabilityError):
            await encoder.render(spec)
    finally:
        _mod.asyncio.create_subprocess_exec = original  # type: ignore[attr-defined]

    assert spawn_count[0] == 0, "AV1+WebM must be rejected before any spawn"


# ---------------------------------------------------------------------------
# Deflicker filter: check presence/absence in argv (not slow render diff)
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_deflicker_on_produces_different_output_than_off(tmp_path: Path) -> None:
    """Deflicker on must reach the encoder; we verify by checking the render succeeds
    and by argv inspection via a monkey-patched subprocess call."""
    import asyncio

    argv_calls: list[list[str]] = []
    original_exec = asyncio.create_subprocess_exec

    async def capture_exec(*args: object, **kwargs: object) -> object:
        argv_calls.append([str(a) for a in args])
        return await original_exec(*args, **kwargs)

    render_root = tmp_path / "renders"
    render_root.mkdir()
    output_on = render_root / "deflicker_on.mp4"
    encoder = FfmpegEncoder()
    spec_on = _minimal_spec(output_on, render_root, deflicker=True)

    import timelapse_manager.encode.ffmpeg_impl as _mod

    original = _mod.asyncio.create_subprocess_exec  # type: ignore[attr-defined]
    _mod.asyncio.create_subprocess_exec = capture_exec  # type: ignore[attr-defined]
    try:
        result = await encoder.render(spec_on)
    finally:
        _mod.asyncio.create_subprocess_exec = original  # type: ignore[attr-defined]

    assert result.success
    argv = argv_calls[-1] if argv_calls else []
    # Find the -vf value and confirm 'deflicker' appears in the filter chain.
    vf_value = ""
    for i, arg in enumerate(argv):
        if arg == "-vf" and i + 1 < len(argv):
            vf_value = argv[i + 1]
            break
    assert "deflicker" in vf_value, (
        f"Expected 'deflicker' in -vf filter string: {vf_value!r}"
    )


@pytest.mark.slow
async def test_deflicker_off_does_not_include_deflicker_filter(tmp_path: Path) -> None:
    import asyncio

    argv_calls: list[list[str]] = []
    original_exec = asyncio.create_subprocess_exec

    async def capture_exec(*args: object, **kwargs: object) -> object:
        argv_calls.append([str(a) for a in args])
        return await original_exec(*args, **kwargs)

    render_root = tmp_path / "renders"
    render_root.mkdir()
    output = render_root / "out_nodefl.mp4"
    encoder = FfmpegEncoder()
    spec = _minimal_spec(output, render_root, deflicker=False)

    import timelapse_manager.encode.ffmpeg_impl as _mod

    original = _mod.asyncio.create_subprocess_exec  # type: ignore[attr-defined]
    _mod.asyncio.create_subprocess_exec = capture_exec  # type: ignore[attr-defined]
    try:
        result = await encoder.render(spec)
    finally:
        _mod.asyncio.create_subprocess_exec = original  # type: ignore[attr-defined]

    assert result.success
    argv = argv_calls[-1] if argv_calls else []
    # Find the -vf value and check it does not contain the deflicker filter.
    vf_value = ""
    for i, arg in enumerate(argv):
        if arg == "-vf" and i + 1 < len(argv):
            vf_value = argv[i + 1]
            break
    assert "deflicker" not in vf_value, (
        f"Expected 'deflicker' to be absent from -vf filter string: {vf_value!r}"
    )


# ---------------------------------------------------------------------------
# Overlay: source frames are byte-identical before and after render
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_overlay_render_does_not_mutate_source_frames(tmp_path: Path) -> None:
    import hashlib

    render_root = tmp_path / "renders"
    render_root.mkdir()
    output = render_root / "overlay.mp4"

    # Compute SHA-256 of each source frame before render.
    frame_paths = [_FIXTURES / f"frame_{i:03d}.jpg" for i in range(8)]
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in frame_paths}

    encoder = FfmpegEncoder()
    overlay = OverlayConfig(text_enabled=True, text_content="Test Caption")
    spec = _minimal_spec(output, render_root, overlay=overlay)

    result = await encoder.render(spec)
    assert result.success

    # Verify source frames are byte-identical after render.
    for p in frame_paths:
        after = hashlib.sha256(p.read_bytes()).hexdigest()
        assert after == before[p], f"Source frame {p.name} was mutated by render"


# ---------------------------------------------------------------------------
# Security: subprocess is never spawned via shell=True
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_encoder_uses_exec_not_shell(tmp_path: Path) -> None:
    """Verify ffmpeg is spawned with create_subprocess_exec, never shell=True."""
    import asyncio

    shell_calls: list[bool] = []
    original_exec = asyncio.create_subprocess_exec

    async def spy_exec(*args: object, **kwargs: object) -> object:
        shell_calls.append(kwargs.get("shell", False))  # type: ignore[arg-type]
        return await original_exec(*args, **kwargs)

    render_root = tmp_path / "renders"
    render_root.mkdir()
    output = render_root / "out.mp4"
    encoder = FfmpegEncoder()
    spec = _minimal_spec(output, render_root)

    import timelapse_manager.encode.ffmpeg_impl as _mod

    original = _mod.asyncio.create_subprocess_exec  # type: ignore[attr-defined]
    _mod.asyncio.create_subprocess_exec = spy_exec  # type: ignore[attr-defined]
    try:
        result = await encoder.render(spec)
    finally:
        _mod.asyncio.create_subprocess_exec = original  # type: ignore[attr-defined]

    assert result.success
    assert shell_calls, "Expected create_subprocess_exec to be called"
    assert all(not s for s in shell_calls), "shell=True was used, which is forbidden"


# ---------------------------------------------------------------------------
# Validation: disallowed codec never spawns a process
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_disallowed_codec_raises_before_spawn(tmp_path: Path) -> None:
    import asyncio

    spawn_count = [0]
    original_exec = asyncio.create_subprocess_exec

    async def count_exec(*args: object, **kwargs: object) -> object:
        spawn_count[0] += 1
        return await original_exec(*args, **kwargs)

    render_root = tmp_path / "renders"
    render_root.mkdir()
    output = render_root / "out.avi"
    encoder = FfmpegEncoder()
    output_settings = OutputSettings(
        fps=1.0, width=64, height=48, codec="mpeg4", container="mp4"
    )
    frames = FrameSequence(project_id=1, frames=_frame_refs())
    spec = RenderSpec(
        project_id=1,
        frames=frames,
        output_settings=output_settings,
        overlay=OverlayConfig(),
        chapters=[],
        deflicker=False,
        output_path=output,
        project_render_root=render_root,
    )

    import timelapse_manager.encode.ffmpeg_impl as _mod

    original = _mod.asyncio.create_subprocess_exec  # type: ignore[attr-defined]
    _mod.asyncio.create_subprocess_exec = count_exec  # type: ignore[attr-defined]
    try:
        with pytest.raises(EncoderCapabilityError):
            await encoder.render(spec)
    finally:
        _mod.asyncio.create_subprocess_exec = original  # type: ignore[attr-defined]

    assert spawn_count[0] == 0, (
        "ffmpeg should not have been spawned for a disallowed codec"
    )


# ---------------------------------------------------------------------------
# Security: output path traversal is rejected
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_output_path_traversal_outside_render_root_rejected(
    tmp_path: Path,
) -> None:
    from timelapse_manager.encode.encoder import EncoderError

    render_root = tmp_path / "renders"
    render_root.mkdir()
    # Attempt to write outside the render root via path traversal.
    evil_output = render_root / ".." / "escape.mp4"
    frames = FrameSequence(project_id=1, frames=_frame_refs())
    spec = RenderSpec(
        project_id=1,
        frames=frames,
        output_settings=OutputSettings(
            fps=1.0, width=64, height=48, codec="h264", container="mp4"
        ),
        overlay=OverlayConfig(),
        chapters=[],
        deflicker=False,
        output_path=evil_output,
        project_render_root=render_root,
    )
    encoder = FfmpegEncoder()

    with pytest.raises((EncoderError, EncoderCapabilityError)):
        await encoder.render(spec)


# ---------------------------------------------------------------------------
# validate() method
# ---------------------------------------------------------------------------


async def test_validate_passes_for_supported_h264_mp4() -> None:
    encoder = FfmpegEncoder()
    output = OutputSettings(
        fps=24.0, width=1920, height=1080, codec="h264", container="mp4"
    )
    # Should not raise.
    await encoder.validate(output, has_chapters=False)


async def test_validate_raises_for_unsupported_codec() -> None:
    encoder = FfmpegEncoder()
    output = OutputSettings(
        fps=24.0, width=1920, height=1080, codec="mpeg4", container="mp4"
    )
    with pytest.raises(EncoderCapabilityError) as exc_info:
        await encoder.validate(output, has_chapters=False)
    assert exc_info.value.option == "codec"


async def test_validate_raises_for_chapters_in_webm() -> None:
    encoder = FfmpegEncoder()
    output = OutputSettings(
        fps=24.0, width=1920, height=1080, codec="vp9", container="webm"
    )
    with pytest.raises(EncoderCapabilityError) as exc_info:
        await encoder.validate(output, has_chapters=True)
    # webm cannot carry chapters.
    assert exc_info.value.option in ("container", "chapters")


async def test_validate_allows_chapters_in_mp4() -> None:
    encoder = FfmpegEncoder()
    output = OutputSettings(
        fps=24.0, width=1920, height=1080, codec="h264", container="mp4"
    )
    await encoder.validate(output, has_chapters=True)  # no exception


async def test_validate_allows_chapters_in_mkv() -> None:
    encoder = FfmpegEncoder()
    output = OutputSettings(
        fps=24.0, width=640, height=480, codec="h264", container="mkv"
    )
    await encoder.validate(output, has_chapters=True)  # no exception
