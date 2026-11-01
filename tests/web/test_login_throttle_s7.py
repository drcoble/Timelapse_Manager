"""Login and password-change throttling through the web layer (#67 S7, S23, R17).

Covers what the unit tests cannot: the async login route (delay on the event
loop, holding no worker thread), the deployment behind EDGE_PROXY (one proxy peer,
clients told apart by the trusted ``X-Forwarded-For``), directory outages that
must not count, the local break-glass account that an outage must not expose,
the owner's LDAP login, the password-change 429, and the proxy warnings.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Generator, Iterator
from contextlib import contextmanager

import anyio.to_thread
import pytest
from alembic import command as alembic_command
from alembic.config import Config
from fastapi.testclient import TestClient
from ldap3.core.exceptions import LDAPException, LDAPSocketOpenError
from sqlalchemy import select, update

import timelapse_manager.web.routers.auth as auth_router
from tests.conftest import csrf_of, login, seed_admin
from tests.presession_support import WebTestClient, without_token
from timelapse_manager.app import create_app
from timelapse_manager.config.settings import ServerSettings, Settings
from timelapse_manager.db.models import LoginAttempt, User
from timelapse_manager.db.models import Session as SessionRow
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.security import ldap_directory
from timelapse_manager.security.ldap_directory import (
    DETAIL_LOST_AFTER_USER_SEARCH,
    DETAIL_NO_SERVER_REACHABLE,
    LdapAuthResult,
    LdapOutcome,
)
from timelapse_manager.security.ldap_directory import (
    authenticate as real_ldap_authenticate,
)
from timelapse_manager.security.ldap_settings_service import (
    LdapSettingsUpdate,
    LdapSettingsView,
    update_settings,
)
from timelapse_manager.security.proxy_networks import parse_networks_cached
from timelapse_manager.security.session_revalidation import breaker_for
from timelapse_manager.web import middleware as middleware_mod

EDGE_PROXY = "192.0.2.10"
CLIENT_A = "10.0.40.21"
CLIENT_B = "10.0.40.22"
ADMIN_PW = "AdminP@ssw0rd1234"
_ADMIN_GROUP = "cn=admins,ou=groups,dc=example,dc=com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def _client(
    settings: Settings,
    alembic_cfg: Config,
    *,
    peer: str = "127.0.0.1",
    trusted_proxies: list[str] | None = None,
) -> Iterator[TestClient]:
    configured = settings.model_copy(
        update={
            "server": ServerSettings(
                redirect_http_to_https=False,
                trusted_proxies=trusted_proxies or [],
            )
        }
    )
    alembic_cfg.set_main_option("sqlalchemy.url", configured.database.url)
    alembic_command.upgrade(alembic_cfg, "head")
    app = create_app(configured)
    with WebTestClient(
        app,
        base_url="http://testserver",
        follow_redirects=False,
        client=(peer, 50000),
    ) as client:
        yield client


def _post_login(
    client: TestClient,
    username: str,
    password: str,
    *,
    forwarded_for: str | None = None,
):
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if forwarded_for is not None:
        headers["X-Forwarded-For"] = forwarded_for
        headers["X-Forwarded-Proto"] = "https"
    return client.post(
        "/login",
        data={"username": username, "password": password},
        headers=headers,
    )


def _attempt_rows() -> list[tuple[str, str, bool]]:
    with session_scope(get_context().session_factory) as db:
        return [
            (row.ip, row.username, row.succeeded)
            for row in db.execute(select(LoginAttempt)).scalars()
        ]


def _seed_failures(username: str, count: int, *, prefix: str = "10.0.41.") -> None:
    """Record ``count`` recent failures for ``username`` from distinct sources."""
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    with session_scope(get_context().session_factory) as db:
        for n in range(count):
            db.add(
                LoginAttempt(
                    ip=f"{prefix}{n}",
                    username=username,
                    attempted_at=now - datetime.timedelta(seconds=count - n),
                    succeeded=False,
                )
            )


def _provision_directory_user(username: str) -> None:
    """Add a directory (LDAP) account row, as a past login would have."""
    with session_scope(get_context().session_factory) as db:
        db.add(User(username=username, auth_source="ldap", role="admin"))


@contextmanager
def _middleware_warnings() -> Iterator[list[str]]:
    """Collect the middleware logger's WARNING messages.

    A handler on the logger itself: the app's logging setup, which runs when
    the client starts inside the test, replaces the root handlers ``caplog``
    relies on.
    """
    messages: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    handler = _Collect(level=logging.WARNING)
    # The trusted-proxy parse warning is logged by the shared parser (#174).
    targets = [
        logging.getLogger(middleware_mod.__name__),
        logging.getLogger("timelapse_manager.security.proxy_networks"),
    ]
    for target in targets:
        target.addHandler(handler)
    try:
        yield messages
    finally:
        for target in targets:
            target.removeHandler(handler)


class _SleepRecorder:
    """Stands in for the login delay; records each delay and the busy threads."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self.borrowed: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        limiter = anyio.to_thread.current_default_thread_limiter()
        self.borrowed.append(limiter.borrowed_tokens)


