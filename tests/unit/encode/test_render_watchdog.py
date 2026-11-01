"""Unit tests for the ffmpeg render watchdog's pure parts (#67 R2).

Covers the ``-progress`` parser, the derived wall-clock ceiling, the bounded
pipe reader, the new ``render.*`` settings, and the path that carries those
settings from ``RenderSettings`` through ``build_encoder`` to the encoder. The
process-level behaviour (kill, partial removal, failure reason) is in
``tests/integration/encode/test_ffmpeg_watchdog.py``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from timelapse_manager.config.settings import RenderSettings, Settings
from timelapse_manager.encode import build_encoder
from timelapse_manager.encode.encoder import EncoderError
from timelapse_manager.encode.ffmpeg_impl import (
    _MAX_LINE_BYTES,
    EncoderTimeoutError,
    FfmpegEncoder,
    _format_seconds,
    _ProgressTracker,
    _read_lines,
    render_time_ceiling,
)
from timelapse_manager.render.queue import RenderQueue


class TestProgressTracker:
    def test_first_frame_value_counts_as_progress(self) -> None:
        assert _ProgressTracker().feed(b"frame=0\n") is True

    def test_increasing_frame_is_progress(self) -> None:
        tracker = _ProgressTracker()
        tracker.feed(b"frame=10")
        assert tracker.feed(b"frame=11") is True

    def test_repeated_frame_is_not_progress(self) -> None:
        # A wedged encode can keep emitting identical blocks.
        tracker = _ProgressTracker()
        tracker.feed(b"frame=5")
        assert tracker.feed(b"frame=5") is False
        assert tracker.feed(b"frame=4") is False

    def test_out_time_us_advances_independently(self) -> None:
        tracker = _ProgressTracker()
        tracker.feed(b"frame=5")
        tracker.feed(b"out_time_us=1000000")
        # Frame unchanged, output time moved on: still progress.
        assert tracker.feed(b"frame=5") is False
        assert tracker.feed(b"out_time_us=1040000") is True

    @pytest.mark.parametrize(
        "line",
        [
            b"out_time_us=N/A",
            b"frame=",
            b"frame=abc",
            b"progress=continue",
            b"progress=end",
            b"out_time_ms=5000000",  # ffmpeg's misnamed duplicate: ignored
            b"total_size=4096",
            b"speed=1.5x",
            b"",
            b"no equals sign",
        ],
    )
    def test_other_lines_are_ignored(self, line: bytes) -> None:
        assert _ProgressTracker().feed(line) is False

    def test_whitespace_and_crlf_are_tolerated(self) -> None:
        assert _ProgressTracker().feed(b"  frame=3\r\n") is True


class TestRenderTimeCeiling:
    def test_explicit_value_wins(self) -> None:
        assert render_time_ceiling(120.0, 1_000_000) == 120.0

    def test_derived_floor_is_one_hour(self) -> None:
        assert render_time_ceiling(0.0, 10) == 3600.0

    def test_derived_scales_two_seconds_per_frame(self) -> None:
        assert render_time_ceiling(0.0, 5000) == 10_000.0


class TestFormatSeconds:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(300.0, "300s"), (3600.0, "3600s"), (0.5, "0.5s"), (1.25, "1.25s")],
    )
    def test_format(self, value: float, expected: str) -> None:
        assert _format_seconds(value) == expected


class TestReadLines:
    async def _collect(self, data: bytes) -> list[bytes]:
        stream = asyncio.StreamReader()
        stream.feed_data(data)
        stream.feed_eof()
        lines: list[bytes] = []
        await _read_lines(stream, lines.append)
        return lines

    async def test_splits_lines_and_keeps_unterminated_tail(self) -> None:
        assert await self._collect(b"a=1\nb=2\ntail") == [b"a=1", b"b=2", b"tail"]

    async def test_overlong_line_is_truncated_not_fatal(self) -> None:
        # readline() would raise on a line over 64 KiB; the chunked reader must
        # cap it and carry on to the next line.
        lines = await self._collect(b"x" * 1_000_000 + b"\nframe=1\n")
        assert lines == [b"x" * _MAX_LINE_BYTES, b"frame=1"]


class TestEncoderConfig:
    def test_timeout_error_is_an_encoder_error(self) -> None:
        assert issubclass(EncoderTimeoutError, EncoderError)

    def test_defaults(self) -> None:
        enc = FfmpegEncoder()
        assert enc._stall_timeout_seconds == 300.0
        assert enc._max_job_seconds == 0.0

    def test_rejects_non_positive_stall_timeout(self) -> None:
        with pytest.raises(ValueError, match="stall_timeout_seconds"):
            FfmpegEncoder(stall_timeout_seconds=0)

    def test_rejects_negative_ceiling(self) -> None:
        with pytest.raises(ValueError, match="max_job_seconds"):
            FfmpegEncoder(max_job_seconds=-1)

    def test_build_encoder_passes_watchdog_limits(self) -> None:
        enc = build_encoder("ffmpeg", stall_timeout_seconds=42.0, max_job_seconds=99.0)
        assert isinstance(enc, FfmpegEncoder)
        assert enc._stall_timeout_seconds == 42.0
        assert enc._max_job_seconds == 99.0

    def test_build_encoder_with_binary_passes_watchdog_limits(self) -> None:
        enc = build_encoder(
            "ffmpeg",
            ffmpeg_binary="/opt/ff/ffmpeg",
            stall_timeout_seconds=7.0,
            max_job_seconds=8.0,
        )
        assert isinstance(enc, FfmpegEncoder)
        assert enc._stall_timeout_seconds == 7.0
        assert enc._max_job_seconds == 8.0


class TestRenderSettings:
    def test_defaults(self) -> None:
        render = RenderSettings()
        assert render.stall_timeout_seconds == 300.0
        assert render.max_job_seconds == 0.0

    def test_stall_timeout_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            RenderSettings(stall_timeout_seconds=0)

    def test_max_job_seconds_must_not_be_negative(self) -> None:
        with pytest.raises(ValidationError):
            RenderSettings(max_job_seconds=-1)

    def test_queue_builds_encoder_with_configured_limits(
        self, settings: Settings
    ) -> None:
        configured = settings.model_copy(
            update={
                "render": settings.render.model_copy(
                    update={"stall_timeout_seconds": 60.0, "max_job_seconds": 7200.0}
                )
            }
        )
        queue = RenderQueue(configured, MagicMock())
        encoder = queue._encoder
        assert isinstance(encoder, FfmpegEncoder)
        assert encoder._stall_timeout_seconds == 60.0
        assert encoder._max_job_seconds == 7200.0
