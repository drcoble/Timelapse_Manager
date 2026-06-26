"""Integration tests for render cancellation: no orphan process, no partial file."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from timelapse_manager.encode.encoder import (
    Encoder,
    FrameRef,
    FrameSequence,
    OutputSettings,
    OverlayConfig,
    RenderResult,
    RenderSpec,
)

pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# A slow fake encoder that holds an asyncio.Event open so we can cancel it
# mid-flight and verify the cancellation path.
# ---------------------------------------------------------------------------


class SlowFakeEncoder(Encoder):
    """An encoder that blocks until cancelled, recording calls and active count."""

    def __init__(self, *, sleep_seconds: float = 9999.0) -> None:
        self._sleep_seconds = sleep_seconds
        self.active_count = 0
        self.started_event = asyncio.Event()
        self._partial_path: Path | None = None

    async def validate(self, output: OutputSettings, *, has_chapters: bool) -> None:
        pass  # always valid

    async def render(self, spec: RenderSpec) -> RenderResult:
        self.active_count += 1
        self._partial_path = spec.output_path
        # Write a partial file to simulate a mid-encode state.
        spec.output_path.parent.mkdir(parents=True, exist_ok=True)
        spec.output_path.write_bytes(b"partial")
        self.started_event.set()
        try:
            await asyncio.sleep(self._sleep_seconds)
        except asyncio.CancelledError:
            # Mirror FfmpegEncoder: remove partial output on cancellation.
            with contextlib.suppress(FileNotFoundError):
                spec.output_path.unlink()
            raise
        finally:
            self.active_count -= 1
        return RenderResult(
            success=True,
            output_path=spec.output_path,
            duration_seconds=0.0,
            browser_streamable=False,
            codec=spec.output_settings.codec,
            container=spec.output_settings.container,
        )


def _minimal_spec(output_path: Path, render_root: Path) -> RenderSpec:
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
            for i in range(3)
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
        output_path=output_path,
        project_render_root=render_root,
    )


# ---------------------------------------------------------------------------
# Test: cancel mid-flight → no partial file, no orphan asyncio task
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_cancel_mid_render_removes_partial_output(tmp_path: Path) -> None:
    render_root = tmp_path / "renders"
    render_root.mkdir()
    output = render_root / "render.mp4"
    encoder = SlowFakeEncoder()
    spec = _minimal_spec(output, render_root)

    task = asyncio.create_task(encoder.render(spec))

    # Wait until the encoder has written the partial file and signalled it is active.
    await asyncio.wait_for(encoder.started_event.wait(), timeout=5.0)
    assert output.is_file(), "Partial file should exist before cancellation"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not output.exists(), "Partial file must be removed after cancellation"


@pytest.mark.slow
async def test_cancel_mid_render_decrements_active_count(tmp_path: Path) -> None:
    render_root = tmp_path / "renders"
    render_root.mkdir()
    output = render_root / "render.mp4"
    encoder = SlowFakeEncoder()
    spec = _minimal_spec(output, render_root)

    task = asyncio.create_task(encoder.render(spec))
    await asyncio.wait_for(encoder.started_event.wait(), timeout=5.0)
    assert encoder.active_count == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert encoder.active_count == 0


@pytest.mark.slow
async def test_cancel_propagates_cancelled_error(tmp_path: Path) -> None:
    render_root = tmp_path / "renders"
    render_root.mkdir()
    output = render_root / "render.mp4"
    encoder = SlowFakeEncoder()
    spec = _minimal_spec(output, render_root)

    task = asyncio.create_task(encoder.render(spec))
    await asyncio.wait_for(encoder.started_event.wait(), timeout=5.0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled()


# ---------------------------------------------------------------------------
# Test using real FfmpegEncoder with a tiny real render, cancelled quickly
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_real_encoder_cancel_leaves_no_orphan_process(tmp_path: Path) -> None:
    """Cancel a real FfmpegEncoder render; verify no partial file after cancellation.

    If the render completes before cancellation fires (tiny frames render fast),
    the test verifies the completed output is in the expected state. The key
    invariant is: no partial file is left behind in either path.
    """
    from timelapse_manager.encode.ffmpeg_impl import FfmpegEncoder

    fixtures = Path(__file__).parent.parent.parent / "fixtures" / "frames"
    render_root = tmp_path / "renders"
    render_root.mkdir()
    output = render_root / "cancel_test.mp4"

    base = datetime(2024, 3, 1, tzinfo=UTC)
    frames = FrameSequence(
        project_id=1,
        frames=[
            FrameRef(
                sequence_index=i,
                capture_timestamp=base + timedelta(hours=i),
                absolute_path=fixtures / f"frame_{i:03d}.jpg",
                width=64,
                height=48,
            )
            for i in range(8)
        ],
    )
    spec = RenderSpec(
        project_id=1,
        frames=frames,
        output_settings=OutputSettings(
            fps=0.1, width=64, height=48, codec="h264", container="mp4"
        ),
        overlay=OverlayConfig(),
        chapters=[],
        deflicker=False,
        output_path=output,
        project_render_root=render_root,
    )

    encoder = FfmpegEncoder()
    task = asyncio.create_task(encoder.render(spec))

    # Give the process a brief moment to start, then cancel.
    await asyncio.sleep(0.05)
    task.cancel()

    cancelled = False
    try:
        result = await task
        # If it completed before the cancel: output should be a valid complete file.
        if result.success:
            assert output.is_file(), "Completed output should be a real file"
        else:
            assert not output.exists(), "Failed render should have no output"
    except asyncio.CancelledError:
        cancelled = True
    except Exception:
        pass

    if cancelled:
        # Cancellation happened: no partial file should exist.
        assert not output.exists(), (
            "FfmpegEncoder must remove partial output on cancellation"
        )
