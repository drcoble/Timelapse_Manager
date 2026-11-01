"""Unit tests for periodic re-evaluation of directory-backed sessions.

These exercise :func:`revalidate_ldap_session` directly with an injected
directory resolver and an injected clock, so no live directory and no real
elapsed time are involved. The settings row (which holds the role group DNs) is
seeded via the real settings service so role mapping during re-evaluation runs
against realistic data; the directory's *current* state for a user is supplied by
the injected resolver.

Behaviours asserted:
* a local session is never re-evaluated (no resolver call, always kept);
* an LDAP session within the interval is not re-evaluated;
* role change in the directory updates the stored role after the interval;
* the account being removed revokes the session on the next due request;
* the account mapping to no role revokes the session;
* an unreachable directory at re-eval is fail-safe (session kept, timestamp not
  advanced) so a transient outage never locks a user out;
* a long-lived "remember me" session is re-evaluated just like any other.
"""

from __future__ import annotations

import datetime
import threading

import pytest

from timelapse_manager.config.settings import SessionSettings
from timelapse_manager.db.models import Session as SessionRow
from timelapse_manager.db.models import User
from timelapse_manager.db.session import session_scope
from timelapse_manager.security.ldap_directory import (
    DETAIL_NO_SERVER_REACHABLE,
    LdapDirectoryState,
    LdapOutcome,
)
from timelapse_manager.security.ldap_settings_service import (
    LdapSettingsUpdate,
    update_settings,
)
from timelapse_manager.security.session_revalidation import (
    DirectoryBreaker,
    breaker_for,
    breaker_window_seconds,
    record_directory_result,
    revalidate_ldap_session,
)

# Placeholder directory tree; never a real domain.
_ADMIN_GROUP = "cn=admins,ou=groups,dc=example,dc=com"
_VIEWER_GROUP = "cn=viewers,ou=groups,dc=example,dc=com"

_T0 = datetime.datetime(2026, 1, 1, 12, 0, 0)
_INTERVAL = 900  # 15 minutes


def _settings(interval: int = _INTERVAL) -> SessionSettings:
    return SessionSettings(ldap_revalidation_interval_seconds=interval)


def _seed_settings(factory) -> None:
    """Seed an enabled LDAP settings row with admin + viewer group DNs."""
    with session_scope(factory) as session:
        update_settings(
            session,
            LdapSettingsUpdate(
                enabled=True,
                server_urls=["ldap://dir.example.com"],
                tls_mode="none",
                tls_ca_cert_path=None,
                bind_dn="cn=svc,dc=example,dc=com",
                bind_password=None,
                search_base="ou=people,dc=example,dc=com",
                search_filter="(objectClass=inetOrgPerson)",
                group_search_base=None,
                username_attribute="uid",
                display_name_attribute="cn",
                membership_mode="memberof",
                nested_groups=False,
                admin_group_dn=_ADMIN_GROUP,
                admin_group_filter=None,
                operator_group_dn=None,
                operator_group_filter=None,
                viewer_group_dn=_VIEWER_GROUP,
                viewer_group_filter=None,
            ),
        )


def _seed_user(session, *, auth_source: str, role: str) -> User:
    user = User(
        username="alice",
        auth_source=auth_source,
        password_hash=None,
        role=role,
        enabled=True,
    )
    session.add(user)
    session.flush()
    return user


def _seed_session(
    session,
    user: User,
    *,
    last_revalidated_at: datetime.datetime | None,
    persistent: bool = False,
) -> SessionRow:
    row = SessionRow(
        user_id=user.id,
        persistent=persistent,
        token_hash=f"hash-{user.id}",
        csrf_secret="csrf",
        created_at=_T0,
        last_active=_T0,
        expires_at=_T0 + datetime.timedelta(days=1),
        last_revalidated_at=last_revalidated_at,
        revoked=False,
    )
    session.add(row)
    session.flush()
    return row


def _state(outcome: LdapOutcome, groups: frozenset[str] = frozenset()):
    return LdapDirectoryState(outcome=outcome, groups=groups)