@pytest.fixture()
def sleep_recorder(monkeypatch: pytest.MonkeyPatch) -> _SleepRecorder:
    recorder = _SleepRecorder()
    monkeypatch.setattr(auth_router, "_backoff_sleep", recorder)
    return recorder


def _seed_ldap(*, enabled: bool = True) -> None:
    with session_scope(get_context().session_factory) as db:
        update_settings(
            db,
            LdapSettingsUpdate(
                enabled=enabled,
                server_urls=["ldaps://dir.example.com"],
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
                operator_group_dn=None,
                operator_group_filter=None,
                viewer_group_dn=None,
                viewer_group_filter=None,
            ),
        )


def _connector(result: LdapAuthResult):
    def _authenticate(*, settings, username, password, bind_password):  # noqa: ANN001
        return result

    return _authenticate


def _owner_directory_login(username: str = "alice.smith") -> LdapAuthResult:
    return LdapAuthResult(
        outcome=LdapOutcome.AUTHENTICATED,
        dn=f"uid={username},ou=people,dc=example,dc=com",
        display_name="Alice",
        attributes={"uid": [username]},
        groups=frozenset({_ADMIN_GROUP}),
    )


_INVALID = LdapAuthResult(LdapOutcome.INVALID_CREDENTIALS, detail="user bind rejected")

_UNREACHABLE = LdapAuthResult(
    LdapOutcome.SERVER_UNREACHABLE, detail=DETAIL_NO_SERVER_REACHABLE
)


# ---------------------------------------------------------------------------
# Behind EDGE_PROXY: one proxy peer, many clients
# ---------------------------------------------------------------------------


class TestBehindEdgeProxy:
    def test_one_client_exhausting_its_budget_does_not_lock_out_another(
        self, web_settings: Settings, alembic_cfg: Config, sleep_recorder
    ) -> None:
        """Every request arrives from the proxy; the trusted XFF tells clients apart."""
        with _client(
            web_settings, alembic_cfg, peer=EDGE_PROXY, trusted_proxies=[EDGE_PROXY]
        ) as client:
            seed_admin(client)
            max_failures = get_context().settings.auth.throttle_max_failures
            for _ in range(max_failures):
                resp = _post_login(client, "admin", "wrong", forwarded_for=CLIENT_A)
                assert resp.status_code == 401
            # Client A is now refused even with the right password ...
            resp = _post_login(client, "admin", ADMIN_PW, forwarded_for=CLIENT_A)
            assert resp.status_code == 401
            # ... while client B, behind the same proxy, signs in.
            resp = _post_login(client, "admin", ADMIN_PW, forwarded_for=CLIENT_B)
            assert resp.status_code == 303
            keys = {ip for ip, _, _ in _attempt_rows()}
            assert EDGE_PROXY not in keys
            assert CLIENT_A in keys

    def test_a_spoofed_left_hop_does_not_give_a_fresh_budget(
        self, web_settings: Settings, alembic_cfg: Config, sleep_recorder
    ) -> None:
        """nginx appends the real client; a client-supplied left hop is ignored."""
        with _client(
            web_settings, alembic_cfg, peer=EDGE_PROXY, trusted_proxies=[EDGE_PROXY]
        ) as client:
            seed_admin(client)
            max_failures = get_context().settings.auth.throttle_max_failures
            for n in range(max_failures + 1):
                _post_login(
                    client, "admin", "wrong", forwarded_for=f"203.0.113.{n}, {CLIENT_A}"
                )
            resp = _post_login(
                client, "admin", ADMIN_PW, forwarded_for=f"198.51.100.1, {CLIENT_A}"
            )
            assert resp.status_code == 401

    def test_without_trust_config_all_clients_share_the_proxy_budget(
        self, web_settings: Settings, alembic_cfg: Config, sleep_recorder
    ) -> None:
        """The misconfiguration R17 warns about: EDGE_PROXY not in trusted_proxies.

        Every client is then keyed as EDGE_PROXY, so one client's failures lock out
        everyone. The middleware says so in the log, once.
        """
        with _client(web_settings, alembic_cfg, peer=EDGE_PROXY) as client:
            seed_admin(client)
            with _middleware_warnings() as messages:
                max_failures = get_context().settings.auth.throttle_max_failures
                for _ in range(max_failures):
                    _post_login(client, "admin", "wrong", forwarded_for=CLIENT_A)
                resp = _post_login(client, "admin", ADMIN_PW, forwarded_for=CLIENT_B)
                assert resp.status_code == 401
            assert {ip for ip, _, _ in _attempt_rows()} == {EDGE_PROXY}
        warnings = [m for m in messages if "server.trusted_proxies" in m]
        assert len(warnings) == 1
        assert EDGE_PROXY in warnings[0]
        assert CLIENT_A not in warnings[0]

    def test_no_warning_when_the_proxy_is_trusted(
        self, web_settings: Settings, alembic_cfg: Config, sleep_recorder
    ) -> None:
        with _client(
            web_settings, alembic_cfg, peer=EDGE_PROXY, trusted_proxies=[EDGE_PROXY]
        ) as client:
            seed_admin(client)
            with _middleware_warnings() as messages:
                _post_login(client, "admin", "wrong", forwarded_for=CLIENT_A)
        assert not [m for m in messages if "trusted_proxies" in m]

    def test_a_malformed_trusted_proxy_entry_is_logged_once(
        self, web_settings: Settings, alembic_cfg: Config
    ) -> None:
        with _client(
            web_settings,
            alembic_cfg,
            peer=EDGE_PROXY,
            trusted_proxies=[EDGE_PROXY, "edgeproxy.example.com"],
        ) as client:
            parse_networks_cached.cache_clear()
            with _middleware_warnings() as messages:
                for _ in range(3):
                    client.get("/login", headers={"X-Forwarded-For": CLIENT_A})
        assert len([m for m in messages if "edgeproxy.example.com" in m]) == 1


