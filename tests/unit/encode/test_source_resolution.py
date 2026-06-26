"""The "source" resolution (no explicit dimensions) omits the scale filter.

Exercises the filtergraph builder directly (no ffmpeg subprocess) so it runs in
the fast suite: a fixed resolution emits a ``scale=`` filter, while ``None``
dimensions (a "source" render) leaves scaling out entirely. Also confirms
validation accepts ``None`` dimensions rather than rejecting them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from timelapse_manager.encode.encoder import (
    FrameRef,
    FrameSequence,
    OutputSettings,
    OverlayConfig,
    RenderSpec,
)
from timelapse_manager.encode.ffmpeg_impl import FfmpegEncoder
from timelapse_manager.encode.hwaccel import EncoderChoice

_SOFTWARE_CHOICE = EncoderChoice(encoder_name="libx264", hwaccel_api=None)


def _spec(width: int | None, height: int | None) -> RenderSpec:
    frame = FrameRef(
        sequence_index=0,
        capture_timestamp=datetime(2024, 3, 1, tzinfo=UTC),
        absolute_path=Path("/tmp/frame.jpg"),
        width=64,
        height=48,
    )
    return RenderSpec(
        project_id=1,
        frames=FrameSequence(project_id=1, frames=[frame]),
        output_settings=OutputSettings(
            fps=24.0,
            width=width,
            height=height,
            codec="h264",
            container="mp4",
        ),
        overlay=OverlayConfig(),
        chapters=[],
        deflicker=False,
        output_path=Path("/tmp/renders/out.mp4"),
        project_render_root=Path("/tmp/renders"),
    )


def test_fixed_resolution_emits_scale_filter() -> None:
    encoder = FfmpegEncoder()
    graph, used = encoder._build_filtergraph(  # noqa: SLF001 - direct unit check
        _spec(1280, 720),
        base_epoch=0,
        offset_seconds=0,
        pre_filters=[],
        image_filter=[],
        choice=_SOFTWARE_CHOICE,
    )
    assert "scale=1280:720" in graph
    assert "scale" in used


def test_source_resolution_omits_scale_filter() -> None:
    encoder = FfmpegEncoder()
    graph, used = encoder._build_filtergraph(  # noqa: SLF001 - direct unit check
        _spec(None, None),
        base_epoch=0,
        offset_seconds=0,
        pre_filters=[],
        image_filter=[],
        choice=_SOFTWARE_CHOICE,
    )
    assert "scale" not in graph
    assert "scale" not in used


async def test_validate_accepts_source_dimensions() -> None:
    encoder = FfmpegEncoder()
    # Must not raise for None width/height.
    await encoder.validate(
        OutputSettings(
            fps=24.0, width=None, height=None, codec="h264", container="mp4"
        ),
        has_chapters=False,
    )


async def test_validate_still_rejects_bad_codec_with_source() -> None:
    from timelapse_manager.encode.encoder import EncoderCapabilityError

    encoder = FfmpegEncoder()
    with pytest.raises(EncoderCapabilityError):
        await encoder.validate(
            OutputSettings(
                fps=24.0, width=None, height=None, codec="mpeg4", container="mp4"
            ),
            has_chapters=False,
        )
