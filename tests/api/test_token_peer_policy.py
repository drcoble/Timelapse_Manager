"""Peer policy for the local API bearer token (#67 S1).

The token is accepted from loopback on any scheme, and from any other peer only
when that peer (or, behind a trusted proxy, the forwarded client) is in
``server.api_token_allowed_clients`` *and* the request is HTTPS. A refused peer
gets 403 before the token is compared, so a wrong token and a right token look
the same from there.

One lifespan-running client keeps the application context installed; the
per-peer clients are plain ``TestClient`` instances on the same app (the peer
address is fixed per client), so the app is built once per settings variant.
"""

from __future__ import annotations

import logging
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient

from timelapse_manager.app import create_app
from timelapse_manager.config.settings import (
    ObservabilitySettings,
    ServerSettings,
    Settings,
)
from timelapse_manager.security import token as token_module
from timelapse_manager.security.token import PEER_REFUSED_DETAIL, ensure_local_token

EDGE_PROXY = "192.0.2.10"
LAN_HOST = "10.0.12.50"
SYSTEM = "/api/v1/system"


def _settings(settings: Settings, **server: object) -> Settings:
    """``settings`` with EDGE_PROXY as the trusted proxy plus any server overrides."""
    merged: dict[str, object] = {"trusted_proxies": [EDGE_PROXY], **server}
    return settings.model_copy(update={"server": ServerSettings(**merged)})  # type: ignore[arg-type]


@contextmanager
def _running(app: FastAPI) -> Iterator[None]:
    """Run the app's lifespan (context, token files) for the block."""
    with TestClient(app):
        yield


def _peer(app: FastAPI, host: str, *, scheme: str = "http") -> TestClient:
    return TestClient(app, base_url=f"{scheme}://testserver", client=(host, 50000))


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def default_app(settings: Settings) -> Generator[tuple[FastAPI, str], None, None]:
    app = create_app(_settings(settings))
    with _running(app):
        yield app, ensure_local_token(settings)


@pytest.fixture()
def lan_allowed_app(settings: Settings) -> Generator[tuple[FastAPI, str], None, None]:
    app = create_app(
        _settings(
            settings,
            api_token_allowed_clients=["127.0.0.1/32", "::1/128", "10.0.12.0/24"],
        )
    )
    with _running(app):
        yield app, ensure_local_token(settings)


# ---------------------------------------------------------------------------
# Loopback: accepted on plain HTTP (the CLI)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_loopback_peer_over_http_is_accepted(
    default_app: tuple[FastAPI, str], host: str
) -> None:
    app, token = default_app
    assert _peer(app, host).get(SYSTEM, headers=_bearer(token)).status_code == 200


def test_loopback_peer_with_wrong_token_is_401(
    default_app: tuple[FastAPI, str],
) -> None:
    app, _ = default_app
    response = _peer(app, "127.0.0.1").get(SYSTEM, headers=_bearer("wrong"))
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_loopback_peer_relaying_without_being_trusted_is_refused(
    default_app: tuple[FastAPI, str],
) -> None:
    """An unlisted same-host proxy hides its client, so its relays are refused."""
    app, token = default_app
    for header in ("X-Forwarded-For", "X-Forwarded-Proto", "Forwarded"):
        response = _peer(app, "127.0.0.1").get(
            SYSTEM, headers={**_bearer(token), header: "for=10.0.12.50"}
        )
        assert response.status_code == 403, header


# ---------------------------------------------------------------------------
# LAN peers: refused before the compare
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("right_token", [True, False])
def test_lan_peer_is_refused_before_the_compare(
    default_app: tuple[FastAPI, str], scheme: str, right_token: bool
) -> None:
    app, token = default_app
    presented = token if right_token else "wrong"
    response = _peer(app, LAN_HOST, scheme=scheme).get(
        SYSTEM, headers=_bearer(presented)
    )
    assert response.status_code == 403
    assert response.json()["detail"] == PEER_REFUSED_DETAIL
    assert "www-authenticate" not in response.headers


def test_lan_peer_without_a_token_is_also_403(
    default_app: tuple[FastAPI, str],
) -> None:
    app, _ = default_app
    assert _peer(app, LAN_HOST).get(SYSTEM).status_code == 403


