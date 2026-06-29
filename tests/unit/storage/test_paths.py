"""Unit tests for storage.paths: frame_dir, resolve_absolute, to_stored,
uses_default_layout, and the relocatability contract.
"""

from __future__ import annotations

from pathlib import Path

from timelapse_manager.config.settings import (
    CaptureSettings,
    DatabaseSettings,
    LoggingSettings,
    PathsSettings,
    Settings,
)
from timelapse_manager.storage.paths import (
    ProjectRef,
    frame_dir,
    resolve_absolute,
    to_stored,
    uses_default_layout,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(frames_root_path: Path) -> Settings:
    data_dir = frames_root_path.parent
    return Settings(
        database=DatabaseSettings(url="sqlite:///./test.db"),
        logging=LoggingSettings(level="WARNING", format="text"),
        paths=PathsSettings(
            data_dir=data_dir,
            frames_root=frames_root_path,
            token_file=data_dir / ".local-token",
        ),
        capture=CaptureSettings(autostart=False),
    )


# ---------------------------------------------------------------------------
# frame_dir: default layout vs storage_path override
# ---------------------------------------------------------------------------


class TestFrameDir:
    def test_default_layout_uses_frames_root_slash_project_id(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "frames"
        settings = _settings(root)
        proj = ProjectRef(id=7, storage_path=None)

        result = frame_dir(settings, proj)

        assert result == root / "7"

    def test_storage_path_override_ignores_frames_root(self, tmp_path: Path) -> None:
        root = tmp_path / "frames"
        override = tmp_path / "custom" / "storage"
        settings = _settings(root)
        proj = ProjectRef(id=7, storage_path=str(override))

        result = frame_dir(settings, proj)

        assert result == override

    def test_different_project_ids_yield_different_dirs(self, tmp_path: Path) -> None:
        root = tmp_path / "frames"
        settings = _settings(root)

        d1 = frame_dir(settings, ProjectRef(id=1))
        d2 = frame_dir(settings, ProjectRef(id=2))

        assert d1 != d2
        assert d1.parent == root
        assert d2.parent == root


# ---------------------------------------------------------------------------
# uses_default_layout
# ---------------------------------------------------------------------------


class TestUsesDefaultLayout:
    def test_none_storage_path_is_default_layout(self) -> None:
        assert uses_default_layout(ProjectRef(id=1, storage_path=None)) is True

    def test_empty_string_storage_path_is_falsy_default(self) -> None:
        # Empty string is falsy — treated as default layout
        assert uses_default_layout(ProjectRef(id=1, storage_path="")) is True

    def test_explicit_storage_path_is_not_default_layout(self) -> None:
        assert uses_default_layout(ProjectRef(id=1, storage_path="/custom")) is False


# ---------------------------------------------------------------------------
# to_stored
# ---------------------------------------------------------------------------


class TestToStored:
    def test_default_layout_stores_filename_only(self, tmp_path: Path) -> None:
        proj = ProjectRef(id=3, storage_path=None)
        absolute = tmp_path / "frames" / "3" / "00000001.jpg"

        stored = to_stored(proj, absolute)

        # Stored value must be just the filename, not a full path
        assert stored == "00000001.jpg"
        assert "/" not in stored

    def test_custom_storage_path_stores_absolute(self, tmp_path: Path) -> None:
        custom = tmp_path / "custom"
        proj = ProjectRef(id=3, storage_path=str(custom))
        absolute = custom / "00000001.jpg"

        stored = to_stored(proj, absolute)

        assert stored == str(absolute)
        assert Path(stored).is_absolute()

    def test_round_trip_default_layout(self, tmp_path: Path) -> None:
        """Stored filename + resolve_absolute should return original absolute path."""
        root = tmp_path / "frames"
        settings = _settings(root)
        proj = ProjectRef(id=5, storage_path=None)
        absolute = root / "5" / "00000001.jpg"

        stored = to_stored(proj, absolute)
        resolved = resolve_absolute(settings, proj.id, stored)

        assert resolved == absolute


# ---------------------------------------------------------------------------
# resolve_absolute
# ---------------------------------------------------------------------------


class TestResolveAbsolute:
    def test_absolute_stored_path_is_returned_unchanged(self, tmp_path: Path) -> None:
        root = tmp_path / "frames"
        settings = _settings(root)
        absolute_stored = str(tmp_path / "custom" / "somefile.jpg")

        result = resolve_absolute(settings, 99, absolute_stored)

        assert result == Path(absolute_stored)

    def test_relative_stored_path_resolved_under_frames_root(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "frames"
        settings = _settings(root)

        result = resolve_absolute(settings, 42, "00000003.jpg")

        assert result == root / "42" / "00000003.jpg"

    def test_relative_path_uses_correct_project_id_subdir(self, tmp_path: Path) -> None:
        root = tmp_path / "frames"
        settings = _settings(root)

        result1 = resolve_absolute(settings, 10, "frame.jpg")
        result2 = resolve_absolute(settings, 20, "frame.jpg")

        assert result1 == root / "10" / "frame.jpg"
        assert result2 == root / "20" / "frame.jpg"
        assert result1 != result2


# ---------------------------------------------------------------------------
# Relocatability: move frames_root; relative paths still resolve correctly
# ---------------------------------------------------------------------------


class TestRelocatability:
    def test_relative_path_resolves_after_frames_root_move(
        self, tmp_path: Path
    ) -> None:
        """Changing frames_root in settings resolves relative paths to new location.

        This is the whole point of relative storage: the tree can be moved
        to a new disk/path and rows remain valid without rewriting.
        """
        old_root = tmp_path / "old_frames"
        new_root = tmp_path / "new_frames"
        new_settings = _settings(new_root)
        proj = ProjectRef(id=9, storage_path=None)

        # Stored using the old layout (only the filename is kept)
        absolute = old_root / "9" / "00000001.jpg"
        stored = to_stored(proj, absolute)
        assert stored == "00000001.jpg"  # just the filename

        # After move, resolves to new location — no DB rewrite needed
        resolved_new = resolve_absolute(new_settings, 9, stored)
        assert resolved_new == new_root / "9" / "00000001.jpg"

    def test_absolute_path_does_not_relocate(self, tmp_path: Path) -> None:
        """Absolute stored paths are pinned — a move leaves them pointing at old loc."""
        old_root = tmp_path / "old_frames"
        new_root = tmp_path / "new_frames"
        new_settings = _settings(new_root)
        absolute_stored = str(old_root / "project_x" / "somefile.jpg")

        resolved = resolve_absolute(new_settings, 1, absolute_stored)

        # Absolute paths are returned as-is regardless of frames_root
        assert resolved == Path(absolute_stored)
        assert "old_frames" in str(resolved)
