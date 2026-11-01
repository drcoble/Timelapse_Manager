"""Web edit/clone forms enforce the storage_path policy (#67 S5).

A changed ``storage_path`` is validated for the submitting user's role and stored
resolved; an unchanged one (the form always posts the field back) is left alone,
so a project whose path predates the policy can still be edited.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.models import Camera, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context


def _seed(*, name: str, storage_path: str | None = None) -> tuple[int, int]:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(name=f"{name}-cam", address="127.0.0.1", protocol="vapix")
        db.add(cam)
        db.flush()
        proj = Project(
            camera_id=cam.id,
            name=name,
            capture_interval_seconds=60,
            lifecycle_state="active",
            operational_status="idle",
            storage_path=storage_path,
        )
        db.add(proj)
        db.flush()
        return cam.id, proj.id


def _stored_path(project_id: int) -> str | None:
    with session_scope(get_context().session_factory) as db:
        proj = db.get(Project, project_id)
        assert proj is not None
        return proj.storage_path


def _project_names() -> set[str]:
    with session_scope(get_context().session_factory) as db:
        return {p.name for p in db.query(Project).all()}


def _outside(factory: pytest.TempPathFactory) -> Path:
    """A directory outside the fixture's protected dirs.

    The fixture keeps its database at ``tmp_path/test.db``, so ``tmp_path``
    itself is protected (the database directory) and cannot host test paths.
    """
    return factory.mktemp("srv")


def _data_dir() -> Path:
    return Path(get_context().settings.paths.data_dir)


def _frames_root() -> Path:
    root = get_context().settings.paths.frames_root
    assert root is not None
    return Path(root)


def _post_edit(
    client: TestClient, pid: int, cam: int, *, name: str, storage_path: str
) -> object:
    csrf = csrf_of(client, f"/projects/{pid}/settings")
    return client.post(
        f"/projects/{pid}/settings",
        data={
            "name": name,
            "camera_id": str(cam),
            "capture_interval_value": "60",
            "capture_interval_unit": "seconds",
            "storage_path": storage_path,
            "csrf_token": csrf,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )


class TestEditStoragePath:
    def test_path_in_data_dir_shows_form_error(self, admin_client: TestClient) -> None:
        cam, pid = _seed(name="sp-data")
        resp = _post_edit(
            admin_client,
            pid,
            cam,
            name="sp-data",
            storage_path=str(_data_dir() / "planted"),
        )
        assert resp.status_code == 400  # type: ignore[attr-defined]
        assert "token or certificate directories" in resp.text  # type: ignore[attr-defined]
        assert _stored_path(pid) is None

    def test_newline_in_path_shows_form_error(self, admin_client: TestClient) -> None:
        cam, pid = _seed(name="sp-nl")
        resp = _post_edit(
            admin_client,
            pid,
            cam,
            name="sp-nl",
            storage_path=f"{_frames_root()}/a\nfile http://x/",
        )
        assert resp.status_code == 400  # type: ignore[attr-defined]
        assert "control characters" in resp.text  # type: ignore[attr-defined]

    def test_valid_change_is_stored_resolved(self, operator_client: TestClient) -> None:
        cam, pid = _seed(name="sp-ok")
        target = _frames_root() / "custom" / "cam1"
        resp = _post_edit(
            operator_client, pid, cam, name="sp-ok", storage_path=f"{target}/"
        )
        assert resp.status_code == 303  # type: ignore[attr-defined]
        assert _stored_path(pid) == os.path.realpath(target)

    def test_operator_outside_allowed_roots_refused(
        self, operator_client: TestClient, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        cam, pid = _seed(name="sp-op")
        resp = _post_edit(
            operator_client,
            pid,
            cam,
            name="sp-op",
            storage_path=str(_outside(tmp_path_factory) / "frames"),
        )
        assert resp.status_code == 400  # type: ignore[attr-defined]
        assert "allowed_roots" in resp.text  # type: ignore[attr-defined]
        assert _stored_path(pid) is None

    def test_admin_outside_allowed_roots_accepted(
        self, admin_client: TestClient, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        cam, pid = _seed(name="sp-admin")
        target = _outside(tmp_path_factory) / "frames"
        resp = _post_edit(
            admin_client, pid, cam, name="sp-admin", storage_path=str(target)
        )
        assert resp.status_code == 303  # type: ignore[attr-defined]
        assert _stored_path(pid) == os.path.realpath(target)

    def test_legacy_violating_path_survives_unrelated_edit(
        self, operator_client: TestClient
    ) -> None:
        # A path set before the policy (inside the data dir, unresolved form):
        # the form posts it back unchanged, so a rename must succeed and leave
        # the stored string byte-for-byte as it was.
        legacy = f"{_data_dir()}/./legacy-frames"
        cam, pid = _seed(name="sp-legacy", storage_path=legacy)
        resp = _post_edit(
            operator_client, pid, cam, name="sp-legacy-renamed", storage_path=legacy
        )
        assert resp.status_code == 303  # type: ignore[attr-defined]
        assert _stored_path(pid) == legacy

    def test_clearing_a_legacy_path_is_allowed(
        self, operator_client: TestClient
    ) -> None:
        legacy = str(_data_dir() / "legacy-frames")
        cam, pid = _seed(name="sp-clear", storage_path=legacy)
        resp = _post_edit(operator_client, pid, cam, name="sp-clear", storage_path="")
        assert resp.status_code == 303  # type: ignore[attr-defined]
        assert _stored_path(pid) is None


def _post_clone(client: TestClient, pid: int, name: str) -> object:
    csrf = csrf_of(client, f"/projects/{pid}/clone")
    return client.post(
        f"/projects/{pid}/clone",
        data={"name": name, "csrf_token": csrf},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )


class TestCloneStoragePath:
    """A clone never copies its source's storage path (#171)."""

    def _clone_path(self, name: str) -> str | None:
        with session_scope(get_context().session_factory) as db:
            return db.query(Project).filter(Project.name == name).one().storage_path

    def test_operator_clone_of_a_custom_path_gets_the_default_layout(
        self, operator_client: TestClient, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        _, pid = _seed(
            name="cl-src", storage_path=str(_outside(tmp_path_factory) / "a")
        )
        resp = _post_clone(operator_client, pid, "cl-copy")
        assert resp.status_code == 303  # type: ignore[attr-defined]
        assert self._clone_path("cl-copy") is None

    def test_admin_clone_does_not_share_the_source_directory(
        self, admin_client: TestClient, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        source = _outside(tmp_path_factory) / "b"
        _, pid = _seed(name="cl-admin-src", storage_path=str(source))
        resp = _post_clone(admin_client, pid, "cl-admin-copy")
        assert resp.status_code == 303  # type: ignore[attr-defined]
        assert self._clone_path("cl-admin-copy") is None
        with session_scope(get_context().session_factory) as db:
            assert db.get(Project, pid).storage_path == str(source)  # type: ignore[union-attr]

    def test_clone_of_a_legacy_path_in_the_data_dir_is_allowed(
        self, admin_client: TestClient
    ) -> None:
        # The source's path breaks today's policy; nothing is copied, so the
        # clone is simply a default-layout project.
        _, pid = _seed(name="cl-legacy", storage_path=str(_data_dir() / "old"))
        resp = _post_clone(admin_client, pid, "cl-legacy-copy")
        assert resp.status_code == 303  # type: ignore[attr-defined]
        assert self._clone_path("cl-legacy-copy") is None
