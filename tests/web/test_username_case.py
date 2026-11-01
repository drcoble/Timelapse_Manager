"""One person, one account: usernames are case- and whitespace-insensitive (#4)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import timelapse_manager.web.routers.auth as routers
from tests.conftest import csrf_of, seed_admin
from tests.web.test_login_ldap import (
    _ADMIN_GROUP,
    _post_login,
    _seed_ldap_settings,
)
from timelapse_manager.db.models import LoginAttempt, User
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.security.ldap_directory import LdapAuthResult, LdapOutcome
from timelapse_manager.usernames import directory_username, username_key

_DIR_UID = "alice.smith"


def _directory_says(uid: str = _DIR_UID):  # noqa: ANN202
    """A connector stand-in whose located entry carries the directory's uid."""

    def _connector(*, settings, username, password, bind_password):  # noqa: ANN001
        return LdapAuthResult(
            outcome=LdapOutcome.AUTHENTICATED,
            dn=f"uid={uid},ou=people,dc=example,dc=com",
            display_name="Alice",
            attributes={"UID": [uid], "cn": ["Alice"]},
            groups=frozenset({_ADMIN_GROUP}),
        )

    return _connector


def _users() -> list[tuple[int, str, str]]:
    with session_scope(get_context().session_factory) as db:
        return [
            (u.id, u.username, u.auth_source)
            for u in db.query(User).order_by(User.id).all()
        ]


def _logout(client: TestClient) -> None:
    client.post(
        "/logout",
        data={"csrf_token": csrf_of(client, "/")},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )


class TestHelpers:
    def test_key_strips_and_casefolds(self) -> None:
        assert username_key("  Alice.Smith ") == "alice.smith"
        assert username_key("STRASSE") == username_key("straße")

    def test_directory_username_prefers_the_entry_value(self) -> None:
        assert directory_username({"Uid": ["alice.smith"]}, "uid", "Alice.Smith") == (
            "alice.smith"
        )
        assert directory_username({}, "uid", " Alice.Smith ") == "Alice.Smith"


class TestLdapSpellings:
    def test_three_spellings_make_one_row_with_the_directory_value(
        self, web_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_admin(web_client)
        _seed_ldap_settings(web_client, enabled=True)
        monkeypatch.setattr(routers, "ldap_authenticate", _directory_says())
        for typed in ("Alice.smith", "alice.smith", " ALICE.SMITH "):
            assert _post_login(web_client, typed, "pw-directory").status_code == 303
            _logout(web_client)
        ldap_rows = [u for u in _users() if u[2] == "ldap"]
        assert [u[1] for u in ldap_rows] == ["alice.smith"]

    def test_existing_row_is_renamed_to_the_directory_spelling(
        self, web_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_admin(web_client)
        _seed_ldap_settings(web_client, enabled=True)
        with session_scope(get_context().session_factory) as db:
            row = User(username="Alice.smith", auth_source="ldap", role="viewer")
            db.add(row)
            db.flush()
            original_id = row.id
        monkeypatch.setattr(routers, "ldap_authenticate", _directory_says())
        assert _post_login(web_client, "ALICE.smith", "pw").status_code == 303
        ldap_rows = [u for u in _users() if u[2] == "ldap"]
        assert ldap_rows == [(original_id, "alice.smith", "ldap")]

    def test_directory_name_matching_a_local_account_by_case_is_refused(
        self, web_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_admin(web_client)  # local "admin"
        _seed_ldap_settings(web_client, enabled=True)
        monkeypatch.setattr(routers, "ldap_authenticate", _directory_says("Admin"))
        resp = _post_login(web_client, "Admin", "not-the-local-password")
        assert resp.status_code != 303
        assert [u[1] for u in _users() if u[2] == "ldap"] == []


class TestLocalAccounts:
    @pytest.mark.parametrize("typed", ["ADMIN", " admin ", "Admin"])
    def test_local_login_ignores_case_and_whitespace(
        self, web_client: TestClient, typed: str
    ) -> None:
        seed_admin(web_client)
        assert _post_login(web_client, typed, "AdminP@ssw0rd1234").status_code == 303

    def test_creating_a_case_variant_of_an_existing_user_conflicts(
        self, admin_client: TestClient
    ) -> None:
        resp = admin_client.post(
            "/users",
            data={
                "username": " ADMIN",
                "password": "AnotherPassw0rd!!",
                "role": "viewer",
                "csrf_token": csrf_of(admin_client, "/users"),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code in (400, 409)
        assert [u[1] for u in _users()].count("admin") == 1
        assert len([u for u in _users() if username_key(u[1]) == "admin"]) == 1


class TestThrottleKey:
    def test_success_clears_failures_recorded_under_another_case(
        self, web_client: TestClient
    ) -> None:
        seed_admin(web_client)
        assert _post_login(web_client, "Admin", "wrong-password-1").status_code != 303
        assert _post_login(web_client, "admin", "AdminP@ssw0rd1234").status_code == 303
        with session_scope(get_context().session_factory) as db:
            names = {a.username for a in db.query(LoginAttempt).all()}
        assert names == {"admin"}
