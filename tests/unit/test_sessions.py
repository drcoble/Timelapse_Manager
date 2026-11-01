"""Unit tests for the server-side session layer."""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path
from types import SimpleNamespace

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
    touch_interval,
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


class TestLastActiveTouch:
    """The ``last_active`` refresh is throttled and holds the write lock briefly (#37).

    Before #37 every lookup flushed ``last_active`` inside the caller's
    transaction, so every authenticated request held SQLite's write lock until it
    ended and a dashboard's parallel requests queued on it.
    """

    @staticmethod
    def _start(
        factory: sessionmaker,  # type: ignore[type-arg]
        admin_user: User,
        ss: SessionSettings,
        t0: datetime.datetime,
    ) -> str:
        with session_scope(factory) as db:
            user = db.merge(admin_user)
            _, raw_token = create_session(
                db, user, remember_me=False, settings=ss, now=lambda: t0
            )
        return raw_token

    @staticmethod
    def _stored_last_active(
        factory: sessionmaker,  # type: ignore[type-arg]
        raw_token: str,
    ) -> datetime.datetime | None:
        with session_scope(factory) as db:
            row = db.query(SessionRow).filter_by(token_hash=hash_token(raw_token)).one()
            return row.last_active

    def test_lookup_within_interval_does_not_write(
        self, factory: sessionmaker, admin_user: User
    ) -> None:  # type: ignore[type-arg]
        ss = _session_settings()
        t0 = datetime.datetime(2025, 1, 1, 12, 0, 0)
        tok = self._start(factory, admin_user, ss, t0)
        just_inside = t0 + touch_interval(ss) - datetime.timedelta(seconds=1)
        with session_scope(factory) as db:
            assert lookup_session(db, tok, settings=ss, now=lambda: just_inside)
            assert not db.dirty
        assert self._stored_last_active(factory, tok) == t0

    def test_lookup_after_interval_refreshes(
        self, factory: sessionmaker, admin_user: User
    ) -> None:  # type: ignore[type-arg]
        ss = _session_settings()
        t0 = datetime.datetime(2025, 1, 1, 12, 0, 0)
        tok = self._start(factory, admin_user, ss, t0)
        later = t0 + touch_interval(ss)
        with session_scope(factory) as db:
            assert lookup_session(db, tok, settings=ss, now=lambda: later)
        assert self._stored_last_active(factory, tok) == later

    def test_activity_still_defers_idle_expiry(
        self, factory: sessionmaker, admin_user: User
    ) -> None:  # type: ignore[type-arg]
        idle = 1800
        ss = _session_settings(idle_seconds=idle)
        t0 = datetime.datetime(2025, 1, 1, 12, 0, 0)
        tok = self._start(factory, admin_user, ss, t0)
        t1 = t0 + datetime.timedelta(seconds=idle - 10)
        t2 = t1 + datetime.timedelta(seconds=idle - 10)
        with session_scope(factory) as db:
            assert lookup_session(db, tok, settings=ss, now=lambda: t1)
        with session_scope(factory) as db:
            assert lookup_session(db, tok, settings=ss, now=lambda: t2)

    def test_interval_is_capped_for_short_idle_timeouts(self) -> None:
        assert (
            touch_interval(_session_settings(idle_seconds=1800)).total_seconds() == 60
        )
        assert touch_interval(_session_settings(idle_seconds=100)).total_seconds() == 10
        assert touch_interval(_session_settings(idle_seconds=5)).total_seconds() == 1

    def test_touch_releases_the_write_lock_before_the_caller_ends(
        self, factory: sessionmaker, admin_user: User, tmp_path: Path
    ) -> None:  # type: ignore[type-arg]
        """A due touch commits at once: another writer can lock immediately."""
        ss = _session_settings()
        t0 = datetime.datetime(2025, 1, 1, 12, 0, 0)
        tok = self._start(factory, admin_user, ss, t0)
        later = t0 + touch_interval(ss)
        with session_scope(factory) as db:
            assert lookup_session(db, tok, settings=ss, now=lambda: later)
            # Still inside the caller's scope (the request): timeout=0 fails at
            # once if the touch's write lock were still held.
            raw = sqlite3.connect(str(tmp_path / "sessions_test.db"), timeout=0)
            try:
                raw.execute("BEGIN IMMEDIATE")
                raw.execute("ROLLBACK")
            finally:
                raw.close()

    def test_touch_does_not_commit_the_callers_earlier_writes(
        self, factory: sessionmaker, admin_user: User, tmp_path: Path
    ) -> None:  # type: ignore[type-arg]
        """With a write already open, the touch joins it and commits with it."""
        ss = _session_settings()
        t0 = datetime.datetime(2025, 1, 1, 12, 0, 0)
        tok = self._start(factory, admin_user, ss, t0)
        later = t0 + touch_interval(ss)

        def visible() -> int:
            raw = sqlite3.connect(str(tmp_path / "sessions_test.db"), timeout=5)
            try:
                return raw.execute(
                    "SELECT count(*) FROM user WHERE username = 'touch-probe'"
                ).fetchone()[0]
            finally:
                raw.close()

        with session_scope(factory) as db:
            db.add(User(username="touch-probe", role="viewer", auth_source="ldap"))
            db.flush()
            assert lookup_session(db, tok, settings=ss, now=lambda: later)
            assert visible() == 0
        assert visible() == 1
        assert self._stored_last_active(factory, tok) == later

    def test_locked_touch_is_skipped_and_lookup_succeeds(
        self,
        admin_user: User,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A touch that cannot get the lock never fails the lookup."""
        import timelapse_manager.db.engine as engine_mod

        monkeypatch.setattr(engine_mod, "SQLITE_BUSY_TIMEOUT_MS", 50)
        db_path = tmp_path / "sessions_test.db"
        engine = create_db_engine(f"sqlite:///{db_path}")
        quick_factory = create_session_factory(engine)
        ss = _session_settings()
        t0 = datetime.datetime(2025, 1, 1, 12, 0, 0)
        tok = self._start(quick_factory, admin_user, ss, t0)
        later = t0 + touch_interval(ss)

        holder = sqlite3.connect(str(db_path), timeout=0)
        try:
            holder.execute("BEGIN IMMEDIATE")
            with caplog.at_level("WARNING"), session_scope(quick_factory) as db:
                user = lookup_session(db, tok, settings=ss, now=lambda: later)
            assert user is not None
            assert "database is locked" in caplog.text
            # The skipped touch left the stored value alone.
            holder.execute("ROLLBACK")
            assert self._stored_last_active(quick_factory, tok) == t0
        finally:
            holder.close()
            engine.dispose()


class TestDirectoryRecheckBookkeeping:
    """The LDAP re-check's writes commit in their own short transaction (#66 C1).

    The re-check used to flush into the caller's transaction, so on a GET the
    request held SQLite's write lock until it ended -- across an ``async``
    route's camera probe -- and a revoke was rolled back with the 401 it
    caused. The directory and the role-group settings are stubbed at the
    revalidation module's seams; everything else is the real session layer.
    """

    _ADMIN_GROUP = "cn=admins,ou=groups,dc=example,dc=com"
    _VIEWER_GROUP = "cn=viewers,ou=groups,dc=example,dc=com"
    _T0 = datetime.datetime(2025, 1, 1, 12, 0, 0)
    # One second later: the re-check is due (never run), the touch is not.
    _T1 = _T0 + datetime.timedelta(seconds=1)

    @pytest.fixture()
    def directory(self, monkeypatch: pytest.MonkeyPatch) -> list[object]:
        """Stub the directory; append an outcome to choose the next answer."""
        import timelapse_manager.security.ldap_settings_service as ldap_settings
        import timelapse_manager.security.session_revalidation as revalidation
        from timelapse_manager.security.ldap_directory import (
            LdapDirectoryState,
            LdapOutcome,
        )

        answers: list[object] = []

        def resolver(_username: str) -> LdapDirectoryState:
            outcome, groups = answers[-1]  # type: ignore[misc]
            assert isinstance(outcome, LdapOutcome)
            return LdapDirectoryState(outcome=outcome, groups=frozenset(groups))

        monkeypatch.setattr(
            revalidation, "_default_directory_resolver", lambda _db: resolver
        )
        monkeypatch.setattr(
            ldap_settings,
            "load_settings",
            lambda _db: SimpleNamespace(
                admin_group_dn=self._ADMIN_GROUP,
                operator_group_dn=None,
                viewer_group_dn=self._VIEWER_GROUP,
            ),
        )
        return answers

    def _start_ldap_session(
        self,
        factory: sessionmaker,  # type: ignore[type-arg]
    ) -> str:
        """Create an LDAP admin with a session whose re-check is due at _T1."""
        with session_scope(factory) as db:
            user = User(
                username="dir-user", auth_source="ldap", role="admin", enabled=True
            )
            db.add(user)
            db.flush()
            _, raw_token = create_session(
                db,
                user,
                remember_me=False,
                settings=_session_settings(),
                now=lambda: self._T0,
            )
        return raw_token

    @staticmethod
    def _stored(
        factory: sessionmaker,  # type: ignore[type-arg]
        raw_token: str,
    ) -> tuple[bool, datetime.datetime | None, str]:
        """Return the stored (revoked, last_revalidated_at, user role)."""
        with session_scope(factory) as db:
            row = db.query(SessionRow).filter_by(token_hash=hash_token(raw_token)).one()
            user = db.get(User, row.user_id)
            assert user is not None
            return bool(row.revoked), row.last_revalidated_at, user.role

    @staticmethod
    def _write_lock_is_free(db_path: Path) -> bool:
        raw = sqlite3.connect(str(db_path), timeout=0)
        try:
            raw.execute("BEGIN IMMEDIATE")
            raw.execute("ROLLBACK")
            return True
        except sqlite3.OperationalError:
            return False
        finally:
            raw.close()

    def test_due_recheck_commits_before_the_caller_ends(
        self, factory: sessionmaker, directory: list[object], tmp_path: Path
    ) -> None:  # type: ignore[type-arg]
        from timelapse_manager.security.ldap_directory import LdapOutcome

        directory.append((LdapOutcome.AUTHENTICATED, {self._ADMIN_GROUP}))
        tok = self._start_ldap_session(factory)
        with session_scope(factory) as db:
            user = lookup_session(
                db, tok, settings=_session_settings(), now=lambda: self._T1
            )
            assert user is not None
            # Still inside the caller's scope (the request), as an async route
            # awaiting a camera would be: the write lock must already be free.
            assert self._write_lock_is_free(tmp_path / "sessions_test.db")
            assert self._stored(factory, tok) == (False, self._T1, "admin")

    def test_role_change_commits_with_the_recheck(
        self, factory: sessionmaker, directory: list[object]
    ) -> None:  # type: ignore[type-arg]
        from timelapse_manager.security.ldap_directory import LdapOutcome

        directory.append((LdapOutcome.AUTHENTICATED, {self._VIEWER_GROUP}))
        tok = self._start_ldap_session(factory)
        db = factory()
        try:
            user = lookup_session(
                db, tok, settings=_session_settings(), now=lambda: self._T1
            )
            assert user is not None and user.role == "viewer"
            db.rollback()  # the request fails later; the demotion must stand
        finally:
            db.close()
        assert self._stored(factory, tok) == (False, self._T1, "viewer")

    def test_revoke_survives_the_callers_rollback(
        self, factory: sessionmaker, directory: list[object]
    ) -> None:  # type: ignore[type-arg]
        """The defect found while verifying C1: a GET's 401 rolled the revoke back."""
        from timelapse_manager.security.ldap_directory import LdapOutcome

        directory.append((LdapOutcome.NO_SUCH_USER, set()))
        tok = self._start_ldap_session(factory)
        db = factory()
        try:
            assert (
                lookup_session(
                    db, tok, settings=_session_settings(), now=lambda: self._T1
                )
                is None
            )
            db.rollback()  # what get_session does when the dependency raises 401
        finally:
            db.close()
        revoked, _, _ = self._stored(factory, tok)
        assert revoked is True

    def test_recheck_joins_the_callers_open_write(
        self, factory: sessionmaker, directory: list[object], tmp_path: Path
    ) -> None:  # type: ignore[type-arg]
        """With a write already open, the re-check is flushed, never committed early."""
        from timelapse_manager.security.ldap_directory import LdapOutcome

        directory.append((LdapOutcome.AUTHENTICATED, {self._VIEWER_GROUP}))
        tok = self._start_ldap_session(factory)

        def visible_probe_rows() -> int:
            raw = sqlite3.connect(str(tmp_path / "sessions_test.db"), timeout=5)
            try:
                return raw.execute(
                    "SELECT count(*) FROM user WHERE username = 'recheck-probe'"
                ).fetchone()[0]
            finally:
                raw.close()

        with session_scope(factory) as db:
            db.add(User(username="recheck-probe", role="viewer", auth_source="ldap"))
            db.flush()
            user = lookup_session(
                db, tok, settings=_session_settings(), now=lambda: self._T1
            )
            assert user is not None and user.role == "viewer"
            assert visible_probe_rows() == 0
        assert visible_probe_rows() == 1
        assert self._stored(factory, tok) == (False, self._T1, "viewer")

    def test_role_change_whose_commit_is_locked_is_denied(
        self,
        factory: sessionmaker,  # type: ignore[type-arg]
        directory: list[object],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Fail closed: an uncommitted demotion is never honoured, nor the old role."""
        from sqlalchemy.exc import OperationalError

        from timelapse_manager.security.ldap_directory import LdapOutcome

        directory.append((LdapOutcome.AUTHENTICATED, {self._VIEWER_GROUP}))
        tok = self._start_ldap_session(factory)
        db = factory()

        def locked_commit() -> None:
            raise OperationalError(
                "COMMIT", {}, sqlite3.OperationalError("database is locked")
            )

        db.commit = locked_commit  # type: ignore[method-assign]
        try:
            with caplog.at_level("WARNING"):
                result = lookup_session(
                    db, tok, settings=_session_settings(), now=lambda: self._T1
                )
            assert result is None
            assert "database is locked" in caplog.text
        finally:
            db.close()
        # The stored role and re-check timestamp are untouched.
        assert self._stored(factory, tok) == (False, None, "admin")

    def test_unchanged_role_whose_commit_is_locked_is_kept(
        self, factory: sessionmaker, directory: list[object]
    ) -> None:  # type: ignore[type-arg]
        """Only a timestamp was lost: the lookup succeeds and re-checks next time."""
        from sqlalchemy.exc import OperationalError

        from timelapse_manager.security.ldap_directory import LdapOutcome

        directory.append((LdapOutcome.AUTHENTICATED, {self._ADMIN_GROUP}))
        tok = self._start_ldap_session(factory)
        db = factory()

        def locked_commit() -> None:
            raise OperationalError(
                "COMMIT", {}, sqlite3.OperationalError("database is locked")
            )

        db.commit = locked_commit  # type: ignore[method-assign]
        try:
            user = lookup_session(
                db, tok, settings=_session_settings(), now=lambda: self._T1
            )
            assert user is not None and user.role == "admin"
        finally:
            db.close()
        assert self._stored(factory, tok) == (False, None, "admin")

    def test_recheck_flush_that_cannot_get_the_lock_is_denied(
        self,
        admin_user: User,
        directory: list[object],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Real contention: the re-check's own flush times out on the lock.

        The failed flush expires the rows before the re-check's outcome can be
        read, so the lookup fails closed, and nothing is stored.
        """
        import timelapse_manager.db.engine as engine_mod
        from timelapse_manager.security.ldap_directory import LdapOutcome

        monkeypatch.setattr(engine_mod, "SQLITE_BUSY_TIMEOUT_MS", 50)
        db_path = tmp_path / "sessions_test.db"
        engine = create_db_engine(f"sqlite:///{db_path}")
        quick_factory = create_session_factory(engine)
        directory.append((LdapOutcome.AUTHENTICATED, {self._VIEWER_GROUP}))
        tok = self._start_ldap_session(quick_factory)

        holder = sqlite3.connect(str(db_path), timeout=0)
        try:
            holder.execute("BEGIN IMMEDIATE")
            with session_scope(quick_factory) as db:
                result = lookup_session(
                    db, tok, settings=_session_settings(), now=lambda: self._T1
                )
            assert result is None
            holder.execute("ROLLBACK")
            assert self._stored(quick_factory, tok) == (False, None, "admin")
        finally:
            holder.close()
            engine.dispose()

    def test_revoke_whose_commit_is_locked_is_still_denied(
        self, factory: sessionmaker, directory: list[object]
    ) -> None:  # type: ignore[type-arg]
        from sqlalchemy.exc import OperationalError

        from timelapse_manager.security.ldap_directory import LdapOutcome

        directory.append((LdapOutcome.NO_SUCH_USER, set()))
        tok = self._start_ldap_session(factory)
        db = factory()

        def locked_commit() -> None:
            raise OperationalError(
                "COMMIT", {}, sqlite3.OperationalError("database is locked")
            )

        db.commit = locked_commit  # type: ignore[method-assign]
        try:
            assert (
                lookup_session(
                    db, tok, settings=_session_settings(), now=lambda: self._T1
                )
                is None
            )
        finally:
            db.close()


class TestPassiveLookupDoesNotTouch:
    """``touch=False`` (a passive background poll) skips only the touch (#70 F-S1)."""

    @staticmethod
    def _start(
        factory: sessionmaker,  # type: ignore[type-arg]
        admin_user: User,
        ss: SessionSettings,
        t0: datetime.datetime,
    ) -> str:
        with session_scope(factory) as db:
            user = db.merge(admin_user)
            _, raw_token = create_session(
                db, user, remember_me=False, settings=ss, now=lambda: t0
            )
        return raw_token

    @staticmethod
    def _stored_last_active(
        factory: sessionmaker,  # type: ignore[type-arg]
        raw_token: str,
    ) -> datetime.datetime | None:
        with session_scope(factory) as db:
            row = db.query(SessionRow).filter_by(token_hash=hash_token(raw_token)).one()
            return row.last_active

    def test_touch_false_leaves_last_active_unchanged(
        self, factory: sessionmaker, admin_user: User
    ) -> None:  # type: ignore[type-arg]
        ss = _session_settings()
        t0 = datetime.datetime(2025, 1, 1, 12, 0, 0)
        tok = self._start(factory, admin_user, ss, t0)
        later = t0 + datetime.timedelta(minutes=5)
        with session_scope(factory) as db:
            user = lookup_session(db, tok, settings=ss, now=lambda: later, touch=False)
            assert user is not None
            assert not db.dirty
        assert self._stored_last_active(factory, tok) == t0

    def test_get_session_row_touch_false_returns_the_live_row(
        self, factory: sessionmaker, admin_user: User
    ) -> None:  # type: ignore[type-arg]
        from timelapse_manager.security.sessions import get_session_row

        ss = _session_settings()
        t0 = datetime.datetime(2025, 1, 1, 12, 0, 0)
        tok = self._start(factory, admin_user, ss, t0)
        later = t0 + datetime.timedelta(minutes=5)
        with session_scope(factory) as db:
            row = get_session_row(db, tok, settings=ss, now=lambda: later, touch=False)
            assert row is not None and row.csrf_secret
        assert self._stored_last_active(factory, tok) == t0

    def test_touch_false_still_expires_after_idle_timeout(
        self,
        factory: sessionmaker,  # type: ignore[type-arg]
        admin_user: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import timelapse_manager.security.session_revalidation as revalidation

        calls: list[int] = []

        def counting_revalidate(*args: object, **kwargs: object) -> bool:
            calls.append(1)
            return True

        monkeypatch.setattr(
            revalidation, "revalidate_ldap_session", counting_revalidate
        )
        idle = 1800
        ss = _session_settings(idle_seconds=idle)
        t0 = datetime.datetime(2025, 1, 1, 12, 0, 0)
        tok = self._start(factory, admin_user, ss, t0)
        # Live but untouched: the directory re-check still runs on a passive poll.
        live = t0 + datetime.timedelta(seconds=idle - 60)
        with session_scope(factory) as db:
            assert lookup_session(db, tok, settings=ss, now=lambda: live, touch=False)
        assert calls == [1]
        # The poll did not defer the idle deadline, so just past it is expired.
        expired = t0 + datetime.timedelta(seconds=idle + 1)
        with session_scope(factory) as db:
            assert (
                lookup_session(db, tok, settings=ss, now=lambda: expired, touch=False)
                is None
            )

    def test_touch_false_still_runs_the_directory_recheck_revoke(
        self,
        factory: sessionmaker,  # type: ignore[type-arg]
        admin_user: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import timelapse_manager.security.session_revalidation as revalidation

        monkeypatch.setattr(
            revalidation, "revalidate_ldap_session", lambda *a, **k: False
        )
        ss = _session_settings()
        t0 = datetime.datetime(2025, 1, 1, 12, 0, 0)
        tok = self._start(factory, admin_user, ss, t0)
        later = t0 + datetime.timedelta(minutes=5)
        with session_scope(factory) as db:
            assert (
                lookup_session(db, tok, settings=ss, now=lambda: later, touch=False)
                is None
            )

    def test_default_touch_is_unchanged(
        self, factory: sessionmaker, admin_user: User
    ) -> None:  # type: ignore[type-arg]
        ss = _session_settings()
        t0 = datetime.datetime(2025, 1, 1, 12, 0, 0)
        tok = self._start(factory, admin_user, ss, t0)
        later = t0 + datetime.timedelta(minutes=5)
        with session_scope(factory) as db:
            assert lookup_session(db, tok, settings=ss, now=lambda: later)
        assert self._stored_last_active(factory, tok) == later


# ---------------------------------------------------------------------------
# One resolution per request (#67 R3)
# ---------------------------------------------------------------------------


def _request(cookie: str | None, *, headers: dict[str, str] | None = None):
    """Build a Starlette request carrying ``cookie`` as the session cookie."""
    from starlette.requests import Request

    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if cookie is not None:
        raw_headers.append((b"cookie", f"tlm_session={cookie}".encode()))
    return Request(
        {"type": "http", "method": "GET", "path": "/", "headers": raw_headers}
    )


class TestResolveRequestSession:
    def test_resolves_once_and_reuses_the_answer(
        self,
        factory: sessionmaker,  # type: ignore[type-arg]
        admin_user: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import timelapse_manager.security.sessions as mod

        ss = _session_settings()
        with session_scope(factory) as db:
            _row, raw = create_session(
                db, db.merge(admin_user), remember_me=True, settings=ss
            )
        calls: list[int] = []
        real = mod.get_session_row
        monkeypatch.setattr(
            mod, "get_session_row", lambda *a, **k: calls.append(1) or real(*a, **k)
        )
        request = _request(raw)
        with session_scope(factory) as db:
            first = mod.resolve_request_session(request, db, settings=ss)
        with session_scope(factory) as other_db:
            second = mod.resolve_request_session(request, other_db, settings=ss)
        assert first is not None and second is first
        assert (first.user_id, first.role, first.auth_source, first.persistent) == (
            admin_user.id,
            "admin",
            "local",
            True,
        )
        assert first.csrf_secret
        assert len(calls) == 1

    def test_no_session_is_cached_too(
        self,
        factory: sessionmaker,  # type: ignore[type-arg]
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import timelapse_manager.security.sessions as mod

        ss = _session_settings()
        calls: list[int] = []
        real = mod.get_session_row
        monkeypatch.setattr(
            mod, "get_session_row", lambda *a, **k: calls.append(1) or real(*a, **k)
        )
        request = _request("bogus")
        for _ in range(3):
            with session_scope(factory) as db:
                assert mod.resolve_request_session(request, db, settings=ss) is None
        assert len(calls) == 1
        with session_scope(factory) as db:  # no cookie at all: nothing to look up
            assert mod.resolve_request_session(_request(None), db, settings=ss) is None
        assert len(calls) == 1

    def test_invalidate_forces_a_fresh_lookup(
        self,
        factory: sessionmaker,  # type: ignore[type-arg]
        admin_user: User,
    ) -> None:
        import timelapse_manager.security.sessions as mod

        ss = _session_settings()
        with session_scope(factory) as db:
            _row, raw = create_session(
                db, db.merge(admin_user), remember_me=False, settings=ss
            )
        request = _request(raw)
        with session_scope(factory) as db:
            assert mod.resolve_request_session(request, db, settings=ss) is not None
            revoke_session(db, raw)
        with session_scope(factory) as db:
            # Still the cached answer: the layer that revoked must say so.
            assert mod.resolve_request_session(request, db, settings=ss) is not None
            mod.invalidate_request_session(request)
            assert mod.resolve_request_session(request, db, settings=ss) is None
        mod.invalidate_request_session(request)  # idempotent

    def test_a_passive_request_does_not_touch(
        self,
        factory: sessionmaker,  # type: ignore[type-arg]
        admin_user: User,
    ) -> None:
        import timelapse_manager.security.sessions as mod

        ss = _session_settings()
        with session_scope(factory) as db:
            row, raw = create_session(
                db, db.merge(admin_user), remember_me=False, settings=ss
            )
            stale = mod._utcnow() - datetime.timedelta(minutes=5)
            row.last_active = stale
            row_id = row.id
        request = _request(raw, headers={"X-TLM-Passive": "1"})
        with session_scope(factory) as db:
            assert mod.resolve_request_session(request, db, settings=ss) is not None
        with session_scope(factory) as db:
            assert db.get(SessionRow, row_id).last_active == stale  # type: ignore[union-attr]
