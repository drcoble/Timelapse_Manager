"""Integration tests for the streamlined local-or-directory login flow.

The directory connector itself is unit-tested elsewhere (ldap3 ``MOCK_SYNC``).
Here the connector boundary is monkeypatched so these tests drive the *login
handler's* resolution logic deterministically: local-first then directory,
just-in-time provisioning with a group-mapped role, generic non-enumerating
failures, the disabled-directory fall-through, and throttle accounting across
both paths. No live directory and no real ldap3 connection are involved.

A monkeypatched ``ldap_authenticate`` stands in for the connector and returns a
canned :class:`LdapAuthResult`; the LDAP settings row (which holds the role group
DNs) is seeded via the real settings service so role mapping runs against
realistic data.
"""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

import timelapse_manager.security.session_revalidation as revalidation
import timelapse_manager.web.routers.auth as routers
from tests.conftest import seed_admin
from timelapse_manager.db.models import Session as SessionRow
from timelapse_manager.db.models import User
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.security.ldap_directory import (
    LdapAuthResult,
    LdapDirectoryState,
    LdapOutcome,
)
from timelapse_manager.security.ldap_settings_service import (
    LdapSettingsUpdate,
    update_settings,
)

# Placeholder directory tree; never a real domain.
_ADMIN_GROUP = "cn=admins,ou=groups,dc=example,dc=com"
_OPERATOR_GROUP = "cn=operators,ou=groups,dc=example,dc=com"
_VIEWER_GROUP = "cn=viewers,ou=groups,dc=example,dc=com"

_LDAP_USER = "ldapalice"
_LDAP_PW = "directory-secret-pw"


def _seed_ldap_settings(client: TestClient, *, enabled: bool = True) -> None:
    """Seed an LDAP settings row into the running client's database."""
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
                bind_password="service-bind-pw",
                search_base="ou=people,dc=example,dc=com",
                search_filter="(objectClass=inetOrgPerson)",
                group_search_base=None,
                username_attribute="uid",
                display_name_attribute="cn",
                membership_mode="memberof",
                nested_groups=False,
                admin_group_dn=_ADMIN_GROUP,
                admin_group_filter=None,
                operator_group_dn=_OPERATOR_GROUP,
                operator_group_filter=None,
                viewer_group_dn=_VIEWER_GROUP,
                viewer_group_filter=None,
            ),
        )


def _fake_connector(result: LdapAuthResult):
    """Return a stand-in for the connector that always yields ``result``."""

    def _connector(*, settings, username, password, bind_password):  # noqa: ANN001
        return result

    return _connector


def _authenticated(groups: frozenset[str]) -> LdapAuthResult:
    return LdapAuthResult(
        outcome=LdapOutcome.AUTHENTICATED,
        dn=f"uid={_LDAP_USER},ou=people,dc=example,dc=com",
        display_name="LDAP Alice",
        groups=groups,
    )


def _post_login(client: TestClient, username: str, password: str):
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
    """Return the stored role for ``username`` (assert the row exists)."""
    user = _find_user(username)
    assert user is not None
    return user.role


def _backdate_revalidation(username: str, *, minutes_ago: int) -> None:
    """Make ``username``'s live session due for re-evaluation.

    Pushes ``last_revalidated_at`` into the past so the next request crosses the
    re-evaluation interval -- the deterministic stand-in for "an interval has
    elapsed" without changing app config or sleeping.
    """
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


def _login_ldap_user(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    groups: frozenset[str],
) -> None:
    """Seed + sign in a directory user, leaving a live session on the client."""
    seed_admin(client)
    _seed_ldap_settings(client, enabled=True)
    monkeypatch.setattr(
        routers, "ldap_authenticate", _fake_connector(_authenticated(groups))
    )
    resp = _post_login(client, _LDAP_USER, _LDAP_PW)
    assert resp.status_code == 303


def _dir_state(
    outcome: LdapOutcome, groups: frozenset[str] = frozenset()
) -> LdapDirectoryState:
    return LdapDirectoryState(outcome=outcome, groups=groups)


