"""A camera still used by a project cannot be deleted (#70 F-R1).

``project.camera_id`` is a ``RESTRICT`` foreign key, so such a delete could
never commit. The service refuses it up front with a ``ConflictError`` (409)
that tells the user what to do, and maps the race where a project appears
between the check and the flush to the same error. The CLI reports the API's
``detail`` for a 409, so a scripted delete sees the same message.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from timelapse_manager.cli import _fail_from_response
from timelapse_manager.db.models import Camera, Event, Project, User
from timelapse_manager.services._base import Actor
from timelapse_manager.services.cameras import delete_camera
from timelapse_manager.services.errors import ConflictError


@pytest.fixture()
def session(migrated_factory: Any) -> Generator[Session, None, None]:
    """A migrated session; rolled back and closed after the test."""
    db = migrated_factory()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture()
def actor(session: Session) -> Actor:
    """An Actor backed by a real user row (satisfies the audit FK)."""
    user = User(username="root", auth_source="ldap", role="admin", enabled=True)
    session.add(user)
    session.flush()
    return Actor(user_id=user.id, role=user.role)


def _camera_with_projects(session: Session, projects: int) -> int:
    cam = Camera(name=f"cam-{projects}", address="192.168.1.70", protocol="vapix")
    session.add(cam)
    session.flush()
    for i in range(projects):
        session.add(
            Project(
                camera_id=cam.id,
                name=f"p-{projects}-{i}",
                capture_interval_seconds=60,
                lifecycle_state="paused",
            )
        )
    session.flush()
    return cam.id


def _delete_audits(session: Session, camera_id: int) -> list[str]:
    rows = session.scalars(
        select(Event.message).where(
            Event.scope == "camera", Event.scope_id == camera_id
        )
    )
    return [m for m in rows if m.endswith("deleted")]


class TestDeleteCameraInUse:
    def test_in_use_camera_raises_conflict_with_count(
        self, session: Session, actor: Actor
    ) -> None:
        camera_id = _camera_with_projects(session, 3)

        with pytest.raises(ConflictError) as info:
            delete_camera(session, actor, camera_id)

        assert info.value.status == 409
        assert info.value.detail == (
            "Camera is used by 3 project(s); move or delete them first."
        )
        assert session.get(Camera, camera_id) is not None
        assert _delete_audits(session, camera_id) == []

    def test_unused_camera_is_deleted_and_audited(
        self, session: Session, actor: Actor
    ) -> None:
        camera_id = _camera_with_projects(session, 0)

        delete_camera(session, actor, camera_id)

        assert session.get(Camera, camera_id) is None
        assert _delete_audits(session, camera_id) == [f"camera {camera_id} deleted"]

    def test_project_created_after_the_check_still_maps_to_conflict(
        self, session: Session, actor: Actor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate the race: the in-use count misses a project that a concurrent
        # request committed, so the RESTRICT foreign key fires at flush instead.
        camera_id = _camera_with_projects(session, 1)
        monkeypatch.setattr(session, "scalar", lambda *_a, **_k: 0)

        with pytest.raises(ConflictError) as info:
            delete_camera(session, actor, camera_id)

        assert info.value.detail == (
            "Camera is used by a project; move or delete them first."
        )


class TestCliReportsConflictDetail:
    def test_cli_prints_detail_of_a_409(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        response = httpx.Response(
            409,
            json={
                "detail": "Camera is used by 1 project(s); move or delete them first.",
                "code": "conflict",
            },
        )

        exit_code = _fail_from_response(response)

        assert exit_code != 0
        assert capsys.readouterr().err.strip() == (
            "error: service returned HTTP 409: "
            "Camera is used by 1 project(s); move or delete them first."
        )
