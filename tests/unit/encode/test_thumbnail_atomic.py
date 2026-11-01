"""Atomic thumbnail writes and the generation cap (#67 P3).

:func:`generate_thumbnail_atomic` must leave either a complete thumbnail at the
destination or nothing new at all, and :func:`generation_slot` must bound how
many generations run at once. ffmpeg is replaced by a stand-in that writes (or
fails to write) the output path, so no binary is needed.
"""

from __future__ import annotations

import contextlib
import struct
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from timelapse_manager.encode import thumbnail
from timelapse_manager.storage.paths import THUMBNAIL_TEMP_MARKER


def _png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


@pytest.fixture()
def source(tmp_path: Path) -> Path:
    path = tmp_path / "frame.png"
    path.write_bytes(_png(640, 480))
    return path


def _install(monkeypatch: pytest.MonkeyPatch, behaviour: str) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(args: list[str], *a: Any, **k: Any) -> None:
        calls.append(list(args))
        out = Path(args[-1])
        if behaviour == "ok":
            out.write_bytes(b"THUMB")
        elif behaviour == "empty":
            out.write_bytes(b"")
        elif behaviour == "partial-then-fail":
            out.write_bytes(b"TH")
            raise subprocess.CalledProcessError(1, args)
        elif behaviour == "timeout":
            out.write_bytes(b"TH")
            raise subprocess.TimeoutExpired(args, 20.0)
        elif behaviour == "nothing":
            pass

    monkeypatch.setattr(thumbnail.subprocess, "run", run)
    return calls


def _call(source: Path, dest: Path) -> None:
    thumbnail.generate_thumbnail_atomic(
        "ffmpeg", str(source), dest, width=320, max_dimension_px=16000
    )


class TestAtomicWrite:
    def test_success_renames_temp_into_place(
        self, tmp_path: Path, source: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _install(monkeypatch, "ok")
        dest = tmp_path / "cache" / "7.jpg"

        _call(source, dest)

        assert dest.read_bytes() == b"THUMB"
        assert sorted(p.name for p in dest.parent.iterdir()) == ["7.jpg"]
        out = Path(calls[0][-1])
        assert out.parent == dest.parent
        assert out.name.startswith(f"7{THUMBNAIL_TEMP_MARKER}")
        assert out.suffix == ".jpg"  # ffmpeg picks the muxer from it

    def test_temp_names_are_unique(
        self, tmp_path: Path, source: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _install(monkeypatch, "ok")
        dest = tmp_path / "7.jpg"

        _call(source, dest)
        _call(source, dest)

        assert calls[0][-1] != calls[1][-1]

    @pytest.mark.parametrize(
        ("behaviour", "error"),
        [
            ("partial-then-fail", subprocess.CalledProcessError),
            ("timeout", subprocess.TimeoutExpired),
            ("empty", thumbnail.ThumbnailOutputError),
            ("nothing", FileNotFoundError),
        ],
    )
    def test_failure_leaves_no_file(
        self,
        tmp_path: Path,
        source: Path,
        monkeypatch: pytest.MonkeyPatch,
        behaviour: str,
        error: type[BaseException],
    ) -> None:
        _install(monkeypatch, behaviour)
        cache = tmp_path / "cache"
        dest = cache / "7.jpg"

        with pytest.raises(error):
            _call(source, dest)

        assert list(cache.iterdir()) == []

    def test_failure_keeps_the_previous_thumbnail_intact(
        self, tmp_path: Path, source: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, "partial-then-fail")
        dest = tmp_path / "7.jpg"
        dest.write_bytes(b"OLD COMPLETE THUMB")

        with pytest.raises(subprocess.CalledProcessError):
            _call(source, dest)

        assert dest.read_bytes() == b"OLD COMPLETE THUMB"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["7.jpg", "frame.png"]

    def test_empty_output_error_is_a_subprocess_error(self) -> None:
        # The route's fallback catches SubprocessError; this must be one.
        assert issubclass(thumbnail.ThumbnailOutputError, subprocess.SubprocessError)


class TestGenerationSlot:
    def test_at_most_limit_slots_are_held(self) -> None:
        with contextlib.ExitStack() as stack:
            held = [
                stack.enter_context(thumbnail.generation_slot(2, timeout=0.01))
                for _ in range(3)
            ]
        assert held == [True, True, False]

    def test_slot_is_released_on_exit_and_on_error(self) -> None:
        with thumbnail.generation_slot(1, timeout=0.01) as first:
            assert first
        with (
            pytest.raises(RuntimeError),
            thumbnail.generation_slot(1, timeout=0.01) as second,
        ):
            assert second
            raise RuntimeError("generation blew up")
        with thumbnail.generation_slot(1, timeout=0.01) as third:
            assert third

    def test_default_timeout_is_read_at_call_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(thumbnail, "SLOT_TIMEOUT_SECONDS", 0.01)
        with (
            thumbnail.generation_slot(1) as first,
            thumbnail.generation_slot(1) as second,
        ):
            assert (first, second) == (True, False)

    def test_waiter_gets_the_slot_once_released(self) -> None:
        got: list[bool] = []
        with thumbnail.generation_slot(1, timeout=0.01):

            def waiter() -> None:
                with thumbnail.generation_slot(1, timeout=5.0) as acquired:
                    got.append(acquired)

            worker = threading.Thread(target=waiter)
            worker.start()
        worker.join(timeout=5.0)
        assert got == [True]

    def test_limit_below_one_is_refused(self) -> None:
        with (
            pytest.raises(ValueError),
            thumbnail.generation_slot(0, timeout=0.01),
        ):
            pass
