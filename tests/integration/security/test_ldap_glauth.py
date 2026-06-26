"""Integration tests for LDAP authentication against a real glauth directory.

These tests require a live glauth server.  They are gated behind the
``ldap_integration`` marker and a socket probe: when the server is unreachable
every test is skipped cleanly.  Set the ``TLM_TEST_LDAP_URL`` environment
variable to point at your glauth instance; the default assumes the server runs
on the same machine as the test runner.

The directory uses example.com placeholder coordinates throughout; nothing here
identifies a real corporate domain, lab IP address, or private network.

Test groups
-----------
1. Role matrix  -- authenticate() + map_groups_to_role() against every seeded user
2. End-to-end login -- web /login handler with LDAP enabled in the DB
3. Failover -- dead-first server_urls list; second server must succeed
4. Session re-evaluation -- role change and revoke paths via real directory
5. Bind secret -- encrypted-at-rest / decrypted-at-use end-to-end
6. TLS -- skipped when no LDAPS port is reachable
"""

from __future__ import annotations

import datetime
import os
import socket
import time
from dataclasses import replace
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from tests.conftest import seed_admin
from timelapse_manager.db.models import Session as SessionRow
from timelapse_manager.db.models import User
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.security.ldap_directory import (
    LdapOutcome,
    authenticate,
    map_groups_to_role,
)
from timelapse_manager.security.ldap_settings_service import (
    LdapSettingsUpdate,
    LdapSettingsView,
    resolve_bind_password,
    update_settings,
)

# ---------------------------------------------------------------------------
# Directory coordinates -- example.com placeholders, safe for a public repo.
# These match the seeded glauth directory exactly.
# ---------------------------------------------------------------------------

_SEARCH_BASE = "dc=example,dc=com"
_BIND_DN = "cn=svc,ou=svcaccts,ou=users,dc=example,dc=com"
_BIND_PW = "Passw0rd!"
_USERNAME_ATTR = "cn"
_DISPLAY_ATTR = "cn"
_MEMBERSHIP_MODE = "memberof"
_ADMIN_GROUP = "ou=admins,ou=groups,dc=example,dc=com"
_OPERATOR_GROUP = "ou=operators,ou=groups,dc=example,dc=com"
_VIEWER_GROUP = "ou=viewers,ou=groups,dc=example,dc=com"

# Seeded users; all share the same password.
_USER_PW = "Passw0rd!"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _ldap_url() -> str:
    """Return the LDAP URL from the environment (or its documented default)."""
    return os.environ.get("TLM_TEST_LDAP_URL", "ldap://127.0.0.1:3893")


def _parse_host_port(url: str) -> tuple[str, int]:
    """Parse host and port from an ldap:// or ldaps:// URL."""
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (636 if parsed.scheme == "ldaps" else 389)
    return host, port


def _is_port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """Return True when a TCP connection to host:port succeeds within timeout."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def ldap_live_url() -> str:
    """Return the glauth URL and skip the entire module if the port is closed.

    Skipping at module scope means one probe check gates all tests in this
    file -- no per-test overhead and a single, clear skip reason.
    """
    url = _ldap_url()
    host, port = _parse_host_port(url)
    if not _is_port_open(host, port):
        pytest.skip(
            f"LDAP server not reachable at {host}:{port} "
            f"(set TLM_TEST_LDAP_URL to override; default is ldap://127.0.0.1:3893)"
        )
    return url


@pytest.fixture()
def glauth_view(ldap_live_url: str) -> LdapSettingsView:
    """Return a LdapSettingsView wired to the live glauth server.

    The bind_password field is the mask sentinel (as load_settings always
    returns); the real plaintext is passed separately to authenticate().
    """
    return LdapSettingsView(
        enabled=True,
        server_urls=[ldap_live_url],
        tls_mode="none",
        bind_dn=_BIND_DN,
        bind_password="***",
        bind_password_set=True,
        search_base=_SEARCH_BASE,
        search_filter="",
        username_attribute=_USERNAME_ATTR,
        display_name_attribute=_DISPLAY_ATTR,
        membership_mode=_MEMBERSHIP_MODE,
        nested_groups=False,
        admin_group_dn=_ADMIN_GROUP,
        operator_group_dn=_OPERATOR_GROUP,
        viewer_group_dn=_VIEWER_GROUP,
    )


def _seed_glauth_settings(
    client: TestClient,
    ldap_live_url: str,
    *,
    enabled: bool = True,
    admin_group_dn: str = _ADMIN_GROUP,
    operator_group_dn: str = _OPERATOR_GROUP,
    viewer_group_dn: str = _VIEWER_GROUP,
) -> None:
    """Write the glauth LDAP settings into the running client's database."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        update_settings(
            db,
            LdapSettingsUpdate(
                enabled=enabled,
                server_urls=[ldap_live_url],
                tls_mode="none",
                tls_ca_cert_path=None,
                bind_dn=_BIND_DN,
                bind_password=_BIND_PW,
                search_base=_SEARCH_BASE,
                search_filter="",
                group_search_base=None,
                username_attribute=_USERNAME_ATTR,
                display_name_attribute=_DISPLAY_ATTR,
                membership_mode=_MEMBERSHIP_MODE,
                nested_groups=False,
                admin_group_dn=admin_group_dn,
                admin_group_filter=None,
                operator_group_dn=operator_group_dn,
                operator_group_filter=None,
                viewer_group_dn=viewer_group_dn,
                viewer_group_filter=None,
            ),
        )


