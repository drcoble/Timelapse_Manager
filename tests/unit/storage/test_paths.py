"""Unit tests for storage.paths: frame_dir, resolve_absolute, to_stored,
uses_default_layout, the relocatability contract, the storage_path policy
(#67 S5) and frame-path containment (#67 S16).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from timelapse_manager.config.settings import (
    CaptureSettings,
    DatabaseSettings,
    LoggingSettings,
    PathsSettings,
    Settings,
    StorageSettings,
)
from timelapse_manager.storage.paths import (
    PathContainmentError,
    ProjectRef,
    StoragePathError,
    frame_dir,
    resolve_absolute,
    resolve_contained_frame_path,
    to_stored,
    uses_default_layout,
    validate_storage_path,
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


# ---------------------------------------------------------------------------
# validate_storage_path: the storage_path policy (#67 S5)
# ---------------------------------------------------------------------------


def _policy_settings(
    tmp_path: Path, *, allowed_roots: list[Path] | None = None
) -> Settings:
    """Settings with data_dir=<tmp>/data and the default frames root under it."""
    data_dir = tmp_path / "data"
    (data_dir / "frames").mkdir(parents=True)
    return Settings(
        database=DatabaseSettings(url=f"sqlite:///{data_dir / 'timelapse.db'}"),
        logging=LoggingSettings(level="WARNING", format="text"),
        paths=PathsSettings(data_dir=data_dir),
        capture=CaptureSettings(autostart=False),
        storage=StorageSettings(allowed_roots=allowed_roots),
    )


def _real(path: Path) -> str:
    return os.path.realpath(path)


@pytest.fixture()
def no_tlm_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TLM_CONFIG", raising=False)


@pytest.mark.usefixtures("no_tlm_config")
class TestValidateStoragePath:
    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_blank_means_default_layout(self, tmp_path: Path, raw: str | None) -> None:
        settings = _policy_settings(tmp_path)
        assert validate_storage_path(raw, settings, is_admin=False) is None

    @pytest.mark.parametrize("bad", ["\n", "\r", "\x00", "\x1b", "\x7f", " "])
    def test_control_characters_rejected(self, tmp_path: Path, bad: str) -> None:
        settings = _policy_settings(tmp_path)
        raw = f"{tmp_path}/data/frames/a{bad}b"
        with pytest.raises(StoragePathError, match="control characters"):
            validate_storage_path(raw, settings, is_admin=True)

    def test_newline_concat_injection_rejected(self, tmp_path: Path) -> None:
        settings = _policy_settings(tmp_path)
        raw = f"{tmp_path}/data/frames/x\nfile http://internal/"
        with pytest.raises(StoragePathError):
            validate_storage_path(raw, settings, is_admin=True)

    def test_relative_path_rejected(self, tmp_path: Path) -> None:
        settings = _policy_settings(tmp_path)
        with pytest.raises(StoragePathError, match="absolute"):
            validate_storage_path("frames/x", settings, is_admin=True)

    def test_dotdot_component_rejected(self, tmp_path: Path) -> None:
        settings = _policy_settings(tmp_path)
        raw = f"{tmp_path}/data/frames/../x"
        with pytest.raises(StoragePathError, match=r"'\.\.'"):
            validate_storage_path(raw, settings, is_admin=True)

    def test_unencodable_text_rejected(self, tmp_path: Path) -> None:
        settings = _policy_settings(tmp_path)
        with pytest.raises(StoragePathError, match="invalid characters"):
            validate_storage_path(f"{tmp_path}/\udc80", settings, is_admin=True)

    def test_overlong_path_rejected(self, tmp_path: Path) -> None:
        settings = _policy_settings(tmp_path)
        raw = f"{tmp_path}/data/frames/" + "a" * 5000
        with pytest.raises(StoragePathError, match="longer than"):
            validate_storage_path(raw, settings, is_admin=True)

    @pytest.mark.parametrize("is_admin", [True, False])
    def test_data_dir_rejected(self, tmp_path: Path, is_admin: bool) -> None:
        settings = _policy_settings(tmp_path)
        with pytest.raises(StoragePathError):
            validate_storage_path(str(tmp_path / "data"), settings, is_admin=is_admin)

    @pytest.mark.parametrize("is_admin", [True, False])
    def test_inside_data_dir_rejected(self, tmp_path: Path, is_admin: bool) -> None:
        settings = _policy_settings(tmp_path)
        raw = str(tmp_path / "data" / "elsewhere")
        with pytest.raises(StoragePathError, match="data, config"):
            validate_storage_path(raw, settings, is_admin=is_admin)

    @pytest.mark.parametrize("sub", ["thumbnails", "reports"])
    def test_thumbnails_and_reports_rejected(self, tmp_path: Path, sub: str) -> None:
        settings = _policy_settings(tmp_path)
        raw = str(tmp_path / "data" / sub / "1")
        with pytest.raises(StoragePathError):
            validate_storage_path(raw, settings, is_admin=True)

    def test_frames_root_itself_rejected(self, tmp_path: Path) -> None:
        # Its parent is data_dir: renders would land beside the database.
        settings = _policy_settings(tmp_path)
        raw = str(tmp_path / "data" / "frames")
        with pytest.raises(StoragePathError, match="frames root"):
            validate_storage_path(raw, settings, is_admin=True)

    def test_ancestor_of_data_dir_rejected(self, tmp_path: Path) -> None:
        settings = _policy_settings(tmp_path)
        for raw in ("/", str(tmp_path)):
            with pytest.raises(StoragePathError, match="must not contain"):
                validate_storage_path(raw, settings, is_admin=True)

    def test_double_leading_slash_still_lands_in_data_dir(self, tmp_path: Path) -> None:
        settings = _policy_settings(tmp_path)
        raw = "/" + str(tmp_path / "data" / "x")
        with pytest.raises(StoragePathError):
            validate_storage_path(raw, settings, is_admin=True)

    def test_subdir_of_frames_root_accepted_and_resolved(self, tmp_path: Path) -> None:
        settings = _policy_settings(tmp_path)
        raw = str(tmp_path / "data" / "frames" / "x") + "/"
        stored = validate_storage_path(raw, settings, is_admin=False)
        assert stored == _real(tmp_path / "data" / "frames" / "x")

    def test_symlinked_parent_into_data_dir_rejected(self, tmp_path: Path) -> None:
        settings = _policy_settings(tmp_path)
        (tmp_path / "outside").mkdir()
        (tmp_path / "outside" / "link").symlink_to(tmp_path / "data")
        raw = str(tmp_path / "outside" / "link" / "planted")
        with pytest.raises(StoragePathError):
            validate_storage_path(raw, settings, is_admin=True)

    def test_symlink_into_frames_root_stores_the_real_path(
        self, tmp_path: Path
    ) -> None:
        settings = _policy_settings(tmp_path)
        (tmp_path / "alias").symlink_to(tmp_path / "data" / "frames")
        stored = validate_storage_path(
            str(tmp_path / "alias" / "cam"), settings, is_admin=False
        )
        assert stored == _real(tmp_path / "data" / "frames" / "cam")

    def test_operator_outside_allowed_roots_rejected(self, tmp_path: Path) -> None:
        settings = _policy_settings(tmp_path)
        raw = str(tmp_path / "srv" / "frames")
        with pytest.raises(StoragePathError, match="allowed_roots"):
            validate_storage_path(raw, settings, is_admin=False)

    def test_admin_outside_allowed_roots_accepted(self, tmp_path: Path) -> None:
        settings = _policy_settings(tmp_path)
        raw = str(tmp_path / "srv" / "frames")
        stored = validate_storage_path(raw, settings, is_admin=True)
        assert stored == _real(tmp_path / "srv" / "frames")

    def test_configured_allowed_root_admits_operator(self, tmp_path: Path) -> None:
        srv = tmp_path / "srv"
        settings = _policy_settings(tmp_path, allowed_roots=[srv])
        stored = validate_storage_path(str(srv / "cam"), settings, is_admin=False)
        assert stored == _real(srv / "cam")
        # The parent must be inside the root too: the root itself is refused.
        with pytest.raises(StoragePathError, match="allowed_roots"):
            validate_storage_path(str(srv), settings, is_admin=False)
        # An explicit root list replaces the frames-root default.
        with pytest.raises(StoragePathError, match="allowed_roots"):
            validate_storage_path(
                str(tmp_path / "data" / "frames" / "x"), settings, is_admin=False
            )

    def test_empty_allowed_roots_is_admin_only(self, tmp_path: Path) -> None:
        settings = _policy_settings(tmp_path, allowed_roots=[])
        raw = str(tmp_path / "data" / "frames" / "x")
        with pytest.raises(StoragePathError, match="allowed_roots"):
            validate_storage_path(raw, settings, is_admin=False)
        assert validate_storage_path(raw, settings, is_admin=True) == _real(
            tmp_path / "data" / "frames" / "x"
        )

    def test_sibling_prefix_is_not_inside(self, tmp_path: Path) -> None:
        # "/…/frames-old" shares a string prefix with "/…/frames" but is not in it.
        settings = _policy_settings(tmp_path)
        raw = str(tmp_path / "data" / "frames-old" / "x")
        with pytest.raises(StoragePathError):
            validate_storage_path(raw, settings, is_admin=False)

    def test_config_dir_from_env_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        etc = tmp_path / "etc" / "timelapse-manager"
        etc.mkdir(parents=True)
        monkeypatch.setenv("TLM_CONFIG", str(etc / "config.yml"))
        settings = _policy_settings(tmp_path)
        with pytest.raises(StoragePathError):
            validate_storage_path(str(etc / "frames"), settings, is_admin=True)
        with pytest.raises(StoragePathError, match="must not contain"):
            validate_storage_path(str(tmp_path / "etc"), settings, is_admin=True)

    def test_tls_and_database_dirs_rejected(self, tmp_path: Path) -> None:
        settings = _policy_settings(tmp_path)
        certs = tmp_path / "certs"
        db_dir = tmp_path / "db"
        settings = settings.model_copy(
            update={
                "tls": settings.tls.model_copy(
                    update={"cert_path": str(certs / "c.pem")}
                ),
                "database": settings.database.model_copy(
                    update={"url": f"sqlite:///{db_dir / 'x.db'}"}
                ),
            }
        )
        for directory in (certs, db_dir):
            with pytest.raises(StoragePathError):
                validate_storage_path(str(directory / "f"), settings, is_admin=True)


class TestAllowedRootsSetting:
    def test_default_is_none(self) -> None:
        assert StorageSettings().allowed_roots is None

    def test_delimited_string_is_split(self) -> None:
        parsed = StorageSettings(allowed_roots="/srv/a, /srv/b")  # type: ignore[arg-type]
        assert parsed.allowed_roots == [Path("/srv/a"), Path("/srv/b")]

    def test_relative_root_refused(self) -> None:
        with pytest.raises(ValueError, match="absolute"):
            StorageSettings(allowed_roots=[Path("srv")])

    @pytest.mark.parametrize(
        "env_value", ["/srv/a,/srv/b", '["/srv/a", "/srv/b"]', "/srv/a /srv/b"]
    )
    def test_env_forms(self, monkeypatch: pytest.MonkeyPatch, env_value: str) -> None:
        from timelapse_manager.config import load_settings

        monkeypatch.delenv("TLM_CONFIG", raising=False)
        monkeypatch.setenv("TLM_STORAGE__ALLOWED_ROOTS", env_value)
        settings = load_settings()
        assert settings.storage.allowed_roots == [Path("/srv/a"), Path("/srv/b")]

    def test_env_empty_list_means_admin_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from timelapse_manager.config import load_settings

        monkeypatch.delenv("TLM_CONFIG", raising=False)
        monkeypatch.setenv("TLM_STORAGE__ALLOWED_ROOTS", "[]")
        assert load_settings().storage.allowed_roots == []


# ---------------------------------------------------------------------------
# resolve_contained_frame_path: frame-path containment (#67 S16)
# ---------------------------------------------------------------------------


class TestResolveContainedFramePath:
    def test_relative_frame_in_default_dir_resolves(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path / "frames")
        (tmp_path / "frames" / "4").mkdir(parents=True)
        (tmp_path / "frames" / "4" / "a.jpg").write_bytes(b"x")
        real = resolve_contained_frame_path(
            settings, ProjectRef(id=4, storage_path=None), "a.jpg"
        )
        assert str(real) == _real(tmp_path / "frames" / "4" / "a.jpg")

    def test_traversal_raises(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path / "frames")
        with pytest.raises(PathContainmentError):
            resolve_contained_frame_path(
                settings, ProjectRef(id=4, storage_path=None), "../5/a.jpg"
            )

    def test_absolute_escape_raises(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path / "frames")
        with pytest.raises(PathContainmentError):
            resolve_contained_frame_path(
                settings, ProjectRef(id=4, storage_path=None), "/etc/passwd"
            )

    def test_symlink_out_of_tree_raises(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path / "frames")
        (tmp_path / "frames" / "4").mkdir(parents=True)
        (tmp_path / "secret").write_bytes(b"s")
        (tmp_path / "frames" / "4" / "a.jpg").symlink_to(tmp_path / "secret")
        with pytest.raises(PathContainmentError):
            resolve_contained_frame_path(
                settings, ProjectRef(id=4, storage_path=None), "a.jpg"
            )

    def test_nul_in_stored_path_raises(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path / "frames")
        with pytest.raises(PathContainmentError):
            resolve_contained_frame_path(
                settings, ProjectRef(id=4, storage_path=None), "a\x00.jpg"
            )

    def test_custom_storage_frame_resolves(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path / "frames")
        custom = tmp_path / "custom"
        custom.mkdir()
        (custom / "b.jpg").write_bytes(b"x")
        real = resolve_contained_frame_path(
            settings, ProjectRef(id=4, storage_path=str(custom)), str(custom / "b.jpg")
        )
        assert str(real) == _real(custom / "b.jpg")

    def test_relative_frame_from_before_a_storage_path_change(
        self, tmp_path: Path
    ) -> None:
        # Captured under the default layout (relative), then storage_path set:
        # the frame still lives in frames_root/<id> and stays renderable.
        settings = _settings(tmp_path / "frames")
        (tmp_path / "frames" / "4").mkdir(parents=True)
        (tmp_path / "frames" / "4" / "old.jpg").write_bytes(b"x")
        project = ProjectRef(id=4, storage_path=str(tmp_path / "custom"))
        real = resolve_contained_frame_path(settings, project, "old.jpg")
        assert str(real) == _real(tmp_path / "frames" / "4" / "old.jpg")
        # ... but not another project's default directory.
        (tmp_path / "frames" / "5").mkdir()
        with pytest.raises(PathContainmentError):
            resolve_contained_frame_path(
                settings, project, str(tmp_path / "frames" / "5" / "x.jpg")
            )


# ---------------------------------------------------------------------------
# Identity, not strings, decides containment (security review on #67 S5)
# ---------------------------------------------------------------------------


def _case_variant(path: Path) -> Path:
    """``path`` with its last component's case swapped."""
    return path.with_name(path.name.swapcase())


