"""The export zip is fed from a file opened with ``O_NOFOLLOW`` (#172).

Each frame's path is checked for containment earlier and is a real path, so a
symlink at the leaf at write time was swapped in after the check. It is skipped,
not read. The bytes written are those of the descriptor that was opened.

The ffmpeg side (render and thumbnail) opens the frame by path in a subprocess and
keeps the check-then-open window; see the PR notes for #172.

Refs #172.
"""

from __future__ import annotations

import logging
import os
import zipfile
from pathlib import Path

import pytest

from timelapse_manager.render import export
from timelapse_manager.render.export import _Source, _write_zip


def _source(frame_id: int, path: Path) -> _Source:
    return _Source(frame_id, path, f"{frame_id:08d}_{path.name}", None)


class TestWriteZipOpensWithoutFollowing:
    def test_regular_files_are_bundled_with_their_bytes(self, tmp_path: Path) -> None:
        first = tmp_path / "a.jpg"
        second = tmp_path / "b.png"
        first.write_bytes(b"\xff\xd8first")
        second.write_bytes(b"\x89PNGsecond")
        out = tmp_path / "out.zip"

        bundled = _write_zip(out, [_source(1, first), _source(2, second)])

        assert bundled is not None and [s.frame_id for s in bundled] == [1, 2]
        with zipfile.ZipFile(out) as archive:
            assert archive.read("00000001_a.jpg") == b"\xff\xd8first"
            assert archive.read("00000002_b.png") == b"\x89PNGsecond"
            assert archive.testzip() is None
            info = archive.getinfo("00000001_a.jpg")
            assert info.compress_type == zipfile.ZIP_STORED  # images are stored

    @pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="needs O_NOFOLLOW")
    def test_a_symlink_swapped_in_after_the_check_is_skipped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        secret = tmp_path / "secret.txt"
        secret.write_bytes(b"not a frame")
        good = tmp_path / "good.jpg"
        good.write_bytes(b"\xff\xd8good")
        swapped = tmp_path / "swapped.jpg"
        swapped.symlink_to(secret)  # what the checked real path became
        out = tmp_path / "out.zip"

        with caplog.at_level(logging.WARNING, logger=export.logger.name):
            bundled = _write_zip(out, [_source(1, swapped), _source(2, good)])

        assert bundled is not None and [s.frame_id for s in bundled] == [2]
        with zipfile.ZipFile(out) as archive:
            assert archive.namelist() == ["00000002_good.jpg"]
        assert "cannot open" in caplog.text
        assert str(secret) not in caplog.text

    def test_a_directory_in_place_of_a_file_is_skipped(self, tmp_path: Path) -> None:
        directory = tmp_path / "dir.jpg"
        directory.mkdir()
        out = tmp_path / "out.zip"

        bundled = _write_zip(out, [_source(1, directory)])

        # ``is_file`` already filters it; nothing is bundled and nothing raises.
        assert bundled == []

    def test_the_space_check_still_stops_the_write(self, tmp_path: Path) -> None:
        files = []
        for n in range(1, 4):
            path = tmp_path / f"{n}.jpg"
            path.write_bytes(b"x" * 10)
            files.append(_source(n, path))
        export_every = export._SPACE_CHECK_EVERY  # noqa: SLF001
        many = files * (export_every // len(files) + 2)
        out = tmp_path / "out.zip"

        result = _write_zip(out, many, has_room=lambda _remaining: False)

        assert result is None
