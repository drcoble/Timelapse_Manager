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

import socket
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import ldap3
import pytest
from ldap3 import MOCK_SYNC, Connection, Server
from ldap3.core.exceptions import (
    LDAPException,
    LDAPResponseTimeoutError,
    LDAPSocketOpenError,
    LDAPSocketReceiveError,
    communication_exception_factory,
)

from timelapse_manager.security.ldap_directory import (
    DETAIL_NO_SERVER_REACHABLE,
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


# ---------------------------------------------------------------------------
# Every connection is unbound on every outcome (#67 R3)
# ---------------------------------------------------------------------------


class _UnbindLedger:
    """Wrap a connection factory, counting each opened connection's unbinds."""

    def __init__(
        self,
        inner: Callable[[object, str | None, str | None], Connection],
        *,
        fail_unbind: bool = False,
        raise_on_search: bool = False,
    ) -> None:
        self._inner = inner
        self._fail_unbind = fail_unbind
        self._raise_on_search = raise_on_search
        self.connections: list[Connection] = []
        self.unbinds: list[int] = []

    def __call__(
        self, pool: object, user: str | None, password: str | None
    ) -> Connection:
        conn = self._inner(pool, user, password)
        index = len(self.connections)
        self.connections.append(conn)
        self.unbinds.append(0)
        real_unbind = conn.unbind

        def unbind(*args: object, **kwargs: object) -> object:
            self.unbinds[index] += 1
            if self._fail_unbind:
                raise OSError("socket already closed")
            return real_unbind(*args, **kwargs)

        conn.unbind = unbind  # type: ignore[method-assign]
        if self._raise_on_search:

            def search(*_a: object, **_k: object) -> bool:
                raise LDAPSocketOpenError("lost mid-flight")

            conn.search = search  # type: ignore[method-assign]
        return conn

    def every_connection_unbound_once(self) -> bool:
        return bool(self.unbinds) and all(n == 1 for n in self.unbinds)


def _auth(ledger: _UnbindLedger, *, username: str = "alice", password: str = _USER_PW):
    return authenticate(
        settings=_base_view(),
        username=username,
        password=password,
        bind_password=_SVC_PW,
        connection_factory=ledger,
    )


class TestUnbindOnEveryOutcome:
    def test_authenticated(self) -> None:
        ledger = _UnbindLedger(_make_factory(_memberof_entries()))
        assert _auth(ledger).outcome is LdapOutcome.AUTHENTICATED
        assert len(ledger.connections) == 2  # service + user bind
        assert ledger.every_connection_unbound_once()

    def test_service_bind_failed(self) -> None:
        ledger = _UnbindLedger(_make_factory(_memberof_entries()))
        result = authenticate(
            settings=_base_view(),
            username="alice",
            password=_USER_PW,
            bind_password="wrong-service-pw",
            connection_factory=ledger,
        )
        assert result.outcome is LdapOutcome.CONFIG_ERROR
        assert ledger.every_connection_unbound_once()

    def test_user_not_found(self) -> None:
        ledger = _UnbindLedger(_make_factory(_memberof_entries()))
        assert _auth(ledger, username="nobody").outcome is LdapOutcome.NO_SUCH_USER
        assert ledger.every_connection_unbound_once()

    def test_user_bind_rejected(self) -> None:
        ledger = _UnbindLedger(_make_factory(_memberof_entries()))
        assert _auth(ledger, password="wrong").outcome is (
            LdapOutcome.INVALID_CREDENTIALS
        )
        assert len(ledger.connections) == 2
        assert ledger.every_connection_unbound_once()

    def test_exception_mid_flight(self) -> None:
        ledger = _UnbindLedger(_make_factory(_memberof_entries()), raise_on_search=True)
        assert _auth(ledger).outcome is LdapOutcome.SERVER_UNREACHABLE
        assert ledger.every_connection_unbound_once()

    def test_a_failing_unbind_does_not_replace_the_result(self) -> None:
        ledger = _UnbindLedger(_make_factory(_memberof_entries()), fail_unbind=True)
        assert _auth(ledger).outcome is LdapOutcome.AUTHENTICATED

    def test_resolve_directory_state_unbinds_on_every_outcome(self) -> None:
        cases = [
            ("alice", _SVC_PW, False, LdapOutcome.AUTHENTICATED),
            ("nobody", _SVC_PW, False, LdapOutcome.NO_SUCH_USER),
            ("alice", "wrong-service-pw", False, LdapOutcome.CONFIG_ERROR),
            ("alice", _SVC_PW, True, LdapOutcome.SERVER_UNREACHABLE),
        ]
        for username, bind_pw, raises, expected in cases:
            ledger = _UnbindLedger(
                _make_factory(_memberof_entries()), raise_on_search=raises
            )
            state = resolve_directory_state(
                settings=_base_view(),
                username=username,
                bind_password=bind_pw,
                connection_factory=ledger,
            )
            assert state.outcome is expected
            assert ledger.every_connection_unbound_once(), expected


# ---------------------------------------------------------------------------
# A directory that accepts the connection and then stalls (#202)
# ---------------------------------------------------------------------------


def _receive_timeout() -> LDAPSocketReceiveError:
    """The exception ldap3 raises when ``receive_timeout`` expires on a read."""
    return communication_exception_factory(
        LDAPSocketReceiveError, TimeoutError("timed out")
    )("error receiving data: timed out")


def _stall_factory(
    inner: Callable[[object, str | None, str | None], Connection],
    *,
    on: str,
    exc: Callable[[], Exception] = _receive_timeout,
) -> Callable[[object, str | None, str | None], Connection]:
    """Wrap ``inner`` so connection method ``on`` raises ``exc()`` (a stall)."""

    def factory(pool: object, user: str | None, password: str | None) -> Connection:
        conn = inner(pool, user, password)
        if on == "service-bind" and user == _SVC_DN:
            conn.bind = lambda *a, **k: (_ for _ in ()).throw(exc())  # type: ignore[method-assign]
        elif on == "search" and user == _SVC_DN:
            conn.search = lambda *a, **k: (_ for _ in ()).throw(exc())  # type: ignore[method-assign]
        elif on == "user-bind" and user == _USER_DN:
            conn.bind = lambda *a, **k: (_ for _ in ()).throw(exc())  # type: ignore[method-assign]
        return conn

    return factory


def _request_specific_stall_factory(
    inner: Callable[[object, str | None, str | None], Connection],
    calls: list[str] | None = None,
) -> Callable[[object, str | None, str | None], Connection]:
    """Stall only searches whose filter carries a client-chosen name.

    The configured (client-independent) searches keep answering, as a healthy
    directory does when one crafted query is slow.
    """

    def factory(pool: object, user: str | None, password: str | None) -> Connection:
        conn = inner(pool, user, password)
        real_search = conn.search

        def search(*args: object, **kwargs: object) -> object:
            filt = str(kwargs.get("search_filter", args[1] if len(args) > 1 else ""))
            if calls is not None:
                calls.append(filt)
            if "uid=" in filt:
                raise _receive_timeout()
            return real_search(*args, **kwargs)

        conn.search = search  # type: ignore[method-assign,assignment]
        return conn

    return factory


class TestStalledDirectory:
    """A stall is "unreachable" only if no request could have caused it.

    Before the service bind completes nothing a client chose has been sent. After
    it, a slow user search may be the client's doing (a crafted name), so it only
    counts when a client-independent canary search stalls too (#216); a stall the
    canary does not reproduce must stay a plain "directory error".
    """

    @pytest.mark.parametrize(
        "exc",
        [_receive_timeout, lambda: LDAPResponseTimeoutError("no response")],
        ids=["receive-timeout", "response-timeout"],
    )
    def test_stall_during_service_bind_is_no_server_reachable(
        self, exc: Callable[[], Exception]
    ) -> None:
        factory = _stall_factory(
            _make_factory(_memberof_entries()), on="service-bind", exc=exc
        )
        auth = authenticate(
            settings=_base_view(),
            username="alice",
            password=_USER_PW,
            bind_password=_SVC_PW,
            connection_factory=factory,
        )
        state = resolve_directory_state(
            settings=_base_view(),
            username="alice",
            bind_password=_SVC_PW,
            connection_factory=factory,
        )
        for result in (auth, state):
            assert result.outcome is LdapOutcome.SERVER_UNREACHABLE
            assert result.detail == DETAIL_NO_SERVER_REACHABLE

    def test_hang_after_the_service_bind_is_no_server_reachable(self) -> None:
        """Bind-then-hang: every search stalls, the canary included (#216)."""
        factory = _stall_factory(_make_factory(_memberof_entries()), on="search")
        auth = authenticate(
            settings=_base_view(),
            username="alice",
            password=_USER_PW,
            bind_password=_SVC_PW,
            connection_factory=factory,
        )
        state = resolve_directory_state(
            settings=_base_view(),
            username="alice",
            bind_password=_SVC_PW,
            connection_factory=factory,
        )
        for result in (auth, state):
            assert result.outcome is LdapOutcome.SERVER_UNREACHABLE
            assert result.detail == DETAIL_NO_SERVER_REACHABLE

    def test_the_canary_carries_nothing_a_request_chose(self) -> None:
        """The confirming search is built from configuration alone."""
        calls: list[str] = []
        inner = _request_specific_stall_factory(
            _make_factory(_memberof_entries()), calls
        )
        authenticate(
            settings=_base_view(),
            username="alice*)(uid=*",
            password=_USER_PW,
            bind_password=_SVC_PW,
            connection_factory=inner,
        )
        assert calls[-1] == "(objectClass=*)"
        assert "alice" not in calls[-1]

    def test_a_request_specific_stall_stays_a_directory_error(self) -> None:
        """A slow crafted search the canary does not reproduce never opens it."""
        factory = _request_specific_stall_factory(_make_factory(_memberof_entries()))
        auth = authenticate(
            settings=_base_view(),
            username="alice",
            password=_USER_PW,
            bind_password=_SVC_PW,
            connection_factory=factory,
        )
        state = resolve_directory_state(
            settings=_base_view(),
            username="alice",
            bind_password=_SVC_PW,
            connection_factory=factory,
        )
        for result in (auth, state):
            assert result.outcome is LdapOutcome.SERVER_UNREACHABLE
            assert result.detail == "directory error"

    def test_a_non_stall_search_fault_stays_a_directory_error(self) -> None:
        factory = _stall_factory(
            _make_factory(_memberof_entries()),
            on="search",
            exc=lambda: LDAPException("bad packet"),
        )
        auth = authenticate(
            settings=_base_view(),
            username="alice",
            password=_USER_PW,
            bind_password=_SVC_PW,
            connection_factory=factory,
        )
        state = resolve_directory_state(
            settings=_base_view(),
            username="alice",
            bind_password=_SVC_PW,
            connection_factory=factory,
        )
        for result in (auth, state):
            assert result.outcome is LdapOutcome.SERVER_UNREACHABLE
            assert result.detail == "directory error"

    def test_stall_during_the_user_bind_stays_a_directory_error(self) -> None:
        """The user bind follows a client-chosen name and password."""
        result = authenticate(
            settings=_base_view(),
            username="alice",
            password=_USER_PW,
            bind_password=_SVC_PW,
            connection_factory=_stall_factory(
                _make_factory(_memberof_entries()), on="user-bind"
            ),
        )
        assert result.outcome is LdapOutcome.SERVER_UNREACHABLE
        assert result.detail == "directory error"

    def test_a_non_stall_fault_before_the_bind_is_not_unreachable(self) -> None:
        """A protocol error is still only a "directory error"."""
        result = authenticate(
            settings=_base_view(),
            username="alice",
            password=_USER_PW,
            bind_password=_SVC_PW,
            connection_factory=_stall_factory(
                _make_factory(_memberof_entries()),
                on="service-bind",
                exc=lambda: LDAPException("bad packet"),
            ),
        )
        assert result.detail == "directory error"

    def test_a_server_that_accepts_and_never_answers(self) -> None:
        """The real default factory against a socket that accepts and stays silent.

        A local listener standing in for a hung directory (no real server is
        contacted). The receive timeout derived from ``connect_timeout`` turns the
        stall into the typed "no server reachable" result, quickly.
        """
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        accepted: list[socket.socket] = []
        stop = threading.Event()

        def accept_and_hold() -> None:
            listener.settimeout(0.2)
            while not stop.is_set():
                try:
                    accepted.append(listener.accept()[0])
                except OSError:
                    continue

        thread = threading.Thread(target=accept_and_hold, daemon=True)
        thread.start()
        try:
            view = _base_view(
                server_urls=[f"ldap://127.0.0.1:{listener.getsockname()[1]}"],
                connect_timeout_seconds=1.0,
            )
            started = time.monotonic()
            result = authenticate(
                settings=view,
                username="alice",
                password=_USER_PW,
                bind_password=_SVC_PW,
            )
            state = resolve_directory_state(
                settings=view, username="alice", bind_password=_SVC_PW
            )
            elapsed = time.monotonic() - started
        finally:
            stop.set()
            thread.join(timeout=2)
            for held in accepted:
                held.close()
            listener.close()
        assert result.outcome is LdapOutcome.SERVER_UNREACHABLE
        assert result.detail == DETAIL_NO_SERVER_REACHABLE
        assert state.outcome is LdapOutcome.SERVER_UNREACHABLE
        assert state.detail == DETAIL_NO_SERVER_REACHABLE
        assert elapsed < 10


# ---------------------------------------------------------------------------
# Account-lock attribute (#67 S10)
# ---------------------------------------------------------------------------


def _locked_entries(value: object, *, attr: str = "nsAccountLock"):
    entries = _memberof_entries()
    entries[_USER_DN][attr] = value
    return entries


class TestLockAttribute:
    @pytest.mark.parametrize("value", ["TRUE", "true", "True"])
    def test_a_locked_account_resolves_as_locked(self, value: str) -> None:
        state = resolve_directory_state(
            settings=_base_view(),
            username="alice",
            bind_password=_SVC_PW,
            connection_factory=_make_factory(_locked_entries(value)),
            lock_attribute="nsAccountLock",
        )
        assert state.outcome is LdapOutcome.LOCKED
        assert state.found is False
        assert state.groups == frozenset()

    def test_the_attribute_name_is_matched_case_insensitively(self) -> None:
        state = resolve_directory_state(
            settings=_base_view(),
            username="alice",
            bind_password=_SVC_PW,
            connection_factory=_make_factory(
                _locked_entries("TRUE", attr="nsaccountlock")
            ),
            lock_attribute="nsAccountLock",
        )
        assert state.outcome is LdapOutcome.LOCKED

    @pytest.mark.parametrize("value", ["FALSE", "false", "", "no"])
    def test_other_values_do_not_lock(self, value: str) -> None:
        state = resolve_directory_state(
            settings=_base_view(),
            username="alice",
            bind_password=_SVC_PW,
            connection_factory=_make_factory(_locked_entries(value)),
            lock_attribute="nsAccountLock",
        )
        assert state.outcome is LdapOutcome.AUTHENTICATED

    def test_an_absent_attribute_changes_nothing(self) -> None:
        """A bind account that cannot read the flag simply never sees it."""
        state = resolve_directory_state(
            settings=_base_view(),
            username="alice",
            bind_password=_SVC_PW,
            connection_factory=_make_factory(_memberof_entries()),
            lock_attribute="nsAccountLock",
        )
        assert state.outcome is LdapOutcome.AUTHENTICATED

    def test_an_empty_attribute_disables_the_check(self) -> None:
        state = resolve_directory_state(
            settings=_base_view(),
            username="alice",
            bind_password=_SVC_PW,
            connection_factory=_make_factory(_locked_entries("TRUE")),
            lock_attribute="",
        )
        assert state.outcome is LdapOutcome.AUTHENTICATED

    def test_the_default_comes_from_settings(self) -> None:
        """No explicit argument and no app context: ``nsAccountLock``."""
        state = resolve_directory_state(
            settings=_base_view(),
            username="alice",
            bind_password=_SVC_PW,
            connection_factory=_make_factory(_locked_entries("TRUE")),
        )
        assert state.outcome is LdapOutcome.LOCKED

    def test_the_lock_attribute_is_requested_by_name(self) -> None:
        """It is operational: ``*`` alone does not return it."""
        seen: list[object] = []

        def spying(pool: object, user: str | None, password: str | None):
            conn = _make_factory(_memberof_entries())(pool, user, password)
            real = conn.search

            def search(*args: object, **kwargs: object) -> object:
                seen.append(kwargs.get("attributes"))
                return real(*args, **kwargs)

            conn.search = search  # type: ignore[method-assign]
            return conn

        resolve_directory_state(
            settings=_base_view(),
            username="alice",
            bind_password=_SVC_PW,
            connection_factory=spying,
            lock_attribute="nsAccountLock",
        )
        assert seen[0] == ["*", "nsAccountLock"]
        seen.clear()
        resolve_directory_state(
            settings=_base_view(),
            username="alice",
            bind_password=_SVC_PW,
            connection_factory=spying,
            lock_attribute="",
        )
        assert seen[0] == "*"

    def test_a_locked_account_cannot_log_in_and_looks_like_a_wrong_password(
        self,
    ) -> None:
        locked = authenticate(
            settings=_base_view(),
            username="alice",
            password=_USER_PW,
            bind_password=_SVC_PW,
            connection_factory=_make_factory(_locked_entries("TRUE")),
            lock_attribute="nsAccountLock",
        )
        wrong = authenticate(
            settings=_base_view(),
            username="alice",
            password="wrong-password",
            bind_password=_SVC_PW,
            connection_factory=_make_factory(_memberof_entries()),
            lock_attribute="nsAccountLock",
        )
        assert locked.outcome is LdapOutcome.INVALID_CREDENTIALS
        assert locked.outcome is wrong.outcome
        assert locked.authenticated is False
        assert locked.groups == frozenset()