class TestLocalSessionsNeverReevaluated:
    def test_local_session_is_kept_without_calling_resolver(
        self, migrated_factory
    ) -> None:
        calls: list[str] = []

        def resolver(username: str):
            calls.append(username)
            return _state(LdapOutcome.NO_SUCH_USER)

        with session_scope(migrated_factory) as session:
            user = _seed_user(session, auth_source="local", role="admin")
            row = _seed_session(session, user, last_revalidated_at=None)
            kept = revalidate_ldap_session(
                session,
                row,
                user,
                settings=_settings(),
                resolver=resolver,
                now=lambda: _T0 + datetime.timedelta(hours=1),
            )
        assert kept is True
        assert calls == []  # a local session never touches the directory


class TestIntervalGate:
    def test_ldap_session_within_interval_is_not_reevaluated(
        self, migrated_factory
    ) -> None:
        _seed_settings(migrated_factory)
        calls: list[str] = []

        def resolver(username: str):
            calls.append(username)
            return _state(LdapOutcome.AUTHENTICATED, frozenset({_ADMIN_GROUP}))

        with session_scope(migrated_factory) as session:
            user = _seed_user(session, auth_source="ldap", role="admin")
            row = _seed_session(session, user, last_revalidated_at=_T0)
            # Only 5 minutes elapsed; the 15-minute interval has not passed.
            kept = revalidate_ldap_session(
                session,
                row,
                user,
                settings=_settings(),
                resolver=resolver,
                now=lambda: _T0 + datetime.timedelta(minutes=5),
            )
        assert kept is True
        assert calls == []  # not yet due


class TestRoleChange:
    def test_role_updates_after_interval(self, migrated_factory) -> None:
        _seed_settings(migrated_factory)

        # Directory now reports the user only in the viewer group.
        def resolver(_username: str):
            return _state(LdapOutcome.AUTHENTICATED, frozenset({_VIEWER_GROUP}))

        with session_scope(migrated_factory) as session:
            user = _seed_user(session, auth_source="ldap", role="admin")
            row = _seed_session(session, user, last_revalidated_at=_T0)
            now = _T0 + datetime.timedelta(minutes=20)
            kept = revalidate_ldap_session(
                session,
                row,
                user,
                settings=_settings(),
                resolver=resolver,
                now=lambda: now,
            )
            assert kept is True
            assert user.role == "viewer"  # demoted from admin
            assert row.last_revalidated_at == now  # timestamp advanced


class TestAccountRemoved:
    def test_removed_user_revokes_session(self, migrated_factory) -> None:
        _seed_settings(migrated_factory)

        def resolver(_username: str):
            return _state(LdapOutcome.NO_SUCH_USER)

        with session_scope(migrated_factory) as session:
            user = _seed_user(session, auth_source="ldap", role="admin")
            row = _seed_session(session, user, last_revalidated_at=_T0)
            kept = revalidate_ldap_session(
                session,
                row,
                user,
                settings=_settings(),
                resolver=resolver,
                now=lambda: _T0 + datetime.timedelta(minutes=20),
            )
            assert kept is False
            assert row.revoked is True


class TestNoMappedGroup:
    def test_user_in_no_mapped_group_revokes_session(self, migrated_factory) -> None:
        _seed_settings(migrated_factory)

        # User is present but in some unrelated group only.
        def resolver(_username: str):
            return _state(
                LdapOutcome.AUTHENTICATED,
                frozenset({"cn=other,ou=groups,dc=example,dc=com"}),
            )

        with session_scope(migrated_factory) as session:
            user = _seed_user(session, auth_source="ldap", role="admin")
            row = _seed_session(session, user, last_revalidated_at=_T0)
            kept = revalidate_ldap_session(
                session,
                row,
                user,
                settings=_settings(),
                resolver=resolver,
                now=lambda: _T0 + datetime.timedelta(minutes=20),
            )
            assert kept is False
            assert row.revoked is True


