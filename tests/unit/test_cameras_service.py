"""Service-layer tests for the camera write use-cases.

These exercise :mod:`timelapse_manager.services.cameras` directly against a
migrated session, pinning the transport-neutral behaviour each rule carries --
the partial-update semantics, the uniqueness ``ConflictError``, the missing-row
``NotFoundError``, and the camera-scoped audit event every write records --
independently of any web/API wiring. The HTTP mapping is covered separately by
the route tests.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from timelapse_manager.config.settings import Settings
from timelapse_manager.db.models import Camera, Event, User
from timelapse_manager.security import hash_password
from timelapse_manager.services._base import Actor, Effect
from timelapse_manager.services.cameras import (
    CameraSpec,
    create_camera,
    delete_camera,
    update_camera,
)
from timelapse_manager.services.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)

_STRONG_PW = "CorrectHorse42!"


@pytest.fixture()
def session(migrated_factory) -> Session:  # type: ignore[no-untyped-def]
    """A migrated session; rolled back and closed after the test."""
    db = migrated_factory()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture()
def settings(settings_no_autostart: Settings) -> Settings:
    return settings_no_autostart


@pytest.fixture()
def actor(session: Session, settings: Settings) -> Actor:
    """An Actor backed by a real seeded admin row (satisfies the audit FK)."""
    admin = User(
        username="root",
        auth_source="local",
        password_hash=hash_password(_STRONG_PW, settings.auth),
        role="admin",
        enabled=True,
    )
    session.add(admin)
    session.flush()
    return Actor.of(admin)


def _camera_audit_messages(session: Session, camera_id: int) -> list[str]:
    return list(
        session.execute(
            select(Event.message).where(
                Event.scope == "camera", Event.scope_id == camera_id
            )
        )
        .scalars()
        .all()
    )


class TestCreateCamera:
    def test_creates_and_audits(self, session: Session, actor: Actor) -> None:
        spec = CameraSpec(name="lobby", address="10.0.0.5", protocol="vapix")
        result = create_camera(session, actor, spec)
        assert result.effects == ()  # camera writes signal nothing post-commit
        assert result.value.id is not None
        assert result.value.name == "lobby"
        assert result.value.address == "10.0.0.5"
        assert _camera_audit_messages(session, result.value.id) == [
            f"camera {result.value.id} created"
        ]

    def test_empty_name_is_validation_error(
        self, session: Session, actor: Actor
    ) -> None:
        with pytest.raises(ValidationError):
            create_camera(session, actor, CameraSpec(name="   "))

    def test_credentials_and_inherit_flag_persist_verbatim(
        self, session: Session, actor: Actor
    ) -> None:
        # The service stores the already-encrypted document as-is (no re-encrypt).
        spec = CameraSpec(
            name="creds",
            credentials={"username": "op", "password": "enc:v1:opaque"},
            credentials_inherit_default=False,
        )
        result = create_camera(session, actor, spec)
        assert result.value.credentials == {
            "username": "op",
            "password": "enc:v1:opaque",
        }
        assert result.value.credentials_inherit_default is False


class TestUpdateCamera:
    def test_partial_update_touches_only_named_fields(
        self, session: Session, actor: Actor
    ) -> None:
        cam = create_camera(
            session,
            actor,
            CameraSpec(
                name="cam",
                address="10.0.0.5",
                protocol="vapix",
                snapshot_uri="http://10.0.0.5/snap",
            ),
        ).value

        # Only ``address`` is in fields_set; the snapshot URI must survive.
        update_camera(
            session,
            actor,
            cam.id,
            CameraSpec(address="10.0.0.9", fields_set=frozenset({"address"})),
        )
        assert cam.address == "10.0.0.9"
        assert cam.snapshot_uri == "http://10.0.0.5/snap"
        assert cam.protocol == "vapix"

    def test_none_valued_field_clears_the_column(
        self, session: Session, actor: Actor
    ) -> None:
        cam = create_camera(
            session,
            actor,
            CameraSpec(name="cam2", snapshot_uri="http://10.0.0.5/snap"),
        ).value
        update_camera(
            session,
            actor,
            cam.id,
            CameraSpec(snapshot_uri=None, fields_set=frozenset({"snapshot_uri"})),
        )
        assert cam.snapshot_uri is None

    def test_rename_to_taken_name_is_conflict(
        self, session: Session, actor: Actor
    ) -> None:
        create_camera(session, actor, CameraSpec(name="taken"))
        other = create_camera(session, actor, CameraSpec(name="other")).value
        with pytest.raises(ConflictError):
            update_camera(
                session,
                actor,
                other.id,
                CameraSpec(name="taken", fields_set=frozenset({"name"})),
            )

    def test_saving_unchanged_name_is_allowed(
        self, session: Session, actor: Actor
    ) -> None:
        cam = create_camera(session, actor, CameraSpec(name="stable")).value
        # Re-saving the same name (its own row is excluded) must not conflict.
        result = update_camera(
            session,
            actor,
            cam.id,
            CameraSpec(name="stable", fields_set=frozenset({"name"})),
        )
        assert result.value.name == "stable"

    def test_empty_name_is_validation_error(
        self, session: Session, actor: Actor
    ) -> None:
        cam = create_camera(session, actor, CameraSpec(name="named")).value
        with pytest.raises(ValidationError):
            update_camera(
                session,
                actor,
                cam.id,
                CameraSpec(name="  ", fields_set=frozenset({"name"})),
            )

    def test_missing_camera_is_not_found(self, session: Session, actor: Actor) -> None:
        with pytest.raises(NotFoundError):
            update_camera(
                session,
                actor,
                999999,
                CameraSpec(name="x", fields_set=frozenset({"name"})),
            )

    def test_update_records_audit(self, session: Session, actor: Actor) -> None:
        cam = create_camera(session, actor, CameraSpec(name="audited")).value
        update_camera(
            session,
            actor,
            cam.id,
            CameraSpec(protocol="rtsp", fields_set=frozenset({"protocol"})),
        )
        assert f"camera {cam.id} updated" in _camera_audit_messages(session, cam.id)

    def test_update_wakes_the_capture_reconcile(
        self, session: Session, actor: Actor
    ) -> None:
        cam = create_camera(session, actor, CameraSpec(name="edited")).value
        result = update_camera(
            session,
            actor,
            cam.id,
            CameraSpec(address="10.0.16.9", fields_set=frozenset({"address"})),
        )
        # The reconcile pass reloads the camera's capture session (#69 CAM-P1).
        assert result.effects == (Effect.CAPTURE_RECONCILE,)


class TestDeleteCamera:
    def test_deletes_and_audits(self, session: Session, actor: Actor) -> None:
        cam = create_camera(session, actor, CameraSpec(name="doomed")).value
        camera_id = cam.id
        result = delete_camera(session, actor, camera_id)
        assert result.value is None
        # Wakes the supervisor to drop the camera's capture session (#69 CAM-P1).
        assert result.effects == (Effect.CAPTURE_RECONCILE,)
        assert session.get(Camera, camera_id) is None
        assert f"camera {camera_id} deleted" in _camera_audit_messages(
            session, camera_id
        )

    def test_missing_camera_is_not_found(self, session: Session, actor: Actor) -> None:
        with pytest.raises(NotFoundError):
            delete_camera(session, actor, 999999)
