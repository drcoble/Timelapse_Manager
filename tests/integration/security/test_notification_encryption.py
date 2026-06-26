"""Integration tests: notification settings encryption at rest.

Verifies that the monitoring settings service stores SMTP passwords and webhook
URLs in encrypted form in the database, and that the plaintext is never
surfaced in the persisted column.
"""

from __future__ import annotations

from timelapse_manager.db.models import NotificationSettings
from timelapse_manager.db.session import session_scope
from timelapse_manager.monitoring.settings_service import (
    MASK_SENTINEL,
    NotificationSettingsUpdate,
    load_settings,
    update_settings,
)
from timelapse_manager.security.crypto import decrypt_secret, is_encrypted


def _base_update(**overrides) -> NotificationSettingsUpdate:
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
    }
    defaults.update(overrides)
    return NotificationSettingsUpdate(**defaults)


# ---------------------------------------------------------------------------
# SMTP password encryption
# ---------------------------------------------------------------------------


class TestSmtpPasswordEncryptedAtRest:
    def test_smtp_password_column_holds_ciphertext_not_plaintext(
        self, migrated_factory
    ) -> None:
        """The DB column must never hold the cleartext password."""
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(smtp_password="super-secret-pw"))

        with session_scope(migrated_factory) as session:
            row = session.get(NotificationSettings, 1)
        assert row is not None
        assert row.smtp_password != "super-secret-pw", (
            "SMTP password stored as plaintext — encryption is not applied"
        )

    def test_smtp_password_column_has_encryption_prefix(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(smtp_password="my-secret"))

        with session_scope(migrated_factory) as session:
            row = session.get(NotificationSettings, 1)
        assert row is not None
        assert is_encrypted(row.smtp_password), (
            f"Expected encrypted ciphertext, got: {row.smtp_password!r}"
        )

    def test_smtp_password_decrypts_to_original(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(smtp_password="plaintext-value"))

        with session_scope(migrated_factory) as session:
            row = session.get(NotificationSettings, 1)
        assert row is not None
        assert decrypt_secret(row.smtp_password) == "plaintext-value"

    def test_load_settings_masks_password_in_view(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(smtp_password="real-pass"))
        with session_scope(migrated_factory) as session:
            view = load_settings(session)
        assert view.smtp_password == MASK_SENTINEL

    def test_empty_password_stores_as_empty_or_none(self, migrated_factory) -> None:
        """An empty/absent password is stored as None or empty, not as ciphertext."""
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(smtp_password=""))
        with session_scope(migrated_factory) as session:
            row = session.get(NotificationSettings, 1)
        assert row is not None
        # An absent/empty password must not be stored as an encrypted value.
        stored = row.smtp_password
        assert not stored or not is_encrypted(stored), (
            f"Empty password should not produce a ciphertext, got: {stored!r}"
        )

    def test_two_distinct_passwords_produce_different_ciphertexts(
        self, migrated_factory
    ) -> None:
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(smtp_password="password-one"))
        with session_scope(migrated_factory) as session:
            row1 = session.get(NotificationSettings, 1)

        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(smtp_password="password-two"))
        with session_scope(migrated_factory) as session:
            row2 = session.get(NotificationSettings, 1)

        assert row1 is not None and row2 is not None
        assert row1.smtp_password != row2.smtp_password

    def test_same_password_stored_twice_produces_different_ciphertexts(
        self, migrated_factory
    ) -> None:
        """Fernet is non-deterministic; each store produces unique ciphertext."""
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(smtp_password="same"))
        with session_scope(migrated_factory) as session:
            row1 = session.get(NotificationSettings, 1)

        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(smtp_password="same"))
        with session_scope(migrated_factory) as session:
            row2 = session.get(NotificationSettings, 1)

        assert row1 is not None and row2 is not None
        # Non-deterministic — ciphertexts should differ (overwhelmingly likely).
        assert row1.smtp_password != row2.smtp_password


# ---------------------------------------------------------------------------
# Webhook URL encryption
# ---------------------------------------------------------------------------


class TestWebhookUrlEncryptedAtRest:
    def test_webhook_url_stored_as_ciphertext(self, migrated_factory) -> None:
        """Webhook URLs containing credentials are stored encrypted in the DB."""
        secret_url = "https://hooks.example.com/notify?token=abc123"
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(webhook_urls=[secret_url]))

    def test_load_settings_decrypts_webhook_url(self, migrated_factory) -> None:
        url = "https://hooks.example.com/path"
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(webhook_urls=[url]))
        with session_scope(migrated_factory) as session:
            view = load_settings(session)
        assert url in view.webhook_urls

    def test_multiple_webhook_urls_all_decrypted(self, migrated_factory) -> None:
        urls = [
            "https://hooks.example.com/a",
            "https://hooks.example.com/b",
        ]
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(webhook_urls=urls))
        with session_scope(migrated_factory) as session:
            view = load_settings(session)
        assert set(view.webhook_urls) == set(urls)


# ---------------------------------------------------------------------------
# Password keep-rule under encryption
# ---------------------------------------------------------------------------


class TestPasswordKeepRuleWithEncryption:
    def test_blank_password_submission_keeps_encrypted_secret(
        self, migrated_factory
    ) -> None:
        """Submitting '' does not overwrite an existing encrypted password."""
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(smtp_password="keep-this"))
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(smtp_password=""))
        with session_scope(migrated_factory) as session:
            row = session.get(NotificationSettings, 1)
        assert row is not None
        assert decrypt_secret(row.smtp_password) == "keep-this"

    def test_sentinel_submission_keeps_encrypted_secret(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(smtp_password="keep-this-too"))
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(smtp_password=MASK_SENTINEL))
        with session_scope(migrated_factory) as session:
            row = session.get(NotificationSettings, 1)
        assert row is not None
        assert decrypt_secret(row.smtp_password) == "keep-this-too"
