"""Unit tests for the default-camera-credentials CRUD service.

Mirrors the LDAP settings-service tests: the password is masked on read, the
masked write-back rule keeps the stored secret on blank/sentinel submissions, a
genuinely new value is encrypted at rest (``enc:v1:`` prefix), and the
decrypt-at-use resolver returns the ``(username, password)`` fallback only when
the row is enabled and a username is present. The autouse ``_crypto_key_provider``
fixture supplies a working encryption key.
"""

from __future__ import annotations

from timelapse_manager.db.models import CameraDefaultCredentials
from timelapse_manager.db.session import session_scope
from timelapse_manager.security.camera_defaults_service import (
    MASK_SENTINEL,
    CameraDefaultsUpdate,
    load_settings,
    resolve_default_credentials,
    update_settings,
)
from timelapse_manager.security.crypto import is_encrypted

_ROW_ID = 1


def _update(**overrides: object) -> CameraDefaultsUpdate:
    defaults: dict[str, object] = {
        "enabled": True,
        "username": "fallback-user",
        "password": None,
    }
    defaults.update(overrides)
    return CameraDefaultsUpdate(**defaults)  # type: ignore[arg-type]


def _store(factory, **overrides: object) -> None:
    with session_scope(factory) as session:
        update_settings(session, _update(**overrides))


class TestLoadMasksPassword:
    def test_stored_password_masked_on_load(self, migrated_factory) -> None:
        _store(migrated_factory, password="real-fallback-secret")
        with session_scope(migrated_factory) as session:
            view = load_settings(session)
        assert view.password == MASK_SENTINEL
        assert view.password_set is True
        assert view.username == "fallback-user"

    def test_no_password_shows_empty(self, migrated_factory) -> None:
        _store(migrated_factory, password=None)
        with session_scope(migrated_factory) as session:
            view = load_settings(session)
        assert view.password == ""
        assert view.password_set is False

    def test_no_row_returns_defaults(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as session:
            view = load_settings(session)
        assert view.enabled is False
        assert view.username == ""
        assert view.password == ""


class TestEncryptedAtRest:
    def test_stored_password_is_encrypted(self, migrated_factory) -> None:
        _store(migrated_factory, password="real-fallback-secret")
        with session_scope(migrated_factory) as session:
            row = session.get(CameraDefaultCredentials, _ROW_ID)
            assert row is not None
            assert row.password is not None
            assert is_encrypted(row.password)
            assert row.password.startswith("enc:v1:")
            assert "real-fallback-secret" not in row.password


class TestMaskedWriteBackRule:
    def test_sentinel_keeps_stored_secret(self, migrated_factory) -> None:
        _store(migrated_factory, password="original-secret")
        with session_scope(migrated_factory) as session:
            update_settings(session, _update(password=MASK_SENTINEL))
        with session_scope(migrated_factory) as session:
            creds = resolve_default_credentials(session)
        assert creds == ("fallback-user", "original-secret")

    def test_blank_keeps_stored_secret(self, migrated_factory) -> None:
        _store(migrated_factory, password="original-secret")
        with session_scope(migrated_factory) as session:
            update_settings(session, _update(password=""))
        with session_scope(migrated_factory) as session:
            creds = resolve_default_credentials(session)
        assert creds == ("fallback-user", "original-secret")

    def test_new_value_overwrites_and_is_not_double_wrapped(
        self, migrated_factory
    ) -> None:
        _store(migrated_factory, password="original-secret")
        with session_scope(migrated_factory) as session:
            update_settings(session, _update(password="rotated-secret"))
        with session_scope(migrated_factory) as session:
            row = session.get(CameraDefaultCredentials, _ROW_ID)
            assert row is not None and row.password is not None
            # Exactly one encryption layer: stripping the prefix once must decrypt.
            assert row.password.count("enc:v1:") == 1
            creds = resolve_default_credentials(session)
        assert creds == ("fallback-user", "rotated-secret")


class TestResolveDefaultCredentials:
    def test_enabled_with_username_returns_pair(self, migrated_factory) -> None:
        _store(migrated_factory, enabled=True, password="secret")
        with session_scope(migrated_factory) as session:
            assert resolve_default_credentials(session) == ("fallback-user", "secret")

    def test_disabled_returns_none(self, migrated_factory) -> None:
        _store(migrated_factory, enabled=False, password="secret")
        with session_scope(migrated_factory) as session:
            assert resolve_default_credentials(session) is None

    def test_unset_username_returns_none(self, migrated_factory) -> None:
        _store(migrated_factory, enabled=True, username=None, password="secret")
        with session_scope(migrated_factory) as session:
            assert resolve_default_credentials(session) is None

    def test_no_row_returns_none(self, migrated_factory) -> None:
        with session_scope(migrated_factory) as session:
            assert resolve_default_credentials(session) is None

    def test_username_only_returns_empty_password(self, migrated_factory) -> None:
        _store(migrated_factory, enabled=True, password=None)
        with session_scope(migrated_factory) as session:
            assert resolve_default_credentials(session) == ("fallback-user", "")
