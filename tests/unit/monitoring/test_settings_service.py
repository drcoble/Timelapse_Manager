"""Unit tests for the notification settings CRUD service.

Covers: load masks password; update with blank/MASK_SENTINEL keeps secret;
new value overwrites; webhook URLs and routing_rules round-trip.
"""

from __future__ import annotations

import pytest

from timelapse_manager.db.session import session_scope
from timelapse_manager.monitoring.settings_service import (
    MASK_SENTINEL,
    NotificationSettingsError,
    NotificationSettingsUpdate,
    load_settings,
    resolve_smtp_password,
    update_settings,
)


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


def _store_with_password(factory, password: str) -> None:
    """Write a settings row with a known SMTP password."""
    with session_scope(factory) as session:
        update_settings(session, _base_update(smtp_password=password))


class TestLoadSettingsMasksPassword:
    def test_stored_password_masked_to_sentinel_on_load(self, migrated_factory) -> None:
        """load_settings returns MASK_SENTINEL when a password is stored."""
        _store_with_password(migrated_factory, "real-password-here")
        with session_scope(migrated_factory) as session:
            view = load_settings(session)
        assert view.smtp_password == MASK_SENTINEL

    def test_smtp_password_set_true_when_password_exists(
        self, migrated_factory
    ) -> None:
        _store_with_password(migrated_factory, "real-password-here")
        with session_scope(migrated_factory) as session:
            view = load_settings(session)
        assert view.smtp_password_set is True

    def test_no_password_stored_shows_empty_string(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(smtp_password=None))
        with session_scope(migrated_factory) as session:
            view = load_settings(session)
        assert view.smtp_password == ""
        assert view.smtp_password_set is False

    def test_load_with_no_row_returns_empty_defaults(self, migrated_factory) -> None:
        """A fresh database has no settings row; load returns all defaults."""
        with session_scope(migrated_factory) as session:
            view = load_settings(session)
        assert view.smtp_password == ""
        assert view.smtp_server == ""
        assert view.smtp_password_set is False


class TestUpdateSettingsPasswordKeepRule:
    def test_update_with_blank_password_keeps_stored_secret(
        self, migrated_factory
    ) -> None:
        """Submitting an empty string leaves the stored password intact."""
        _store_with_password(migrated_factory, "do-not-overwrite")
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(smtp_password=""))
        with session_scope(migrated_factory) as session:
            view = load_settings(session)
        assert view.smtp_password == MASK_SENTINEL
        assert view.smtp_password_set is True

    def test_update_with_sentinel_keeps_stored_secret(self, migrated_factory) -> None:
        """Submitting the mask sentinel leaves the stored password intact."""
        _store_with_password(migrated_factory, "do-not-overwrite")
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(smtp_password=MASK_SENTINEL))
        with session_scope(migrated_factory) as session:
            view = load_settings(session)
        assert view.smtp_password_set is True

    def test_update_with_none_password_keeps_stored_secret(
        self, migrated_factory
    ) -> None:
        """Submitting None for the password leaves the stored password intact."""
        _store_with_password(migrated_factory, "do-not-overwrite")
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(smtp_password=None))
        with session_scope(migrated_factory) as session:
            view = load_settings(session)
        assert view.smtp_password_set is True

    def test_update_with_new_value_overwrites_stored_password(
        self, migrated_factory
    ) -> None:
        """A genuinely new password value is stored (replacing the old secret)."""
        _store_with_password(migrated_factory, "old-password")
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(smtp_password="brand-new-password"))
        with session_scope(migrated_factory) as session:
            view = load_settings(session)
        # The new password is stored (masked) but was genuinely changed.
        assert view.smtp_password_set is True
        # Internal verification: read raw from DB and decrypt to confirm new value.
        from timelapse_manager.db.models import NotificationSettings
        from timelapse_manager.security.crypto import decrypt_secret

        with session_scope(migrated_factory) as session:
            row = session.get(NotificationSettings, 1)
        assert row is not None
        assert decrypt_secret(row.smtp_password) == "brand-new-password"


