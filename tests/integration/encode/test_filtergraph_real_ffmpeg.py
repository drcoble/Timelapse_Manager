"""Real-ffmpeg checks for the #67 S4/S15 escaping (skipped when ffmpeg is absent).

The unit tests read the built strings back through a *model* of ffmpeg's
parsers. These run the real binary, the CI release build's pinned ffmpeg, on
the same hostile values:

* a render with a hostile caption and a colon-heavy timestamp format succeeds:
  the graph parses, drawtext accepts its options and expansion, and the concat
  input opens local frames under ``-protocol_whitelist file``;
* hostile chapter titles come back from ffprobe one chapter each, unchanged.

Unlike ``test_ffmpeg_render.py``, these skip rather than fail without ffmpeg, so
the local no-ffmpeg baseline is unchanged.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from timelapse_manager.encode.encoder import (
    Chapter,
    FrameRef,
    FrameSequence,
    OutputSettings,
    OverlayConfig,
    RenderSpec,
)
from timelapse_manager.encode.ffmpeg_impl import FfmpegEncoder, _find_default_font

_FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "frames"
_FFMPEG = shutil.which("ffmpeg")
_FFPROBE = shutil.which("ffprobe")

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(_FFMPEG is None, reason="needs a real ffmpeg on PATH"),
]

_CAPTIONS = [
    "x',movie=/etc/passwd",
    "[a];movie=x",
    "\\%{localtime} 100% a:b:c \\ end\\",
    "é 日本語 -dash ,;[] 'quoted'",
]


def _spec(tmp_path: Path, overlay: OverlayConfig, **kwargs: object) -> RenderSpec:
    base = datetime(2024, 3, 1, 0, tzinfo=UTC)
    frames = [
        FrameRef(
            sequence_index=i,
            capture_timestamp=base + timedelta(hours=i),
            absolute_path=_FIXTURES / f"frame_{i:03d}.jpg",
            width=64,
            height=48,
        )
        for i in range(4)
    ]
    render_root = tmp_path / "renders"
    render_root.mkdir(exist_ok=True)
    container = str(kwargs.get("container", "mp4"))
    return RenderSpec(
        project_id=1,
        frames=FrameSequence(project_id=1, frames=frames),
        output_settings=OutputSettings(
            fps=2.0, width=64, height=48, codec="h264", container=container, crf=30
        ),
        overlay=overlay,
        chapters=kwargs.get("chapters", []),  # type: ignore[arg-type]
        deflicker=False,
        output_path=render_root / f"out.{container}",
        project_render_root=render_root,
    )


@pytest.mark.skipif(_find_default_font() is None, reason="needs a default font")
@pytest.mark.parametrize("caption", _CAPTIONS)
async def test_hostile_caption_and_format_render(tmp_path: Path, caption: str) -> None:
    overlay = OverlayConfig(
        timestamp_enabled=True,
        timestamp_format="%Y-%m-%d %H:%M:%S %%",
        text_enabled=True,
        text_content=caption,
    )

    result = await FfmpegEncoder().render(_spec(tmp_path, overlay))

    assert result.success, result.error


@pytest.mark.skipif(_FFPROBE is None, reason="needs ffprobe on PATH")
async def test_hostile_chapter_titles_read_back(tmp_path: Path) -> None:
    titles = ["a=b;c#d", "ends with backslash\\", "line\nbreak", "[CHAPTER]"]
    chapters = [
        Chapter(timecode_seconds=float(i) * 0.5, label=t) for i, t in enumerate(titles)
    ]

    result = await FfmpegEncoder().render(
        _spec(tmp_path, OverlayConfig(), chapters=chapters, container="mkv")
    )

    assert result.success, result.error
    assert _FFPROBE is not None
    probe = subprocess.run(
        [
            _FFPROBE,
            "-v",
            "error",
            "-show_chapters",
            "-of",
            "json",
            str(result.output_path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    read_back = [c["tags"]["title"] for c in json.loads(probe.stdout)["chapters"]]
    assert read_back == ["a=b;c#d", "ends with backslash", "line break", "[CHAPTER]"]
