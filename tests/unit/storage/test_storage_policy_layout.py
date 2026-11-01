"""storage_path policy: layout collisions, shared paths, clones, --config (#171, #172).

* A clone gets the default layout, never its source's ``storage_path`` (#171).
* The policy refuses a path that collides with the default layout (a directory
  named like the render subdirectory, another project's numbered directory) or
  that another project already uses (#171).
* The directory of the config file in use is protected whether it came from
  ``--config`` or ``TLM_CONFIG`` (#172).

Refs #171, #172.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.unit.storage.test_paths import _policy_settings
from tests.unit.test_storage_path_containment import _seed
from timelapse_manager.config import load_settings
from timelapse_manager.config.loader import settings_config_file
from timelapse_manager.config.settings import Settings
from timelapse_manager.db.models import Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.services import projects as project_service
from timelapse_manager.services._base import Actor
from timelapse_manager.services.errors import ValidationError
from timelapse_manager.storage import paths
from timelapse_manager.storage.paths import StoragePathError, validate_storage_path

_ADMIN = Actor(user_id=1, role="admin")
_OPERATOR = Actor(user_id=1, role="operator")  # the sentinel admin row, as a role


@pytest.fixture(autouse=True)
def _no_tlm_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TLM_CONFIG", raising=False)


def _frames(settings: Settings) -> Path:
    return paths.frames_root(settings)


class TestLayoutCollisions:
    @pytest.mark.parametrize("is_admin", [True, False])
    def test_frames_root_renders_is_refused(
        self, tmp_path: Path, is_admin: bool
    ) -> None:
        settings = _policy_settings(tmp_path)

        with pytest.raises(StoragePathError, match="rendered videos"):
            validate_storage_path(
                str(_frames(settings) / "renders"), settings, is_admin=is_admin
            )

    def test_a_directory_named_like_the_render_subdirectory_is_refused_anywhere(
        self, tmp_path: Path
    ) -> None:
        # Its own render root would be its own frame directory.
        settings = _policy_settings(tmp_path)

        with pytest.raises(StoragePathError, match="rendered videos"):
            validate_storage_path(
                str(tmp_path / "srv" / "renders"), settings, is_admin=True
            )

    def test_another_projects_numbered_directory_is_refused(
        self, tmp_path: Path
    ) -> None:
        settings = _policy_settings(tmp_path)

        with pytest.raises(StoragePathError, match="numbered directory"):
            validate_storage_path(
                str(_frames(settings) / "7"), settings, is_admin=True, project_id=3
            )

    def test_a_project_that_is_not_saved_yet_may_use_no_numbered_directory(
        self, tmp_path: Path
    ) -> None:
        settings = _policy_settings(tmp_path)

        with pytest.raises(StoragePathError, match="numbered directory"):
            validate_storage_path(str(_frames(settings) / "7"), settings, is_admin=True)

    def test_a_projects_own_default_directory_is_allowed(self, tmp_path: Path) -> None:
        settings = _policy_settings(tmp_path)
        own = _frames(settings) / "3"

        stored = validate_storage_path(str(own), settings, is_admin=True, project_id=3)

        assert stored == os.path.realpath(own)

    def test_an_ordinary_custom_directory_is_still_allowed(
        self, tmp_path: Path
    ) -> None:
        settings = _policy_settings(tmp_path)

        assert validate_storage_path(
            str(_frames(settings) / "custom"), settings, is_admin=False, project_id=3
        )

    def test_a_path_another_project_uses_is_refused(self, tmp_path: Path) -> None:
        settings = _policy_settings(tmp_path)
        shared = _frames(settings) / "shared"

        with pytest.raises(StoragePathError, match="already used by another project"):
            validate_storage_path(
                str(shared),
                settings,
                is_admin=True,
                project_id=3,
                other_storage_paths=[str(shared / ".." / "shared")],
            )

    def test_an_unresolvable_stored_path_of_another_project_is_ignored(
        self, tmp_path: Path
    ) -> None:
        settings = _policy_settings(tmp_path)

        stored = validate_storage_path(
            str(_frames(settings) / "ok"),
            settings,
            is_admin=True,
            project_id=3,
            other_storage_paths=["/legacy\x00row", str(_frames(settings) / "other")],
        )

        assert stored


class TestCloneGetsItsOwnDirectory:
    def test_clone_has_no_storage_path_and_a_different_frame_directory(
        self, migrated_factory: sessionmaker[Session], tmp_path: Path
    ) -> None:
        settings = _policy_settings(tmp_path)
        source_dir = str(tmp_path / "elsewhere" / "frames")
        source_id = _seed(migrated_factory, name="src", storage_path=source_dir)

        with session_scope(migrated_factory) as session:
            clone = project_service.clone_project(
                session, _ADMIN, source_id, name="clone", settings=settings
            ).value
            source = session.get(Project, source_id)
            assert source is not None
            assert clone.storage_path is None
            assert source.storage_path == source_dir  # the source is untouched
            assert paths.frame_dir(settings, clone) == _frames(settings) / str(clone.id)
            assert paths.frame_dir(settings, clone) != paths.frame_dir(settings, source)

    def test_editing_a_path_to_one_another_project_uses_is_a_422(
        self, migrated_factory: sessionmaker[Session], tmp_path: Path
    ) -> None:
        settings = _policy_settings(tmp_path)
        taken = str(_frames(settings) / "taken")
        _seed(migrated_factory, name="owner", storage_path=taken)
        other = _seed(migrated_factory, name="other")

        with (
            session_scope(migrated_factory) as session,
            pytest.raises(ValidationError, match="already used by another project"),
        ):
            project_service.update_project(
                session,
                _OPERATOR,
                other,
                project_service.ProjectSpec(
                    storage_path=taken, fields_set=frozenset({"storage_path"})
                ),
                settings=settings,
            )

    def test_a_project_may_move_to_a_new_directory_of_its_own(
        self, migrated_factory: sessionmaker[Session], tmp_path: Path
    ) -> None:
        settings = _policy_settings(tmp_path)
        mine = str(_frames(settings) / "mine")
        pid = _seed(migrated_factory, name="mine", storage_path=mine)

        with session_scope(migrated_factory) as session:
            result = project_service.update_project(
                session,
                _OPERATOR,
                pid,
                project_service.ProjectSpec(
                    storage_path=str(_frames(settings) / "mine2"),
                    fields_set=frozenset({"storage_path"}),
                ),
                settings=settings,
            )
            assert result.value.storage_path == os.path.realpath(
                _frames(settings) / "mine2"
            )


class TestStartupScanFindsSharedPaths:
    """Clones made before #171 may share a directory; the scan says so."""

    def test_warns_about_projects_sharing_a_path_and_edits_nothing(
        self,
        migrated_factory: sessionmaker[Session],
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        settings = _policy_settings(tmp_path)
        shared = str(_frames(settings) / "shared")
        ids = [
            _seed(migrated_factory, name="orig", storage_path=shared),
            _seed(migrated_factory, name="old-clone", storage_path=shared),
            _seed(
                migrated_factory,
                name="alone",
                storage_path=str(_frames(settings) / "alone"),
            ),
        ]

        with (
            caplog.at_level(logging.WARNING, "timelapse_manager.services.projects"),
            session_scope(migrated_factory) as session,
        ):
            warned = project_service.warn_on_storage_path_policy(session, settings)

        assert warned == 2
        [message] = [
            r.getMessage() for r in caplog.records if "share" in r.getMessage()
        ]
        assert "'orig'" in message
        assert "'old-clone'" in message
        with session_scope(migrated_factory) as session:
            stored = [session.get(Project, pid).storage_path for pid in ids]  # type: ignore[union-attr]
        assert stored[:2] == [shared, shared]

    def test_a_stored_path_that_cannot_be_resolved_does_not_stop_the_scan(
        self,
        migrated_factory: sessionmaker[Session],
        tmp_path: Path,
    ) -> None:
        settings = _policy_settings(tmp_path)
        _seed(migrated_factory, name="nul", storage_path="/legacy\x00row")
        _seed(
            migrated_factory,
            name="fine",
            storage_path=str(_frames(settings) / "fine"),
        )

        with session_scope(migrated_factory) as session:
            warned = project_service.warn_on_storage_path_policy(session, settings)

        assert warned == 1  # the NUL row is reported by the policy check only


class TestConfigFileIsProtected:
    def _write_config(self, tmp_path: Path) -> tuple[Path, Path]:
        data_dir = tmp_path / "data"
        (data_dir / "frames").mkdir(parents=True)
        etc = tmp_path / "etc-tlm"
        etc.mkdir()
        config = etc / "config.json"
        config.write_text(
            json.dumps(
                {
                    "paths": {"data_dir": str(data_dir)},
                    "database": {"url": f"sqlite:///{data_dir / 'x.db'}"},
                }
            )
        )
        return config, etc

    def test_a_config_passed_explicitly_protects_its_directory(
        self, tmp_path: Path
    ) -> None:
        config, etc = self._write_config(tmp_path)
        settings = load_settings(str(config))  # --config, TLM_CONFIG unset

        assert settings_config_file(settings) == config
        with pytest.raises(StoragePathError, match="data, config"):
            validate_storage_path(str(etc / "frames"), settings, is_admin=True)

    def test_a_relative_config_path_is_made_absolute_when_loaded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config, etc = self._write_config(tmp_path)
        monkeypatch.chdir(etc)
        settings = load_settings("config.json")
        monkeypatch.chdir(tmp_path)

        assert settings_config_file(settings) == config
        with pytest.raises(StoragePathError, match="data, config"):
            validate_storage_path(str(etc / "frames"), settings, is_admin=True)

    def test_the_environment_config_is_still_protected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config, etc = self._write_config(tmp_path)
        monkeypatch.setenv("TLM_CONFIG", str(config))
        settings = load_settings()

        assert settings_config_file(settings) == config
        with pytest.raises(StoragePathError, match="data, config"):
            validate_storage_path(str(etc / "frames"), settings, is_admin=True)

    def test_settings_built_without_the_loader_have_no_config_file(
        self, tmp_path: Path
    ) -> None:
        assert settings_config_file(_policy_settings(tmp_path)) is None

    def test_a_config_directory_elsewhere_does_not_block_ordinary_paths(
        self, tmp_path: Path
    ) -> None:
        config, _etc = self._write_config(tmp_path)
        settings = load_settings(str(config))

        stored = validate_storage_path(
            str(tmp_path / "data" / "frames" / "custom"), settings, is_admin=True
        )

        assert stored