class TestWebhookUrlsAndRulesRoundTrip:
    def test_webhook_urls_round_trip(self, migrated_factory) -> None:
        urls = ["https://hooks.example.com/a", "https://hooks.example.com/b"]
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(webhook_urls=urls))
        with session_scope(migrated_factory) as session:
            view = load_settings(session)
        assert view.webhook_urls == urls

    def test_routing_rules_round_trip(self, migrated_factory) -> None:
        rules = [
            {
                "event_types": ["capture.gap"],
                "min_level": "warning",
                "channels": ["email"],
            },
            {"event_types": ["all"], "min_level": "error", "channels": ["webhook"]},
        ]
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(routing_rules=rules))
        with session_scope(migrated_factory) as session:
            view = load_settings(session)
        assert view.routing_rules == rules

    def test_enabled_channels_round_trip(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as session:
            update_settings(
                session, _base_update(enabled_channels=["email", "webhook"])
            )
        with session_scope(migrated_factory) as session:
            view = load_settings(session)
        assert set(view.enabled_channels) == {"email", "webhook"}

    def test_smtp_recipients_round_trip(self, migrated_factory) -> None:
        recipients = ["alice@example.com", "bob@example.com"]
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(smtp_recipients=recipients))
        with session_scope(migrated_factory) as session:
            view = load_settings(session)
        assert view.smtp_recipients == recipients

    def test_smtp_server_and_port_round_trip(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as session:
            update_settings(
                session,
                _base_update(smtp_server="smtp.custom.com", smtp_port=465),
            )
        with session_scope(migrated_factory) as session:
            view = load_settings(session)
        assert view.smtp_server == "smtp.custom.com"
        assert view.smtp_port == 465


class TestSmtpRetargetClearsStoredSecret:
    """Repointing the SMTP target must not carry the old password along (#67 S9)."""

    @staticmethod
    def _stored(factory) -> str | None:
        with session_scope(factory) as session:
            return resolve_smtp_password(session)

    @pytest.mark.parametrize(
        "change",
        [
            {"smtp_server": "other.example.com"},
            {"smtp_port": 25},
            {"smtp_security": "tls"},
            {"smtp_username": "someone-else"},
            {"smtp_username": None},
        ],
    )
    @pytest.mark.parametrize("submitted", [None, "", MASK_SENTINEL])
    def test_retarget_with_blank_or_masked_password_clears_it(
        self, migrated_factory, change: dict, submitted: str | None
    ) -> None:
        _store_with_password(migrated_factory, "old-server-password")
        with session_scope(migrated_factory) as session:
            view = update_settings(
                session, _base_update(smtp_password=submitted, **change)
            )
        assert view.smtp_password_set is False
        assert view.smtp_password == ""
        assert self._stored(migrated_factory) is None

    def test_retarget_with_a_new_password_stores_it(self, migrated_factory) -> None:
        _store_with_password(migrated_factory, "old-server-password")
        with session_scope(migrated_factory) as session:
            update_settings(
                session,
                _base_update(
                    smtp_server="other.example.com", smtp_password="new-one-1234"
                ),
            )
        assert self._stored(migrated_factory) == "new-one-1234"

    @pytest.mark.parametrize("submitted", [None, "", MASK_SENTINEL])
    def test_unchanged_target_keeps_the_secret(
        self, migrated_factory, submitted: str | None
    ) -> None:
        _store_with_password(migrated_factory, "kept-password")
        with session_scope(migrated_factory) as session:
            update_settings(
                session,
                _base_update(
                    smtp_password=submitted,
                    smtp_from_address="other-from@example.com",
                    smtp_recipients=["a@example.com", "b@example.com"],
                ),
            )
        assert self._stored(migrated_factory) == "kept-password"

    def test_the_update_repr_never_shows_the_password(self) -> None:
        assert "hunter2-secret" not in repr(
            _base_update(smtp_password="hunter2-secret")
        )


class TestUnknownSecurityModeIsRejected:
    """A typo must never become plaintext SMTP (#214)."""

    @pytest.mark.parametrize("mode", ["", "TLS", "ssl", "starttls ", "off", "None"])
    def test_unknown_mode_raises_and_writes_nothing(
        self, migrated_factory, mode: str
    ) -> None:
        _store_with_password(migrated_factory, "kept-password")
        with (
            pytest.raises(NotificationSettingsError, match="security mode"),
            session_scope(migrated_factory) as session,
        ):
            update_settings(
                session,
                _base_update(smtp_security=mode, smtp_server="other.example.com"),
            )
        with session_scope(migrated_factory) as session:
            view = load_settings(session)
            assert resolve_smtp_password(session) == "kept-password"
        assert view.smtp_security == "starttls"
        assert view.smtp_server == "mail.example.com"

    @pytest.mark.parametrize("mode", ["none", "tls", "starttls"])
    def test_known_modes_are_stored_as_submitted(
        self, migrated_factory, mode: str
    ) -> None:
        with session_scope(migrated_factory) as session:
            view = update_settings(session, _base_update(smtp_security=mode))
        assert view.smtp_security == mode
