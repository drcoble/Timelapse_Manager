"""Unit tests for the LDAP directory connector.

Driven entirely by ldap3's in-process mock server (``client_strategy=MOCK_SYNC``)
so no live directory is required. A small connection factory seeds the same
fixture entries into every connection the connector opens (service bind, user
rebind, group search), since each MOCK_SYNC connection carries its own in-memory
store.

What is and is not covered here:
* Covered: service-bind + user search + user-rebind success, wrong-password and
  unknown-user clean negatives, memberOf and group_search membership resolution
  (incl. nested), config-guard short-circuits, and that no raw ldap3 exception
  escapes (the unreachable/error outcomes via an injected raising factory).
* Deferred to the glauth integration pass: genuine multi-server socket failover.
  MOCK_SYNC cannot simulate a server pool where the first socket is down and the
  second serves, so pool *construction/ordering* is asserted here and live
  failover is left to the integration suite.
"""

from __future__ import annotations

import ssl
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import ldap3
import pytest
from ldap3 import MOCK_SYNC, Connection, Server
from ldap3.core.exceptions import LDAPSocketOpenError

from timelapse_manager.security.ldap_directory import (
    LdapOutcome,
    _build_server_pool,
    authenticate,
    resolve_directory_state,
)
from timelapse_manager.security.ldap_settings_service import LdapSettingsView

# Placeholder directory tree used throughout; never a real domain.
_SVC_DN = "cn=svc,dc=example,dc=com"
_SVC_PW = "service-bind-pw"
_USER_DN = "uid=alice,ou=people,dc=example,dc=com"
_USER_PW = "alice-secret-pw"
_ADMIN_GROUP = "cn=admins,ou=groups,dc=example,dc=com"


def _base_view(**overrides: object) -> LdapSettingsView:
    """A fully-configured, enabled settings view for the happy path."""
    defaults: dict[str, object] = {
        "enabled": True,
        "server_urls": ["ldap://dir1.example.com"],
        "tls_mode": "none",
        "bind_dn": _SVC_DN,
        "bind_password": "***",  # display mask; real secret passed separately
        "search_base": "ou=people,dc=example,dc=com",
        "search_filter": "(objectClass=inetOrgPerson)",
        "username_attribute": "uid",
        "display_name_attribute": "cn",
        "membership_mode": "memberof",
        "nested_groups": False,
        "admin_group_dn": _ADMIN_GROUP,
    }
    defaults.update(overrides)
    return LdapSettingsView(**defaults)


def _make_factory(
    entries: dict[str, dict[str, object]],
) -> Callable[[object, str | None, str | None], Connection]:
    """Return a connection factory that seeds ``entries`` into every connection."""
    server = Server("ldap://mock.example.com")

    def factory(_pool: object, user: str | None, password: str | None) -> Connection:
        conn = Connection(
            server,
            user=user,
            password=password,
            client_strategy=MOCK_SYNC,
            raise_exceptions=False,
        )
        for dn, attrs in entries.items():
            conn.strategy.add_entry(dn, attrs)
        return conn

    return factory


def _memberof_entries() -> dict[str, dict[str, object]]:
    return {
        _SVC_DN: {"userPassword": _SVC_PW, "objectClass": "person", "cn": "svc"},
        _USER_DN: {
            "userPassword": _USER_PW,
            "objectClass": "inetOrgPerson",
            "uid": "alice",
            "cn": "Alice Example",
            "memberOf": [_ADMIN_GROUP],
        },
    }


class TestAuthenticateSuccess:
    def test_valid_credentials_authenticate_with_groups(self) -> None:
        result = authenticate(
            settings=_base_view(),
            username="alice",
            password=_USER_PW,
            bind_password=_SVC_PW,
            connection_factory=_make_factory(_memberof_entries()),
        )
        assert result.outcome is LdapOutcome.AUTHENTICATED
        assert result.authenticated is True
        assert result.dn == _USER_DN
        assert result.display_name == "Alice Example"
        assert _ADMIN_GROUP in result.groups

    def test_display_name_falls_back_to_username_when_attr_absent(self) -> None:
        entries = _memberof_entries()
        # Drop the cn so the display-name attribute resolves to nothing.
        del entries[_USER_DN]["cn"]
        result = authenticate(
            settings=_base_view(),
            username="alice",
            password=_USER_PW,
            bind_password=_SVC_PW,
            connection_factory=_make_factory(entries),
        )
        assert result.authenticated is True
        assert result.display_name == "alice"


