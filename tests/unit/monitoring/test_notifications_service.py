"""Unit tests for the notification-settings write service.

Covers the contract the thin web adapter relies on: the save returns a Result with
no post-commit effects, writes a system-scoped audit event attributed to the actor
(without materialising a sentinel user), and applies the webhook signing secret's
masked write-back exactly as the SMTP password's (a new value encrypts at rest; a
blank / sentinel submission keeps the stored ciphertext).
"""

from __future__ import annotations

from sqlalchemy import select

from timelapse_manager.db.models import Event, NotificationSettings, User
from timelapse_manager.db.session import session_scope
from timelapse_manager.security.crypto import decrypt_secret, is_encrypted
from timelapse_manager.services._base import Actor
from timelapse_manager.services.notifications import (
    NotificationSettingsSpec,
    update_notification_settings,
)


def _base_spec(**overrides: object) -> NotificationSettingsSpec:
    defaults: dict = {
        "enabled_channels": ["email"],
        "smtp_server": "mail.example.com",
        "smtp_port": 587,
        "smtp_security": "starttls",
        "smtp_username": "alerts",
        "smtp_password": None,
        "smtp_from_address": "from@example.com",
        "smtp_recipients": ["ops@example.com"],
        "webhook_urls": [],
        "routing_rules": [],
        "webhook_signing_secret": None,
    }
    defaults.update(overrides)
    return NotificationSettingsSpec(**defaults)


def _make_admin(session) -> User:  # type: ignore[no-untyped-def]
    """Create and flush a real admin user to satisfy the audit event's FK."""
    user = User(username="admin", role="admin", auth_source="local")
    session.add(user)
    session.flush()
    return user


class TestUpdateNotificationSettings:
    def test_returns_no_post_commit_effects(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        with session_scope(migrated_factory) as session:
            actor = Actor.of(_make_admin(session))
            result = update_notification_settings(session, actor, _base_spec())
            assert result.effects == ()

    def test_writes_system_scoped_audit_event_for_actor(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        with session_scope(migrated_factory) as session:
            actor = Actor.of(_make_admin(session))
            update_notification_settings(session, actor, _base_spec())

        with session_scope(migrated_factory) as session:
            event = session.execute(
                select(Event).where(Event.message == "notification settings updated")
            ).scalar_one()
            assert event.scope == "system"
            assert event.scope_id is None
            assert event.actor_user_id == actor.user_id

    def test_does_not_materialise_a_sentinel_user(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        """Web-only domain: the save must not create the API sentinel admin row."""
        with session_scope(migrated_factory) as session:
            actor = Actor.of(_make_admin(session))
            update_notification_settings(session, actor, _base_spec())

        with session_scope(migrated_factory) as session:
            usernames = set(session.execute(select(User.username)).scalars().all())
        assert usernames == {"admin"}

    def test_new_signing_secret_stored_encrypted(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        with session_scope(migrated_factory) as session:
            actor = Actor.of(_make_admin(session))
            update_notification_settings(
                session, actor, _base_spec(webhook_signing_secret="hmac-key")
            )

        with session_scope(migrated_factory) as session:
            row = session.get(NotificationSettings, 1)
        assert row is not None
        assert row.webhook_signing_secret is not None
        assert is_encrypted(row.webhook_signing_secret)
        assert decrypt_secret(row.webhook_signing_secret) == "hmac-key"

    def test_sentinel_signing_secret_keeps_stored_value(self, migrated_factory) -> None:  # type: ignore[no-untyped-def]
        with session_scope(migrated_factory) as session:
            actor = Actor.of(_make_admin(session))
            update_notification_settings(
                session, actor, _base_spec(webhook_signing_secret="keep-me")
            )
            # A sentinel re-submission must leave the stored ciphertext untouched.
            update_notification_settings(
                session, actor, _base_spec(webhook_signing_secret="***")
            )

        with session_scope(migrated_factory) as session:
            row = session.get(NotificationSettings, 1)
        assert row is not None
        assert decrypt_secret(row.webhook_signing_secret) == "keep-me"
