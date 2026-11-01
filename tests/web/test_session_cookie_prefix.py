"""The ``__Host-`` session cookie (#67 S18) and the end of the old-name path (#238).

Over (effective) HTTPS the session cookie is ``__Host-tlm_session``: ``Secure``,
``Path=/``, no ``Domain``, and it is the only name read. Over plain HTTP it keeps
the old name, because a browser rejects a ``__Host-`` cookie without ``Secure``.
The transition that honoured and promoted the old name over HTTPS ended after
0.2.0: a session cookie under the old name is refused there.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from alembic import command as alembic_command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.conftest import LOOPBACK_PEER, csrf_of, login, seed_admin
from tests.presession_support import WebTestClient, ensure_presession, session_cookie
from timelapse_manager.app import create_app
from timelapse_manager.config.settings import ServerSettings, SessionSettings, Settings
from timelapse_manager.db.models import Session as SessionRow
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.security.sessions import (
    TOKEN_PREFIX,
    get_session_row,
    hash_token,
)

PREFIXED = "__Host-tlm_session"
LEGACY = "tlm_session"
EDGE_PROXY = "192.0.2.10"
_FORM = {"Content-Type": "application/x-www-form-urlencoded"}
_PASSWORD = "AdminP@ssw0rd1234"


@contextmanager
def _client(
    settings: Settings,
    alembic_cfg: Config,
    *,
    base_url: str = "https://testserver",
    peer: str = LOOPBACK_PEER[0],
    trusted_proxies: list[str] | None = None,
    **session_overrides: Any,
) -> Iterator[TestClient]:
    configured = settings.model_copy(
        update={
            "session": SessionSettings(**session_overrides),
            "server": ServerSettings(
                redirect_http_to_https=False, trusted_proxies=trusted_proxies or []
            ),
        }
    )
    alembic_cfg.set_main_option("sqlalchemy.url", configured.database.url)
    alembic_command.upgrade(alembic_cfg, "head")
    app = create_app(configured)
    with WebTestClient(
        app, base_url=base_url, follow_redirects=False, client=(peer, 50000)
    ) as client:
        yield client


def _post_login(
    client: TestClient,
    *,
    remember_me: bool = False,
    headers: dict[str, str] | None = None,
) -> Any:
    data = {"username": "admin", "password": _PASSWORD}
    if remember_me:
        data["remember_me"] = "1"
    return client.post(
        "/login",
        data=data,
        headers={**_FORM, **(headers or {})},
        follow_redirects=False,
    )


def _set_cookies(resp: Any) -> list[str]:
    return list(resp.headers.get_list("set-cookie"))


def _setting(resp: Any, name: str) -> str | None:
    """Return the ``Set-Cookie`` that sets (not deletes) ``name``."""
    for header in _set_cookies(resp):
        if header.startswith(f"{name}=") and "Max-Age=0" not in header:
            return header
    return None


def _deletes(resp: Any, name: str) -> bool:
    return any(
        header.startswith(f"{name}=") and "Max-Age=0" in header
        for header in _set_cookies(resp)
    )


def _as_legacy_only(client: TestClient, token: str) -> None:
    """Make the client hold just the old-name cookie, as a pre-upgrade browser."""
    client.cookies.clear()
    client.cookies.set(LEGACY, token)


class TestIssuingTheCookie:
    def test_https_login_uses_the_host_prefix(self, web_client: TestClient) -> None:
        seed_admin(web_client)
        resp = _post_login(web_client)
        assert resp.status_code == 303
        header = _setting(resp, PREFIXED)
        assert header is not None
        assert "Secure" in header
        assert "Path=/" in header
        assert "HttpOnly" in header
        assert "Domain" not in header
        assert _setting(resp, LEGACY) is None
        # The old name is deleted so it cannot shadow or outlive the new one.
        assert _deletes(resp, LEGACY)

    def test_plain_http_keeps_the_old_name(
        self, web_client_no_redirect: TestClient
    ) -> None:
        seed_admin(web_client_no_redirect)
        resp = _post_login(web_client_no_redirect)
        header = _setting(resp, LEGACY)
        assert header is not None
        assert "Secure" not in header
        assert not any(h.startswith("__Host-") for h in _set_cookies(resp))

    def test_a_forged_forwarded_proto_does_not_earn_the_prefix(
        self, web_client_no_redirect: TestClient
    ) -> None:
        """No trusted proxy configured: the header is ignored, so no ``__Host-``."""
        seed_admin(web_client_no_redirect)
        resp = _post_login(
            web_client_no_redirect, headers={"X-Forwarded-Proto": "https"}
        )
        assert _setting(resp, LEGACY) is not None
        assert _setting(resp, PREFIXED) is None

    def test_behind_a_trusted_proxy_the_app_sees_http_and_still_gets_secure(
        self, web_settings: Settings, alembic_cfg: Config
    ) -> None:
        """EDGE_PROXY terminates TLS and sends plain HTTP with ``X-Forwarded-Proto``."""
        with _client(
            web_settings,
            alembic_cfg,
            base_url="http://testserver",
            peer=EDGE_PROXY,
            trusted_proxies=[EDGE_PROXY],
        ) as client:
            seed_admin(client)
            resp = _post_login(client, headers={"X-Forwarded-Proto": "https"})
            header = _setting(resp, PREFIXED)
            assert header is not None
            assert "Secure" in header and "Path=/" in header

    def test_a_trusted_proxy_saying_http_gets_the_old_name_never_a_bare_prefix(
        self, web_settings: Settings, alembic_cfg: Config
    ) -> None:
        with _client(
            web_settings,
            alembic_cfg,
            base_url="http://testserver",
            peer=EDGE_PROXY,
            trusted_proxies=[EDGE_PROXY],
        ) as client:
            seed_admin(client)
            resp = _post_login(client, headers={"X-Forwarded-Proto": "http"})
            assert _setting(resp, LEGACY) is not None
            assert not any(h.startswith("__Host-") for h in _set_cookies(resp))

    def test_host_prefix_off_keeps_the_old_name_over_https(
        self, web_settings: Settings, alembic_cfg: Config
    ) -> None:
        with _client(web_settings, alembic_cfg, host_prefix=False) as client:
            seed_admin(client)
            resp = _post_login(client)
            assert _setting(resp, LEGACY) is not None
            assert _setting(resp, PREFIXED) is None
            assert client.get("/").status_code == 200

    def test_remember_me_is_persistent_under_the_new_name(
        self, web_client: TestClient
    ) -> None:
        seed_admin(web_client)
        header = _setting(_post_login(web_client, remember_me=True), PREFIXED)
        assert header is not None
        assert "Max-Age=2592000" in header


def _make_pre_upgrade(token: str) -> str:
    """Re-key a session as one issued before the upgrade; return its old-style token.

    Tokens issued by this version start with ``s2.``; a session that predates it
    has a plain ``token_urlsafe`` token. The only way to get one now is to swap
    the stored hash, which is what a pre-upgrade database looks like.
    """
    old_style = secrets.token_urlsafe(32)
    with session_scope(get_context().session_factory) as db:
        row = db.execute(
            select(SessionRow).where(SessionRow.token_hash == hash_token(token))
        ).scalar_one()
        row.token_hash = hash_token(old_style)
    return old_style


def _session_is_live(token: str) -> bool:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        return (
            get_session_row(db, token, settings=ctx.settings.session, touch=False)
            is not None
        )


def _second_session(web_client: TestClient) -> str:
    """Log the admin in again on a separate client; return that session's token."""
    other = WebTestClient(
        web_client.app, base_url="https://testserver", client=LOOPBACK_PEER
    )
    return login(other)