def _case_insensitive(directory: Path) -> bool:
    variant = _case_variant(directory)
    return variant != directory and variant.exists()


@pytest.mark.usefixtures("no_tlm_config")
class TestIdentityContainment:
    def test_case_variant_of_data_dir_rejected(self, tmp_path: Path) -> None:
        settings = _policy_settings(tmp_path)
        data_dir = tmp_path / "data"
        if not _case_insensitive(data_dir):
            pytest.skip("filesystem is case-sensitive")
        variant = _case_variant(data_dir)
        for raw in (str(variant / "planted"), str(variant / "new" / "deeper")):
            with pytest.raises(StoragePathError, match="data, config"):
                validate_storage_path(raw, settings, is_admin=True)
        # ... and as an ancestor: the variant spelling of tmp_path's child.
        with pytest.raises(StoragePathError, match="must not contain"):
            validate_storage_path(str(variant), settings, is_admin=True)

    def test_case_variant_of_frames_root_rejected(self, tmp_path: Path) -> None:
        settings = _policy_settings(tmp_path)
        frames = tmp_path / "data" / "frames"
        if not _case_insensitive(frames):
            pytest.skip("filesystem is case-sensitive")
        with pytest.raises(StoragePathError, match="frames root"):
            validate_storage_path(str(_case_variant(frames)), settings, is_admin=True)

    def test_case_variant_of_a_real_frames_subdir_still_accepted(
        self, tmp_path: Path
    ) -> None:
        # Identity only rejects aliases of protected dirs, not real sub-paths.
        settings = _policy_settings(tmp_path)
        sub = tmp_path / "data" / "frames" / "garden"
        sub.mkdir()
        if not _case_insensitive(sub):
            pytest.skip("filesystem is case-sensitive")
        stored = validate_storage_path(
            str(_case_variant(sub)), settings, is_admin=False
        )
        assert stored is not None
        assert os.path.samefile(stored, sub)

    def test_case_variant_frame_path_is_contained_not_escaped(
        self, tmp_path: Path
    ) -> None:
        # The same file under another spelling is the project's own frame.
        settings = _settings(tmp_path / "frames")
        frame_dir_ = tmp_path / "frames" / "dir4"
        frame_dir_.mkdir(parents=True)
        (frame_dir_ / "a.jpg").write_bytes(b"x")
        if not _case_insensitive(frame_dir_):
            pytest.skip("filesystem is case-sensitive")
        project = ProjectRef(id=4, storage_path=str(frame_dir_))
        stored = str(_case_variant(frame_dir_) / "a.jpg")
        assert resolve_contained_frame_path(settings, project, stored).name == "a.jpg"

    def test_symlink_alias_of_data_dir_rejected(self, tmp_path: Path) -> None:
        settings = _policy_settings(tmp_path)
        (tmp_path / "alias").symlink_to(tmp_path / "data")
        with pytest.raises(StoragePathError):
            validate_storage_path(
                str(tmp_path / "alias" / "x"), settings, is_admin=True
            )

    def test_bind_mount_like_alias_via_symlinked_parent_rejected(
        self, tmp_path: Path
    ) -> None:
        # A second name for the data dir reached through a symlinked parent,
        # the closest a test can get to a bind mount without privileges.
        settings = _policy_settings(tmp_path)
        (tmp_path / "mnt").symlink_to(tmp_path)
        raw = str(tmp_path / "mnt" / "data" / "reports" / "x")
        with pytest.raises(StoragePathError):
            validate_storage_path(raw, settings, is_admin=True)

    def test_not_yet_existing_path_under_protected_dir_rejected(
        self, tmp_path: Path
    ) -> None:
        settings = _policy_settings(tmp_path)
        raw = str(tmp_path / "data" / "a" / "b" / "c")
        with pytest.raises(StoragePathError):
            validate_storage_path(raw, settings, is_admin=True)

    def test_identity_match_on_an_aliased_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulates a bind mount: two different strings, one directory identity.
        # The string test fails; the identity test must still say "inside".
        from timelapse_manager.storage import paths as paths_mod

        real_root = tmp_path / "data"
        alias = tmp_path / "bind"
        real_root.mkdir()
        alias.mkdir()
        (alias / "sub").mkdir()
        real_stat = os.stat(real_root)
        real_identity = os.stat

        def fake_stat(path, *args, **kwargs):  # type: ignore[no-untyped-def]
            if os.fspath(path) == str(alias):
                return real_stat
            return real_identity(path, *args, **kwargs)

        monkeypatch.setattr(paths_mod.os, "stat", fake_stat)
        assert paths_mod._is_within(str(alias / "sub" / "new"), str(real_root))
        assert not paths_mod._is_within(str(tmp_path / "other"), str(real_root))

    def test_linux_string_semantics_unchanged(self, tmp_path: Path) -> None:
        # Unrelated siblings stay outside whatever the filesystem.
        from timelapse_manager.storage.paths import _is_within

        (tmp_path / "frames").mkdir()
        (tmp_path / "frames-old").mkdir()
        assert not _is_within(str(tmp_path / "frames-old"), str(tmp_path / "frames"))
        assert _is_within(str(tmp_path / "frames" / "x"), str(tmp_path / "frames"))