class TestUnreachableFailSafe:
    def test_unreachable_directory_keeps_session_and_does_not_advance(
        self, migrated_factory
    ) -> None:
        _seed_settings(migrated_factory)

        def resolver(_username: str):
            return _state(LdapOutcome.SERVER_UNREACHABLE)

        with session_scope(migrated_factory) as session:
            user = _seed_user(session, auth_source="ldap", role="admin")
            row = _seed_session(session, user, last_revalidated_at=_T0)
            kept = revalidate_ldap_session(
                session,
                row,
                user,
                settings=_settings(),
                resolver=resolver,
                now=lambda: _T0 + datetime.timedelta(minutes=20),
            )
            # Fail safe: a transient outage must not lock the user out.
            assert kept is True
            assert row.revoked is False
            assert user.role == "admin"  # unchanged
            # Timestamp NOT advanced, so the next request retries promptly.
            assert row.last_revalidated_at == _T0


class TestRememberMeReevaluated:
    def test_persistent_session_is_reevaluated(self, migrated_factory) -> None:
        _seed_settings(migrated_factory)

        def resolver(_username: str):
            return _state(LdapOutcome.NO_SUCH_USER)

        with session_scope(migrated_factory) as session:
            user = _seed_user(session, auth_source="ldap", role="admin")
            # A long-lived "remember me" session.
            row = _seed_session(session, user, last_revalidated_at=_T0, persistent=True)
            kept = revalidate_ldap_session(
                session,
                row,
                user,
                settings=_settings(),
                resolver=resolver,
                now=lambda: _T0 + datetime.timedelta(minutes=20),
            )
            # A deprovisioned user loses a persistent session at the next re-eval.
            assert kept is False
            assert row.revoked is True


# ---------------------------------------------------------------------------
# Circuit breaker (#67 R3)
# ---------------------------------------------------------------------------