def _pre_upgrade_session(web_client: TestClient) -> str:
    return _make_pre_upgrade(_second_session(web_client))


class TestReadingTheCookie:
    def test_the_prefixed_cookie_authenticates(self, admin_client: TestClient) -> None:
        assert admin_client.cookies.get(PREFIXED)
        assert admin_client.get("/").status_code == 200

    def test_the_prefixed_cookie_wins_and_a_planted_old_one_is_ignored(
        self, admin_client: TestClient
    ) -> None:
        """A planted legacy cookie, even a valid pre-upgrade session, never shadows."""
        planted = _pre_upgrade_session(admin_client)
        admin_client.cookies.set(LEGACY, planted)
        resp = admin_client.get("/")
        assert resp.status_code == 200
        assert _setting(resp, PREFIXED) is None  # nothing promoted
        assert _session_is_live(planted)  # untouched, not revoked or replaced

    def test_a_prefixed_cookie_alone_still_needs_a_csrf_token(
        self, admin_client: TestClient
    ) -> None:
        """The CSRF rule must see the new name, or every mutation skips it."""
        token = admin_client.cookies.get(PREFIXED)
        assert token
        admin_client.cookies.clear()
        resp = admin_client.post(
            "/logout", headers={**_FORM, "Cookie": f"{PREFIXED}={token}"}
        )
        assert resp.status_code == 403
        assert resp.text == "CSRF validation failed."