# ---------------------------------------------------------------------------
# Per-username delay
# ---------------------------------------------------------------------------


class TestLocalDelay:
    def test_correct_password_still_signs_in_after_the_delay(
        self, web_settings: Settings, alembic_cfg: Config, sleep_recorder
    ) -> None:
        """Failures from other sources delay the break-glass login; never refuse it."""
        with _client(web_settings, alembic_cfg) as client:
            seed_admin(client)
            _seed_failures("admin", 4)
            resp = _post_login(client, "Admin", ADMIN_PW)
            assert resp.status_code == 303
        assert sleep_recorder.delays == [2.0]

    def test_no_worker_thread_is_held_during_the_delay(
        self, web_settings: Settings, alembic_cfg: Config, sleep_recorder
    ) -> None:
        with _client(web_settings, alembic_cfg) as client:
            seed_admin(client)
            _seed_failures("admin", 3)
            _post_login(client, "admin", ADMIN_PW)
        assert sleep_recorder.delays == [1.0]
        assert sleep_recorder.borrowed == [0]

    def test_no_delay_below_the_threshold(
        self, web_settings: Settings, alembic_cfg: Config, sleep_recorder
    ) -> None:
        with _client(web_settings, alembic_cfg) as client:
            seed_admin(client)
            _seed_failures("admin", 2)
            assert _post_login(client, "admin", ADMIN_PW).status_code == 303
        assert sleep_recorder.delays == []

    def test_success_clears_the_reservation_row(
        self, web_settings: Settings, alembic_cfg: Config, sleep_recorder
    ) -> None:
        with _client(web_settings, alembic_cfg) as client:
            seed_admin(client)
            assert _post_login(client, " ADMIN ", ADMIN_PW).status_code == 303
            assert _attempt_rows() == [("127.0.0.1", "admin", True)]


