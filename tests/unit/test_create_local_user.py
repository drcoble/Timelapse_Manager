"""Unit tests for the local-account creation helper.

``create_local_user`` is the single place a local account is materialised; both
first-run admin setup and the non-interactive ``user create`` deploy command
flow through it. These tests exercise it directly against a real migrated SQLite
database so the assertions are structurally meaningful (the row really lands and
the stored hash really verifies), not vacuous.

Fast (low-cost) Argon2 parameters are used throughout so the tests stay quick
while still going through the genuine hashing path.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from timelapse_manager.config.settings import AuthSettings
from timelapse_manager.db.engine import create_db_engine
from timelapse_manager.db.models import User
from timelapse_manager.db.session import create_session_factory, session_scope
from timelapse_manager.security.login import create_local_user
from timelapse_manager.security.passwords import verify_password


def _fast_auth_settings() -> AuthSettings:
    return AuthSettings(argon2_memory_kib=256, argon2_time_cost=1, argon2_parallelism=1)


@pytest.fixture()
def factory(tmp_path: Path) -> sessionmaker:  # type: ignore[type-arg]
    """Return a session factory backed by a fresh migrated SQLite database."""
    db_path = tmp_path / "create_local_user_test.db"
    url = f"sqlite:///{db_path}"
    alembic_ini = Path(__file__).parent.parent.parent / "alembic.ini"
    cfg = Config(str(alembic_ini))
    cfg.set_main_option("sqlalchemy.url", url)
    alembic_command.upgrade(cfg, "head")
    engine = create_db_engine(url)
    return create_session_factory(engine)


class TestCreateLocalUser:
    def test_creates_enabled_local_user_with_role_and_verifiable_hash(
        self,
        factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        """The created row is enabled+local, carries the role, and its hash verifies."""
        auth = _fast_auth_settings()
        password = "OperatorPass123!"  # noqa: S105 - test fixture credential
        with session_scope(factory) as db:
            user = create_local_user(db, "op-user", password, "operator", settings=auth)

        with session_scope(factory) as db:
            row = db.execute(
                select(User).where(User.username == "op-user")
            ).scalar_one()
            assert row.enabled is True
            assert row.auth_source == "local"
            assert row.role == "operator"
            assert row.password_hash is not None
            assert verify_password(password, row.password_hash, auth) is True
            # The plaintext must never be stored verbatim.
            assert password not in row.password_hash
        # The returned object refers to the persisted row.
        assert user.username == "op-user"

    def test_default_role_is_viewer(
        self,
        factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        """Omitting the role must default to the least-privileged viewer role."""
        auth = _fast_auth_settings()
        with session_scope(factory) as db:
            create_local_user(db, "default-role", "ViewerPass123!", settings=auth)

        with session_scope(factory) as db:
            row = db.execute(
                select(User).where(User.username == "default-role")
            ).scalar_one()
            assert row.role == "viewer"

    def test_admin_role_is_honoured(
        self,
        factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        """An explicit admin role must be stored as admin."""
        auth = _fast_auth_settings()
        with session_scope(factory) as db:
            create_local_user(
                db, "admin-user", "AdminPass1234!", "admin", settings=auth
            )

        with session_scope(factory) as db:
            row = db.execute(
                select(User).where(User.username == "admin-user")
            ).scalar_one()
            assert row.role == "admin"

    def test_duplicate_username_raises(
        self,
        factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        """A second account with the same username violates the unique constraint."""
        auth = _fast_auth_settings()
        with session_scope(factory) as db:
            create_local_user(db, "dup", "FirstPass123!", settings=auth)

        with (
            pytest.raises(Exception),  # noqa: B017,PT011 - IntegrityError surfaces
            session_scope(factory) as db,
        ):
            create_local_user(db, "dup", "SecondPass123!", settings=auth)
