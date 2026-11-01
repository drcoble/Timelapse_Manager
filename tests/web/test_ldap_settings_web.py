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

import re
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
from timelapse_manager.security.session_revalidation import breaker_for

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
                "ldap_tls_mode": "none",
                "ldap_bind_dn": "cn=svc,dc=example,dc=com",
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


# ---------------------------------------------------------------------------
# Retarget, unknown TLS mode, default (#67 S9)
# ---------------------------------------------------------------------------


def _unchanged_form(**overrides: str) -> dict[str, str]:
    """The seeded configuration, resubmitted as the form would (password masked)."""
    data = {
        "ldap_enabled": "on",
        "ldap_server_urls": "ldap://dir.example.com",
        "ldap_tls_mode": "none",
        "ldap_bind_dn": "cn=svc,dc=example,dc=com",
        "ldap_bind_password": "***",
        "ldap_search_base": "ou=people,dc=example,dc=com",
        "ldap_username_attribute": "uid",
        "ldap_membership_mode": "memberof",
    }
    data.update(overrides)
    return data


class TestRetargetAndTlsMode:
    @pytest.mark.parametrize(
        "change",
        [
            {"ldap_server_urls": "ldap://other.example.com"},
            {"ldap_tls_mode": "ldaps"},
            {"ldap_bind_dn": "cn=other,dc=example,dc=com"},
        ],
    )
    def test_retarget_with_masked_password_clears_the_secret(
        self, admin_client: TestClient, change: dict[str, str]
    ) -> None:
        _seed_ldap(admin_client)
        assert _stored_bind_password() is not None
        assert _post_ldap(admin_client, _unchanged_form(**change)).status_code == 303
        assert _stored_bind_password() is None

    def test_the_form_then_asks_for_the_password_again(
        self, admin_client: TestClient
    ) -> None:
        _seed_ldap(admin_client)
        _post_ldap(
            admin_client, _unchanged_form(ldap_server_urls="ldap://other.example.com")
        )
        page = admin_client.get("/settings").text
        assert "Re-enter the bind" in page
        assert "stored-service-secret" not in page

    def test_the_hint_is_absent_while_a_password_is_stored(
        self, admin_client: TestClient
    ) -> None:
        _seed_ldap(admin_client)
        assert "Re-enter the bind" not in admin_client.get("/settings").text

    def test_retarget_with_a_new_password_keeps_the_new_one(
        self, admin_client: TestClient
    ) -> None:
        _seed_ldap(admin_client)
        _post_ldap(
            admin_client,
            _unchanged_form(
                ldap_server_urls="ldap://other.example.com",
                ldap_bind_password="new-target-secret",
            ),
        )
        assert _stored_bind_password() is not None

    def test_an_unchanged_target_keeps_the_secret(
        self, admin_client: TestClient
    ) -> None:
        _seed_ldap(admin_client)
        before = _stored_bind_password()
        assert _post_ldap(admin_client, _unchanged_form()).status_code == 303
        assert _stored_bind_password() == before

    def test_an_unknown_tls_mode_is_refused_and_nothing_is_saved(
        self, admin_client: TestClient
    ) -> None:
        _seed_ldap(admin_client)
        before = _stored_bind_password()
        resp = _post_ldap(
            admin_client,
            _unchanged_form(ldap_tls_mode="plaintext-please", ldap_bind_password="x1"),
        )
        assert resp.status_code == 200  # the page re-rendered with an inline error
        assert "Unknown TLS mode" in resp.text
        assert "plaintext-please" not in resp.text
        assert _stored_bind_password() == before
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            row = db.get(LdapSettings, 1)
            assert row is not None and row.tls_mode == "none"

    def test_a_missing_tls_field_defaults_to_ldaps_not_plaintext(
        self, admin_client: TestClient
    ) -> None:
        data = _unchanged_form()
        del data["ldap_tls_mode"]
        _post_ldap(admin_client, data)
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            row = db.get(LdapSettings, 1)
            assert row is not None and row.tls_mode == "ldaps"

    def test_a_fresh_install_preselects_ldaps(self, admin_client: TestClient) -> None:
        page = admin_client.get("/settings").text
        assert re.search(r'<option value="ldaps"\s+selected', page)

    def test_test_connection_after_a_retarget_uses_no_stored_password(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_ldap(admin_client)
        _post_ldap(
            admin_client, _unchanged_form(ldap_server_urls="ldap://other.example.com")
        )
        seen: dict[str, Any] = {}

        def fake(**kw: Any) -> LdapDirectoryState:
            seen.update(kw)
            return _fake_dir_state(LdapOutcome.CONFIG_ERROR)

        monkeypatch.setattr(routers, "ldap_resolve_directory_state", fake)
        csrf = csrf_of(admin_client, "/settings")
        admin_client.post(
            "/settings/ldap/test-connection",
            data={"csrf_token": csrf},
            headers={"HX-Request": "true"},
        )
        assert seen["bind_password"] is None
        assert seen["settings"].server_urls == ["ldap://other.example.com"]


# ---------------------------------------------------------------------------
# The directory breaker is reset by an admin's save / successful test (#203)
# ---------------------------------------------------------------------------


def _open_breaker() -> None:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        breaker_for(db).record_unreachable(3600)
        assert breaker_for(db).is_open() is True


def _breaker_open() -> bool:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        return breaker_for(db).is_open()


class TestBreakerReset:
    def test_saving_the_settings_closes_an_open_breaker(
        self, admin_client: TestClient
    ) -> None:
        _seed_ldap(admin_client)
        _open_breaker()
        assert _post_ldap(admin_client, _unchanged_form()).status_code == 303
        assert _breaker_open() is False

    def test_a_refused_save_leaves_it_open(self, admin_client: TestClient) -> None:
        _seed_ldap(admin_client)
        _open_breaker()
        # Enabled but incomplete: refused with an inline error.
        resp = _post_ldap(
            admin_client,
            {"ldap_enabled": "on", "ldap_server_urls": "", "ldap_tls_mode": "ldaps"},
        )
        assert resp.status_code == 200
        assert _breaker_open() is True
        resp = _post_ldap(admin_client, _unchanged_form(ldap_tls_mode="nope"))
        assert resp.status_code == 200
        assert _breaker_open() is True

    @pytest.mark.parametrize(
        "outcome", [LdapOutcome.AUTHENTICATED, LdapOutcome.NO_SUCH_USER]
    )
    def test_a_successful_test_connection_closes_it(
        self,
        admin_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        outcome: LdapOutcome,
    ) -> None:
        _open_breaker()
        resp = TestLdapTestConnection()._post_test(admin_client, monkeypatch, outcome)
        assert resp.status_code == 200
        assert _breaker_open() is False

    @pytest.mark.parametrize(
        "outcome",
        [
            LdapOutcome.SERVER_UNREACHABLE,
            LdapOutcome.CONFIG_ERROR,
            LdapOutcome.INVALID_CREDENTIALS,
            LdapOutcome.DISABLED,
        ],
    )
    def test_a_failed_test_connection_leaves_it_open(
        self,
        admin_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        outcome: LdapOutcome,
    ) -> None:
        _open_breaker()
        resp = TestLdapTestConnection()._post_test(admin_client, monkeypatch, outcome)
        assert resp.status_code == 200
        assert _breaker_open() is True

    def test_the_test_connection_runs_while_the_breaker_is_open(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The admin's test is not itself refused by an open breaker."""
        _open_breaker()
        resp = TestLdapTestConnection()._post_test(
            admin_client, monkeypatch, LdapOutcome.AUTHENTICATED
        )
        assert "Connected and bind succeeded" in resp.text


# ---------------------------------------------------------------------------
# Admin-only banner while LDAP runs without TLS (#67 S9)
# ---------------------------------------------------------------------------


def _set_tls_mode(client: TestClient, mode: str, *, enabled: bool = True) -> None:
    _seed_ldap(client, enabled=enabled)
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        row = db.get(LdapSettings, 1)
        assert row is not None
        row.tls_mode = mode


class TestPlaintextBanner:
    MARKER = "ldap-plaintext-banner"

    def test_admin_sees_it_with_tls_none(self, admin_client: TestClient) -> None:
        _set_tls_mode(admin_client, "none")
        resp = admin_client.get("/partials/status")
        assert resp.status_code == 200
        assert self.MARKER in resp.text
        assert "stored-service-secret" not in resp.text

    @pytest.mark.parametrize("mode", ["ldaps", "starttls"])
    def test_it_is_hidden_when_encrypted(
        self, admin_client: TestClient, mode: str
    ) -> None:
        _set_tls_mode(admin_client, mode)
        assert self.MARKER not in admin_client.get("/partials/status").text

    def test_it_is_hidden_while_ldap_is_disabled(
        self, admin_client: TestClient
    ) -> None:
        _set_tls_mode(admin_client, "none", enabled=False)
        assert self.MARKER not in admin_client.get("/partials/status").text

    def test_it_is_hidden_when_ldap_was_never_configured(
        self, admin_client: TestClient
    ) -> None:
        assert self.MARKER not in admin_client.get("/partials/status").text

    def test_an_operator_does_not_see_it(self, operator_client: TestClient) -> None:
        _set_tls_mode(operator_client, "none")
        resp = operator_client.get("/partials/status")
        assert resp.status_code == 200
        assert self.MARKER not in resp.text

    def test_a_viewer_does_not_see_it(self, viewer_client: TestClient) -> None:
        _set_tls_mode(viewer_client, "none")
        resp = viewer_client.get("/partials/status")
        assert resp.status_code == 200
        assert self.MARKER not in resp.text

    def test_it_sits_in_the_status_banner_not_base(
        self, admin_client: TestClient
    ) -> None:
        """base.html only hosts the lazy placeholder; the content is the partial."""
        _set_tls_mode(admin_client, "none")
        assert self.MARKER not in admin_client.get("/").text
