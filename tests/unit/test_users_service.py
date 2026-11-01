"""Service-layer tests for the user-administration write use-cases.

These exercise :mod:`timelapse_manager.services.users` directly against a
migrated session, pinning the account-security invariants (last-admin lockout,
directory-account refusal) and the transport-neutral ``AppError`` each rule
raises -- independently of any web wiring. The web adapter's HTTP mapping is
covered separately by the route tests.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from timelapse_manager.config.settings import Settings
from timelapse_manager.db.models import User
from timelapse_manager.security import hash_password, verify_password
from timelapse_manager.services._base import Actor
from timelapse_manager.services.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from timelapse_manager.services.users import (
    UserSpec,
    create_user,
    delete_user,
    disable_user,
    edit_user_role,
    enable_user,
    reset_user_password,
    revoke_user_sessions,
)

_STRONG_PW = "CorrectHorse42!"


def _make_admin(session: Session, settings: Settings, username: str = "root") -> User:
    """Seed and flush a real, enabled local administrator, returning the row."""
    admin = User(
        username=username,
        auth_source="local",
        password_hash=hash_password(_STRONG_PW, settings.auth),
        role="admin",
        enabled=True,
    )
    session.add(admin)
    session.flush()
    return admin


@pytest.fixture()
def session(migrated_factory) -> Iterator[Session]:  # type: ignore[no-untyped-def]
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
    return Actor.of(_make_admin(session, settings))


class TestCreateUser:
    def test_creates_local_account_hashed(
        self, session: Session, settings: Settings, actor: Actor
    ) -> None:
        spec = UserSpec(username="alice", password=_STRONG_PW, role="operator")
        result = create_user(session, actor, spec, settings=settings)
        assert result.effects == ()  # user writes signal nothing post-commit
        assert result.value.username == "alice"
        assert result.value.role == "operator"
        assert result.value.password_hash is not None
        assert verify_password(_STRONG_PW, result.value.password_hash, settings.auth)

    def test_missing_password_is_validation_error(
        self, session: Session, settings: Settings, actor: Actor
    ) -> None:
        spec = UserSpec(username="bob", password="", role="viewer")
        with pytest.raises(ValidationError):
            create_user(session, actor, spec, settings=settings)

    def test_bad_role_is_validation_error(
        self, session: Session, settings: Settings, actor: Actor
    ) -> None:
        spec = UserSpec(username="bob", password=_STRONG_PW, role="superuser")
        with pytest.raises(ValidationError):
            create_user(session, actor, spec, settings=settings)

    def test_confirmation_mismatch_is_validation_error(
        self, session: Session, settings: Settings, actor: Actor
    ) -> None:
        spec = UserSpec(
            username="bob",
            password=_STRONG_PW,
            role="viewer",
            password_confirm=_STRONG_PW + "x",
        )
        with pytest.raises(ValidationError):
            create_user(session, actor, spec, settings=settings)

    def test_short_password_is_validation_error(
        self, session: Session, settings: Settings, actor: Actor
    ) -> None:
        spec = UserSpec(username="bob", password="short", role="viewer")
        with pytest.raises(ValidationError):
            create_user(session, actor, spec, settings=settings)

    def test_duplicate_username_is_conflict(
        self, session: Session, settings: Settings, actor: Actor
    ) -> None:
        create_user(
            session,
            actor,
            UserSpec(username="dup", password=_STRONG_PW, role="viewer"),
            settings=settings,
        )
        with pytest.raises(ConflictError):
            create_user(
                session,
                actor,
                UserSpec(username="dup", password=_STRONG_PW, role="viewer"),
                settings=settings,
            )


class TestEditUserRole:
    def test_missing_user_is_not_found(self, session: Session, actor: Actor) -> None:
        with pytest.raises(NotFoundError):
            edit_user_role(session, actor, 999999, role="viewer")

    def test_bad_role_is_validation_error(
        self, session: Session, settings: Settings, actor: Actor
    ) -> None:
        victim = _make_admin(session, settings, username="victim")
        with pytest.raises(ValidationError):
            edit_user_role(session, actor, victim.id, role="wizard")

    def test_promote_viewer(
        self, session: Session, settings: Settings, actor: Actor
    ) -> None:
        viewer = User(
            username="v",
            auth_source="local",
            password_hash=hash_password(_STRONG_PW, settings.auth),
            role="viewer",
            enabled=True,
        )
        session.add(viewer)
        session.flush()
        result = edit_user_role(session, actor, viewer.id, role="admin")
        assert result.value.role == "admin"

    def test_cannot_demote_last_admin(self, session: Session, actor: Actor) -> None:
        # The only real admin is the actor's own seeded row.
        with pytest.raises(ConflictError):
            edit_user_role(session, actor, actor.user_id, role="viewer")

    def test_can_demote_when_another_admin_remains(
        self, session: Session, settings: Settings, actor: Actor
    ) -> None:
        spare = _make_admin(session, settings, username="spare")
        result = edit_user_role(session, actor, spare.id, role="viewer")
        assert result.value.role == "viewer"


class TestDisableEnable:
    def test_cannot_disable_last_admin(self, session: Session, actor: Actor) -> None:
        with pytest.raises(ConflictError):
            disable_user(session, actor, actor.user_id)

    def test_disable_then_enable_viewer(
        self, session: Session, settings: Settings, actor: Actor
    ) -> None:
        viewer = User(
            username="v",
            auth_source="local",
            password_hash=hash_password(_STRONG_PW, settings.auth),
            role="viewer",
            enabled=True,
        )
        session.add(viewer)
        session.flush()
        assert disable_user(session, actor, viewer.id).value.enabled is False
        assert enable_user(session, actor, viewer.id).value.enabled is True


class TestDeleteUser:
    def test_cannot_delete_last_admin(self, session: Session, actor: Actor) -> None:
        with pytest.raises(ConflictError):
            delete_user(session, actor, actor.user_id)

    def test_delete_viewer(
        self, session: Session, settings: Settings, actor: Actor
    ) -> None:
        viewer = User(
            username="v",
            auth_source="local",
            password_hash=hash_password(_STRONG_PW, settings.auth),
            role="viewer",
            enabled=True,
        )
        session.add(viewer)
        session.flush()
        vid = viewer.id
        result = delete_user(session, actor, vid)
        assert result.value is None
        assert result.effects == ()
        assert session.get(User, vid) is None


class TestResetPassword:
    def test_directory_account_refused(
        self, session: Session, settings: Settings, actor: Actor
    ) -> None:
        ldap_user = User(
            username="ldapper", auth_source="ldap", role="viewer", enabled=True
        )
        session.add(ldap_user)
        session.flush()
        with pytest.raises(ValidationError):
            reset_user_password(
                session,
                actor,
                ldap_user.id,
                password=_STRONG_PW,
                password_confirm=_STRONG_PW,
                settings=settings,
            )
        refreshed = session.get(User, ldap_user.id)
        assert refreshed is not None
        assert refreshed.password_hash is None

    def test_short_password_refused(
        self, session: Session, settings: Settings, actor: Actor
    ) -> None:
        with pytest.raises(ValidationError):
            reset_user_password(
                session,
                actor,
                actor.user_id,
                password="short",
                password_confirm="short",
                settings=settings,
            )

    def test_sets_new_hash(
        self, session: Session, settings: Settings, actor: Actor
    ) -> None:
        target = _make_admin(session, settings, username="spare")
        old_hash = target.password_hash
        new_pw = "BrandNewPass99!"
        result = reset_user_password(
            session,
            actor,
            target.id,
            password=new_pw,
            password_confirm=new_pw,
            settings=settings,
        )
        assert result.value.password_hash != old_hash
        assert verify_password(new_pw, result.value.password_hash, settings.auth)

    def test_no_password_just_revokes(
        self, session: Session, settings: Settings, actor: Actor
    ) -> None:
        target = _make_admin(session, settings, username="spare")
        old_hash = target.password_hash
        result = reset_user_password(
            session,
            actor,
            target.id,
            password=None,
            password_confirm=None,
            settings=settings,
        )
        # No password supplied: the hash is left untouched (sessions-only revoke).
        assert result.value.password_hash == old_hash


class TestRevokeSessions:
    def test_missing_user_is_not_found(self, session: Session, actor: Actor) -> None:
        with pytest.raises(NotFoundError):
            revoke_user_sessions(session, actor, 999999)


class TestDemoteCountsOnlyEnabledAdmins:
    """S22 (#67): a disabled admin does not keep the system reachable."""

    def test_demotion_refused_when_the_other_admin_is_disabled(
        self, session: Session, settings: Settings, actor: Actor
    ) -> None:
        dormant = _make_admin(session, settings, username="dormant")
        dormant.enabled = False
        session.flush()
        with pytest.raises(ConflictError):
            edit_user_role(session, actor, actor.user_id, role="operator")
        refreshed = session.get(User, actor.user_id)
        assert refreshed is not None and refreshed.role == "admin"

    def test_disabled_admin_can_be_demoted(
        self, session: Session, settings: Settings, actor: Actor
    ) -> None:
        dormant = _make_admin(session, settings, username="dormant")
        dormant.enabled = False
        session.flush()
        result = edit_user_role(session, actor, dormant.id, role="viewer")
        assert result.value.role == "viewer"