class TestAuthenticateNegatives:
    def test_wrong_password_is_clean_invalid_credentials(self) -> None:
        result = authenticate(
            settings=_base_view(),
            username="alice",
            password="wrong-password",
            bind_password=_SVC_PW,
            connection_factory=_make_factory(_memberof_entries()),
        )
        assert result.outcome is LdapOutcome.INVALID_CREDENTIALS
        assert result.authenticated is False
        assert result.dn == ""
        assert result.groups == frozenset()

    def test_empty_password_rejected_without_binding(self) -> None:
        result = authenticate(
            settings=_base_view(),
            username="alice",
            password="",
            bind_password=_SVC_PW,
            connection_factory=_make_factory(_memberof_entries()),
        )
        assert result.outcome is LdapOutcome.INVALID_CREDENTIALS

    def test_unknown_user_is_clean_no_such_user(self) -> None:
        result = authenticate(
            settings=_base_view(),
            username="nobody",
            password=_USER_PW,
            bind_password=_SVC_PW,
            connection_factory=_make_factory(_memberof_entries()),
        )
        assert result.outcome is LdapOutcome.NO_SUCH_USER
        assert result.authenticated is False


class TestAuthenticateConfigGuards:
    def test_disabled_is_distinct_disabled_outcome(self) -> None:
        # DISABLED is intentionally distinct from CONFIG_ERROR so the login flow
        # can fall through to local auth without string-matching the detail.
        result = authenticate(
            settings=_base_view(enabled=False),
            username="alice",
            password=_USER_PW,
            bind_password=_SVC_PW,
            connection_factory=_make_factory(_memberof_entries()),
        )
        assert result.outcome is LdapOutcome.DISABLED
        assert result.outcome is not LdapOutcome.CONFIG_ERROR
        assert result.authenticated is False

    def test_no_server_is_config_error(self) -> None:
        result = authenticate(
            settings=_base_view(server_urls=[]),
            username="alice",
            password=_USER_PW,
            bind_password=_SVC_PW,
            connection_factory=_make_factory(_memberof_entries()),
        )
        assert result.outcome is LdapOutcome.CONFIG_ERROR

    def test_missing_search_base_is_config_error(self) -> None:
        result = authenticate(
            settings=_base_view(search_base=""),
            username="alice",
            password=_USER_PW,
            bind_password=_SVC_PW,
            connection_factory=_make_factory(_memberof_entries()),
        )
        assert result.outcome is LdapOutcome.CONFIG_ERROR

    def test_service_bind_failure_is_config_error_not_user_denial(self) -> None:
        # Service account presents the wrong bind password.
        result = authenticate(
            settings=_base_view(),
            username="alice",
            password=_USER_PW,
            bind_password="wrong-service-pw",
            connection_factory=_make_factory(_memberof_entries()),
        )
        assert result.outcome is LdapOutcome.CONFIG_ERROR


class TestAuthenticateInfraErrorsAreResults:
    def test_unreachable_server_maps_to_typed_result(self) -> None:
        def raising_factory(*_args: object, **_kwargs: object) -> Connection:
            raise LDAPSocketOpenError("connection refused")

        result = authenticate(
            settings=_base_view(),
            username="alice",
            password=_USER_PW,
            bind_password=_SVC_PW,
            connection_factory=raising_factory,
        )
        assert result.outcome is LdapOutcome.SERVER_UNREACHABLE
        assert result.authenticated is False


# Groups deliberately live in a SIBLING subtree (ou=groups), not under the user
# OU (ou=people), so the test proves group search reaches the directory root and
# does not silently rely on groups being colocated with users.
_OPS_GROUP = "cn=ops,ou=groups,dc=example,dc=com"
_PARENT_GROUP = "cn=parent,ou=groups,dc=example,dc=com"


