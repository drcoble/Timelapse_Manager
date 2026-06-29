"""Unit tests for the server-side session layer."""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy.orm import sessionmaker

from timelapse_manager.config.settings import AuthSettings, SessionSettings
from timelapse_manager.db.engine import create_db_engine
from timelapse_manager.db.models import Session as SessionRow
from timelapse_manager.db.models import User
from timelapse_manager.db.session import create_session_factory, session_scope
from timelapse_manager.security.sessions import (
    create_session,
    hash_token,
    lookup_session,
    revoke_all_user_sessions,
    revoke_session,
    rotate_session,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fast_auth_settings() -> AuthSettings:
    return AuthSettings(argon2_memory_kib=256, argon2_time_cost=1, argon2_parallelism=1)


def _session_settings(
    idle_seconds: int = 1800,
    absolute_seconds: int = 86400,
    persistent_seconds: int = 2592000,
) -> SessionSettings:
    return SessionSettings(
        idle_timeout_seconds=idle_seconds,
        absolute_timeout_seconds=absolute_seconds,
        persistent_timeout_seconds=persistent_seconds,
    )


def _now(dt: datetime.datetime) -> datetime.datetime:
    """Return a fixed naive-UTC datetime (strips tzinfo like the real _utcnow)."""
    return dt.replace(tzinfo=None)


@pytest.fixture()
def factory(tmp_path: Path) -> sessionmaker:  # type: ignore[type-arg]
    """Return a session factory backed by a fresh migrated SQLite database."""
    db_path = tmp_path / "sessions_test.db"
    url = f"sqlite:///{db_path}"
    alembic_ini = Path(__file__).parent.parent.parent / "alembic.ini"
    alembic_dir = Path(__file__).parent.parent.parent / "alembic"
    cfg = Config(str(alembic_ini))
    cfg.set_main_option("script_location", str(alembic_dir))
    cfg.set_main_option("sqlalchemy.url", url)
    alembic_command.upgrade(cfg, "head")
    engine = create_db_engine(url)
    return create_session_factory(engine)


@pytest.fixture()
def admin_user(factory: sessionmaker) -> User:  # type: ignore[type-arg]
    """Seed and return a real admin User row."""
    from timelapse_manager.security.passwords import hash_password

    s = _fast_auth_settings()
    with session_scope(factory) as db:
        user = User(
            username="test-admin",
            auth_source="local",
            password_hash=hash_password("StrongPassword99!", s),
            role="admin",
            enabled=True,
        )
        db.add(user)
        db.flush()
        user_id = user.id
    with session_scope(factory) as db:
        return db.get(User, user_id)  # type: ignore[return-value]


class TestCreateSession:
    def test_returns_row_and_raw_token(
        self, factory: sessionmaker, admin_user: User
    ) -> None:  # type: ignore[type-arg]
        ss = _session_settings()
        with session_scope(factory) as db:
            user = db.merge(admin_user)
            row, raw_token = create_session(db, user, remember_me=False, settings=ss)
            assert isinstance(raw_token, str) and len(raw_token) > 20
            assert isinstance(row, SessionRow)

    def test_token_hash_stored_not_raw_token(
        self, factory: sessionmaker, admin_user: User
    ) -> None:  # type: ignore[type-arg]
        ss = _session_settings()
        with session_scope(factory) as db:
            user = db.merge(admin_user)
            row, raw_token = create_session(db, user, remember_me=False, settings=ss)
            assert row.token_hash == hash_token(raw_token)
            assert row.token_hash != raw_token

    def test_csrf_secret_is_populated(
        self, factory: sessionmaker, admin_user: User
    ) -> None:  # type: ignore[type-arg]
        ss = _session_settings()
        with session_scope(factory) as db:
            user = db.merge(admin_user)
            row, _ = create_session(db, user, remember_me=False, settings=ss)
            assert row.csrf_secret is not None
            assert len(row.csrf_secret) > 10

    def test_non_persistent_session_uses_absolute_cap(
        self, factory: sessionmaker, admin_user: User
    ) -> None:  # type: ignore[type-arg]
        ss = _session_settings(absolute_seconds=86400, persistent_seconds=2592000)
        t0 = datetime.datetime(2025, 1, 1, 12, 0, 0)
        with session_scope(factory) as db:
            user = db.merge(admin_user)
            row, _ = create_session(
                db, user, remember_me=False, settings=ss, now=lambda: t0
            )
            expected_expires = t0 + datetime.timedelta(seconds=86400)
            assert row.expires_at == expected_expires

    def test_persistent_session_uses_persistent_cap(
        self, factory: sessionmaker, admin_user: User
    ) -> None:  # type: ignore[type-arg]
        ss = _session_settings(absolute_seconds=86400, persistent_seconds=2592000)
        t0 = datetime.datetime(2025, 1, 1, 12, 0, 0)
        with session_scope(factory) as db:
            user = db.merge(admin_user)
            row, _ = create_session(
                db, user, remember_me=True, settings=ss, now=lambda: t0
            )
            expected_expires = t0 + datetime.timedelta(seconds=2592000)
            assert row.expires_at == expected_expires


class TestLookupSession:
    def test_valid_session_returns_user(
        self, factory: sessionmaker, admin_user: User
    ) -> None:  # type: ignore[type-arg]
        ss = _session_settings()
        t0 = datetime.datetime(2025, 1, 1, 12, 0, 0)
        with session_scope(factory) as db:
            user = db.merge(admin_user)
            _, raw_token = create_session(
                db, user, remember_me=False, settings=ss, now=lambda: t0
            )
        with session_scope(factory) as db:
            result = lookup_session(db, raw_token, settings=ss, now=lambda: t0)
            assert result is not None
            assert result.id == admin_user.id

    def test_unknown_token_returns_none(self, factory: sessionmaker) -> None:  # type: ignore[type-arg]
        ss = _session_settings()
        with session_scope(factory) as db:
            result = lookup_session(db, "completely-bogus-token", settings=ss)
            assert result is None

    def test_empty_token_returns_none(self, factory: sessionmaker) -> None:  # type: ignore[type-arg]
        ss = _session_settings()
        with session_scope(factory) as db:
            result = lookup_session(db, "", settings=ss)
            assert result is None

    def test_revoked_session_returns_none(
        self, factory: sessionmaker, admin_user: User
    ) -> None:  # type: ignore[type-arg]
        ss = _session_settings()
        t0 = datetime.datetime(2025, 1, 1, 12, 0, 0)
        with session_scope(factory) as db:
            user = db.merge(admin_user)
            _, raw_token = create_session(
                db, user, remember_me=False, settings=ss, now=lambda: t0
            )
            revoke_session(db, raw_token, now=lambda: t0)
        with session_scope(factory) as db:
            result = lookup_session(db, raw_token, settings=ss, now=lambda: t0)
            assert result is None

    def test_disabled_user_session_returns_none(
        self, factory: sessionmaker, admin_user: User
    ) -> None:  # type: ignore[type-arg]
        ss = _session_settings()
        t0 = datetime.datetime(2025, 1, 1, 12, 0, 0)
        with session_scope(factory) as db:
            user = db.merge(admin_user)
            _, raw_token = create_session(
                db, user, remember_me=False, settings=ss, now=lambda: t0
            )
            # Disable the user.
            user = db.get(User, admin_user.id)
            user.enabled = False
        with session_scope(factory) as db:
            result = lookup_session(db, raw_token, settings=ss, now=lambda: t0)
            assert result is None

    def test_idle_expired_session_returns_none(
        self, factory: sessionmaker, admin_user: User
    ) -> None:  # type: ignore[type-arg]
        idle = 1800
        ss = _session_settings(idle_seconds=idle, absolute_seconds=86400)
        t0 = datetime.datetime(2025, 1, 1, 12, 0, 0)
        with session_scope(factory) as db:
            user = db.merge(admin_user)
            _, raw_token = create_session(
                db, user, remember_me=False, settings=ss, now=lambda: t0
            )
        # Advance just past idle timeout but well within absolute cap.
        t_idle = t0 + datetime.timedelta(seconds=idle + 1)
        with session_scope(factory) as db:
            result = lookup_session(db, raw_token, settings=ss, now=lambda: t_idle)
            assert result is None

    def test_absolute_cap_expired_non_persistent_session_returns_none(
        self, factory: sessionmaker, admin_user: User
    ) -> None:
        # Isolate the absolute cap: last_active is fresh (no idle expiry),
        # created_at is old enough to breach the absolute cap.
        ss = _session_settings(idle_seconds=99999, absolute_seconds=86400)
        t0 = datetime.datetime(2025, 1, 1, 12, 0, 0)
        with session_scope(factory) as db:
            user = db.merge(admin_user)
            row, raw_token = create_session(
                db, user, remember_me=False, settings=ss, now=lambda: t0
            )
            # Manually backdate created_at so the absolute cap has lapsed.
            row.created_at = t0 - datetime.timedelta(seconds=86401)
            row.last_active = t0  # Keep last_active fresh — idle clock OK.
        with session_scope(factory) as db:
            result = lookup_session(db, raw_token, settings=ss, now=lambda: t0)
            assert result is None

    def test_idle_timeout_applies_to_persistent_sessions(
        self, factory: sessionmaker, admin_user: User
    ) -> None:
        # The flagged semantic: persistent sessions are still subject to idle expiry.
        idle = 1800
        ss = _session_settings(
            idle_seconds=idle, absolute_seconds=86400, persistent_seconds=2592000
        )
        t0 = datetime.datetime(2025, 1, 1, 12, 0, 0)
        with session_scope(factory) as db:
            user = db.merge(admin_user)
            row, raw_token = create_session(
                db, user, remember_me=True, settings=ss, now=lambda: t0
            )
            # created_at is fresh (well within persistent cap).
            # last_active is old (idle deadline exceeded).
            row.last_active = t0 - datetime.timedelta(seconds=idle + 1)
        with session_scope(factory) as db:
            result = lookup_session(db, raw_token, settings=ss, now=lambda: t0)
            assert result is None, (
                "Persistent session should expire on idle timeout"
                " even within creation cap"
            )

    def test_persistent_session_survives_beyond_absolute_cap(
        self, factory: sessionmaker, admin_user: User
    ) -> None:
        # A persistent session created at t0 should still be live at t0+2d
        # if idle is satisfied and absolute_timeout < 2d < persistent_timeout.
        # Use an idle timeout large enough that it does not fire before the
        # absolute cap (idle must be > absolute+1 seconds to isolate the cap check).
        absolute = 86400  # 1 day
        persistent = 2592000  # 30 days
        idle = 9999999  # far beyond test window so idle does not fire
        ss = _session_settings(
            idle_seconds=idle, absolute_seconds=absolute, persistent_seconds=persistent
        )
        t0 = datetime.datetime(2025, 1, 1, 12, 0, 0)
        with session_scope(factory) as db:
            user = db.merge(admin_user)
            _, raw_token = create_session(
                db, user, remember_me=True, settings=ss, now=lambda: t0
            )
        # 2 days later (past absolute cap) but still within persistent cap.
        # last_active == t0 (set at creation); idle = t0 + 9999999s, well in future.
        t_later = t0 + datetime.timedelta(seconds=absolute + 1)
        with session_scope(factory) as db:
            result = lookup_session(db, raw_token, settings=ss, now=lambda: t_later)
            assert result is not None, (
                "Persistent session should survive beyond absolute cap"
            )


class TestRotateSession:
    def test_rotate_revokes_old_token_and_returns_new_one(
        self, factory: sessionmaker, admin_user: User
    ) -> None:
        ss = _session_settings()
        t0 = datetime.datetime(2025, 1, 1, 12, 0, 0)
        with session_scope(factory) as db:
            user = db.merge(admin_user)
            _, old_token = create_session(
                db, user, remember_me=False, settings=ss, now=lambda: t0
            )
            _, new_token = rotate_session(
                db, old_token, user, remember_me=False, settings=ss, now=lambda: t0
            )
            assert new_token != old_token
        with session_scope(factory) as db:
            assert lookup_session(db, old_token, settings=ss, now=lambda: t0) is None
            assert (
                lookup_session(db, new_token, settings=ss, now=lambda: t0) is not None
            )

    def test_rotate_with_unknown_old_token_still_creates_new_session(
        self, factory: sessionmaker, admin_user: User
    ) -> None:
        ss = _session_settings()
        t0 = datetime.datetime(2025, 1, 1, 12, 0, 0)
        with session_scope(factory) as db:
            user = db.merge(admin_user)
            _, new_token = rotate_session(
                db,
                "nonexistent-token",
                user,
                remember_me=False,
                settings=ss,
                now=lambda: t0,
            )
        with session_scope(factory) as db:
            assert (
                lookup_session(db, new_token, settings=ss, now=lambda: t0) is not None
            )


class TestRevokeSession:
    def test_revoke_makes_session_invalid(
        self, factory: sessionmaker, admin_user: User
    ) -> None:  # type: ignore[type-arg]
        ss = _session_settings()
        t0 = datetime.datetime(2025, 1, 1, 12, 0, 0)
        with session_scope(factory) as db:
            user = db.merge(admin_user)
            _, raw_token = create_session(
                db, user, remember_me=False, settings=ss, now=lambda: t0
            )
            revoke_session(db, raw_token, now=lambda: t0)
        with session_scope(factory) as db:
            assert lookup_session(db, raw_token, settings=ss, now=lambda: t0) is None

    def test_revoke_unknown_token_is_noop(self, factory: sessionmaker) -> None:  # type: ignore[type-arg]
        with session_scope(factory) as db:
            revoke_session(db, "unknown-token-xyz")  # should not raise

    def test_revoke_empty_token_is_noop(self, factory: sessionmaker) -> None:  # type: ignore[type-arg]
        with session_scope(factory) as db:
            revoke_session(db, "")  # should not raise

    def test_revoke_already_revoked_is_idempotent(
        self, factory: sessionmaker, admin_user: User
    ) -> None:
        ss = _session_settings()
        t0 = datetime.datetime(2025, 1, 1, 12, 0, 0)
        with session_scope(factory) as db:
            user = db.merge(admin_user)
            _, raw_token = create_session(
                db, user, remember_me=False, settings=ss, now=lambda: t0
            )
            revoke_session(db, raw_token, now=lambda: t0)
            revoke_session(
                db, raw_token, now=lambda: t0
            )  # second call should not raise


class TestRevokeAllUserSessions:
    def test_revokes_all_sessions_for_user(
        self, factory: sessionmaker, admin_user: User
    ) -> None:
        ss = _session_settings()
        t0 = datetime.datetime(2025, 1, 1, 12, 0, 0)
        tokens: list[str] = []
        with session_scope(factory) as db:
            user = db.merge(admin_user)
            for _ in range(3):
                _, tok = create_session(
                    db, user, remember_me=False, settings=ss, now=lambda: t0
                )
                tokens.append(tok)
            revoke_all_user_sessions(db, admin_user.id, now=lambda: t0)
        with session_scope(factory) as db:
            for tok in tokens:
                assert lookup_session(db, tok, settings=ss, now=lambda: t0) is None

    def test_revoke_all_with_no_sessions_is_noop(self, factory: sessionmaker) -> None:  # type: ignore[type-arg]
        with session_scope(factory) as db:
            revoke_all_user_sessions(db, user_id=9999)  # should not raise