class TestLocalStillWorks:
    def test_local_login_succeeds_with_ldap_enabled(
        self, web_client: TestClient
    ) -> None:
        """Regression: a local account authenticates even with directory on."""
        seed_admin(web_client)
        _seed_ldap_settings(web_client, enabled=True)
        resp = _post_login(web_client, "admin", "AdminP@ssw0rd1234")
        assert resp.status_code == 303
        assert "tlm_session" in resp.headers.get("set-cookie", "")

    def test_local_login_does_not_consult_directory(
        self, web_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_admin(web_client)
        _seed_ldap_settings(web_client, enabled=True)
        called: list[str] = []

        def _spy(*, settings, username, password, bind_password):  # noqa: ANN001
            called.append(username)
            raise AssertionError("directory must not be consulted for local user")

        monkeypatch.setattr(routers, "ldap_authenticate", _spy)
        resp = _post_login(web_client, "admin", "AdminP@ssw0rd1234")
        assert resp.status_code == 303
        assert called == []


class TestLdapProvisioningAndRoles:
    @pytest.mark.parametrize(
        ("groups", "expected_role"),
        [
            (frozenset({_ADMIN_GROUP}), "admin"),
            (frozenset({_OPERATOR_GROUP}), "operator"),
            (frozenset({_VIEWER_GROUP}), "viewer"),
            # Highest-privilege-wins: admin + viewer -> admin.
            (frozenset({_ADMIN_GROUP, _VIEWER_GROUP}), "admin"),
        ],
    )
    def test_valid_ldap_user_is_provisioned_with_mapped_role(
        self,
        web_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        groups: frozenset[str],
        expected_role: str,
    ) -> None:
        seed_admin(web_client)
        _seed_ldap_settings(web_client, enabled=True)
        monkeypatch.setattr(
            routers, "ldap_authenticate", _fake_connector(_authenticated(groups))
        )

        resp = _post_login(web_client, _LDAP_USER, _LDAP_PW)
        assert resp.status_code == 303
        assert "tlm_session" in resp.headers.get("set-cookie", "")

        user = _find_user(_LDAP_USER)
        assert user is not None
        assert user.auth_source == "ldap"
        assert user.password_hash is None  # directory users carry no local hash
        assert user.role == expected_role

    def test_role_is_refreshed_on_subsequent_login(
        self, web_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_admin(web_client)
        _seed_ldap_settings(web_client, enabled=True)

        monkeypatch.setattr(
            routers,
            "ldap_authenticate",
            _fake_connector(_authenticated(frozenset({_ADMIN_GROUP}))),
        )
        _post_login(web_client, _LDAP_USER, _LDAP_PW)
        assert _role_of(_LDAP_USER) == "admin"

        # Re-login from a clean slate (no carried session cookie), as a fresh
        # browser sign-in would. The directory now reports only viewer
        # membership; the role is re-derived and the account demoted.
        web_client.cookies.clear()
        monkeypatch.setattr(
            routers,
            "ldap_authenticate",
            _fake_connector(_authenticated(frozenset({_VIEWER_GROUP}))),
        )
        resp = _post_login(web_client, _LDAP_USER, _LDAP_PW)
        assert resp.status_code == 303
        assert _role_of(_LDAP_USER) == "viewer"


class TestNoMappedGroupDenied:
    def test_authenticated_but_no_mapped_group_is_denied_and_not_provisioned(
        self, web_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_admin(web_client)
        _seed_ldap_settings(web_client, enabled=True)
        # Authenticated, but in only an unrelated group.
        monkeypatch.setattr(
            routers,
            "ldap_authenticate",
            _fake_connector(
                _authenticated(frozenset({"cn=other,ou=groups,dc=example,dc=com"}))
            ),
        )
        resp = _post_login(web_client, _LDAP_USER, _LDAP_PW)
        assert resp.status_code == 401  # generic denial
        assert _find_user(_LDAP_USER) is None  # never provisioned


class TestLdapDisabled:
    def test_directory_user_rejected_when_ldap_disabled(
        self, web_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_admin(web_client)
        _seed_ldap_settings(web_client, enabled=False)
        # Even if the connector would say yes, a disabled directory must not be
        # consulted: only local accounts are accepted.
        called: list[str] = []

        def _spy(*, settings, username, password, bind_password):  # noqa: ANN001
            called.append(username)
            return _authenticated(frozenset({_ADMIN_GROUP}))

        monkeypatch.setattr(routers, "ldap_authenticate", _spy)
        resp = _post_login(web_client, _LDAP_USER, _LDAP_PW)
        assert resp.status_code == 401
        assert called == []  # disabled directory short-circuits before the bind
        assert _find_user(_LDAP_USER) is None


class TestUnreachableAndThrottle:
    def test_server_unreachable_is_generic_error(
        self, web_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_admin(web_client)
        _seed_ldap_settings(web_client, enabled=True)
        monkeypatch.setattr(
            routers,
            "ldap_authenticate",
            _fake_connector(
                LdapAuthResult(LdapOutcome.SERVER_UNREACHABLE, detail="no server")
            ),
        )
        resp = _post_login(web_client, _LDAP_USER, _LDAP_PW)
        assert resp.status_code == 401  # generic; no infra detail leaked

    def test_failed_ldap_attempts_count_toward_throttle(
        self, web_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_admin(web_client)
        _seed_ldap_settings(web_client, enabled=True)
        # Directory outage on every attempt -> each is a failed attempt.
        monkeypatch.setattr(
            routers,
            "ldap_authenticate",
            _fake_connector(
                LdapAuthResult(LdapOutcome.SERVER_UNREACHABLE, detail="no server")
            ),
        )
        ctx = get_context()
        max_failures = ctx.settings.auth.throttle_max_failures

        for _ in range(max_failures + 1):
            resp = _post_login(web_client, _LDAP_USER, _LDAP_PW)
            assert resp.status_code == 401

        # The source is now throttled: a would-be-valid directory login is still
        # rejected at the web layer before the connector is consulted.
        monkeypatch.setattr(
            routers,
            "ldap_authenticate",
            _fake_connector(_authenticated(frozenset({_ADMIN_GROUP}))),
        )
        resp = _post_login(web_client, _LDAP_USER, _LDAP_PW)
        assert resp.status_code == 401
        assert _find_user(_LDAP_USER) is None  # no session/account established


class TestSessionReevalOnRequest:
    """End-to-end re-evaluation: the production hook in ``get_session_row`` fires
    the directory re-check on a real authenticated request once the interval has
    elapsed. ``resolve_directory_state`` is patched (the connector boundary inside
    the default resolver) so the directory's *current* answer is controllable; the
    settings load and bind-password resolve stay real against the seeded row."""

    def test_removed_user_is_revoked_on_next_request(
        self, web_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _login_ldap_user(web_client, monkeypatch, frozenset({_ADMIN_GROUP}))
        _backdate_revalidation(_LDAP_USER, minutes_ago=60)
        # The directory no longer knows this account.
        monkeypatch.setattr(
            revalidation,
            "resolve_directory_state",
            lambda **_kw: _dir_state(LdapOutcome.NO_SUCH_USER),
        )
        resp = web_client.get(
            "/", headers={"Accept": "text/html"}, follow_redirects=False
        )
        # Session revoked -> a browser navigation is bounced to login.
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/login")

    def test_role_change_is_applied_on_next_request(
        self, web_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _login_ldap_user(web_client, monkeypatch, frozenset({_ADMIN_GROUP}))
        assert _role_of(_LDAP_USER) == "admin"
        _backdate_revalidation(_LDAP_USER, minutes_ago=60)
        # The directory now reports only viewer membership.
        monkeypatch.setattr(
            revalidation,
            "resolve_directory_state",
            lambda **_kw: _dir_state(
                LdapOutcome.AUTHENTICATED, frozenset({_VIEWER_GROUP})
            ),
        )
        resp = web_client.get(
            "/", headers={"Accept": "text/html"}, follow_redirects=False
        )
        assert resp.status_code == 200  # still a live session
        assert _role_of(_LDAP_USER) == "viewer"  # demoted in place

    def test_unreachable_directory_is_fail_safe_on_next_request(
        self, web_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _login_ldap_user(web_client, monkeypatch, frozenset({_ADMIN_GROUP}))
        _backdate_revalidation(_LDAP_USER, minutes_ago=60)
        # The directory is unreachable at re-eval time.
        monkeypatch.setattr(
            revalidation,
            "resolve_directory_state",
            lambda **_kw: _dir_state(LdapOutcome.SERVER_UNREACHABLE),
        )
        resp = web_client.get(
            "/", headers={"Accept": "text/html"}, follow_redirects=False
        )
        # Fail safe: a transient outage must not lock the user out.
        assert resp.status_code == 200
        assert _role_of(_LDAP_USER) == "admin"  # unchanged

    def test_local_session_is_never_reevaluated(
        self, web_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_admin(web_client)
        _seed_ldap_settings(web_client, enabled=True)
        # Sign in the LOCAL admin account.
        resp = _post_login(web_client, "admin", "AdminP@ssw0rd1234")
        assert resp.status_code == 303
        _backdate_revalidation("admin", minutes_ago=60)
        # If re-eval ran for a local session this would revoke it; it must not.
        monkeypatch.setattr(
            revalidation,
            "resolve_directory_state",
            lambda **_kw: (_ for _ in ()).throw(
                AssertionError("local session must not be re-evaluated")
            ),
        )
        resp = web_client.get(
            "/", headers={"Accept": "text/html"}, follow_redirects=False
        )
        assert resp.status_code == 200  # local session unaffected