class TestGroupSearchMembership:
    def _group_search_entries(self, nested: bool) -> dict[str, dict[str, object]]:
        entries: dict[str, dict[str, object]] = {
            _SVC_DN: {"userPassword": _SVC_PW, "objectClass": "person", "cn": "svc"},
            _USER_DN: {
                "userPassword": _USER_PW,
                "objectClass": "inetOrgPerson",
                "uid": "alice",
                "cn": "Alice Example",
            },
            # Direct group whose member is the user -- in a sibling OU.
            _OPS_GROUP: {
                "objectClass": "groupOfNames",
                "cn": "ops",
                "member": [_USER_DN],
            },
        }
        if nested:
            # A parent group whose member is the ops group (transitive membership).
            entries[_PARENT_GROUP] = {
                "objectClass": "groupOfNames",
                "cn": "parent",
                "member": [_OPS_GROUP],
            }
        return entries

    def test_group_search_finds_direct_group_in_sibling_subtree(self) -> None:
        # group_search_base unset -> connector falls back to the directory suffix
        # of the user base (dc=example,dc=com), reaching the sibling ou=groups.
        view = _base_view(membership_mode="group_search", nested_groups=False)
        assert view.group_search_base == ""
        result = authenticate(
            settings=view,
            username="alice",
            password=_USER_PW,
            bind_password=_SVC_PW,
            connection_factory=_make_factory(self._group_search_entries(nested=False)),
        )
        assert result.authenticated is True
        assert _OPS_GROUP in result.groups
        assert _PARENT_GROUP not in result.groups

    def test_group_search_honours_explicit_group_search_base(self) -> None:
        view = _base_view(
            membership_mode="group_search",
            nested_groups=False,
            group_search_base="ou=groups,dc=example,dc=com",
        )
        result = authenticate(
            settings=view,
            username="alice",
            password=_USER_PW,
            bind_password=_SVC_PW,
            connection_factory=_make_factory(self._group_search_entries(nested=False)),
        )
        assert result.authenticated is True
        assert _OPS_GROUP in result.groups

    def test_group_search_nested_follows_parent_groups(self) -> None:
        view = _base_view(membership_mode="group_search", nested_groups=True)
        result = authenticate(
            settings=view,
            username="alice",
            password=_USER_PW,
            bind_password=_SVC_PW,
            connection_factory=_make_factory(self._group_search_entries(nested=True)),
        )
        assert result.authenticated is True
        assert _OPS_GROUP in result.groups
        assert _PARENT_GROUP in result.groups


class TestAnonymousServiceBind:
    def test_no_bind_dn_uses_anonymous_service_search(self) -> None:
        # With no bind_dn configured, the service connection binds anonymously.
        # MOCK_SYNC permits an anonymous bind, so the search still succeeds.
        view = _base_view(bind_dn="")
        result = authenticate(
            settings=view,
            username="alice",
            password=_USER_PW,
            bind_password=None,
            connection_factory=_make_factory(_memberof_entries()),
        )
        assert result.authenticated is True


