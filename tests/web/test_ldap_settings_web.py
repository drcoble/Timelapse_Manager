"""Web-layer tests for the LDAP / Directory settings panel (admin-only).

Covers:
- Admin-only gate: non-admin roles get 403 on all LDAP endpoints.
- Settings round-trip: fields save and are masked on re-read; blank bind
  password keeps the stored secret unchanged.
- Validation: enabling without required fields returns 200 (full settings page)
  with an inline error message and does not persist a broken config.
- Test-connection: outcome->message mapping; connector is monkeypatched so no
  live server is needed.
- Auth-source badges: the users list renders correct badges for local and LDAP
  users.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import timelapse_manager.web.routers.ldap as routers
from tests.conftest import csrf_of
from timelapse_manager.db.models import LdapSettings, User
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.security.ldap_directory import LdapDirectoryState, LdapOutcome
from timelapse_manager.security.ldap_settings_service import (
    LdapSettingsUpdate,
    update_settings,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ADMIN_PW = "AdminP@ssw0rd1234"


def _seed_ldap(client: TestClient, *, enabled: bool = True) -> None:
    """Insert a minimal valid LDAP row into the running client's database."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        update_settings(
            db,
            LdapSettingsUpdate(
                enabled=enabled,
                server_urls=["ldap://dir.example.com"],
                tls_mode="none",
                tls_ca_cert_path=None,
                bind_dn="cn=svc,dc=example,dc=com",
                bind_password="stored-service-secret",
                search_base="ou=people,dc=example,dc=com",
                search_filter="(objectClass=inetOrgPerson)",
                group_search_base=None,
                username_attribute="uid",
                display_name_attribute="cn",
                membership_mode="memberof",
                nested_groups=False,
                admin_group_dn="cn=admins,ou=groups,dc=example,dc=com",
                admin_group_filter=None,
                operator_group_dn="cn=operators,ou=groups,dc=example,dc=com",
                operator_group_filter=None,
                viewer_group_dn="cn=viewers,ou=groups,dc=example,dc=com",
                viewer_group_filter=None,
            ),
        )


def _stored_bind_password() -> str | None:
    """Return the raw (encrypted) bind password from the LdapSettings row."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        row = db.get(LdapSettings, 1)
        return row.bind_password if row else None


def _stored_enabled() -> bool:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        row = db.get(LdapSettings, 1)
        return bool(row.enabled) if row else False


def _post_ldap(
    client: TestClient,
    data: dict[str, str],
    *,
    follow_redirects: bool = False,
) -> Any:
    csrf = csrf_of(client, "/settings")
    payload = {"csrf_token": csrf, **data}
    return client.post(
        "/settings/ldap",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=follow_redirects,
    )


def _seed_ldap_user(username: str = "alice") -> None:
    """Insert an LDAP-sourced user row directly into the running DB."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        user = User(
            username=username,
            auth_source="ldap",
            password_hash=None,
            role="viewer",
            enabled=True,
        )
        db.add(user)


# ---------------------------------------------------------------------------
# Authorization: LDAP endpoints are admin-only
# ---------------------------------------------------------------------------


