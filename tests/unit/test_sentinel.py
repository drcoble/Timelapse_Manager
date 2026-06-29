"""Unit tests for the sentinel user invariants.

The sentinel is the service principal (id=1, username="system") whose sole
purpose is providing a stable actor id for audit foreign keys.  It must:

  - NOT count as a real administrator for the first-run gate.
  - NOT be authenticatable via any credential.

These tests exercise the security layer functions directly against a real
(in-memory-style) database so the assertions are structurally meaningful, not
vacuous.  The sentinel is injected via ``ensure_sentinel_admin`` to guarantee
it is present before any assertion is made.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy.orm import sessionmaker

from timelapse_manager.config.settings import AuthSettings
from timelapse_manager.db.engine import create_db_engine
from timelapse_manager.db.session import create_session_factory, session_scope
from timelapse_manager.security.login import authenticate_user, first_run_needed
from timelapse_manager.security.principal import ensure_sentinel_admin


def _fast_auth_settings() -> AuthSettings:
    return AuthSettings(argon2_memory_kib=256, argon2_time_cost=1, argon2_parallelism=1)


@pytest.fixture()
def factory(tmp_path: Path) -> sessionmaker:  # type: ignore[type-arg]
    """Return a session factory backed by a fresh migrated SQLite database."""
    db_path = tmp_path / "sentinel_test.db"
    url = f"sqlite:///{db_path}"
    alembic_ini = Path(__file__).parent.parent.parent / "alembic.ini"
    alembic_dir = Path(__file__).parent.parent.parent / "alembic"
    cfg = Config(str(alembic_ini))
    cfg.set_main_option("script_location", str(alembic_dir))
    cfg.set_main_option("sqlalchemy.url", url)
    alembic_command.upgrade(cfg, "head")
    engine = create_db_engine(url)
    return create_session_factory(engine)


class TestSentinelDoesNotSatisfyFirstRun:
    def test_sentinel_present_still_requires_first_run(
        self,
        factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        """A DB containing only the sentinel must still need first-run setup.

        The sentinel has role='admin' but no password_hash.  The first_run_needed
        predicate must exclude null-hash rows and return True.
        """
        with session_scope(factory) as db:
            ensure_sentinel_admin(db)
            # Sentinel is now in the DB; first-run must still be required.
            assert first_run_needed(db) is True, (
                "first_run_needed must return True when only the sentinel exists"
            )

    def test_first_run_satisfied_only_after_real_admin_added(
        self,
        factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        """first_run_needed returns False only once a real admin (with hash) exists."""
        from timelapse_manager.security.login import create_initial_admin

        with session_scope(factory) as db:
            ensure_sentinel_admin(db)
            assert first_run_needed(db) is True

            # Add a real admin; the gate must now be satisfied.
            create_initial_admin(
                db, "realadmin", "RealAdminPass99!", settings=_fast_auth_settings()
            )
            assert first_run_needed(db) is False, (
                "first_run_needed must return False after a real admin is created"
            )


class TestSentinelCannotAuthenticate:
    def test_sentinel_does_not_authenticate_with_any_password(
        self,
        factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        """authenticate_user must return None for the sentinel regardless of password.

        The sentinel has a NULL password_hash; verify_password returns False for
        null/empty hashes, so the sentinel is never admitted as a real user.
        """
        s = _fast_auth_settings()
        with session_scope(factory) as db:
            ensure_sentinel_admin(db)
            # Try several passwords to confirm no credential works.
            for attempt in ("", "anything", "system", "password", "AdminP@ssw0rd1234"):
                result = authenticate_user(db, "system", attempt, settings=s)
                assert result is None, (
                    f"authenticate_user must return None for sentinel;"
                    f" returned a user for password {attempt!r}"
                )

    def test_sentinel_does_not_authenticate_with_empty_string(
        self,
        factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        """Empty-string password against the null-hash sentinel returns None."""
        s = _fast_auth_settings()
        with session_scope(factory) as db:
            ensure_sentinel_admin(db)
            result = authenticate_user(db, "system", "", settings=s)
            assert result is None

    def test_nonexistent_username_returns_none(
        self,
        factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        """authenticate_user returns None for a username that does not exist."""
        s = _fast_auth_settings()
        with session_scope(factory) as db:
            result = authenticate_user(db, "no-such-user", "anything", settings=s)
            assert result is None
