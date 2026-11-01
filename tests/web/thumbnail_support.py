"""A stand-in for ffmpeg in the thumbnail route tests (#67 P3, #70 F-P2).

This Mac and most dev machines have no ffmpeg, so these tests replace
``subprocess.run`` as seen by :mod:`timelapse_manager.encode.thumbnail` with a
recorder that writes a small fake JPEG to the output path (``argv[-1]``), or
fails in a chosen way. That exercises everything around the ffmpeg call -- the
size whitelist, cache naming, the atomic write, the generation cap and the
fallbacks -- without a real binary. The real downscale is covered by
``TestFrameThumbnail`` in ``test_frame_image.py``, which needs ffmpeg (CI).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from timelapse_manager.encode import thumbnail

FAKE_THUMB_BYTES = b"\xff\xd8FAKE-THUMBNAIL\xff\xd9"


@dataclass
class FakeFfmpeg:
    """Records each ffmpeg argv; ``mode`` picks what the next runs do.

    * ``"ok"`` writes :data:`FAKE_THUMB_BYTES` to the output path;
    * ``"partial-then-fail"`` writes half a file, then exits non-zero;
    * ``"empty"`` writes a zero-byte file and exits zero.
    """

    mode: str = "ok"
    calls: list[list[str]] = field(default_factory=list)

    def run(self, args: list[str], *a: Any, **k: Any) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        self.calls.append(list(args))
        out = Path(args[-1])
        if self.mode == "ok":
            out.write_bytes(FAKE_THUMB_BYTES)
        elif self.mode == "partial-then-fail":
            out.write_bytes(FAKE_THUMB_BYTES[:6])
            raise subprocess.CalledProcessError(1, args)
        elif self.mode == "empty":
            out.write_bytes(b"")
        else:  # pragma: no cover - a test typo
            raise AssertionError(f"unknown fake ffmpeg mode {self.mode!r}")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    def scale_widths(self) -> list[str]:
        """Return the ``-vf`` value of each recorded run."""
        return [argv[argv.index("-vf") + 1] for argv in self.calls]


def install_fake_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> FakeFfmpeg:
    """Install a :class:`FakeFfmpeg` in place of the thumbnail ffmpeg spawn."""
    fake = FakeFfmpeg()
    monkeypatch.setattr(thumbnail.subprocess, "run", fake.run)
    return fake


def cache_entries(cache_dir: Path) -> list[str]:
    """Return the sorted names in a thumbnail cache dir (empty if missing)."""
    if not cache_dir.is_dir():
        return []
    return sorted(entry.name for entry in cache_dir.iterdir())