class TestSameTreatmentForEveryName:
    """Timing and throttle state never tell local, directory and unknown names apart.

    Security review of WP-67-H: the delay depended on the account's source, and
    an outage released the reservation for every name but a local one.
    """

    _NAMES = ("admin", "alice.smith", "no-such-user")

    def test_same_failure_history_gives_the_same_delay(
        self,
        web_settings: Settings,
        alembic_cfg: Config,
        sleep_recorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with _client(web_settings, alembic_cfg) as client:
            seed_admin(client)
            _seed_ldap()
            monkeypatch.setattr(
                auth_router, "ldap_authenticate", _connector(_owner_directory_login())
            )
            _provision_directory_user("alice.smith")
            monkeypatch.setattr(auth_router, "ldap_authenticate", _connector(_INVALID))
            for n, name in enumerate(self._NAMES):
                _seed_failures(name, 4, prefix=f"10.0.4{n}.")
            for name in self._NAMES:
                resp = _post_login(client, name, "wrong")
                assert resp.status_code == 401
        assert sleep_recorder.delays == [2.0, 2.0, 2.0]

    def test_outage_gives_the_same_state_and_page_for_every_name(
        self,
        web_settings: Settings,
        alembic_cfg: Config,
        sleep_recorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """During an outage every name counts, and every name sees the same page."""
        pages: dict[str, str] = {}
        counts: dict[str, int] = {}
        with _client(web_settings, alembic_cfg) as client:
            seed_admin(client)
            _seed_ldap()
            monkeypatch.setattr(
                auth_router, "ldap_authenticate", _connector(_owner_directory_login())
            )
            _provision_directory_user("alice.smith")
            monkeypatch.setattr(
                auth_router, "ldap_authenticate", _connector(_UNREACHABLE)
            )
            for name in self._NAMES:
                resp = _post_login(client, name, "guess")
                assert resp.status_code == 401
                pages[name] = without_token(resp.text)
                counts[name] = sum(
                    1 for _, user, ok in _attempt_rows() if user == name and not ok
                )
        assert len(set(pages.values())) == 1
        assert "directory server could not be reached" in pages["admin"]
        assert "Invalid credentials" not in pages["admin"]
        assert counts == dict.fromkeys(self._NAMES, 1)


class TestOwnerDirectoryLogin:
    def test_owner_signs_in_in_any_case_after_a_spray(
        self,
        web_settings: Settings,
        alembic_cfg: Config,
        sleep_recorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A spray on the owner's LDAP name delays, never blocks, the login (#4)."""
        with _client(web_settings, alembic_cfg) as client:
            seed_admin(client)
            _seed_ldap()
            monkeypatch.setattr(
                auth_router, "ldap_authenticate", _connector(_owner_directory_login())
            )
            _provision_directory_user("alice.smith")
            _seed_failures("alice.smith", 20)
            resp = _post_login(client, "Alice.Smith", "dir-pw")
            assert resp.status_code == 303
            rows = _attempt_rows()
            with session_scope(get_context().session_factory) as db:
                names = list(db.execute(select(User.username)).scalars())
        assert sleep_recorder.delays == [30.0]
        assert ("127.0.0.1", "alice.smith", False) not in rows
        assert names.count("alice.smith") == 1


# ---------------------------------------------------------------------------
# Directory outages
# ---------------------------------------------------------------------------


class TestDirectoryOutage:
    def test_outage_counts_and_the_owner_signs_in_once_it_ends(
        self,
        web_settings: Settings,
        alembic_cfg: Config,
        sleep_recorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with _client(web_settings, alembic_cfg) as client:
            seed_admin(client)
            _seed_ldap()
            monkeypatch.setattr(
                auth_router, "ldap_authenticate", _connector(_UNREACHABLE)
            )
            for _ in range(2):
                resp = _post_login(client, "alice.smith", "dir-pw")
                assert resp.status_code == 401
                assert "directory server could not be reached" in resp.text
            assert len(_attempt_rows()) == 2
            # The outage opened the circuit breaker (#67 R3); the directory is
            # asked again once its window is over, which this stands in for.
            with session_scope(get_context().session_factory) as db:
                breaker_for(db).record_answered()
            monkeypatch.setattr(
                auth_router, "ldap_authenticate", _connector(_owner_directory_login())
            )
            assert _post_login(client, "alice.smith", "dir-pw").status_code == 303

    def test_outage_does_not_open_guessing_of_the_local_account(
        self,
        web_settings: Settings,
        alembic_cfg: Config,
        sleep_recorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An outage must not open unlimited guessing of the break-glass account."""
        with _client(web_settings, alembic_cfg) as client:
            seed_admin(client)
            _seed_ldap()
            monkeypatch.setattr(
                auth_router, "ldap_authenticate", _connector(_UNREACHABLE)
            )
            max_failures = get_context().settings.auth.throttle_max_failures
            for _ in range(max_failures):
                resp = _post_login(client, "admin", "guess")
                assert "directory server could not be reached" in resp.text
            resp = _post_login(client, "admin", ADMIN_PW)
            assert resp.status_code == 401
            assert "Invalid credentials" in resp.text

    def test_config_error_shows_the_outage_page(
        self,
        web_settings: Settings,
        alembic_cfg: Config,
        sleep_recorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with _client(web_settings, alembic_cfg) as client:
            seed_admin(client)
            _seed_ldap()
            monkeypatch.setattr(
                auth_router,
                "ldap_authenticate",
                _connector(
                    LdapAuthResult(
                        LdapOutcome.CONFIG_ERROR, detail="service bind failed"
                    )
                ),
            )
            resp = _post_login(client, "someone", "pw")
            assert "directory server could not be reached" in resp.text

    def test_a_protocol_error_after_the_bind_gets_the_generic_page(
        self,
        web_settings: Settings,
        alembic_cfg: Config,
        sleep_recorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A generic directory error may follow a correct bind: no distinct page."""
        with _client(web_settings, alembic_cfg) as client:
            seed_admin(client)
            _seed_ldap()
            monkeypatch.setattr(
                auth_router,
                "ldap_authenticate",
                _connector(
                    LdapAuthResult(
                        LdapOutcome.SERVER_UNREACHABLE, detail="directory error"
                    )
                ),
            )
            resp = _post_login(client, "someone", "pw")
            assert "Invalid credentials" in resp.text
            assert _attempt_rows() == [("127.0.0.1", "someone", False)]


class TestConnectorDetailContract:
    """Pins how the connector reports outages before and after the user search."""

    @staticmethod
    def _view() -> LdapSettingsView:
        return LdapSettingsView(
            enabled=True,
            server_urls=["ldaps://dir.example.com"],
            bind_dn="cn=svc,dc=example,dc=com",
            search_base="ou=people,dc=example,dc=com",
            search_filter="(objectClass=inetOrgPerson)",
            username_attribute="uid",
            display_name_attribute="cn",
            admin_group_dn=_ADMIN_GROUP,
        )

    @staticmethod
    def _authenticate(factory: object) -> LdapAuthResult:
        return real_ldap_authenticate(
            settings=TestConnectorDetailContract._view(),
            username="alice.smith",
            password="pw",
            bind_password="svc",
            connection_factory=factory,  # type: ignore[arg-type]
        )

    def test_unreachable_before_the_search_is_unavailable(self) -> None:
        def factory(*_args: object, **_kwargs: object) -> object:
            raise LDAPSocketOpenError("connection refused")

        result = self._authenticate(factory)
        assert result.detail == DETAIL_NO_SERVER_REACHABLE
        assert auth_router._directory_was_unavailable(  # noqa: SLF001
            result.outcome, result.detail
        )

    def test_lost_after_the_search_is_not_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The user search found the name; the user bind's socket then fails."""
        monkeypatch.setattr(
            ldap_directory,
            "_find_user",
            lambda *_a, **_k: ldap_directory._LocatedUser(  # noqa: SLF001
                dn="uid=alice.smith,ou=people,dc=example,dc=com", attributes={}
            ),
        )
        calls: list[object] = []

        class _ServiceConnection:
            def bind(self) -> bool:
                return True

        def factory(_pool: object, user: object, _password: object) -> object:
            calls.append(user)
            if len(calls) == 1:
                return _ServiceConnection()
            raise LDAPSocketOpenError("connection reset")

        result = self._authenticate(factory)
        assert len(calls) == 2
        assert result.outcome is LdapOutcome.SERVER_UNREACHABLE
        assert result.detail == DETAIL_LOST_AFTER_USER_SEARCH
        assert not auth_router._directory_was_unavailable(  # noqa: SLF001
            result.outcome, result.detail
        )

    def test_other_protocol_errors_are_not(self) -> None:
        def factory(*_args: object, **_kwargs: object) -> object:
            raise LDAPException("boom")

        result = self._authenticate(factory)
        assert result.outcome is LdapOutcome.SERVER_UNREACHABLE
        assert not auth_router._directory_was_unavailable(  # noqa: SLF001
            result.outcome, result.detail
        )


class TestOutageAfterUserSearch:
    """An outage after the search must look exactly like an unknown name.

    Security review of WP-67-H: only a name the search finds reaches the user
    bind, so a distinct outage answer there would confirm the name exists.
    """

    def test_same_page_and_throttle_effect_as_an_unknown_user(
        self,
        web_settings: Settings,
        alembic_cfg: Config,
        sleep_recorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        results = {
            "alice.smith": LdapAuthResult(
                LdapOutcome.SERVER_UNREACHABLE, detail=DETAIL_LOST_AFTER_USER_SEARCH
            ),
            "no-such-user": LdapAuthResult(
                LdapOutcome.NO_SUCH_USER, detail="user not found"
            ),
        }

        def connector(*, settings, username, password, bind_password):  # noqa: ANN001
            return results[username]

        pages: dict[str, str] = {}
        statuses: dict[str, int] = {}
        with _client(web_settings, alembic_cfg) as client:
            seed_admin(client)
            _seed_ldap()
            monkeypatch.setattr(auth_router, "ldap_authenticate", connector)
            for name in results:
                resp = _post_login(client, name, "pw")
                pages[name] = without_token(resp.text)
                statuses[name] = resp.status_code
            rows = sorted(_attempt_rows())
        assert statuses == {"alice.smith": 401, "no-such-user": 401}
        assert pages["alice.smith"] == pages["no-such-user"]
        assert "Invalid credentials" in pages["alice.smith"]
        assert "directory server could not be reached" not in pages["alice.smith"]
        assert rows == [
            ("127.0.0.1", "alice.smith", False),
            ("127.0.0.1", "no-such-user", False),
        ]

    def test_outage_before_the_search_gets_the_outage_page(
        self,
        web_settings: Settings,
        alembic_cfg: Config,
        sleep_recorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with _client(web_settings, alembic_cfg) as client:
            seed_admin(client)
            _seed_ldap()
            monkeypatch.setattr(
                auth_router, "ldap_authenticate", _connector(_UNREACHABLE)
            )
            resp = _post_login(client, "alice.smith", "pw")
            assert "directory server could not be reached" in resp.text
            assert _attempt_rows() == [("127.0.0.1", "alice.smith", False)]


# ---------------------------------------------------------------------------
# Password change (S23)
# ---------------------------------------------------------------------------


def _post_password(client: TestClient, current: str, new: str = "BrandNewPass99!"):
    csrf = csrf_of(client, "/account/preferences")
    return client.post(
        "/account/password",
        data={
            "current_password": current,
            "new_password": new,
            "confirm_password": new,
            "csrf_token": csrf,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )


class TestPasswordChangeThrottle:
    def test_sixth_wrong_current_password_is_429(
        self, admin_client: TestClient
    ) -> None:
        max_failures = get_context().settings.auth.throttle_max_failures
        for _ in range(max_failures):
            resp = _post_password(admin_client, "not-my-password")
            assert resp.status_code == 303
            assert "password_error=current" in resp.headers["location"]
        resp = _post_password(admin_client, ADMIN_PW)
        assert resp.status_code == 429
        assert "Too many incorrect current passwords" in resp.text

    def test_password_change_failures_do_not_slow_login(
        self, admin_client: TestClient, sleep_recorder
    ) -> None:
        for _ in range(4):
            _post_password(admin_client, "not-my-password")
        admin_client.cookies.clear()
        login(admin_client)
        assert sleep_recorder.delays == []

    def test_correct_current_password_releases_its_reservation(
        self, admin_client: TestClient
    ) -> None:
        _post_password(admin_client, "not-my-password")
        resp = _post_password(admin_client, ADMIN_PW)
        assert resp.status_code == 303
        assert "password_changed=1" in resp.headers["location"]
        failures = [
            row
            for row in _attempt_rows()
            if row[0].startswith("account:") and not row[2]
        ]
        assert len(failures) == 1

    def test_change_with_a_stale_session_touch_does_not_deadlock(
        self, admin_client: TestClient
    ) -> None:
        """The touch write and the reservation's own connection must not collide."""
        csrf = csrf_of(admin_client, "/account/preferences")
        stale = datetime.datetime.now(datetime.UTC).replace(
            tzinfo=None
        ) - datetime.timedelta(minutes=10)
        with session_scope(get_context().session_factory) as db:
            db.execute(update(SessionRow).values(last_active=stale))
        resp = admin_client.post(
            "/account/password",
            data={
                "current_password": ADMIN_PW,
                "new_password": "BrandNewPass99!",
                "confirm_password": "BrandNewPass99!",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "password_changed=1" in resp.headers["location"]


@pytest.fixture(autouse=True)
def _clear_proxy_cache() -> Generator[None, None, None]:
    parse_networks_cached.cache_clear()
    yield
    parse_networks_cached.cache_clear()
