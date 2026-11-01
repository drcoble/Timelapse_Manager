"""Uvicorn runs with ``proxy_headers=False`` (#67 S1).

With Uvicorn's default ``proxy_headers=True`` and ``forwarded_allow_ips``
``127.0.0.1``, a loopback peer's ``X-Forwarded-For`` rewrote the ASGI client
address before the app saw it, so a local process could pose as any client to
the token peer policy and the login throttle. These tests serve the app through
the real Uvicorn config the service builds, so they exercise what a
``TestClient`` (which never runs Uvicorn) cannot:

* a loopback bearer call carrying a spoofed ``X-Forwarded-For`` is still judged
  as loopback (Uvicorn did not rewrite the peer);
* with loopback listed in ``trusted_proxies``, the app's own middleware resolves
  the forwarded client and the peer policy judges it;
* ``/healthz`` still answers with compact ``"app_version"`` for
  ``tlm-upgrade.sh`` (loopback) and ``"db_status":"ok"`` for the OpenNMS
  poller, which no longer sees the version (#67 S19).

The deployment-shaped cases (EDGE_PROXY as the trusted proxy, the OpenNMS poller at
10.0.12.104) use a ``TestClient`` peer, since they cannot originate here.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from alembic import command as alembic_command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import LOOPBACK_PEER, seed_admin
from tests.presession_support import WebTestClient
from timelapse_manager.app import create_app
from timelapse_manager.config.settings import ServerSettings, Settings
from timelapse_manager.security.csrf import mask_secret
from timelapse_manager.security.token import ensure_local_token
from timelapse_manager.service import _build_http_config, _build_https_config
from timelapse_manager.version import get_app_version
from timelapse_manager.web.routers import auth as auth_router

LAN_HOST = "10.0.12.50"
# Per-request timeout for the real-server calls: httpx defaults to 5 s, which a
# loaded parallel suite can exceed on a cold first request.
_HTTP_TIMEOUT = 60.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _served_settings(settings: Settings, **server: object) -> Settings:
    fields: dict[str, object] = {
        "http_port": _free_port(),
        "bind_address": "127.0.0.1",
        "redirect_http_to_https": True,
        **server,
    }
    return settings.model_copy(update={"server": ServerSettings(**fields)})  # type: ignore[arg-type]


@contextmanager
def _serve(app: FastAPI, settings: Settings) -> Iterator[str]:
    """Serve ``app`` with the service's real HTTP Uvicorn config; yield its URL.

    A ``TestClient`` block runs the lifespan (context and token files), because
    the service's HTTP listener runs with ``lifespan="off"``.
    """
    config = _build_http_config(app, settings)
    assert config.proxy_headers is False
    server = uvicorn.Server(config)
    with TestClient(app, client=LOOPBACK_PEER):
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        # Generous: under a loaded parallel suite a cold Uvicorn start (and the
        # lifespan above) can take far longer than on an idle machine.
        deadline = time.monotonic() + 60
        while not server.started:
            if time.monotonic() > deadline or not thread.is_alive():
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.02)
        try:
            yield f"http://127.0.0.1:{settings.server.http_port}"
        finally:
            server.should_exit = True
            thread.join(timeout=10)


@pytest.fixture()
def migrated(settings_no_autostart: Settings, alembic_cfg: Config) -> Settings:
    alembic_cfg.set_main_option("sqlalchemy.url", settings_no_autostart.database.url)
    alembic_command.upgrade(alembic_cfg, "head")
    return settings_no_autostart


def test_both_listener_configs_disable_proxy_headers(settings: Settings) -> None:
    https = _build_https_config(FastAPI(), settings, Path("c.pem"), Path("k.pem"))
    http = _build_http_config(FastAPI(), settings)
    assert https.proxy_headers is False
    assert http.proxy_headers is False


def test_loopback_bearer_call_over_real_uvicorn(migrated: Settings) -> None:
    """The CLI contract: plain HTTP from loopback with the token."""
    configured = _served_settings(migrated)
    app = create_app(configured)
    with _serve(app, configured) as base:
        token = ensure_local_token(configured)
        response = httpx.get(
            f"{base}/api/v1/system",
            headers={"Authorization": f"Bearer {token}"},
            timeout=_HTTP_TIMEOUT,
        )
    assert response.status_code == 200


def test_spoofed_xff_from_loopback_does_not_move_the_throttle_key(
    web_settings_no_redirect: Settings,
    alembic_cfg: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local process cannot pose as another client to the login throttle.

    Under Uvicorn's ``proxy_headers=True`` the loopback peer's XFF rewrote the
    ASGI client, so the recorded key would be the spoofed address.
    """
    alembic_cfg.set_main_option("sqlalchemy.url", web_settings_no_redirect.database.url)
    alembic_command.upgrade(alembic_cfg, "head")
    configured = _served_settings(
        web_settings_no_redirect, redirect_http_to_https=False
    )
    seen: list[str] = []
    real_client_ip = auth_router._client_ip

    def recording_client_ip(request: object) -> str:
        ip = real_client_ip(request)  # type: ignore[arg-type]
        seen.append(ip)
        return ip

    monkeypatch.setattr(auth_router, "_client_ip", recording_client_ip)
    app = create_app(configured)
    with _serve(app, configured) as base:
        seed_admin(TestClient(app))
        httpx.post(
            f"{base}/login",
            data={
                "username": "admin",
                "password": "wrong-password-123",
                "csrf_token": mask_secret("p" * 43),
            },
            cookies={"tlm_presession": "p" * 43},
            headers={"X-Forwarded-For": LAN_HOST},
            timeout=_HTTP_TIMEOUT,
        )
    assert seen == ["127.0.0.1"]