class _Clock:
    """A settable monotonic clock for the breaker."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _down() -> LdapDirectoryState:
    """What the connector returns when no server accepted a connection."""
    return LdapDirectoryState(
        LdapOutcome.SERVER_UNREACHABLE, detail=DETAIL_NO_SERVER_REACHABLE
    )


class _CountingResolver:
    def __init__(self, *answers: LdapDirectoryState) -> None:
        self.answers = list(answers)
        self.calls = 0

    def __call__(self, _username: str) -> LdapDirectoryState:
        self.calls += 1
        return self.answers[min(self.calls, len(self.answers)) - 1]


def _revalidate(factory, resolver, breaker, *, minutes: int = 20):
    """Run one due re-check in its own transaction; return (kept, last_revalidated)."""
    with session_scope(factory) as session:
        user = session.query(User).one()
        row = session.query(SessionRow).one()
        kept = revalidate_ldap_session(
            session,
            row,
            user,
            settings=_settings(),
            resolver=resolver,
            now=lambda: _T0 + datetime.timedelta(minutes=minutes),
            breaker=breaker,
        )
        return kept, row.last_revalidated_at


class TestDirectoryBreaker:
    def test_unreachable_directory_is_asked_once_per_window(
        self, migrated_factory
    ) -> None:
        _seed_settings(migrated_factory)
        with session_scope(migrated_factory) as session:
            user = _seed_user(session, auth_source="ldap", role="admin")
            _seed_session(session, user, last_revalidated_at=_T0)
        clock = _Clock()
        breaker = DirectoryBreaker(clock=clock)
        resolver = _CountingResolver(_down())

        for _ in range(5):  # five due requests inside the window
            kept, stamp = _revalidate(migrated_factory, resolver, breaker)
            assert kept is True  # fail safe: the session is kept ...
            assert stamp == _T0  # ... and the check is still due
        assert resolver.calls == 1

        clock.t += breaker_window_seconds() + 1  # the window is over
        _revalidate(migrated_factory, resolver, breaker)
        assert resolver.calls == 2  # asked again

    def test_a_deprovisioned_user_is_revoked_once_the_breaker_closes(
        self, migrated_factory
    ) -> None:
        """An outage defers a revocation; it never cancels it."""
        _seed_settings(migrated_factory)
        with session_scope(migrated_factory) as session:
            user = _seed_user(session, auth_source="ldap", role="admin")
            _seed_session(session, user, last_revalidated_at=_T0)
        clock = _Clock()
        breaker = DirectoryBreaker(clock=clock)
        resolver = _CountingResolver(_down(), _state(LdapOutcome.NO_SUCH_USER))

        assert _revalidate(migrated_factory, resolver, breaker)[0] is True
        assert _revalidate(migrated_factory, resolver, breaker)[0] is True  # open
        clock.t += breaker_window_seconds() + 1
        kept, _ = _revalidate(migrated_factory, resolver, breaker)
        assert kept is False
        with session_scope(migrated_factory) as session:
            assert session.query(SessionRow).one().revoked is True

    def test_an_answer_closes_the_breaker(self, migrated_factory) -> None:
        _seed_settings(migrated_factory)
        with session_scope(migrated_factory) as session:
            user = _seed_user(session, auth_source="ldap", role="admin")
            _seed_session(session, user, last_revalidated_at=_T0)
        clock = _Clock()
        breaker = DirectoryBreaker(clock=clock)
        resolver = _CountingResolver(
            _down(), _state(LdapOutcome.AUTHENTICATED, frozenset({_ADMIN_GROUP}))
        )
        _revalidate(migrated_factory, resolver, breaker)
        clock.t += breaker_window_seconds() + 1
        _revalidate(migrated_factory, resolver, breaker)  # the probe answers
        assert breaker.is_open() is False
        _revalidate(migrated_factory, resolver, breaker, minutes=40)
        assert resolver.calls == 3  # closed: asked without waiting for a window

    @pytest.mark.parametrize(
        "state",
        [
            LdapDirectoryState(
                LdapOutcome.SERVER_UNREACHABLE, detail="directory error"
            ),
            LdapDirectoryState(LdapOutcome.SERVER_UNREACHABLE),
            LdapDirectoryState(LdapOutcome.CONFIG_ERROR, detail="service bind failed"),
        ],
    )
    def test_only_a_truly_unreachable_directory_opens_it(
        self, migrated_factory, state: LdapDirectoryState
    ) -> None:
        """A request-provokable fault must not be able to hold the breaker open.

        An open breaker defers revocations, so anything a login attempt can
        cause (a protocol error, a bad bind) is not allowed to open it.
        """
        _seed_settings(migrated_factory)
        with session_scope(migrated_factory) as session:
            user = _seed_user(session, auth_source="ldap", role="admin")
            _seed_session(session, user, last_revalidated_at=_T0)
        breaker = DirectoryBreaker(clock=_Clock())
        resolver = _CountingResolver(state)
        for _ in range(3):
            assert _revalidate(migrated_factory, resolver, breaker)[0] is True
        assert resolver.calls == 3
        assert breaker.is_open() is False

    def test_after_the_window_exactly_one_caller_probes(self) -> None:
        clock = _Clock()
        breaker = DirectoryBreaker(clock=clock)
        breaker.record_unreachable(60)
        assert breaker.allow(60) is False
        clock.t += 61
        # The first caller wins the probe; nobody else does until it reports.
        assert breaker.allow(60) is True
        assert breaker.allow(60) is False
        assert breaker.allow(60) is False
        breaker.record_unreachable(60)  # the probe failed: re-opened
        assert breaker.allow(60) is False

    def test_concurrent_callers_after_the_window_do_not_stampede(self) -> None:
        clock = _Clock()
        breaker = DirectoryBreaker(clock=clock)
        breaker.record_unreachable(60)
        clock.t += 61
        winners: list[bool] = []
        start = threading.Barrier(16)

        def caller() -> None:
            start.wait()
            winners.append(breaker.allow(60))

        threads = [threading.Thread(target=caller) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert winners.count(True) == 1

    def test_the_breaker_is_per_engine(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as one:
            first = breaker_for(one)
        with session_scope(migrated_factory) as two:
            assert breaker_for(two) is first


def test_breaker_window_default_and_floor() -> None:
    from pydantic import ValidationError

    from timelapse_manager.config.settings import AuthSettings

    assert AuthSettings().ldap_breaker_seconds == 60
    with pytest.raises(ValidationError):
        AuthSettings(ldap_breaker_seconds=0)


class TestLockedAndDisabledRevoke:
    """#67 S10: a lock in the directory, or LDAP switched off, ends the session."""

    def _setup(self, factory, *, auth_source: str = "ldap") -> None:
        _seed_settings(factory)
        with session_scope(factory) as session:
            user = _seed_user(session, auth_source=auth_source, role="admin")
            _seed_session(session, user, last_revalidated_at=_T0)

    @pytest.mark.parametrize("outcome", [LdapOutcome.LOCKED, LdapOutcome.DISABLED])
    def test_revokes_an_ldap_session(
        self, migrated_factory, outcome: LdapOutcome
    ) -> None:
        self._setup(migrated_factory)
        resolver = _CountingResolver(_state(outcome))
        kept, _ = _revalidate(migrated_factory, resolver, DirectoryBreaker())
        assert kept is False
        with session_scope(migrated_factory) as session:
            row = session.query(SessionRow).one()
            assert row.revoked is True
            assert row.revoked_at is not None

    @pytest.mark.parametrize("outcome", [LdapOutcome.LOCKED, LdapOutcome.DISABLED])
    def test_never_touches_a_local_session(
        self, migrated_factory, outcome: LdapOutcome
    ) -> None:
        self._setup(migrated_factory, auth_source="local")
        resolver = _CountingResolver(_state(outcome))
        kept, _ = _revalidate(migrated_factory, resolver, DirectoryBreaker())
        assert kept is True
        assert resolver.calls == 0
        with session_scope(migrated_factory) as session:
            assert session.query(SessionRow).one().revoked is False

    @pytest.mark.parametrize(
        "state",
        [
            LdapDirectoryState(
                LdapOutcome.SERVER_UNREACHABLE, detail=DETAIL_NO_SERVER_REACHABLE
            ),
            LdapDirectoryState(LdapOutcome.CONFIG_ERROR, detail="service bind failed"),
        ],
    )
    def test_an_outage_or_misconfiguration_still_keeps_the_session(
        self, migrated_factory, state: LdapDirectoryState
    ) -> None:
        self._setup(migrated_factory)
        kept, stamp = _revalidate(
            migrated_factory, _CountingResolver(state), DirectoryBreaker()
        )
        assert kept is True
        assert stamp == _T0

    def test_a_locked_answer_closes_the_breaker(self, migrated_factory) -> None:
        self._setup(migrated_factory)
        breaker = DirectoryBreaker(clock=_Clock())
        breaker.record_unreachable(60)
        record_directory_result(breaker, LdapOutcome.LOCKED, "account locked")
        assert breaker.is_open() is False