class TestOldNameRefused:
    """The unprefixed name is not read over HTTPS any more (#238)."""

    def test_a_pre_upgrade_session_under_the_old_name_is_refused(
        self, web_client: TestClient
    ) -> None:
        seed_admin(web_client)
        login(web_client)
        token = session_cookie(web_client)
        assert token
        token = _make_pre_upgrade(token)
        _as_legacy_only(web_client, token)

        for path in ("/", "/alerts/summary"):
            resp = web_client.get(path)
            assert resp.status_code in (303, 401)  # signed out, not served
            assert _setting(resp, PREFIXED) is None  # nothing promoted
        assert _session_is_live(token)  # not revoked or replaced either

    def test_a_current_session_under_the_old_name_is_refused(
        self, web_client: TestClient
    ) -> None:
        seed_admin(web_client)
        login(web_client)
        token = session_cookie(web_client)
        assert token
        assert token.startswith(TOKEN_PREFIX)
        _as_legacy_only(web_client, token)
        assert web_client.get("/alerts/summary").status_code == 401

    def test_no_marker_check_or_data_directory_file_decides_it(
        self, web_client: TestClient
    ) -> None:
        seed_admin(web_client)
        login(web_client)
        data_dir = get_context().settings.paths.data_dir
        assert not any(p.name.startswith(".session-prefix") for p in data_dir.iterdir())

    def test_the_old_name_stays_refused_after_a_restart(
        self, web_settings: Settings, alembic_cfg: Config
    ) -> None:
        with _client(web_settings, alembic_cfg) as first:
            seed_admin(first)
            login(first)
            token = session_cookie(first)
            assert token
        with _client(web_settings, alembic_cfg) as second:
            _as_legacy_only(second, token)
            assert second.get("/alerts/summary").status_code == 401

    def test_a_dead_old_cookie_changes_nothing(self, web_client: TestClient) -> None:
        seed_admin(web_client)
        web_client.cookies.set(LEGACY, "not-a-session")
        resp = web_client.get("/alerts/summary")
        assert resp.status_code == 401
        assert _setting(resp, PREFIXED) is None

    def test_a_planted_old_cookie_cannot_sign_out_a_real_session(
        self, admin_client: TestClient
    ) -> None:
        """The old name is not even looked at, so planting it is harmless."""
        admin_client.cookies.set(LEGACY, "planted;garbage")
        assert admin_client.get("/alerts/summary").status_code == 200

    def test_the_removed_setting_in_an_old_config_is_ignored(self) -> None:
        """An existing config file that still sets it loads and changes nothing."""
        loaded = SessionSettings.model_validate({"accept_legacy_cookie": True})
        assert not hasattr(loaded, "accept_legacy_cookie")

    def test_plain_http_still_reads_the_old_name(
        self, web_settings: Settings, alembic_cfg: Config
    ) -> None:
        """Over plain HTTP the old name is the normal one (loopback, no proxy)."""
        with _client(web_settings, alembic_cfg, base_url="http://testserver") as client:
            seed_admin(client)
            login(client)
            assert client.cookies.get(LEGACY)
            assert client.get("/").status_code == 200

    def test_plain_http_does_not_read_the_prefixed_name(
        self, web_settings: Settings, alembic_cfg: Config
    ) -> None:
        with _client(web_settings, alembic_cfg, base_url="http://testserver") as client:
            seed_admin(client)
            login(client)
            token = client.cookies.get(LEGACY)
            assert token
            client.cookies.clear()
            client.cookies.set(PREFIXED, token)
            assert client.get("/alerts/summary").status_code == 401

    def test_host_prefix_off_reads_the_old_name_over_https(
        self, web_settings: Settings, alembic_cfg: Config
    ) -> None:
        with _client(web_settings, alembic_cfg, host_prefix=False) as client:
            seed_admin(client)
            login(client)
            assert client.cookies.get(LEGACY)
            assert client.get("/").status_code == 200

    def test_signing_in_ignores_an_old_cookie(self, web_client: TestClient) -> None:
        """Login never treats the old name as a session, nor revokes it."""
        seed_admin(web_client)
        planted = _pre_upgrade_session(web_client)
        _as_legacy_only(web_client, planted)
        ensure_presession(web_client)

        resp = _post_login(web_client)

        assert resp.status_code == 303
        prefixed = [h for h in _set_cookies(resp) if h.startswith(f"{PREFIXED}=")]
        assert len(prefixed) == 1
        assert not prefixed[0].startswith(f"{PREFIXED}={planted};")
        assert _deletes(resp, LEGACY)
        assert _session_is_live(planted)  # login did not act on it
        assert web_client.get("/").status_code == 200


class TestLogout:
    def test_logout_clears_both_names(self, admin_client: TestClient) -> None:
        csrf = csrf_of(admin_client, "/")
        resp = admin_client.post(
            "/logout", data={"csrf_token": csrf}, headers=_FORM, follow_redirects=False
        )
        assert resp.status_code == 303
        assert _deletes(resp, PREFIXED)
        assert _deletes(resp, LEGACY)
        assert admin_client.get("/alerts/summary").status_code == 401