def test_refusal_never_reads_the_token(
    default_app: tuple[FastAPI, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, token = default_app

    def must_not_load() -> str:
        raise AssertionError("token loaded for a refused peer")

    monkeypatch.setattr(token_module, "current_local_token", must_not_load)
    response = _peer(app, LAN_HOST).get(SYSTEM, headers=_bearer(token))
    assert response.status_code == 403


def test_unparseable_peer_is_refused(default_app: tuple[FastAPI, str]) -> None:
    app, token = default_app
    assert (
        _peer(app, "testclient").get(SYSTEM, headers=_bearer(token)).status_code == 403
    )


def test_allowlisted_lan_peer_needs_https(
    lan_allowed_app: tuple[FastAPI, str],
) -> None:
    app, token = lan_allowed_app
    https = _peer(app, LAN_HOST, scheme="https").get(SYSTEM, headers=_bearer(token))
    http = _peer(app, LAN_HOST, scheme="http").get(SYSTEM, headers=_bearer(token))
    assert https.status_code == 200
    assert http.status_code == 403


# ---------------------------------------------------------------------------
# Through the trusted proxy (EDGE_PROXY): the forwarded client is judged
# ---------------------------------------------------------------------------


def _via_edge_proxy(
    app: FastAPI, token: str, *, client: str = LAN_HOST, proto: str = "https"
) -> int:
    response = _peer(app, EDGE_PROXY).get(
        SYSTEM,
        headers={
            **_bearer(token),
            "X-Forwarded-For": client,
            "X-Forwarded-Proto": proto,
        },
    )
    return response.status_code


def test_proxied_lan_client_is_refused_by_default(
    default_app: tuple[FastAPI, str],
) -> None:
    app, token = default_app
    assert _via_edge_proxy(app, token) == 403


def test_proxied_allowlisted_client_over_https_is_accepted(
    lan_allowed_app: tuple[FastAPI, str],
) -> None:
    app, token = lan_allowed_app
    assert _via_edge_proxy(app, token) == 200


def test_proxied_allowlisted_client_over_http_is_refused(
    lan_allowed_app: tuple[FastAPI, str],
) -> None:
    app, token = lan_allowed_app
    assert _via_edge_proxy(app, token, proto="http") == 403


def test_proxied_loopback_client_is_refused(
    default_app: tuple[FastAPI, str],
) -> None:
    """``XFF: 127.0.0.1`` from EDGE_PROXY is EDGE_PROXY's loopback, not this host's."""
    app, token = default_app
    assert _via_edge_proxy(app, token, client="127.0.0.1") == 403


def test_spoofed_left_hops_do_not_help(default_app: tuple[FastAPI, str]) -> None:
    """The real client is the right-most untrusted hop, not an injected one."""
    app, token = default_app
    assert _via_edge_proxy(app, token, client=f"127.0.0.1, {LAN_HOST}") == 403


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"X-Forwarded-Proto": "https"}, id="no-xff"),
        pytest.param(
            {"X-Forwarded-For": "", "X-Forwarded-Proto": "https"}, id="empty-xff"
        ),
        pytest.param(
            {"X-Forwarded-For": EDGE_PROXY, "X-Forwarded-Proto": "https"},
            id="only-trusted-hops",
        ),
        pytest.param({"Forwarded": f"for={LAN_HOST};proto=https"}, id="forwarded-only"),
        pytest.param(
            {"X-Forwarded-For": f"{LAN_HOST}, garbage", "X-Forwarded-Proto": "https"},
            id="malformed-hop",
        ),
        pytest.param(
            {"X-Forwarded-For": "127.0.0.1", "X-Forwarded-Proto": "https"},
            id="spoofed-loopback",
        ),
    ],
)
def test_unresolved_relays_from_edge_proxy_are_refused(
    settings: Settings, alembic_cfg: Config, headers: dict[str, str]
) -> None:
    """No explicitly resolved client hop, no token, even with EDGE_PROXY allowlisted.

    Allowlisting EDGE_PROXY and loopback makes a fallback to the proxy's address (or a
    spoofed loopback hop) observable: any such fallback would turn into a 200.
    """
    alembic_cfg.set_main_option("sqlalchemy.url", settings.database.url)
    alembic_command.upgrade(alembic_cfg, "head")
    app = create_app(
        _settings(
            settings,
            api_token_allowed_clients=["127.0.0.1/32", "::1/128", f"{EDGE_PROXY}/32"],
        )
    )
    with _running(app):
        token = ensure_local_token(settings)
        response = _peer(app, EDGE_PROXY).get(
            SYSTEM, headers={**_bearer(token), **headers}
        )
        # The web UI behind EDGE_PROXY does not use the token and keeps working.
        page = _peer(app, EDGE_PROXY).get(
            "/login", headers=headers, follow_redirects=False
        )
    assert response.status_code == 403
    assert page.status_code != 403


