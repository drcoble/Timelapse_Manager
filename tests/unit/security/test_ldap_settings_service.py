"""Unit tests for the LDAP settings CRUD service.

Mirrors the notification settings-service tests: bind password is masked on read,
the masked write-back rule keeps the stored secret on blank/sentinel submissions,
a genuinely new value is encrypted at rest (``enc:v1:`` prefix), and the
decrypt-at-use resolver returns plaintext for the connector. The autouse
``_crypto_key_provider`` fixture supplies a working encryption key.
"""

from __future__ import annotations

import pytest

from timelapse_manager.db.models import LdapSettings
from timelapse_manager.db.session import session_scope
from timelapse_manager.security.crypto import is_encrypted
from timelapse_manager.security.ldap_settings_service import (
    MASK_SENTINEL,
    LdapSettingsError,
    LdapSettingsUpdate,
    LdapSettingsView,
    load_settings,
    resolve_bind_password,
    update_settings,
)

_ROW_ID = 1


def _base_update(**overrides: object) -> LdapSettingsUpdate:
    defaults: dict[str, object] = {
        "enabled": True,
        "server_urls": ["ldap://dir.example.com"],
        "tls_mode": "starttls",
        "tls_ca_cert_path": None,
        "bind_dn": "cn=svc,dc=example,dc=com",
        "bind_password": None,
        "search_base": "ou=people,dc=example,dc=com",
        "search_filter": "(objectClass=inetOrgPerson)",
        "group_search_base": None,
        "username_attribute": "uid",
        "display_name_attribute": "cn",
        "membership_mode": "memberof",
        "nested_groups": False,
        "admin_group_dn": "cn=admins,ou=groups,dc=example,dc=com",
        "admin_group_filter": None,
        "operator_group_dn": None,
        "operator_group_filter": None,
        "viewer_group_dn": None,
        "viewer_group_filter": None,
    }
    defaults.update(overrides)
    return LdapSettingsUpdate(**defaults)  # type: ignore[arg-type]


def _store_with_password(factory, password: str) -> None:
    with session_scope(factory) as session:
        update_settings(session, _base_update(bind_password=password))


class TestLoadMasksBindPassword:
    def test_stored_password_masked_on_load(self, migrated_factory) -> None:
        _store_with_password(migrated_factory, "real-bind-secret")
        with session_scope(migrated_factory) as session:
            view = load_settings(session)
        assert view.bind_password == MASK_SENTINEL
        assert view.bind_password_set is True

    def test_no_password_shows_empty(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(bind_password=None))
        with session_scope(migrated_factory) as session:
            view = load_settings(session)
        assert view.bind_password == ""
        assert view.bind_password_set is False

    def test_no_row_returns_defaults(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as session:
            view = load_settings(session)
        assert view.enabled is False
        assert view.bind_password == ""
        assert view.membership_mode == "memberof"


class TestEncryptedAtRest:
    def test_stored_password_is_encrypted(self, migrated_factory) -> None:
        _store_with_password(migrated_factory, "real-bind-secret")
        with session_scope(migrated_factory) as session:
            row = session.get(LdapSettings, _ROW_ID)
            assert row is not None
            assert row.bind_password is not None
            assert is_encrypted(row.bind_password)
            assert row.bind_password.startswith("enc:v1:")
            # The plaintext must never be the stored value.
            assert "real-bind-secret" not in row.bind_password

    def test_resolve_returns_plaintext(self, migrated_factory) -> None:
        _store_with_password(migrated_factory, "real-bind-secret")
        with session_scope(migrated_factory) as session:
            assert resolve_bind_password(session) == "real-bind-secret"

    def test_resolve_none_when_no_password(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(bind_password=None))
        with session_scope(migrated_factory) as session:
            assert resolve_bind_password(session) is None


class TestMaskedWriteBackRule:
    def test_sentinel_keeps_stored_secret(self, migrated_factory) -> None:
        _store_with_password(migrated_factory, "original-secret")
        # An admin re-saves the form without retyping the password (sees "***").
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(bind_password=MASK_SENTINEL))
        with session_scope(migrated_factory) as session:
            assert resolve_bind_password(session) == "original-secret"

    def test_blank_keeps_stored_secret(self, migrated_factory) -> None:
        _store_with_password(migrated_factory, "original-secret")
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(bind_password=""))
        with session_scope(migrated_factory) as session:
            assert resolve_bind_password(session) == "original-secret"

    def test_new_value_overwrites_and_is_not_double_wrapped(
        self, migrated_factory
    ) -> None:
        _store_with_password(migrated_factory, "original-secret")
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(bind_password="rotated-secret"))
        with session_scope(migrated_factory) as session:
            row = session.get(LdapSettings, _ROW_ID)
            assert row is not None and row.bind_password is not None
            # Exactly one encryption layer: stripping the prefix once must decrypt.
            assert row.bind_password.count("enc:v1:") == 1
            assert resolve_bind_password(session) == "rotated-secret"