class TestBreakerReset:
    """#203: an administrator's save / successful test closes an open breaker."""

    def test_reset_closes_an_open_breaker(self) -> None:
        breaker = DirectoryBreaker(clock=_Clock())
        breaker.record_unreachable(60)
        assert breaker.allow(60) is False
        breaker.reset()
        assert breaker.is_open() is False
        assert breaker.allow(60) is True

    def test_reset_on_a_closed_breaker_is_harmless(self) -> None:
        breaker = DirectoryBreaker(clock=_Clock())
        breaker.reset()
        assert breaker.allow(60) is True


class TestStalledDirectoryOpensBreaker:
    """#202: a stall before the service bind opens it; each window still probes."""

    @staticmethod
    def _stalled(*, before_bind: bool, request_specific: bool = False):
        from tests.unit.security.test_ldap_directory import (
            _base_view,
            _make_factory,
            _memberof_entries,
            _stall_factory,
        )
        from timelapse_manager.security.ldap_directory import resolve_directory_state

        if request_specific:
            from tests.unit.security.test_ldap_directory import (
                _request_specific_stall_factory,
            )

            factory = _request_specific_stall_factory(
                _make_factory(_memberof_entries())
            )
        else:
            factory = _stall_factory(
                _make_factory(_memberof_entries()),
                on="service-bind" if before_bind else "search",
            )
        return resolve_directory_state(
            settings=_base_view(),
            username="alice",
            bind_password="service-bind-pw",
            connection_factory=factory,
        )

    def test_a_stall_before_the_bind_opens_it_and_every_window_reprobes(self) -> None:
        clock = _Clock()
        breaker = DirectoryBreaker(clock=clock)
        window = breaker_window_seconds()
        for _ in range(3):  # three windows in a row, the directory still hung
            assert breaker.allow(window) is True  # exactly one probe per window
            state = self._stalled(before_bind=True)
            record_directory_result(breaker, state.outcome, state.detail)
            assert breaker.is_open() is True
            assert breaker.allow(window) is False  # everyone else is refused
            clock.t += window + 1

    def test_a_hang_after_the_bind_opens_it_and_every_window_reprobes(self) -> None:
        """Bind-then-hang (#216): the canary stalls too, so it is not a request."""
        clock = _Clock()
        breaker = DirectoryBreaker(clock=clock)
        window = breaker_window_seconds()
        for _ in range(3):
            assert breaker.allow(window) is True
            state = self._stalled(before_bind=False)
            record_directory_result(breaker, state.outcome, state.detail)
            assert breaker.is_open() is True
            clock.t += window + 1

    def test_a_request_specific_stall_never_opens_it(self) -> None:
        """A crafted slow search the canary does not reproduce cannot hold it."""
        breaker = DirectoryBreaker(clock=_Clock())
        for _ in range(5):
            state = self._stalled(before_bind=False, request_specific=True)
            record_directory_result(breaker, state.outcome, state.detail)
            assert breaker.is_open() is False