class TestLdapSettingsAdminOnly:
    def test_viewer_get_settings_is_403(self, viewer_client: TestClient) -> None:
        """GET /settings is admin-only; viewer is denied before seeing the panel."""
        resp = viewer_client.get("/settings", follow_redirects=False)
        assert resp.status_code == 403

    def test_operator_get_settings_is_403(self, operator_client: TestClient) -> None:
        resp = operator_client.get("/settings", follow_redirects=False)
        assert resp.status_code == 403

    def test_viewer_post_ldap_is_403(self, viewer_client: TestClient) -> None:
        """Direct POST to /settings/ldap by a viewer is denied server-side."""
        csrf = csrf_of(viewer_client, "/")
        resp = viewer_client.post(
            "/settings/ldap",
            data={"csrf_token": csrf, "ldap_enabled": "on"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_operator_post_ldap_is_403(self, operator_client: TestClient) -> None:
        csrf = csrf_of(operator_client, "/")
        resp = operator_client.post(
            "/settings/ldap",
            data={"csrf_token": csrf, "ldap_enabled": "on"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_viewer_post_ldap_test_connection_is_403(
        self, viewer_client: TestClient
    ) -> None:
        csrf = csrf_of(viewer_client, "/")
        resp = viewer_client.post(
            "/settings/ldap/test-connection",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_operator_post_ldap_test_connection_is_403(
        self, operator_client: TestClient
    ) -> None:
        csrf = csrf_of(operator_client, "/")
        resp = operator_client.post(
            "/settings/ldap/test-connection",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_admin_can_view_settings_page(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/settings", follow_redirects=False)
        assert resp.status_code == 200
        assert "LDAP" in resp.text or "Directory" in resp.text


# ---------------------------------------------------------------------------
# Settings page renders LDAP panel
# ---------------------------------------------------------------------------


class TestLdapSettingsPageRenders:
    def test_settings_page_shows_ldap_section(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/settings", follow_redirects=False)
        assert resp.status_code == 200
        # Sidenav entry and section heading
        assert "Directory" in resp.text
        assert "ldap" in resp.text

    def test_settings_page_shows_masked_password_when_set(
        self, admin_client: TestClient
    ) -> None:
        _seed_ldap(admin_client, enabled=True)
        resp = admin_client.get("/settings", follow_redirects=False)
        assert resp.status_code == 200
        assert "***" in resp.text

    def test_settings_page_renders_ca_cert_field(
        self, admin_client: TestClient
    ) -> None:
        resp = admin_client.get("/settings", follow_redirects=False)
        assert resp.status_code == 200
        assert 'name="ldap_tls_ca_cert_path"' in resp.text

    def test_ca_cert_path_is_prefilled_unmasked(self, admin_client: TestClient) -> None:
        # Saving a CA path and re-rendering must show the real path (it is plain
        # config, never masked like the bind password).
        path = "/etc/ssl/certs/internal-ca.pem"
        _post_ldap(
            admin_client,
            {
                "ldap_server_urls": "ldap://dir.example.com",
                "ldap_tls_mode": "ldaps",
                "ldap_tls_ca_cert_path": path,
                "ldap_search_base": "ou=people,dc=example,dc=com",
                "ldap_username_attribute": "uid",
            },
        )
        resp = admin_client.get("/settings", follow_redirects=False)
        assert resp.status_code == 200
        assert path in resp.text


# ---------------------------------------------------------------------------
# Save round-trip and masked bind-password rule
# ---------------------------------------------------------------------------


class TestLdapSettingsSave:
    def test_save_persists_fields_and_redirects(self, admin_client: TestClient) -> None:
        resp = _post_ldap(
            admin_client,
            {
                "ldap_enabled": "on",
                "ldap_server_urls": "ldap://dir.example.com",
                "ldap_tls_mode": "ldaps",
                "ldap_bind_dn": "cn=svc,dc=example,dc=com",
                "ldap_bind_password": "initial-secret",
                "ldap_search_base": "ou=people,dc=example,dc=com",
                "ldap_username_attribute": "sAMAccountName",
                "ldap_membership_mode": "group_search",
                "ldap_admin_group_dn": "cn=admins,ou=groups,dc=example,dc=com",
            },
        )
        # Successful save is a 303 redirect
        assert resp.status_code == 303

        # Verify representative fields persisted in DB
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            row = db.get(LdapSettings, 1)
            assert row is not None
            assert row.enabled is True
            assert row.tls_mode == "ldaps"
            assert row.search_base == "ou=people,dc=example,dc=com"
            assert row.username_attribute == "sAMAccountName"
            assert row.membership_mode == "group_search"
            assert row.admin_group_dn == "cn=admins,ou=groups,dc=example,dc=com"

    def test_save_persists_ca_cert_path(self, admin_client: TestClient) -> None:
        path = "/etc/ssl/certs/internal-ca.pem"
        resp = _post_ldap(
            admin_client,
            {
                "ldap_enabled": "on",
                "ldap_server_urls": "ldaps://dir.example.com",
                "ldap_tls_mode": "ldaps",
                "ldap_tls_ca_cert_path": path,
                "ldap_search_base": "ou=people,dc=example,dc=com",
                "ldap_username_attribute": "uid",
            },
        )
        assert resp.status_code == 303
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            row = db.get(LdapSettings, 1)
            assert row is not None
            assert row.tls_ca_cert_path == path

    def test_blank_ca_cert_path_saves_as_none(self, admin_client: TestClient) -> None:
        resp = _post_ldap(
            admin_client,
            {
                "ldap_server_urls": "ldap://dir.example.com",
                "ldap_tls_mode": "none",
                "ldap_tls_ca_cert_path": "   ",
                "ldap_search_base": "ou=people,dc=example,dc=com",
                "ldap_username_attribute": "uid",
            },
        )
        assert resp.status_code == 303
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            row = db.get(LdapSettings, 1)
            assert row is not None
            assert row.tls_ca_cert_path is None

    def test_save_redirects_to_full_settings_page(
        self, admin_client: TestClient
    ) -> None:
        """Following the 303 redirect must yield the full settings page, not
        a nested partial — guards against the HTMX outerHTML swap bug."""
        resp = _post_ldap(
            admin_client,
            {
                "ldap_enabled": "on",
                "ldap_server_urls": "ldap://dir.example.com",
                "ldap_search_base": "ou=people,dc=example,dc=com",
                "ldap_username_attribute": "uid",
                "ldap_membership_mode": "memberof",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        # Full page includes the outer shell, not just the LDAP partial
        assert "<html" in resp.text
        assert "Directory" in resp.text

    def test_bind_password_is_masked_on_read(self, admin_client: TestClient) -> None:
        _seed_ldap(admin_client, enabled=True)
        resp = admin_client.get("/settings", follow_redirects=False)
        assert resp.status_code == 200
        # Stored password shows as mask, never plaintext
        assert "stored-service-secret" not in resp.text
        assert "***" in resp.text

    def test_blank_password_keeps_stored_secret(self, admin_client: TestClient) -> None:
        """Submitting a blank bind password must not overwrite the stored secret."""
        _seed_ldap(admin_client, enabled=True)
        stored_before = _stored_bind_password()
        assert stored_before is not None
        # Ensure it's encrypted at rest
        assert stored_before.startswith("enc:v1:")

        # Submit with blank bind_password — the masked "***" value
        resp = _post_ldap(
            admin_client,
            {
                "ldap_enabled": "on",
                "ldap_server_urls": "ldap://dir.example.com",
                "ldap_tls_mode": "none",
                "ldap_bind_dn": "cn=svc,dc=example,dc=com",
                "ldap_bind_password": "***",  # the mask sentinel
                "ldap_search_base": "ou=people,dc=example,dc=com",
                "ldap_username_attribute": "uid",
                "ldap_membership_mode": "memberof",
            },
        )
        assert resp.status_code == 303

        stored_after = _stored_bind_password()
        # Ciphertext must be byte-for-byte identical — no double-wrap, no loss
        assert stored_after == stored_before

    def test_empty_password_also_keeps_stored_secret(
        self, admin_client: TestClient
    ) -> None:
        _seed_ldap(admin_client, enabled=True)
        stored_before = _stored_bind_password()

        resp = _post_ldap(
            admin_client,
            {
                "ldap_enabled": "on",
                "ldap_server_urls": "ldap://dir.example.com",
                "ldap_bind_password": "",  # empty keeps stored
                "ldap_search_base": "ou=people,dc=example,dc=com",
                "ldap_username_attribute": "uid",
                "ldap_membership_mode": "memberof",
            },
        )
        assert resp.status_code == 303
        assert _stored_bind_password() == stored_before

    def test_new_password_overwrites_stored(self, admin_client: TestClient) -> None:
        _seed_ldap(admin_client, enabled=True)
        stored_before = _stored_bind_password()

        resp = _post_ldap(
            admin_client,
            {
                "ldap_enabled": "on",
                "ldap_server_urls": "ldap://dir.example.com",
                "ldap_bind_password": "brand-new-secret-value",
                "ldap_search_base": "ou=people,dc=example,dc=com",
                "ldap_username_attribute": "uid",
                "ldap_membership_mode": "memberof",
            },
        )
        assert resp.status_code == 303
        stored_after = _stored_bind_password()
        # New ciphertext — different from the old one
        assert stored_after != stored_before
        assert stored_after is not None
        assert stored_after.startswith("enc:v1:")

    def test_server_urls_multiline_textarea(self, admin_client: TestClient) -> None:
        """Multiple URLs submitted as newline-separated text are stored as a list."""
        resp = _post_ldap(
            admin_client,
            {
                "ldap_enabled": "on",
                "ldap_server_urls": (
                    "ldap://primary.example.com\nldap://secondary.example.com"
                ),
                "ldap_search_base": "ou=people,dc=example,dc=com",
                "ldap_username_attribute": "uid",
                "ldap_membership_mode": "memberof",
            },
        )
        assert resp.status_code == 303
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            row = db.get(LdapSettings, 1)
            assert row is not None
            urls: list[str] = row.server_urls or []
            assert len(urls) == 2


# ---------------------------------------------------------------------------
# Validation: enabled with missing required fields is refused
# ---------------------------------------------------------------------------


class TestLdapSettingsValidation:
    def test_enabled_without_server_url_is_rejected(
        self, admin_client: TestClient
    ) -> None:
        resp = _post_ldap(
            admin_client,
            {
                "ldap_enabled": "on",
                # no ldap_server_urls
                "ldap_search_base": "ou=people,dc=example,dc=com",
                "ldap_username_attribute": "uid",
                "ldap_membership_mode": "memberof",
            },
        )
        # Returns the full settings page (200) with an inline error in the panel
        assert resp.status_code == 200
        assert "<html" in resp.text
        assert "server URL" in resp.text.lower() or "url" in resp.text.lower()

    def test_enabled_without_search_base_is_rejected(
        self, admin_client: TestClient
    ) -> None:
        resp = _post_ldap(
            admin_client,
            {
                "ldap_enabled": "on",
                "ldap_server_urls": "ldap://dir.example.com",
                # no ldap_search_base
                "ldap_username_attribute": "uid",
                "ldap_membership_mode": "memberof",
            },
        )
        assert resp.status_code == 200
        assert "<html" in resp.text
        assert "search base" in resp.text.lower()

    def test_enabled_without_username_attribute_is_rejected(
        self, admin_client: TestClient
    ) -> None:
        resp = _post_ldap(
            admin_client,
            {
                "ldap_enabled": "on",
                "ldap_server_urls": "ldap://dir.example.com",
                "ldap_search_base": "ou=people,dc=example,dc=com",
                # no ldap_username_attribute
                "ldap_membership_mode": "memberof",
            },
        )
        assert resp.status_code == 200
        assert "<html" in resp.text
        assert "username attribute" in resp.text.lower()

    def test_validation_error_does_not_persist_broken_config(
        self, admin_client: TestClient
    ) -> None:
        """A rejected enabled-with-missing-fields must not save a broken row."""
        _post_ldap(
            admin_client,
            {
                "ldap_enabled": "on",
                # missing all required fields
                "ldap_membership_mode": "memberof",
            },
        )
        # No row created — or row was already there but enabled stays False
        assert _stored_enabled() is False

    def test_disabled_without_required_fields_saves_ok(
        self, admin_client: TestClient
    ) -> None:
        """A disabled LDAP config may be saved without required fields."""
        resp = _post_ldap(
            admin_client,
            {
                # no ldap_enabled → disabled
                "ldap_server_urls": "",
                "ldap_search_base": "",
                "ldap_username_attribute": "",
                "ldap_membership_mode": "memberof",
            },
        )
        assert resp.status_code == 303
        assert _stored_enabled() is False


# ---------------------------------------------------------------------------
# Test-connection action
# ---------------------------------------------------------------------------


def _fake_dir_state(outcome: LdapOutcome) -> LdapDirectoryState:
    return LdapDirectoryState(outcome=outcome, detail="test-stub")


class TestLdapTestConnection:
    def _post_test(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        outcome: LdapOutcome,
    ) -> Any:
        _seed_ldap(client, enabled=True)
        monkeypatch.setattr(
            routers,
            "ldap_resolve_directory_state",
            lambda **_kw: _fake_dir_state(outcome),
        )
        csrf = csrf_of(client, "/settings")
        return client.post(
            "/settings/ldap/test-connection",
            data={"csrf_token": csrf},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "HX-Request": "true",
            },
            follow_redirects=False,
        )

    def test_authenticated_outcome_shows_success(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        resp = self._post_test(admin_client, monkeypatch, LdapOutcome.AUTHENTICATED)
        assert resp.status_code == 200
        assert "Connected and bind succeeded" in resp.text

    def test_no_such_user_outcome_shows_success(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NO_SUCH_USER: service bind worked; probe username simply did not match."""
        resp = self._post_test(admin_client, monkeypatch, LdapOutcome.NO_SUCH_USER)
        assert resp.status_code == 200
        assert "Connected and bind succeeded" in resp.text

    def test_server_unreachable_outcome_shows_error(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        resp = self._post_test(
            admin_client, monkeypatch, LdapOutcome.SERVER_UNREACHABLE
        )
        assert resp.status_code == 200
        assert "Server unreachable" in resp.text

    def test_config_error_outcome_shows_bind_failed(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        resp = self._post_test(admin_client, monkeypatch, LdapOutcome.CONFIG_ERROR)
        assert resp.status_code == 200
        assert "Bind failed" in resp.text

    def test_invalid_credentials_outcome_shows_bind_failed(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        resp = self._post_test(
            admin_client, monkeypatch, LdapOutcome.INVALID_CREDENTIALS
        )
        assert resp.status_code == 200
        assert "Bind failed" in resp.text

    def test_disabled_outcome_shows_disabled(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        resp = self._post_test(admin_client, monkeypatch, LdapOutcome.DISABLED)
        assert resp.status_code == 200
        assert "LDAP is disabled" in resp.text

    def test_bind_password_is_not_echoed(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real bind password must never appear in the test-connection response."""
        resp = self._post_test(admin_client, monkeypatch, LdapOutcome.AUTHENTICATED)
        assert "stored-service-secret" not in resp.text


# ---------------------------------------------------------------------------
# Auth-source badges in the users list
# ---------------------------------------------------------------------------


class TestAuthSourceBadges:
    def test_local_user_shows_local_badge(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/users", follow_redirects=False)
        assert resp.status_code == 200
        # The admin user is local; the badge text is uppercased by the template.
        assert "LOCAL" in resp.text
        assert (
            "auth-badge local" in resp.text or 'class="auth-badge local"' in resp.text
        )

    def test_ldap_user_shows_ldap_badge(self, admin_client: TestClient) -> None:
        _seed_ldap_user("ldap-bob")
        resp = admin_client.get("/users", follow_redirects=False)
        assert resp.status_code == 200
        assert "LDAP" in resp.text
        assert "auth-badge ldap" in resp.text or 'class="auth-badge ldap"' in resp.text

    def test_both_badge_types_present_when_mixed_users(
        self, admin_client: TestClient
    ) -> None:
        _seed_ldap_user("ldap-carol")
        resp = admin_client.get("/users", follow_redirects=False)
        assert resp.status_code == 200
        assert "LOCAL" in resp.text
        assert "LDAP" in resp.text
