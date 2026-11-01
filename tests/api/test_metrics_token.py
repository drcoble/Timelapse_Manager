"""The read-only metrics token (#67 S2).

While ``observability.metrics_enabled`` is set, startup creates
``<data_dir>/.metrics-token`` (0600). ``/api/v1/metrics`` accepts it from a peer
in ``observability.metrics_allowed_clients`` over plain HTTP; it opens nothing
else. The admin cookie and the local admin token keep working on the scrape.
"""

from __future__ import annotations

import stat
import sys
from collections.abc import Generator
from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import LOOPBACK_PEER, login, seed_admin
from timelapse_manager.app import create_app
from timelapse_manager.config.settings import (
    ObservabilitySettings,
    ServerSettings,
    Settings,
)
from timelapse_manager.security.token import ensure_local_token, read_token_file

METRICS = "/api/v1/metrics"
OPENNMS = "10.0.12.104"
EDGE_PROXY = "192.0.2.10"


def _metrics_settings(settings: Settings, **observability: object) -> Settings:
    return settings.model_copy(
        update={
            "observability": ObservabilitySettings(
                metrics_enabled=True,
                debug_endpoints_enabled=True,
                **observability,  # type: ignore[arg-type]
            ),
            "server": ServerSettings(trusted_proxies=[EDGE_PROXY]),
        }
    )


def _metrics_token(settings: Settings) -> str:
    return read_token_file(settings.paths.data_dir / ".metrics-token")


def _peer(app: FastAPI, host: str, *, scheme: str = "http") -> TestClient:
    return TestClient(app, base_url=f"{scheme}://testserver", client=(host, 50000))


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def metrics_app(
    settings_no_autostart: Settings, alembic_cfg: Config
) -> Generator[tuple[FastAPI, Settings], None, None]:
    configured = _metrics_settings(settings_no_autostart)
    alembic_cfg.set_main_option("sqlalchemy.url", configured.database.url)
    alembic_command.upgrade(alembic_cfg, "head")
    app = create_app(configured)
    with TestClient(app, client=LOOPBACK_PEER):
        yield app, configured


def test_startup_creates_the_metrics_token_file(
    metrics_app: tuple[FastAPI, Settings],
) -> None:
    _, configured = metrics_app
    path = configured.paths.data_dir / ".metrics-token"
    assert path.exists()
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert _metrics_token(configured) != ensure_local_token(configured)


def test_no_metrics_token_while_metrics_disabled(settings: Settings) -> None:
    with TestClient(create_app(settings), client=LOOPBACK_PEER):
        pass
    assert not (settings.paths.data_dir / ".metrics-token").exists()


def test_metrics_token_from_loopback_over_http(
    metrics_app: tuple[FastAPI, Settings],
) -> None:
    app, configured = metrics_app
    response = _peer(app, "127.0.0.1").get(
        METRICS, headers=_bearer(_metrics_token(configured))
    )
    assert response.status_code == 200
    assert "frames_captured_total" in response.text


def test_metrics_token_from_an_unlisted_peer_is_403(
    metrics_app: tuple[FastAPI, Settings],
) -> None:
    app, configured = metrics_app
    response = _peer(app, OPENNMS).get(
        METRICS, headers=_bearer(_metrics_token(configured))
    )
    assert response.status_code == 403


def test_metrics_token_from_an_allowlisted_poller_over_http(
    settings_no_autostart: Settings, alembic_cfg: Config
) -> None:
    configured = _metrics_settings(
        settings_no_autostart, metrics_allowed_clients=["127.0.0.1", OPENNMS]
    )
    alembic_cfg.set_main_option("sqlalchemy.url", configured.database.url)
    alembic_command.upgrade(alembic_cfg, "head")
    app = create_app(configured)
    with TestClient(app, client=LOOPBACK_PEER):
        token = _metrics_token(configured)
        poller = _peer(app, OPENNMS).get(METRICS, headers=_bearer(token))
        wrong = _peer(app, OPENNMS).get(METRICS, headers=_bearer("wrong"))
        # The admin token stays loopback/HTTPS-only even where metrics is allowed.
        admin = _peer(app, OPENNMS).get(
            METRICS, headers=_bearer(ensure_local_token(configured))
        )
    assert poller.status_code == 200
    assert wrong.status_code == 401
    assert admin.status_code == 401


def test_metrics_token_through_edge_proxy_is_judged_by_the_forwarded_client(
    metrics_app: tuple[FastAPI, Settings],
) -> None:
    app, configured = metrics_app
    response = _peer(app, EDGE_PROXY).get(
        METRICS,
        headers={
            **_bearer(_metrics_token(configured)),
            "X-Forwarded-For": "10.0.12.50",
            "X-Forwarded-Proto": "https",
        },
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "path",
    ["/api/v1/system", "/api/v1/projects", "/api/v1/system/debug/threads"],
)
def test_metrics_token_opens_nothing_else(
    metrics_app: tuple[FastAPI, Settings], path: str
) -> None:
    app, configured = metrics_app
    response = _peer(app, "127.0.0.1").get(
        path, headers=_bearer(_metrics_token(configured))
    )
    assert response.status_code == 401


def test_local_admin_token_still_scrapes(
    metrics_app: tuple[FastAPI, Settings],
) -> None:
    app, configured = metrics_app
    response = _peer(app, "127.0.0.1").get(
        METRICS, headers=_bearer(ensure_local_token(configured))
    )
    assert response.status_code == 200


def test_missing_token_from_an_allowed_peer_is_401(
    metrics_app: tuple[FastAPI, Settings],
) -> None:
    app, _ = metrics_app
    assert _peer(app, "127.0.0.1").get(METRICS).status_code == 401


def test_metrics_still_404_while_disabled(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app, client=LOOPBACK_PEER) as client:
        response = client.get(METRICS, headers=_bearer(ensure_local_token(settings)))
    assert response.status_code == 404


def test_rotated_metrics_token_takes_effect_without_restart(
    metrics_app: tuple[FastAPI, Settings],
) -> None:
    from timelapse_manager.security.token import rotate_token_file

    app, configured = metrics_app
    before = _metrics_token(configured)
    rotate_token_file(configured.paths.data_dir / ".metrics-token")
    after = _metrics_token(configured)
    client = _peer(app, "127.0.0.1")
    assert client.get(METRICS, headers=_bearer(before)).status_code == 401
    assert client.get(METRICS, headers=_bearer(after)).status_code == 200


def test_admin_cookie_scrape_from_edge_proxy_is_unaffected(
    web_settings_no_redirect: Settings, alembic_cfg: Config, tmp_path: Path
) -> None:
    """Cookie-authenticated metrics through the proxy do not use the peer policy."""
    configured = web_settings_no_redirect.model_copy(
        update={
            "observability": ObservabilitySettings(metrics_enabled=True),
            "server": ServerSettings(
                redirect_http_to_https=False, trusted_proxies=[EDGE_PROXY]
            ),
        }
    )
    alembic_cfg.set_main_option("sqlalchemy.url", configured.database.url)
    alembic_command.upgrade(alembic_cfg, "head")
    app = create_app(configured)
    with TestClient(
        app,
        base_url="http://testserver",
        follow_redirects=False,
        client=(EDGE_PROXY, 50000),
    ) as client:
        seed_admin(client)
        login(client)
        response = client.get(METRICS, headers={"X-Forwarded-For": "10.0.12.50"})
    assert response.status_code == 200