def _post_login(client: TestClient, username: str, password: str):  # noqa: ANN201
    return client.post(
        "/login",
        data={"username": username, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )


def _find_user(username: str) -> User | None:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        return db.query(User).filter(User.username == username).one_or_none()


def _role_of(username: str) -> str:
    user = _find_user(username)
    assert user is not None, f"user {username!r} not found in DB"
    return str(user.role)


def _backdate_revalidation(username: str, *, minutes_ago: int) -> None:
    """Push last_revalidated_at into the past so re-eval fires on the next request."""
    past = datetime.datetime.now(datetime.UTC).replace(
        tzinfo=None
    ) - datetime.timedelta(minutes=minutes_ago)
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        user = db.query(User).filter(User.username == username).one()
        row = (
            db.query(SessionRow)
            .filter(SessionRow.user_id == user.id, SessionRow.revoked.is_(False))
            .one()
        )
        row.last_revalidated_at = past


pytestmark = pytest.mark.ldap_integration


# ---------------------------------------------------------------------------
# 1. Role matrix -- connector-level authenticate() + map_groups_to_role()
# ---------------------------------------------------------------------------


class TestRoleMatrix:
    """Verify every seeded user maps to the expected role (or is denied).

    These tests call the connector directly with a real ldap3 connection; they
    do NOT go through the web layer or the session system.
    """

    def test_alice_authenticates_as_admin(self, glauth_view: LdapSettingsView) -> None:
        """alice is in the admins group and must resolve to the admin role."""
        result = authenticate(
            settings=glauth_view,
            username="alice",
            password=_USER_PW,
            bind_password=_BIND_PW,
        )
        assert result.outcome is LdapOutcome.AUTHENTICATED
        assert result.authenticated is True
        role = map_groups_to_role(
            result.groups,
            admin_group_dn=glauth_view.admin_group_dn,
            operator_group_dn=glauth_view.operator_group_dn,
            viewer_group_dn=glauth_view.viewer_group_dn,
        )
        assert role == "admin"

    def test_bob_authenticates_as_operator(self, glauth_view: LdapSettingsView) -> None:
        """bob is in the operators group and must resolve to the operator role."""
        result = authenticate(
            settings=glauth_view,
            username="bob",
            password=_USER_PW,
            bind_password=_BIND_PW,
        )
        assert result.outcome is LdapOutcome.AUTHENTICATED
        role = map_groups_to_role(
            result.groups,
            admin_group_dn=glauth_view.admin_group_dn,
            operator_group_dn=glauth_view.operator_group_dn,
            viewer_group_dn=glauth_view.viewer_group_dn,
        )
        assert role == "operator"

    def test_carol_authenticates_as_viewer(self, glauth_view: LdapSettingsView) -> None:
        """carol is in the viewers group and must resolve to the viewer role."""
        result = authenticate(
            settings=glauth_view,
            username="carol",
            password=_USER_PW,
            bind_password=_BIND_PW,
        )
        assert result.outcome is LdapOutcome.AUTHENTICATED
        role = map_groups_to_role(
            result.groups,
            admin_group_dn=glauth_view.admin_group_dn,
            operator_group_dn=glauth_view.operator_group_dn,
            viewer_group_dn=glauth_view.viewer_group_dn,
        )
        assert role == "viewer"

    def test_dave_authenticates_but_maps_to_no_role(
        self, glauth_view: LdapSettingsView
    ) -> None:
        """dave is a valid directory user but belongs to no configured role group.

        The connector must return AUTHENTICATED (the credentials are valid);
        map_groups_to_role must return None (the denial is at the role layer).
        """
        result = authenticate(
            settings=glauth_view,
            username="dave",
            password=_USER_PW,
            bind_password=_BIND_PW,
        )
        # dave IS in the directory -- his credentials are correct.
        assert result.outcome is LdapOutcome.AUTHENTICATED
        assert result.authenticated is True
        # But none of his groups match a configured role group.
        role = map_groups_to_role(
            result.groups,
            admin_group_dn=glauth_view.admin_group_dn,
            operator_group_dn=glauth_view.operator_group_dn,
            viewer_group_dn=glauth_view.viewer_group_dn,
        )
        assert role is None

    def test_wrong_password_returns_invalid_credentials(
        self, glauth_view: LdapSettingsView
    ) -> None:
        """A correct username with a wrong password must return INVALID_CREDENTIALS."""
        result = authenticate(
            settings=glauth_view,
            username="alice",
            password="completely-wrong-password",
            bind_password=_BIND_PW,
        )
        assert result.outcome is LdapOutcome.INVALID_CREDENTIALS
        assert result.authenticated is False

    def test_unknown_user_returns_no_such_user(
        self, glauth_view: LdapSettingsView
    ) -> None:
        """A username that does not exist in the directory must return NO_SUCH_USER."""
        result = authenticate(
            settings=glauth_view,
            username="ghost_user_does_not_exist",
            password=_USER_PW,
            bind_password=_BIND_PW,
        )
        assert result.outcome is LdapOutcome.NO_SUCH_USER
        assert result.authenticated is False

    def test_empty_password_returns_invalid_credentials(
        self, glauth_view: LdapSettingsView
    ) -> None:
        """An empty password is rejected before any bind (LDAP anon-bind risk)."""
        result = authenticate(
            settings=glauth_view,
            username="alice",
            password="",
            bind_password=_BIND_PW,
        )
        assert result.outcome is LdapOutcome.INVALID_CREDENTIALS
        assert result.authenticated is False


# ---------------------------------------------------------------------------
# 2. End-to-end login through the web /login handler
# ---------------------------------------------------------------------------


class TestEndToEndLogin:
    """Verify the full login flow: DB-seeded LDAP settings -> JIT provisioning.

    The bind password is stored encrypted in the DB (via update_settings) and
    decrypted at bind time (via resolve_bind_password).  Every test in this
    class exercises the complete round-trip: settings write, login POST, DB
    inspection.
    """

    def test_alice_is_jit_provisioned_on_first_login(
        self, web_client: TestClient, ldap_live_url: str
    ) -> None:
        """alice's first login must create a User row with auth_source='ldap'."""
        seed_admin(web_client)
        _seed_glauth_settings(web_client, ldap_live_url)

        resp = _post_login(web_client, "alice", _USER_PW)
        assert resp.status_code == 303, (
            f"Expected 303, got {resp.status_code}: {resp.text[:200]}"
        )
        assert "tlm_session" in resp.headers.get("set-cookie", "")

        user = _find_user("alice")
        assert user is not None
        assert user.auth_source == "ldap"
        # Directory users carry no local password hash.
        assert user.password_hash is None
        assert user.role == "admin"
        assert user.enabled is True

    def test_local_admin_coexists_with_ldap(
        self, web_client: TestClient, ldap_live_url: str
    ) -> None:
        """A local admin account must still authenticate with the directory on."""
        seed_admin(web_client)
        _seed_glauth_settings(web_client, ldap_live_url)

        resp = _post_login(web_client, "admin", "AdminP@ssw0rd1234")
        assert resp.status_code == 303
        assert "tlm_session" in resp.headers.get("set-cookie", "")

    def test_dave_is_refused_because_no_mapped_role(
        self, web_client: TestClient, ldap_live_url: str
    ) -> None:
        """dave authenticates in the directory but maps to no role -- must be denied."""
        seed_admin(web_client)
        _seed_glauth_settings(web_client, ldap_live_url)

        resp = _post_login(web_client, "dave", _USER_PW)
        assert resp.status_code == 401
        # dave must never be provisioned.
        assert _find_user("dave") is None

    def test_second_login_does_not_create_duplicate_user(
        self, web_client: TestClient, ldap_live_url: str
    ) -> None:
        """A second login by alice must reuse the existing User row."""
        seed_admin(web_client)
        _seed_glauth_settings(web_client, ldap_live_url)

        # First login.
        resp = _post_login(web_client, "alice", _USER_PW)
        assert resp.status_code == 303

        # Clear the cookie so the second POST is treated as a fresh login.
        web_client.cookies.clear()

        # Second login.
        resp = _post_login(web_client, "alice", _USER_PW)
        assert resp.status_code == 303

        # Only one alice row must exist.
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            count = db.query(User).filter(User.username == "alice").count()
        assert count == 1

    def test_alice_role_refreshed_if_mapping_changed_at_re_login(
        self, web_client: TestClient, ldap_live_url: str
    ) -> None:
        """A changed group mapping takes effect on alice's next login."""
        seed_admin(web_client)

        # First login: alice maps to admin.
        _seed_glauth_settings(web_client, ldap_live_url, admin_group_dn=_ADMIN_GROUP)
        resp = _post_login(web_client, "alice", _USER_PW)
        assert resp.status_code == 303
        assert _role_of("alice") == "admin"

        # Clear cookie; remap alice's real group (admins) to operator.
        web_client.cookies.clear()
        _seed_glauth_settings(
            web_client,
            ldap_live_url,
            # Swap: what was admin is now operator, clearing the real admin slot.
            admin_group_dn="ou=nonexistent,ou=groups,dc=example,dc=com",
            operator_group_dn=_ADMIN_GROUP,  # alice's actual group
            viewer_group_dn=_VIEWER_GROUP,
        )
        resp = _post_login(web_client, "alice", _USER_PW)
        assert resp.status_code == 303
        # alice's group is now mapped to operator; her stored role must be updated.
        assert _role_of("alice") == "operator"

    def test_bob_and_carol_provisioned_with_correct_roles(
        self, web_client: TestClient, ldap_live_url: str
    ) -> None:
        """bob→operator and carol→viewer must both provision correctly."""
        seed_admin(web_client)
        _seed_glauth_settings(web_client, ldap_live_url)

        resp = _post_login(web_client, "bob", _USER_PW)
        assert resp.status_code == 303
        assert _role_of("bob") == "operator"

        web_client.cookies.clear()
        resp = _post_login(web_client, "carol", _USER_PW)
        assert resp.status_code == 303
        assert _role_of("carol") == "viewer"


# ---------------------------------------------------------------------------
# 3. Failover -- dead server first in the list
# ---------------------------------------------------------------------------


class TestFailover:
    """The connector must try all servers in the pool before giving up."""

    def test_dead_first_server_falls_through_to_live_server(
        self, glauth_view: LdapSettingsView, ldap_live_url: str
    ) -> None:
        """Authentication succeeds when the first URL is dead but the second is live."""
        # Build a view with the dead server first and the live server second.
        # The dead port is a closed localhost port; it refuses fast.
        view_with_failover = LdapSettingsView(
            enabled=True,
            server_urls=["ldap://127.0.0.1:9999", ldap_live_url],
            tls_mode="none",
            bind_dn=_BIND_DN,
            bind_password="***",
            bind_password_set=True,
            search_base=_SEARCH_BASE,
            search_filter="",
            username_attribute=_USERNAME_ATTR,
            display_name_attribute=_DISPLAY_ATTR,
            membership_mode=_MEMBERSHIP_MODE,
            nested_groups=False,
            admin_group_dn=_ADMIN_GROUP,
            operator_group_dn=_OPERATOR_GROUP,
            viewer_group_dn=_VIEWER_GROUP,
        )
        result = authenticate(
            settings=view_with_failover,
            username="alice",
            password=_USER_PW,
            bind_password=_BIND_PW,
        )
        assert result.outcome is LdapOutcome.AUTHENTICATED, (
            f"Failover must succeed via the second server; got {result.outcome}"
        )
        role = map_groups_to_role(
            result.groups,
            admin_group_dn=view_with_failover.admin_group_dn,
            operator_group_dn=view_with_failover.operator_group_dn,
            viewer_group_dn=view_with_failover.viewer_group_dn,
        )
        assert role == "admin"

    def test_all_dead_servers_return_unreachable_in_bounded_time(
        self, glauth_view: LdapSettingsView
    ) -> None:
        """Every server unreachable must yield SERVER_UNREACHABLE, fast.

        Regression guard for an availability defect: with no per-server connect
        timeout, an all-unreachable pool waited on the OS default connect timeout
        (tens of seconds) before the connector could return, stalling every login
        while the directory was down. With a short connect_timeout the worst case
        is bounded at roughly ``connect_timeout * len(server_urls)``.

        Two closed localhost ports are used (not the live glauth URL). The time
        budget below is far under the old multi-tens-of-seconds hang; if the
        bound regresses this test fails rather than hanging the suite (the module
        is still env-gated, so it skips cleanly without a reachable directory).
        """
        connect_timeout = 1.0
        num_servers = 2
        dead_only = replace(
            glauth_view,
            server_urls=["ldap://127.0.0.1:9", "ldap://127.0.0.1:10"],
            connect_timeout_seconds=connect_timeout,
        )

        start = time.monotonic()
        result = authenticate(
            settings=dead_only,
            username="alice",
            password=_USER_PW,
            bind_password=_BIND_PW,
        )
        elapsed = time.monotonic() - start

        assert result.outcome is LdapOutcome.SERVER_UNREACHABLE, (
            f"All servers down must map to SERVER_UNREACHABLE; got {result.outcome}"
        )
        assert result.authenticated is False
        # Generous ceiling over the worst-case bound to absorb scheduling jitter,
        # while still far below the unbounded OS-default hang the fix prevents.
        budget = connect_timeout * num_servers + 8.0
        assert elapsed < budget, (
            f"All-dead pool must fail fast; took {elapsed:.1f}s (budget {budget:.1f}s)"
        )


# ---------------------------------------------------------------------------
# 4. Session re-evaluation against the real directory
# ---------------------------------------------------------------------------


class TestSessionRevalidation:
    """The production re-evaluation hook fires a real directory lookup.

    These tests do NOT patch resolve_directory_state -- they hit glauth for
    both the initial login and the re-evaluation round-trip.  The interval
    gate is bypassed by backdating last_revalidated_at.
    """

    def test_role_update_applied_when_mapping_changes_mid_session(
        self, web_client: TestClient, ldap_live_url: str
    ) -> None:
        """Re-evaluation must update the stored role when the group mapping changes.

        alice logs in as admin.  We remap the admin group to a non-existent DN
        and reassign alice's real group (admins) as the operator group.  On the
        next request (after backdating), re-eval must demote alice to operator.
        """
        seed_admin(web_client)
        _seed_glauth_settings(web_client, ldap_live_url)

        resp = _post_login(web_client, "alice", _USER_PW)
        assert resp.status_code == 303
        assert _role_of("alice") == "admin"

        # Change the mapping: alice's actual group now maps to operator.
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            update_settings(
                db,
                LdapSettingsUpdate(
                    enabled=True,
                    server_urls=[ldap_live_url],
                    tls_mode="none",
                    tls_ca_cert_path=None,
                    bind_dn=_BIND_DN,
                    # Sentinel: keep the stored ciphertext, do not re-encrypt.
                    bind_password="***",
                    search_base=_SEARCH_BASE,
                    search_filter="",
                    group_search_base=None,
                    username_attribute=_USERNAME_ATTR,
                    display_name_attribute=_DISPLAY_ATTR,
                    membership_mode=_MEMBERSHIP_MODE,
                    nested_groups=False,
                    admin_group_dn="ou=nonexistent,ou=groups,dc=example,dc=com",
                    admin_group_filter=None,
                    operator_group_dn=_ADMIN_GROUP,  # alice's actual group -> operator
                    operator_group_filter=None,
                    viewer_group_dn=_VIEWER_GROUP,
                    viewer_group_filter=None,
                ),
            )

        _backdate_revalidation("alice", minutes_ago=20)

        # The next authenticated request triggers re-evaluation.
        resp = web_client.get(
            "/", headers={"Accept": "text/html"}, follow_redirects=False
        )
        assert resp.status_code == 200, (
            f"Session must remain live after role update; got {resp.status_code}"
        )
        assert _role_of("alice") == "operator"

    def test_session_revoked_when_user_maps_to_no_role(
        self, web_client: TestClient, ldap_live_url: str
    ) -> None:
        """Re-evaluation must revoke the session when the user maps to no role group.

        alice logs in as admin.  We clear all three group-to-role mappings so
        alice's groups match nothing.  On the next request (after backdating),
        re-eval must revoke the session.
        """
        seed_admin(web_client)
        _seed_glauth_settings(web_client, ldap_live_url)

        resp = _post_login(web_client, "alice", _USER_PW)
        assert resp.status_code == 303

        # Remove all configured role groups so alice maps to nothing.
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            update_settings(
                db,
                LdapSettingsUpdate(
                    enabled=True,
                    server_urls=[ldap_live_url],
                    tls_mode="none",
                    tls_ca_cert_path=None,
                    bind_dn=_BIND_DN,
                    bind_password="***",
                    search_base=_SEARCH_BASE,
                    search_filter="",
                    group_search_base=None,
                    username_attribute=_USERNAME_ATTR,
                    display_name_attribute=_DISPLAY_ATTR,
                    membership_mode=_MEMBERSHIP_MODE,
                    nested_groups=False,
                    admin_group_dn=None,
                    admin_group_filter=None,
                    operator_group_dn=None,
                    operator_group_filter=None,
                    viewer_group_dn=None,
                    viewer_group_filter=None,
                ),
            )

        _backdate_revalidation("alice", minutes_ago=20)

        resp = web_client.get(
            "/", headers={"Accept": "text/html"}, follow_redirects=False
        )
        # Session revoked -> redirect to /login.
        assert resp.status_code == 303, (
            f"Revoked session must redirect; got {resp.status_code}"
        )
        assert "/login" in resp.headers.get("location", "")

    def test_local_session_is_not_reevaluated(
        self, web_client: TestClient, ldap_live_url: str
    ) -> None:
        """The local admin session must never be submitted to the directory for re-eval.

        Even after backdating and enabling LDAP, a request on the local admin
        session must succeed (no revoke, no role change from the directory).
        """
        seed_admin(web_client)
        _seed_glauth_settings(web_client, ldap_live_url)

        resp = _post_login(web_client, "admin", "AdminP@ssw0rd1234")
        assert resp.status_code == 303

        _backdate_revalidation("admin", minutes_ago=20)

        # Re-evaluation must be a no-op for local sessions.
        resp = web_client.get(
            "/", headers={"Accept": "text/html"}, follow_redirects=False
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 5. Bind secret -- encrypted-at-rest / decrypted-at-use
# ---------------------------------------------------------------------------


class TestBindSecret:
    """Verify the full encrypt/decrypt lifecycle of the bind password.

    update_settings() encrypts the plaintext before storage.
    resolve_bind_password() decrypts it at use time.
    A successful login proves the decrypted password was usable for the
    service bind (the connector would not have been able to search the directory
    without it).
    """

    def test_stored_bind_password_is_not_plaintext(
        self, web_client: TestClient, ldap_live_url: str
    ) -> None:
        """The DB row must not store the bind password in plaintext."""
        from timelapse_manager.db.models import LdapSettings

        seed_admin(web_client)
        _seed_glauth_settings(web_client, ldap_live_url)

        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            row = db.get(LdapSettings, 1)
            assert row is not None
            stored = row.bind_password
            # The ciphertext must not equal the plaintext password.
            assert stored != _BIND_PW, "Bind password must be encrypted at rest"
            # The versioned envelope prefix confirms encryption was applied.
            stored_prefix = (stored or "")[:10]
            assert stored is not None and stored.startswith("enc:v1:"), (
                f"Expected enc:v1: prefix; got {stored_prefix!r}"
            )

    def test_resolve_bind_password_returns_plaintext(
        self, web_client: TestClient, ldap_live_url: str
    ) -> None:
        """resolve_bind_password() must decrypt and return the original plaintext."""
        seed_admin(web_client)
        _seed_glauth_settings(web_client, ldap_live_url)

        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            decrypted = resolve_bind_password(db)
        assert decrypted == _BIND_PW

    def test_login_succeeds_proving_decrypt_at_use_works_end_to_end(
        self, web_client: TestClient, ldap_live_url: str
    ) -> None:
        """A successful LDAP login proves the full encrypt-store-decrypt-bind cycle.

        The connector receives the decrypted password from resolve_bind_password,
        binds as the service account, searches for alice, and returns AUTHENTICATED.
        If any step fails the login returns a non-303 status.
        """
        seed_admin(web_client)
        _seed_glauth_settings(web_client, ldap_live_url)

        resp = _post_login(web_client, "alice", _USER_PW)
        assert resp.status_code == 303, (
            f"End-to-end bind-secret cycle failed: expected 303, got {resp.status_code}"
        )

    def test_sentinel_password_preserves_stored_secret(
        self, web_client: TestClient, ldap_live_url: str
    ) -> None:
        """Submitting the mask sentinel must leave the stored ciphertext intact."""
        seed_admin(web_client)
        _seed_glauth_settings(web_client, ldap_live_url)

        # Re-save settings with the sentinel; the stored secret must survive.
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            update_settings(
                db,
                LdapSettingsUpdate(
                    enabled=True,
                    server_urls=[ldap_live_url],
                    tls_mode="none",
                    tls_ca_cert_path=None,
                    bind_dn=_BIND_DN,
                    bind_password="***",  # sentinel: keep the stored secret
                    search_base=_SEARCH_BASE,
                    search_filter="",
                    group_search_base=None,
                    username_attribute=_USERNAME_ATTR,
                    display_name_attribute=_DISPLAY_ATTR,
                    membership_mode=_MEMBERSHIP_MODE,
                    nested_groups=False,
                    admin_group_dn=_ADMIN_GROUP,
                    admin_group_filter=None,
                    operator_group_dn=_OPERATOR_GROUP,
                    operator_group_filter=None,
                    viewer_group_dn=_VIEWER_GROUP,
                    viewer_group_filter=None,
                ),
            )

        # The decrypted secret must still be the original password.
        with session_scope(ctx.session_factory) as db:
            decrypted = resolve_bind_password(db)
        assert decrypted == _BIND_PW

        # And a login must still succeed.
        resp = _post_login(web_client, "alice", _USER_PW)
        assert resp.status_code == 303


# ---------------------------------------------------------------------------
# 6. TLS -- optional / best-effort
# ---------------------------------------------------------------------------


class TestTls:
    """TLS / LDAPS connectivity tests.

    glauth is currently configured for plain LDAP only; LDAPS is not enabled.
    This test is skipped if the LDAPS port (636 by default) is closed, which
    it will be in most environments running the reference glauth config.
    The connector's TLS wiring is covered by unit tests; this class exists
    to catch regressions if TLS is ever enabled on the test directory.
    """

    def test_ldaps_authentication_if_port_reachable(
        self, glauth_view: LdapSettingsView, ldap_live_url: str
    ) -> None:
        """Authenticate via LDAPS when the LDAPS port is reachable; skip otherwise."""
        parsed = urlparse(ldap_live_url)
        host = parsed.hostname or "127.0.0.1"
        ldaps_port = 636

        if not _is_port_open(host, ldaps_port, timeout=1.5):
            pytest.skip(
                f"LDAPS port {host}:{ldaps_port} is not reachable "
                "(glauth is configured for plain LDAP only in the reference setup; "
                "the connector's TLS wiring is covered by unit tests)"
            )

        ldaps_url = f"ldaps://{host}:{ldaps_port}"
        view_tls = LdapSettingsView(
            enabled=True,
            server_urls=[ldaps_url],
            tls_mode="ldaps",
            bind_dn=_BIND_DN,
            bind_password="***",
            bind_password_set=True,
            search_base=_SEARCH_BASE,
            search_filter="",
            username_attribute=_USERNAME_ATTR,
            display_name_attribute=_DISPLAY_ATTR,
            membership_mode=_MEMBERSHIP_MODE,
            nested_groups=False,
            admin_group_dn=_ADMIN_GROUP,
            operator_group_dn=_OPERATOR_GROUP,
            viewer_group_dn=_VIEWER_GROUP,
        )
        result = authenticate(
            settings=view_tls,
            username="alice",
            password=_USER_PW,
            bind_password=_BIND_PW,
        )
        assert result.outcome is LdapOutcome.AUTHENTICATED