class TestServerPoolConstruction:
    """Failover ordering is unit-tested at construction; live socket failover is
    deferred to the glauth integration pass (MOCK_SYNC cannot down a server)."""

    def test_pool_built_in_configured_order_first_strategy(self) -> None:
        view = _base_view(server_urls=["ldap://a.example.com", "ldap://b.example.com"])
        pool = _build_server_pool(view)
        hosts = [s.host for s in pool.servers]
        assert hosts == ["a.example.com", "b.example.com"]
        assert pool.strategy == ldap3.FIRST

    def test_ldaps_mode_sets_ssl_on_servers(self) -> None:
        view = _base_view(tls_mode="ldaps", server_urls=["ldaps://a.example.com"])
        pool = _build_server_pool(view)
        assert pool.servers[0].ssl is True

    def test_plain_mode_no_ssl(self) -> None:
        view = _base_view(tls_mode="none")
        pool = _build_server_pool(view)
        assert pool.servers[0].ssl is False

    def test_every_server_carries_connect_timeout(self) -> None:
        # Regression guard: without a connect_timeout on each Server, an all-down
        # pool hangs on the OS default connect wait instead of returning
        # SERVER_UNREACHABLE in bounded time. Every server must carry the
        # configured timeout so the worst case is connect_timeout * num_servers.
        view = _base_view(
            server_urls=["ldap://a.example.com", "ldap://b.example.com"],
            connect_timeout_seconds=3.0,
        )
        pool = _build_server_pool(view)
        assert [s.connect_timeout for s in pool.servers] == [3.0, 3.0]

    def test_connect_timeout_defaults_when_unset(self) -> None:
        # A view built without an explicit timeout still stamps the module default
        # so a misconfigured deployment is never left with an unbounded connect.
        from timelapse_manager.security.ldap_settings_service import (
            DEFAULT_CONNECT_TIMEOUT_SECONDS,
        )

        pool = _build_server_pool(_base_view())
        assert pool.servers[0].connect_timeout == DEFAULT_CONNECT_TIMEOUT_SECONDS

    @pytest.mark.parametrize("tls_mode", ["ldaps", "starttls"])
    def test_ca_cert_path_sets_ca_certs_file_on_tls(
        self, tls_mode: str, tmp_path: Path
    ) -> None:
        # When a CA-cert trust-anchor path is configured, the Tls object stamped on
        # every server must carry it as ``ca_certs_file`` -- for BOTH ldaps and
        # StartTLS, which share one Tls object. The file must exist because ldap3
        # validates its presence at Tls construction.
        ca_file = tmp_path / "internal-ca.pem"
        ca_file.write_text("-----BEGIN CERTIFICATE-----\nplaceholder\n")
        view = _base_view(
            tls_mode=tls_mode,
            tls_ca_cert_path=str(ca_file),
            server_urls=["ldaps://a.example.com"],
        )
        pool = _build_server_pool(view)
        assert pool.servers[0].tls is not None
        assert pool.servers[0].tls.ca_certs_file == str(ca_file)
        # Validation is never relaxed below CERT_REQUIRED.
        assert pool.servers[0].tls.validate == ssl.CERT_REQUIRED

    @pytest.mark.parametrize("tls_mode", ["ldaps", "starttls"])
    def test_no_ca_cert_path_leaves_ca_certs_file_none(self, tls_mode: str) -> None:
        # Unset -> no pinned CA file; ldap3 falls back to the platform trust store /
        # SSL_CERT_FILE. CERT_REQUIRED still holds.
        view = _base_view(tls_mode=tls_mode, tls_ca_cert_path=None)
        pool = _build_server_pool(view)
        assert pool.servers[0].tls is not None
        assert pool.servers[0].tls.ca_certs_file is None
        assert pool.servers[0].tls.validate == ssl.CERT_REQUIRED

    @pytest.mark.parametrize("tls_mode", ["ldaps", "starttls"])
    def test_empty_ca_cert_path_leaves_ca_certs_file_none(self, tls_mode: str) -> None:
        # An empty string is normalised to None (no file pinned), so a blank form
        # field never produces an invalid empty path passed to ldap3.
        view = _base_view(tls_mode=tls_mode, tls_ca_cert_path="")
        pool = _build_server_pool(view)
        assert pool.servers[0].tls is not None
        assert pool.servers[0].tls.ca_certs_file is None


class TestMissingCaCertPathSurfacesTyped:
    """A configured-but-missing CA path must surface as a typed outcome.

    ldap3 raises ``LDAPSSLConfigurationError`` (an ``LDAPException``) when a CA
    file is configured but absent. The save path never builds a pool, so it is
    unaffected; but ``authenticate`` / ``resolve_directory_state`` build the pool
    inside their ``LDAPException`` guard, so the fault maps to a typed result
    rather than escaping as an unhandled error.
    """

    def test_authenticate_with_missing_ca_file_is_typed_not_raised(
        self, tmp_path: Path
    ) -> None:
        missing = tmp_path / "does-not-exist.pem"
        view = _base_view(
            tls_mode="ldaps",
            tls_ca_cert_path=str(missing),
            server_urls=["ldaps://a.example.com"],
        )
        result = authenticate(
            settings=view,
            username="alice",
            password=_USER_PW,
            bind_password=_SVC_PW,
        )
        assert result.outcome is LdapOutcome.SERVER_UNREACHABLE

    def test_resolve_with_missing_ca_file_is_typed_not_raised(
        self, tmp_path: Path
    ) -> None:
        missing = tmp_path / "does-not-exist.pem"
        view = _base_view(
            tls_mode="starttls",
            tls_ca_cert_path=str(missing),
            server_urls=["ldap://a.example.com"],
        )
        state = resolve_directory_state(
            settings=view,
            username="alice",
            bind_password=_SVC_PW,
        )
        assert state.outcome is LdapOutcome.SERVER_UNREACHABLE


@pytest.mark.parametrize(
    "mode",
    ["memberof", "group_search"],
)
def test_replace_view_keeps_other_fields(mode: str) -> None:
    """Sanity: the frozen view's replace() keeps unrelated fields intact."""
    view = replace(_base_view(), membership_mode=mode)
    assert view.membership_mode == mode
    assert view.enabled is True
