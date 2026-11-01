"""Thumbnail cache naming, containment and cleanup (#67 P3, R12).

Covers :func:`~timelapse_manager.storage.paths.contained_thumbnail_dir` (the
cache directory may not resolve outside ``<data_dir>/thumbnails``), the cache
file names per size, and the best-effort removals in
:mod:`timelapse_manager.storage.thumbnail_cache`: per frame, per project, and
the housekeeping sweep of orphaned project directories and stale temp files.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from timelapse_manager.config.settings import PathsSettings, Settings
from timelapse_manager.storage import paths
from timelapse_manager.storage.thumbnail_cache import (
    TEMP_FILE_MAX_AGE_SECONDS,
    prune_thumbnail_cache,
    remove_frame_thumbnails,
    remove_project_thumbnails,
)


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(paths=PathsSettings(data_dir=tmp_path / "data"))


def _cache(settings: Settings, project_id: int, *names: str) -> Path:
    cache_dir = paths.thumbnail_cache_dir(settings, project_id)
    cache_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (cache_dir / name).write_bytes(b"T")
    return cache_dir


def _age(path: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then), follow_symlinks=False)


class TestNamingAndContainment:
    def test_file_names_per_size(self) -> None:
        assert paths.thumbnail_file_name(42, 320) == "42.jpg"
        assert paths.thumbnail_file_name(42, 640) == "42_640.jpg"

    def test_unknown_size_is_refused(self) -> None:
        with pytest.raises(ValueError):
            paths.thumbnail_file_name(42, 1024)

    def test_contained_dir_is_under_the_root(self, settings: Settings) -> None:
        got = paths.contained_thumbnail_dir(settings, 5)
        root = Path(os.path.realpath(paths.thumbnail_cache_root(settings)))
        assert got == root / "5"

    def test_symlink_out_of_root_is_refused(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        root = paths.thumbnail_cache_root(settings)
        root.mkdir(parents=True)
        (root / "5").symlink_to(outside, target_is_directory=True)

        with pytest.raises(paths.PathContainmentError):
            paths.contained_thumbnail_dir(settings, 5)

    def test_symlink_to_the_root_itself_is_refused(self, settings: Settings) -> None:
        root = paths.thumbnail_cache_root(settings)
        root.mkdir(parents=True)
        (root / "5").symlink_to(root, target_is_directory=True)

        with pytest.raises(paths.PathContainmentError):
            paths.contained_thumbnail_dir(settings, 5)

    def test_symlinked_root_is_followed_consistently(self, tmp_path: Path) -> None:
        # An operator may put the whole data dir (or the cache) on another disk.
        real_data = tmp_path / "disk2"
        real_data.mkdir()
        link = tmp_path / "data-link"
        link.symlink_to(real_data, target_is_directory=True)
        settings = Settings(paths=PathsSettings(data_dir=link))

        got = paths.contained_thumbnail_dir(settings, 3)

        assert got == Path(os.path.realpath(real_data)) / "thumbnails" / "3"


class TestRemoveFrameThumbnails:
    def test_removes_every_size_of_that_frame_only(self, settings: Settings) -> None:
        cache_dir = _cache(settings, 1, "7.jpg", "7_640.jpg", "8.jpg", "70.jpg")

        remove_frame_thumbnails(settings, 1, 7)

        assert sorted(p.name for p in cache_dir.iterdir()) == ["70.jpg", "8.jpg"]

    def test_missing_files_and_dir_are_fine(self, settings: Settings) -> None:
        remove_frame_thumbnails(settings, 1, 7)  # no cache dir at all
        _cache(settings, 1)
        remove_frame_thumbnails(settings, 1, 7)  # empty cache dir

    def test_escaped_cache_is_left_alone(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "7.jpg").write_bytes(b"KEEP")
        root = paths.thumbnail_cache_root(settings)
        root.mkdir(parents=True)
        (root / "1").symlink_to(outside, target_is_directory=True)

        remove_frame_thumbnails(settings, 1, 7)

        assert (outside / "7.jpg").read_bytes() == b"KEEP"


class TestRemoveProjectThumbnails:
    def test_removes_the_whole_directory(self, settings: Settings) -> None:
        cache_dir = _cache(settings, 2, "1.jpg", "1_640.jpg")
        other = _cache(settings, 3, "9.jpg")

        remove_project_thumbnails(settings, 2)

        assert not cache_dir.exists()
        assert (other / "9.jpg").exists()

    def test_missing_directory_is_fine(self, settings: Settings) -> None:
        remove_project_thumbnails(settings, 2)

    def test_escaped_directory_is_left_alone(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "precious").write_bytes(b"KEEP")
        root = paths.thumbnail_cache_root(settings)
        root.mkdir(parents=True)
        (root / "2").symlink_to(outside, target_is_directory=True)

        remove_project_thumbnails(settings, 2)

        assert (outside / "precious").read_bytes() == b"KEEP"


class TestPruneThumbnailCache:
    def test_missing_root_is_a_no_op(self, settings: Settings) -> None:
        result = prune_thumbnail_cache(settings, lambda: set(), now=time.time())
        assert (result.orphan_dirs, result.temp_files) == (0, 0)

    def test_removes_orphaned_project_dirs_only(self, settings: Settings) -> None:
        live = _cache(settings, 1, "5.jpg")
        orphan = _cache(settings, 2, "6.jpg", "6_640.jpg")

        result = prune_thumbnail_cache(settings, lambda: {1}, now=time.time())

        assert result.orphan_dirs == 1
        assert (live / "5.jpg").exists()
        assert not orphan.exists()

    def test_leaves_unrecognised_entries_and_symlinks(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        root = paths.thumbnail_cache_root(settings)
        (root / "notes").mkdir(parents=True)
        (root / "README").write_bytes(b"x")
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "precious").write_bytes(b"KEEP")
        (root / "9").symlink_to(outside, target_is_directory=True)

        result = prune_thumbnail_cache(settings, lambda: set(), now=time.time())

        assert result.orphan_dirs == 0
        assert (root / "notes").is_dir()
        assert (root / "README").exists()
        assert (root / "9").is_symlink()
        assert (outside / "precious").read_bytes() == b"KEEP"

    def test_removes_only_stale_temp_files(self, settings: Settings) -> None:
        cache_dir = _cache(
            settings,
            1,
            "5.jpg",
            "5.tmp-1-aaaa.jpg",
            "6_640.tmp-1-bbbb.jpg",
        )
        _age(cache_dir / "5.tmp-1-aaaa.jpg", TEMP_FILE_MAX_AGE_SECONDS + 60)
        _age(cache_dir / "5.jpg", TEMP_FILE_MAX_AGE_SECONDS + 60)

        result = prune_thumbnail_cache(settings, lambda: {1}, now=time.time())

        assert result.temp_files == 1
        # The finished thumbnail and the in-progress (fresh) temp file stay.
        assert sorted(p.name for p in cache_dir.iterdir()) == [
            "5.jpg",
            "6_640.tmp-1-bbbb.jpg",
        ]

    def test_lists_the_cache_before_reading_project_ids(
        self, settings: Settings
    ) -> None:
        # A project whose directory appears after the listing (created during
        # the pass) is not in the listing, so it can never be taken for an
        # orphan even though the id set was read too early to include it.
        def ids_while_a_project_is_created() -> set[int]:
            _cache(settings, 4, "1.jpg")
            return {1}

        _cache(settings, 1, "1.jpg")

        prune_thumbnail_cache(settings, ids_while_a_project_is_created, now=time.time())

        assert (paths.thumbnail_cache_dir(settings, 4) / "1.jpg").exists()
