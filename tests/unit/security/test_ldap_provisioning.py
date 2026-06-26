"""Unit tests for just-in-time provisioning of directory-backed users.

Uses the migrated-DB session factory so the real ``User`` table and its enum
constraints are exercised. Covers: first login creates an ldap user with the
mapped role and no password hash; second login updates the role without
duplicating the row; and a username collision with an existing *local* account is
refused rather than silently taken over.
"""

from __future__ import annotations

import pytest

from timelapse_manager.db.models import User
from timelapse_manager.db.session import session_scope
from timelapse_manager.security.ldap_directory import (
    LdapProvisioningError,
    provision_user,
)


class TestFirstLoginCreatesUser:
    def test_creates_ldap_user_with_mapped_role_and_no_hash(
        self, migrated_factory
    ) -> None:
        with session_scope(migrated_factory) as session:
            user = provision_user(
                session,
                username="alice",
                role="operator",
                display_name="Alice Example",
            )
            assert user.auth_source == "ldap"
            assert user.role == "operator"
            assert user.password_hash is None
            assert user.enabled is True

        with session_scope(migrated_factory) as session:
            rows = session.query(User).filter(User.username == "alice").all()
            assert len(rows) == 1


class TestSecondLoginUpdatesNoDuplicate:
    def test_role_refreshed_and_no_duplicate_row(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as session:
            provision_user(session, username="bob", role="viewer", display_name="Bob")
        # Second login: group membership changed, so the mapped role is now admin.
        with session_scope(migrated_factory) as session:
            user = provision_user(
                session, username="bob", role="admin", display_name="Bob Newname"
            )
            assert user.role == "admin"

        with session_scope(migrated_factory) as session:
            rows = session.query(User).filter(User.username == "bob").all()
            assert len(rows) == 1
            assert rows[0].role == "admin"
            assert rows[0].auth_source == "ldap"
            assert rows[0].password_hash is None


class TestLocalAccountCollisionRefused:
    def test_existing_local_user_is_not_taken_over(self, migrated_factory) -> None:
        # Seed a pre-existing local account with the same username.
        with session_scope(migrated_factory) as session:
            session.add(
                User(
                    username="carol",
                    auth_source="local",
                    password_hash="$argon2id$fake",
                    role="admin",
                    enabled=True,
                )
            )

        with (
            session_scope(migrated_factory) as session,
            pytest.raises(LdapProvisioningError),
        ):
            provision_user(
                session, username="carol", role="viewer", display_name="Carol"
            )

        # The local account is untouched: still local, still has its hash and role.
        with session_scope(migrated_factory) as session:
            row = session.query(User).filter(User.username == "carol").one()
            assert row.auth_source == "local"
            assert row.password_hash == "$argon2id$fake"
            assert row.role == "admin"
