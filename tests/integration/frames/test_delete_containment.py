"""Deletes never unlink a file outside the project's directories (#170).

#169 confined frame *reads* to the project's frame directories. The delete
paths still unlinked whatever a row pointed at: a frame row whose ``file_path``
escapes the frames tree (an old import, a hand edit, a symlink planted in the
frame directory) made a permanent frame delete or a project delete remove a
file elsewhere. Now the row goes and the outside file stays; render outputs are
unlinked only inside the project's render root.
"""

from __future__ import annotations

from pathlib import Path

from tests.integration.frames.test_lifecycle import (
    _ACTOR_USER_ID,
    _make_settings,
    _seed_project,
    _write_frame_via_writer,
)
from timelapse_manager.db.models import Frame, Project, RenderJob
from timelapse_manager.db.session import session_scope
from timelapse_manager.render.spec import project_render_root
from timelapse_manager.storage import frames as frame_service
from timelapse_manager.storage.projects import delete_project_with_files


def _point_frame_at(migrated_factory, frame_id: int, stored: str) -> None:
    with session_scope(migrated_factory) as session:
        frame = session.get(Frame, frame_id)
        assert frame is not None
        frame.file_path = stored


def _outside_file(tmp_path: Path, name: str) -> Path:
    victim = tmp_path / "outside" / name
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_bytes(b"precious")
    return victim


class TestPermanentDeleteContainment:
    def test_absolute_path_outside_the_tree_is_not_unlinked(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "pd-escape")
        frame_id, _ = _write_frame_via_writer(
            migrated_factory, settings, ctx["project_id"]
        )
        victim = _outside_file(tmp_path, "victim.jpg")
        _point_frame_at(migrated_factory, frame_id, str(victim))

        with session_scope(migrated_factory) as session:
            frame_service.permanent_delete(
                session, frame_id, _ACTOR_USER_ID, confirm=True, settings=settings
            )

        assert victim.read_bytes() == b"precious"
        with session_scope(migrated_factory) as session:
            assert session.get(Frame, frame_id) is None

    def test_traversal_path_is_not_unlinked(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "pd-dotdot")
        frame_id, _ = _write_frame_via_writer(
            migrated_factory, settings, ctx["project_id"]
        )
        victim = _outside_file(tmp_path, "dotdot.jpg")
        _point_frame_at(migrated_factory, frame_id, "../../../outside/dotdot.jpg")

        with session_scope(migrated_factory) as session:
            frame_service.permanent_delete(
                session, frame_id, _ACTOR_USER_ID, confirm=True, settings=settings
            )

        assert victim.exists()

    def test_symlink_in_the_frame_dir_does_not_delete_its_target(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "pd-link")
        frame_id, abs_path = _write_frame_via_writer(
            migrated_factory, settings, ctx["project_id"]
        )
        victim = _outside_file(tmp_path, "linked.jpg")
        abs_path.unlink()
        abs_path.symlink_to(victim)

        with session_scope(migrated_factory) as session:
            frame_service.permanent_delete(
                session, frame_id, _ACTOR_USER_ID, confirm=True, settings=settings
            )

        assert victim.read_bytes() == b"precious"

    def test_a_contained_frame_is_still_unlinked(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "pd-normal")
        frame_id, abs_path = _write_frame_via_writer(
            migrated_factory, settings, ctx["project_id"]
        )

        with session_scope(migrated_factory) as session:
            frame_service.permanent_delete(
                session, frame_id, _ACTOR_USER_ID, confirm=True, settings=settings
            )

        assert not abs_path.exists()


class TestProjectDeleteContainment:
    def test_only_files_inside_the_project_are_unlinked(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        ctx = _seed_project(migrated_factory, tmp_path, "proj-escape")
        project_id = ctx["project_id"]
        kept_id, kept_path = _write_frame_via_writer(
            migrated_factory, settings, project_id
        )
        escaped_id, _ = _write_frame_via_writer(migrated_factory, settings, project_id)
        frame_victim = _outside_file(tmp_path, "frame-victim.jpg")
        _point_frame_at(migrated_factory, escaped_id, str(frame_victim))
        render_victim = _outside_file(tmp_path, "render-victim.mp4")

        with session_scope(migrated_factory) as session:
            project = session.get(Project, project_id)
            assert project is not None
            render_root = project_render_root(settings, project)
            render_root.mkdir(parents=True, exist_ok=True)
            inside_render = render_root / "render-1.mp4"
            inside_render.write_bytes(b"video")
            for path in (inside_render, render_victim):
                session.add(
                    RenderJob(
                        project_id=project_id,
                        status="completed",
                        output_file_path=str(path),
                    )
                )

        with session_scope(migrated_factory) as session:
            project = session.get(Project, project_id)
            assert project is not None
            delete_project_with_files(session, settings, project)

        assert not kept_path.exists()
        assert not inside_render.exists()
        assert frame_victim.read_bytes() == b"precious"
        assert render_victim.read_bytes() == b"precious"
