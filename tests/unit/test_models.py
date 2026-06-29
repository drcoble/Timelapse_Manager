"""Tests for the ORM models and session management utilities.

Uses a real migrated temp SQLite database per test to verify round-trip
persistence, constraints (unique, FK cascade/restrict, singleton check),
and session_scope commit/rollback behavior.
"""

from __future__ import annotations

import pytest
from alembic import command as alembic_command
from sqlalchemy.exc import IntegrityError

from timelapse_manager.db.engine import create_db_engine
from timelapse_manager.db.models import (
    Camera,
    Frame,
    LdapSettings,
    NotificationSettings,
    Project,
    User,
)
from timelapse_manager.db.session import create_session_factory, session_scope

# ---------------------------------------------------------------------------
# Fixture: migrated temp database
# ---------------------------------------------------------------------------


@pytest.fixture()
def migrated_factory(alembic_cfg, tmp_db_url):  # type: ignore[no-untyped-def]
    """Return a session factory backed by a fully-migrated temp SQLite DB."""
    alembic_command.upgrade(alembic_cfg, "head")
    engine = create_db_engine(tmp_db_url)
    factory = create_session_factory(engine)
    yield factory
    engine.dispose()


# ---------------------------------------------------------------------------
# Camera round-trip
# ---------------------------------------------------------------------------


