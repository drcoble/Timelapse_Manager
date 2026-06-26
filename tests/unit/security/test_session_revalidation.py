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

from timelapse_manager.config.settings import SessionSettings
from timelapse_manager.db.models import Session as SessionRow
from timelapse_manager.db.models import User
from timelapse_manager.db.session import session_scope
from timelapse_manager.security.ldap_directory import (
    LdapDirectoryState,
    LdapOutcome,
)
from timelapse_manager.security.ldap_settings_service import (
    LdapSettingsUpdate,
    update_settings,
)
from timelapse_manager.security.session_revalidation import revalidate_ldap_session

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