def test_trusted_proxy_itself_is_not_a_token_client(
    default_app: tuple[FastAPI, str],
) -> None:
    app, token = default_app
    response = _peer(app, EDGE_PROXY, scheme="https").get(
        SYSTEM, headers=_bearer(token)
    )
    assert response.status_code == 403


def test_same_host_proxy_cannot_make_remote_clients_local(settings: Settings) -> None:
    """A loopback trusted proxy that relays a request is judged by its client."""
    app = create_app(_settings(settings, trusted_proxies=["127.0.0.1"]))
    with _running(app):
        token = ensure_local_token(settings)
        relayed = _peer(app, "127.0.0.1").get(
            SYSTEM,
            headers={
                **_bearer(token),
                "X-Forwarded-For": LAN_HOST,
                "X-Forwarded-Proto": "https",
            },
        )
        direct = _peer(app, "127.0.0.1").get(SYSTEM, headers=_bearer(token))
    assert relayed.status_code == 403
    assert direct.status_code == 200


# ---------------------------------------------------------------------------
# Routes outside the bearer router use the same policy
# ---------------------------------------------------------------------------


def test_debug_routes_apply_the_peer_policy(settings: Settings) -> None:
    observability = ObservabilitySettings(debug_endpoints_enabled=True)
    app = create_app(
        _settings(settings).model_copy(update={"observability": observability})
    )
    with _running(app):
        token = ensure_local_token(settings)
        path = "/api/v1/system/debug/threads"
        lan = _peer(app, LAN_HOST, scheme="https").get(path, headers=_bearer(token))
        local = _peer(app, "127.0.0.1").get(path, headers=_bearer(token))
    assert lan.status_code == 403
    assert local.status_code == 200


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def test_refusal_is_logged_once_per_window_without_secrets(
    default_app: tuple[FastAPI, str],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, token = default_app
    monkeypatch.setattr(
        token_module,
        "_peer_warnings",
        token_module._PeerWarningLimiter(interval_seconds=600, max_entries=4),
    )
    with caplog.at_level(logging.WARNING, logger=token_module.__name__):
        for _ in range(3):
            _peer(app, "10.0.12.77").get(SYSTEM, headers=_bearer(token))
    records = [r for r in caplog.records if "refused from peer" in r.getMessage()]
    assert len(records) == 1
    assert "10.0.12.77" in records[0].getMessage()
    assert token not in caplog.text


def test_warning_limiter_is_bounded() -> None:
    limiter = token_module._PeerWarningLimiter(interval_seconds=600, max_entries=3)
    for i in range(10):
        assert limiter.should_log((f"10.0.0.{i}", "-"), now=0.0)
    assert len(limiter._last) == 3
    # The oldest keys were evicted, so they may log again; recent ones may not.
    assert limiter.should_log(("10.0.0.0", "-"), now=1.0)
    assert not limiter.should_log(("10.0.0.9", "-"), now=1.0)


def test_settings_reject_a_malformed_allowlist_entry(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not an IP address"):
        ServerSettings(api_token_allowed_clients=["10.0.12.0/24", "edge_proxy"])


def test_allowlist_accepts_a_comma_separated_string() -> None:
    server = ServerSettings(
        api_token_allowed_clients="127.0.0.1/32, ::1/128, 10.0.12.5"  # type: ignore[arg-type]
    )
    assert server.api_token_allowed_clients == ["127.0.0.1/32", "::1/128", "10.0.12.5"]


def test_refusal_log_cannot_be_split_by_a_crafted_path(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from starlette.requests import Request

    monkeypatch.setattr(
        token_module,
        "_peer_warnings",
        token_module._PeerWarningLimiter(interval_seconds=600, max_entries=4),
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("10.0.12.78", 50000),
            "path": "/api/v1/x\n2026-10-27 ERROR forged",
            "root_path": "",
            "query_string": b"",
            "headers": [],
            "state": {},
        }
    )
    with caplog.at_level(logging.WARNING, logger=token_module.__name__):
        error = token_module.refuse_token_peer(request, "local API token")
    assert error.status_code == 403
    records = [r for r in caplog.records if "refused from peer" in r.getMessage()]
    assert len(records) == 1
    assert "\n" not in records[0].getMessage()