class TestCameraRoundTrip:
    def test_insert_and_select_camera(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        with session_scope(migrated_factory) as session:
            cam = Camera(name="rooftop-cam", address="192.168.1.10")
            session.add(cam)

        with session_scope(migrated_factory) as session:
            retrieved = session.query(Camera).filter_by(name="rooftop-cam").one()
        assert retrieved.address == "192.168.1.10"

    def test_camera_id_is_assigned_on_insert(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        with session_scope(migrated_factory) as session:
            cam = Camera(name="cam-id-test")
            session.add(cam)

        with session_scope(migrated_factory) as session:
            cam = session.query(Camera).filter_by(name="cam-id-test").one()
        assert cam.id is not None and cam.id > 0

    def test_camera_name_unique_constraint(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        with session_scope(migrated_factory) as session:
            session.add(Camera(name="dup-cam"))

        with pytest.raises(IntegrityError), session_scope(migrated_factory) as session:
            session.add(Camera(name="dup-cam"))


# ---------------------------------------------------------------------------
# Frame unique constraint on (project_id, sequence_index)
# ---------------------------------------------------------------------------


class TestFrameUniqueConstraint:
    def _create_camera_and_project(self, session):  # type: ignore[no-untyped-def]
        cam = Camera(name="frame-test-cam")
        session.add(cam)
        session.flush()
        proj = Project(
            name="frame-test-proj",
            camera_id=cam.id,
            operational_status="idle",
            lifecycle_state="active",
            frame_count=0,
        )
        session.add(proj)
        session.flush()
        return proj.id

    def test_unique_seq_index_per_project_is_enforced(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        with session_scope(migrated_factory) as session:
            project_id = self._create_camera_and_project(session)
            session.add(
                Frame(
                    project_id=project_id,
                    sequence_index=0,
                    capture_status="pending",
                    origin="captured",
                    lifecycle_state="active",
                )
            )

        with pytest.raises(IntegrityError), session_scope(migrated_factory) as session:
            # Must look up the project_id in a fresh session.
            proj = session.query(Project).filter_by(name="frame-test-proj").one()
            session.add(
                Frame(
                    project_id=proj.id,
                    sequence_index=0,  # duplicate
                    capture_status="pending",
                    origin="captured",
                    lifecycle_state="active",
                )
            )

    def test_same_seq_index_on_different_projects_is_allowed(
        self, migrated_factory
    ) -> None:  # type: ignore[no-untyped-def]
        with session_scope(migrated_factory) as session:
            cam = Camera(name="multi-proj-cam")
            session.add(cam)
            session.flush()
            proj_a = Project(
                name="proj-a",
                camera_id=cam.id,
                operational_status="idle",
                lifecycle_state="active",
                frame_count=0,
            )
            proj_b = Project(
                name="proj-b",
                camera_id=cam.id,
                operational_status="idle",
                lifecycle_state="active",
                frame_count=0,
            )
            session.add_all([proj_a, proj_b])
            session.flush()
            session.add(
                Frame(
                    project_id=proj_a.id,
                    sequence_index=0,
                    capture_status="pending",
                    origin="captured",
                    lifecycle_state="active",
                )
            )
            session.add(
                Frame(
                    project_id=proj_b.id,
                    sequence_index=0,  # same index, different project — OK
                    capture_status="pending",
                    origin="captured",
                    lifecycle_state="active",
                )
            )


# ---------------------------------------------------------------------------
# FK CASCADE: delete project removes its frames
# ---------------------------------------------------------------------------


class TestForeignKeyCascade:
    def test_delete_project_cascades_to_frames(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        with session_scope(migrated_factory) as session:
            cam = Camera(name="cascade-cam")
            session.add(cam)
            session.flush()
            proj = Project(
                name="cascade-proj",
                camera_id=cam.id,
                operational_status="idle",
                lifecycle_state="active",
                frame_count=0,
            )
            session.add(proj)
            session.flush()
            session.add(
                Frame(
                    project_id=proj.id,
                    sequence_index=0,
                    capture_status="pending",
                    origin="captured",
                    lifecycle_state="active",
                )
            )

        # Delete the project; frames should cascade.
        with session_scope(migrated_factory) as session:
            proj = session.query(Project).filter_by(name="cascade-proj").one()
            session.delete(proj)

        with session_scope(migrated_factory) as session:
            frame_count = session.query(Frame).count()
        assert frame_count == 0


# ---------------------------------------------------------------------------
# FK RESTRICT: delete camera with a referencing project must fail
# ---------------------------------------------------------------------------


class TestForeignKeyRestrict:
    def test_delete_camera_with_active_project_raises(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        with session_scope(migrated_factory) as session:
            cam = Camera(name="restrict-cam")
            session.add(cam)
            session.flush()
            session.add(
                Project(
                    name="restrict-proj",
                    camera_id=cam.id,
                    operational_status="idle",
                    lifecycle_state="active",
                    frame_count=0,
                )
            )

        with pytest.raises(IntegrityError), session_scope(migrated_factory) as session:
            cam = session.query(Camera).filter_by(name="restrict-cam").one()
            session.delete(cam)


# ---------------------------------------------------------------------------
# Singleton CHECK constraint on ldap_settings / notification_settings
# ---------------------------------------------------------------------------


class TestSingletonCheckConstraint:
    def test_ldap_settings_id_must_be_1(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        with session_scope(migrated_factory) as session:
            session.add(LdapSettings(id=1, tls_mode="none", nested_groups=False))

        with pytest.raises(IntegrityError), session_scope(migrated_factory) as session:
            # id=2 violates CHECK(id=1)
            session.add(LdapSettings(id=2, tls_mode="none", nested_groups=False))

    def test_notification_settings_id_must_be_1(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        with session_scope(migrated_factory) as session:
            session.add(NotificationSettings(id=1, smtp_security="none"))

        with pytest.raises(IntegrityError), session_scope(migrated_factory) as session:
            # id=2 violates CHECK(id=1)
            session.add(NotificationSettings(id=2, smtp_security="none"))


# ---------------------------------------------------------------------------
# session_scope commit/rollback semantics
# ---------------------------------------------------------------------------


class TestSessionScope:
    def test_session_scope_commits_on_success(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        with session_scope(migrated_factory) as session:
            session.add(Camera(name="commit-test-cam"))

        with session_scope(migrated_factory) as session:
            count = session.query(Camera).filter_by(name="commit-test-cam").count()
        assert count == 1

    def test_session_scope_rolls_back_on_exception(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(RuntimeError), session_scope(migrated_factory) as session:
            session.add(Camera(name="rollback-test-cam"))
            raise RuntimeError("deliberate rollback")

        with session_scope(migrated_factory) as session:
            count = session.query(Camera).filter_by(name="rollback-test-cam").count()
        assert count == 0

    def test_user_round_trip(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        with session_scope(migrated_factory) as session:
            session.add(
                User(
                    username="admin",
                    auth_source="local",
                    role="admin",
                    enabled=True,
                )
            )

        with session_scope(migrated_factory) as session:
            user = session.query(User).filter_by(username="admin").one()
        assert user.role == "admin"
        assert user.enabled is True