class TestSavingLdapOffRevokesDirectorySessions:
    """#216: turning LDAP off ends directory sessions at save time."""

    @staticmethod
    def _save(factory, *, enabled: bool) -> None:
        from tests.unit.security.test_ldap_settings_service import _base_update

        with session_scope(factory) as session:
            update_settings(session, _base_update(enabled=enabled))

    @staticmethod
    def _revoked(factory, row_id: int) -> bool:
        with session_scope(factory) as session:
            row = session.get(SessionRow, row_id)
            assert row is not None
            return row.revoked

    def _seed(self, factory) -> tuple[int, int]:
        with session_scope(factory) as session:
            ldap_user = _seed_user(session, auth_source="ldap", role="admin")
            ldap_row = _seed_session(session, ldap_user, last_revalidated_at=_T0)
            local = User(
                username="local", auth_source="local", role="admin", enabled=True
            )
            session.add(local)
            session.flush()
            local_row = SessionRow(
                user_id=local.id,
                token_hash="hash-local",
                csrf_secret="c",
                created_at=_T0,
                last_active=_T0,
                expires_at=_T0 + datetime.timedelta(days=1),
                revoked=False,
            )
            session.add(local_row)
            session.flush()
            return ldap_row.id, local_row.id

    def test_disabling_revokes_ldap_sessions_only(self, migrated_factory) -> None:
        self._save(migrated_factory, enabled=True)
        ldap_id, local_id = self._seed(migrated_factory)
        self._save(migrated_factory, enabled=True)
        assert not self._revoked(
            migrated_factory, ldap_id
        )  # staying on revokes nothing
        self._save(migrated_factory, enabled=False)
        assert self._revoked(migrated_factory, ldap_id)
        assert not self._revoked(migrated_factory, local_id)

    def test_revoke_is_atomic_with_the_settings_change(self, migrated_factory) -> None:
        self._save(migrated_factory, enabled=True)
        ldap_id, _ = self._seed(migrated_factory)
        from tests.unit.security.test_ldap_settings_service import _base_update

        with (
            pytest.raises(RuntimeError),
            session_scope(migrated_factory) as session,
        ):
            update_settings(session, _base_update(enabled=False))
            raise RuntimeError("caller fails after the save")
        assert not self._revoked(migrated_factory, ldap_id)