@pytest.mark.parametrize(
    ("allowed", "expected"),
    [(["127.0.0.1/32"], 403), (["127.0.0.1/32", "10.0.12.0/24"], 200)],
)
def test_trusted_loopback_proxy_is_resolved_by_the_app(
    migrated: Settings, allowed: list[str], expected: int
) -> None:
    configured = _served_settings(
        migrated, trusted_proxies=["127.0.0.1"], api_token_allowed_clients=allowed
    )
    app = create_app(configured)
    with _serve(app, configured) as base:
        token = ensure_local_token(configured)
        response = httpx.get(
            f"{base}/api/v1/system",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Forwarded-For": LAN_HOST,
                "X-Forwarded-Proto": "https",
            },
            timeout=_HTTP_TIMEOUT,
        )
    assert response.status_code == expected


def test_loopback_healthz_keeps_the_upgrade_script_contract(
    migrated: Settings,
) -> None:
    configured = _served_settings(migrated)
    app = create_app(configured)
    with _serve(app, configured) as base:
        response = httpx.get(f"{base}/healthz", timeout=_HTTP_TIMEOUT)
    assert response.status_code == 200
    assert f'"app_version":"{get_app_version()}"' in response.text
    assert '"db_status":"ok"' in response.text


def test_healthz_from_the_opennms_poller(migrated: Settings) -> None:
    app = create_app(migrated)
    with TestClient(app, client=("10.0.12.104", 40000)) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert '"db_status":"ok"' in response.text
    # A remote poller gets the short body: no version, path or components.
    assert '"app_version"' not in response.text
    assert response.json() == {"status": "ok", "db_status": "ok"}


def test_login_throttle_keys_on_the_forwarded_client_via_edge_proxy(
    web_settings_no_redirect: Settings,
    alembic_cfg: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real client IP still reaches the throttle through EDGE_PROXY."""
    configured = web_settings_no_redirect.model_copy(
        update={
            "server": ServerSettings(
                redirect_http_to_https=False, trusted_proxies=["192.0.2.10"]
            )
        }
    )
    alembic_cfg.set_main_option("sqlalchemy.url", configured.database.url)
    alembic_command.upgrade(alembic_cfg, "head")
    seen: list[str] = []
    real_client_ip = auth_router._client_ip

    def recording_client_ip(request: object) -> str:
        ip = real_client_ip(request)  # type: ignore[arg-type]
        seen.append(ip)
        return ip

    monkeypatch.setattr(auth_router, "_client_ip", recording_client_ip)
    app = create_app(configured)
    with WebTestClient(
        app,
        base_url="http://testserver",
        follow_redirects=False,
        client=("192.0.2.10", 50000),
    ) as client:
        seed_admin(client)
        client.post(
            "/login",
            data={"username": "admin", "password": "wrong-password-123"},
            headers={
                "X-Forwarded-For": f"203.0.113.9, {LAN_HOST}",
                "X-Forwarded-Proto": "https",
            },
            timeout=_HTTP_TIMEOUT,
        )
    assert seen == [LAN_HOST]


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Keep httpx from routing loopback calls through an environment proxy."""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    yield