class TestEnumFallbacks:
    def test_invalid_tls_mode_is_rejected_not_coerced(self, migrated_factory) -> None:
        """An unknown mode must not silently become plaintext (#67 S9, R17)."""
        _store_with_password(migrated_factory, "kept-secret")
        with (
            pytest.raises(LdapSettingsError),
            session_scope(migrated_factory) as session,
        ):
            update_settings(
                session, _base_update(tls_mode="bogus", bind_password="x-new")
            )
        with session_scope(migrated_factory) as session:
            view = load_settings(session)
            # Nothing was written: the previous mode and secret are intact.
            assert view.tls_mode == "starttls"
            assert resolve_bind_password(session) == "kept-secret"

    def test_rejection_message_does_not_echo_the_submission(
        self, migrated_factory
    ) -> None:
        update = _base_update(tls_mode="s3cret-looking-mode", bind_password="pw-9876")
        with (
            pytest.raises(LdapSettingsError) as caught,
            session_scope(migrated_factory) as session,
        ):
            update_settings(session, update)
        assert "s3cret-looking-mode" not in str(caught.value)
        assert "pw-9876" not in str(caught.value)
        assert "pw-9876" not in repr(update)  # the secret is out of the repr

    def test_new_configuration_defaults_to_ldaps(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as session:
            assert load_settings(session).tls_mode == "ldaps"  # no row yet
        assert LdapSettingsView().tls_mode == "ldaps"

    def test_invalid_membership_mode_falls_back_to_memberof(
        self, migrated_factory
    ) -> None:
        with session_scope(migrated_factory) as session:
            view = update_settings(session, _base_update(membership_mode="bogus"))
        assert view.membership_mode == "memberof"

    def test_roundtrip_preserves_config_fields(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as session:
            update_settings(
                session,
                _base_update(
                    membership_mode="group_search",
                    nested_groups=True,
                    group_search_base="ou=groups,dc=example,dc=com",
                    server_urls=["ldap://a.example.com", "ldap://b.example.com"],
                ),
            )
        with session_scope(migrated_factory) as session:
            view = load_settings(session)
        assert view.membership_mode == "group_search"
        assert view.nested_groups is True
        assert view.group_search_base == "ou=groups,dc=example,dc=com"
        assert view.server_urls == ["ldap://a.example.com", "ldap://b.example.com"]


class TestTlsCaCertPath:
    """The CA-cert trust-anchor path is non-secret plain config.

    Unlike the bind password it is stored verbatim, read back unmasked, and never
    encrypted. An empty string normalises to ``None``.
    """

    def test_persists_and_reads_back_verbatim(self, migrated_factory) -> None:
        path = "/etc/ssl/certs/internal-ca.pem"
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(tls_ca_cert_path=path))
        with session_scope(migrated_factory) as session:
            view = load_settings(session)
        assert view.tls_ca_cert_path == path

    def test_empty_string_normalises_to_none(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as session:
            view = update_settings(session, _base_update(tls_ca_cert_path=""))
        assert view.tls_ca_cert_path is None
        with session_scope(migrated_factory) as session:
            row = session.get(LdapSettings, _ROW_ID)
            assert row is not None
            assert row.tls_ca_cert_path is None

    def test_not_masked_or_encrypted_at_rest(self, migrated_factory) -> None:
        path = "/etc/ssl/certs/internal-ca.pem"
        with session_scope(migrated_factory) as session:
            update_settings(session, _base_update(tls_ca_cert_path=path))
        with session_scope(migrated_factory) as session:
            row = session.get(LdapSettings, _ROW_ID)
            assert row is not None
            # Stored verbatim: no mask sentinel, no enc:v1: ciphertext prefix.
            assert row.tls_ca_cert_path == path
            assert row.tls_ca_cert_path != MASK_SENTINEL
            assert not is_encrypted(row.tls_ca_cert_path)
        # And the display view shows the real path, never a mask.
        with session_scope(migrated_factory) as session:
            view = load_settings(session)
        assert view.tls_ca_cert_path == path


class TestRetargetClearsStoredSecret:
    """Repointing the directory must not carry the old service password along."""

    @pytest.mark.parametrize(
        "change",
        [
            {"server_urls": ["ldap://other.example.com"]},
            {"server_urls": ["ldap://dir.example.com", "ldap://second.example.com"]},
            {"tls_mode": "none"},
            {"bind_dn": "cn=other,dc=example,dc=com"},
            {"bind_dn": None},
            # A different trust anchor can vouch for a different directory (#215).
            {"tls_ca_cert_path": "/etc/ssl/certs/other-ca.pem"},
        ],
    )
    @pytest.mark.parametrize("submitted", [None, "", MASK_SENTINEL])
    def test_retarget_with_blank_or_masked_password_clears_it(
        self, migrated_factory, change: dict[str, object], submitted: str | None
    ) -> None:
        _store_with_password(migrated_factory, "old-target-secret")
        with session_scope(migrated_factory) as session:
            view = update_settings(
                session, _base_update(bind_password=submitted, **change)
            )
        assert view.bind_password_set is False
        assert view.bind_password == ""
        with session_scope(migrated_factory) as session:
            row = session.get(LdapSettings, _ROW_ID)
            assert row is not None and row.bind_password is None
            assert resolve_bind_password(session) is None

    def test_retarget_with_a_new_password_stores_the_new_one(
        self, migrated_factory
    ) -> None:
        _store_with_password(migrated_factory, "old-target-secret")
        with session_scope(migrated_factory) as session:
            update_settings(
                session,
                _base_update(
                    server_urls=["ldap://other.example.com"],
                    bind_password="new-target-secret",
                ),
            )
        with session_scope(migrated_factory) as session:
            assert resolve_bind_password(session) == "new-target-secret"

    @pytest.mark.parametrize("submitted", [None, "", MASK_SENTINEL])
    def test_unchanged_target_keeps_the_secret(
        self, migrated_factory, submitted: str | None
    ) -> None:
        _store_with_password(migrated_factory, "kept-secret")
        with session_scope(migrated_factory) as session:
            # Everything else may change; the target (urls, tls mode, bind DN)
            # does not.
            update_settings(
                session,
                _base_update(
                    bind_password=submitted,
                    search_base="ou=staff,dc=example,dc=com",
                    admin_group_dn="cn=root,ou=groups,dc=example,dc=com",
                ),
            )
        with session_scope(migrated_factory) as session:
            assert resolve_bind_password(session) == "kept-secret"


class TestCaTrustAnchorRetarget:
    """The CA path is part of the target the stored password was issued for (#215)."""

    @staticmethod
    def _store(factory, ca_path: str | None) -> None:
        with session_scope(factory) as session:
            update_settings(
                session,
                _base_update(
                    bind_password="old-target-secret", tls_ca_cert_path=ca_path
                ),
            )

    @pytest.mark.parametrize(
        ("stored", "submitted"),
        [
            (None, "/etc/ssl/certs/attacker-ca.pem"),
            ("/etc/ssl/certs/internal-ca.pem", "/etc/ssl/certs/other-ca.pem"),
            ("/etc/ssl/certs/internal-ca.pem", None),
            ("/etc/ssl/certs/internal-ca.pem", ""),
        ],
    )
    def test_changing_the_trust_anchor_clears_a_blank_password(
        self, migrated_factory, stored: str | None, submitted: str | None
    ) -> None:
        self._store(migrated_factory, stored)
        with session_scope(migrated_factory) as session:
            view = update_settings(
                session,
                _base_update(bind_password="", tls_ca_cert_path=submitted),
            )
        assert view.bind_password_set is False
        with session_scope(migrated_factory) as session:
            assert resolve_bind_password(session) is None

    @pytest.mark.parametrize("same", [None, "", "/etc/ssl/certs/internal-ca.pem"])
    def test_an_unchanged_trust_anchor_keeps_the_password(
        self, migrated_factory, same: str | None
    ) -> None:
        stored = same or None
        self._store(migrated_factory, stored)
        with session_scope(migrated_factory) as session:
            update_settings(
                session,
                _base_update(bind_password=MASK_SENTINEL, tls_ca_cert_path=same),
            )
        with session_scope(migrated_factory) as session:
            assert resolve_bind_password(session) == "old-target-secret"

    def test_a_new_password_with_a_new_anchor_is_stored(self, migrated_factory) -> None:
        self._store(migrated_factory, None)
        with session_scope(migrated_factory) as session:
            update_settings(
                session,
                _base_update(
                    bind_password="fresh", tls_ca_cert_path="/etc/ssl/certs/new.pem"
                ),
            )
        with session_scope(migrated_factory) as session:
            assert resolve_bind_password(session) == "fresh"
