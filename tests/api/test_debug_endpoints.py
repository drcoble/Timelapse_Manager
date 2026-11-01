"""API tests for the admin debug endpoints (issue #7).

``/api/v1/system/debug/{tasks,threads,runners}`` and ``POST .../log-level`` are
off by default (404, like ``/metrics``), administrator-only when enabled
(session or local bearer token), and never echo a secret. Fixtures are local so
each app can toggle ``observability.debug_endpoints_enabled``.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.conftest import LOOPBACK_PEER, csrf_of, login, seed_admin
from timelapse_manager.api import debug as debug_api
from timelapse_manager.app import create_app
from timelapse_manager.capture.supervisor import (
    CaptureState,
    CaptureTarget,
    _ListenerRunner,
    _ProjectRunner,
)
from timelapse_manager.config.settings import (
    AuthSettings,
    CaptureSettings,
    DatabaseSettings,
    LoggingSettings,
    MonitoringSettings,
    ObservabilitySettings,
    PathsSettings,
    RenderSettings,
    SecretsSettings,
    ServerSettings,
    Settings,
    TlsSettings,
)
from timelapse_manager.db.models import Camera, Event, ExactTimeFire, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.monitoring import settings_service as notification_settings
from timelapse_manager.runtime import get_context
from timelapse_manager.security import ldap_settings_service
from timelapse_manager.security.token import ensure_local_token

_READ_ROUTES = (
    "/api/v1/system/debug/tasks",
    "/api/v1/system/debug/threads",
    "/api/v1/system/debug/runners",
)
_LOG_LEVEL_ROUTE = "/api/v1/system/debug/log-level"

# Secrets seeded into the database / runner state; none may appear in a payload.
_LDAP_BIND_PASSWORD = "Ldap-Bind-S3cret-7731"
_SMTP_PASSWORD = "Smtp-Relay-S3cret-4410"
_WEBHOOK_TOKEN = "hook-token-S3cret-9902"
_CAMERA_PASSWORD = "Cam-S3cret-1187"


def _debug_settings(tmp_path: Path, *, enabled: bool) -> Settings:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    return Settings(
        database=DatabaseSettings(url=f"sqlite:///{tmp_path / 'debug_test.db'}"),
        logging=LoggingSettings(level="WARNING", format="text"),
        paths=PathsSettings(
            data_dir=data_dir,
            frames_root=data_dir / "frames",
            token_file=data_dir / ".local-token",
        ),
        capture=CaptureSettings(autostart=False),
        render=RenderSettings(autostart=False),
        monitoring=MonitoringSettings(autostart=False),
        observability=ObservabilitySettings(debug_endpoints_enabled=enabled),
        auth=AuthSettings(
            argon2_memory_kib=256,
            argon2_time_cost=1,
            argon2_parallelism=1,
            password_min_length=12,
        ),
        tls=TlsSettings(auto_generate=False),
        server=ServerSettings(redirect_http_to_https=True),
        secrets=SecretsSettings(use_os_keystore=False),
    )


def _client(
    settings: Settings, alembic_cfg: Config
) -> Generator[TestClient, None, None]:
    alembic_cfg.set_main_option("sqlalchemy.url", settings.database.url)
    alembic_command.upgrade(alembic_cfg, "head")
    app = create_app(settings)
    with TestClient(app, base_url="https://testserver", client=LOOPBACK_PEER) as c:
        yield c


@pytest.fixture()
def disabled_client(
    tmp_path: Path, alembic_cfg: Config
) -> Generator[TestClient, None, None]:
    yield from _client(_debug_settings(tmp_path, enabled=False), alembic_cfg)


@pytest.fixture()
def enabled_client(
    tmp_path: Path, alembic_cfg: Config
) -> Generator[TestClient, None, None]:
    yield from _client(_debug_settings(tmp_path, enabled=True), alembic_cfg)


def _bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {ensure_local_token(get_context().settings)}"}


def _login_viewer(client: TestClient) -> None:
    """Seed an admin, create a viewer through the UI, and log in as the viewer."""
    seed_admin(client)
    login(client)
    client.post(
        "/users",
        data={
            "username": "viewer",
            "password": "ViewerPass12345!",
            "role": "viewer",
            "csrf_token": csrf_of(client, "/users"),
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    client.post(
        "/logout",
        data={"csrf_token": csrf_of(client, "/")},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    login(client, username="viewer", password="ViewerPass12345!")


def _seed_project() -> int:
    with session_scope(get_context().session_factory) as session:
        camera = Camera(name="cam", address="127.0.0.1", protocol="vapix")
        session.add(camera)
        session.flush()
        project = Project(
            camera_id=camera.id,
            name="Roof",
            lifecycle_state="active",
            operational_status="idle",
        )
        session.add(project)
        session.flush()
        return int(project.id)


def _register_runner(project_id: int, *, last_error: str | None = None) -> None:
    """Put a runner in the supervisor's registry without starting capture."""
    supervisor = get_context().capture_supervisor
    assert supervisor is not None
    now = datetime.now(UTC)
    state = CaptureState(
        project_id=project_id,
        camera_id=1,
        state="error" if last_error else "running",
        started_at=now,
        last_cycle_at=now,
        last_error=last_error,
    )
    target = CaptureTarget(
        project_id=project_id, project_name="Roof", camera_id=1, interval_seconds=60
    )
    supervisor._runners[project_id] = _ProjectRunner(state=state, target=target)  # noqa: SLF001


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _READ_ROUTES)
def test_disabled_routes_are_404_even_for_the_admin(
    disabled_client: TestClient, path: str
) -> None:
    assert disabled_client.get(path).status_code == 404
    assert disabled_client.get(path, headers=_bearer()).status_code == 404


def test_disabled_log_level_is_404(disabled_client: TestClient) -> None:
    resp = disabled_client.post(
        _LOG_LEVEL_ROUTE, json={"level": "DEBUG"}, headers=_bearer()
    )
    assert resp.status_code == 404


@pytest.mark.parametrize("path", _READ_ROUTES)
def test_enabled_unauthenticated_is_401(enabled_client: TestClient, path: str) -> None:
    assert enabled_client.get(path).status_code == 401


@pytest.mark.parametrize("path", _READ_ROUTES)
def test_enabled_wrong_token_is_401(enabled_client: TestClient, path: str) -> None:
    resp = enabled_client.get(path, headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_enabled_non_admin_session_is_403(enabled_client: TestClient) -> None:
    _login_viewer(enabled_client)
    for path in _READ_ROUTES:
        assert enabled_client.get(path).status_code == 403, path


@pytest.mark.parametrize("path", _READ_ROUTES)
def test_enabled_bearer_admin_is_200(enabled_client: TestClient, path: str) -> None:
    assert enabled_client.get(path, headers=_bearer()).status_code == 200


def test_enabled_admin_session_is_200(enabled_client: TestClient) -> None:
    seed_admin(enabled_client)
    login(enabled_client)
    for path in _READ_ROUTES:
        assert enabled_client.get(path).status_code == 200, path


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------


def test_tasks_lists_named_tasks_and_executor_stats(enabled_client: TestClient) -> None:
    body = enabled_client.get(_READ_ROUTES[0], headers=_bearer()).json()
    names = {task["name"] for task in body["tasks"]}
    assert "loop-lag" in names
    lag_task = next(task for task in body["tasks"] if task["name"] == "loop-lag")
    assert lag_task["state"] == "pending"
    assert lag_task["awaiting"]["kind"] == "asyncio.tasks.sleep"
    assert set(body["executor"]) >= {"created", "max_workers", "threads", "queue_depth"}
    assert "event_loop_lag_seconds" in body
    assert body["runners"] == []


def test_threads_lists_every_thread_with_a_stack(enabled_client: TestClient) -> None:
    body = enabled_client.get(_READ_ROUTES[1], headers=_bearer()).json()
    names = [thread["name"] for thread in body["threads"]]
    assert "MainThread" in names
    assert all(isinstance(thread["stack"], list) for thread in body["threads"])


def test_runners_report_state_heartbeat_and_latest_fire(
    enabled_client: TestClient,
) -> None:
    project_id = _seed_project()
    with session_scope(get_context().session_factory) as session:
        for day, status in (("2026-10-20", "captured"), ("2026-10-21", "failed")):
            session.add(
                ExactTimeFire(
                    project_id=project_id,
                    anchor_id="noon",
                    local_date=day,
                    status=status,
                    fired_at=datetime.fromisoformat(f"{day}T16:00:00"),
                )
            )
    _register_runner(project_id)

    body = enabled_client.get(_READ_ROUTES[2], headers=_bearer()).json()
    [runner] = body["runners"]
    assert runner["project_id"] == project_id
    assert runner["project_name"] == "Roof"
    assert runner["state"]["state"] == "running"
    assert set(runner["state"]) >= {"last_cycle_at", "restarts", "next_retry_at"}
    assert runner["last_cycle_age_seconds"] >= 0
    assert runner["plan"] is None  # no cycle has run
    assert runner["latest_exact_time_fire"]["local_date"] == "2026-10-21"
    assert runner["latest_exact_time_fire"]["status"] == "failed"


def test_listeners_report_state_without_a_secret(enabled_client: TestClient) -> None:
    supervisor = get_context().capture_supervisor
    assert supervisor is not None
    target = CaptureTarget(
        project_id=41, project_name="Door", camera_id=3, interval_seconds=None
    )
    runner = _ListenerRunner(target=target)
    runner.state.transport = "onvif-pullpoint"
    runner.state.connected_since = datetime.now(UTC)
    runner.state.last_progress_mono = supervisor._clock.monotonic()  # noqa: SLF001
    runner.state.last_error = "cannot reach rtsp://***@10.0.0.5/stream"
    runner.state.consecutive_failures = 2
    runner.state.dropped_events = 5
    supervisor._listeners[41] = runner  # noqa: SLF001

    body = enabled_client.get(_READ_ROUTES[2], headers=_bearer()).json()
    [listener] = body["listeners"]
    assert listener["project_id"] == 41
    assert listener["camera_id"] == 3
    assert listener["transport"] == "onvif-pullpoint"
    assert listener["connected"] is True
    assert listener["consecutive_failures"] == 2
    assert listener["dropped_events"] == 5
    assert listener["last_error"] == "cannot reach rtsp://***@10.0.0.5/stream"
    assert listener["last_progress_age_seconds"] >= 0
    # Ages, not raw monotonic readings.
    assert "last_progress_mono" not in listener


def test_read_payloads_are_cached(
    enabled_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A long TTL keeps the test independent of wall-clock speed; the cache reads
    # the constant when it is first built on this app.
    monkeypatch.setattr(debug_api, "_CACHE_TTL_SECONDS", 3600.0)
    first = enabled_client.get(_READ_ROUTES[2], headers=_bearer()).json()
    _register_runner(_seed_project())
    second = enabled_client.get(_READ_ROUTES[2], headers=_bearer()).json()
    assert first == second == {"runners": [], "listeners": []}


def test_expired_cache_rebuilds(
    enabled_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(debug_api, "_CACHE_TTL_SECONDS", 0.0)
    assert enabled_client.get(_READ_ROUTES[2], headers=_bearer()).json() == {
        "runners": [],
        "listeners": [],
    }
    _register_runner(_seed_project())
    body = enabled_client.get(_READ_ROUTES[2], headers=_bearer()).json()
    assert len(body["runners"]) == 1


def test_no_secret_appears_in_any_debug_payload(enabled_client: TestClient) -> None:
    factory = get_context().session_factory
    with session_scope(factory) as session:
        ldap_settings_service.update_settings(
            session,
            ldap_settings_service.LdapSettingsUpdate(
                enabled=True,
                server_urls=["ldaps://auth.example.test"],
                tls_mode="ldaps",
                tls_ca_cert_path=None,
                bind_dn="uid=bind,dc=example,dc=test",
                bind_password=_LDAP_BIND_PASSWORD,
                search_base="dc=example,dc=test",
                search_filter=None,
                group_search_base=None,
                username_attribute=None,
                display_name_attribute=None,
                membership_mode="memberof",
                nested_groups=False,
                admin_group_dn=None,
                admin_group_filter=None,
                operator_group_dn=None,
                operator_group_filter=None,
                viewer_group_dn=None,
                viewer_group_filter=None,
            ),
        )
        notification_settings.update_settings(
            session,
            notification_settings.NotificationSettingsUpdate(
                enabled_channels=["email", "webhook"],
                smtp_server="smtp.example.test",
                smtp_port=587,
                smtp_security="starttls",
                smtp_username="relay",
                smtp_password=_SMTP_PASSWORD,
                smtp_from_address="tlm@example.test",
                smtp_recipients=["ops@example.test"],
                webhook_urls=[f"https://hooks.example.test/x?token={_WEBHOOK_TOKEN}"],
                routing_rules=[],
            ),
        )
    _register_runner(
        _seed_project(),
        last_error=f"capture failed: rtsp://admin:{_CAMERA_PASSWORD}@10.0.0.5/stream",
    )

    for path in _READ_ROUTES:
        text = enabled_client.get(path, headers=_bearer()).text
        for secret in (
            _LDAP_BIND_PASSWORD,
            _SMTP_PASSWORD,
            _WEBHOOK_TOKEN,
            _CAMERA_PASSWORD,
        ):
            assert secret not in text, f"{secret!r} leaked from {path}"
    runners = enabled_client.get(_READ_ROUTES[2], headers=_bearer()).json()
    assert "rtsp://***@10.0.0.5/stream" in runners["runners"][0]["state"]["last_error"]


# ---------------------------------------------------------------------------
# Runtime log level
# ---------------------------------------------------------------------------


@pytest.fixture()
def capture_logger() -> Generator[logging.Logger, None, None]:
    target = logging.getLogger("timelapse_manager.capture")
    original = target.level
    yield target
    target.setLevel(original)


def test_log_level_override_applies_and_is_audited(
    enabled_client: TestClient, capture_logger: logging.Logger
) -> None:
    resp = enabled_client.post(
        _LOG_LEVEL_ROUTE,
        json={"level": "DEBUG", "logger": "timelapse_manager.capture", "minutes": 30},
        headers=_bearer(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"]["level"] == "DEBUG"
    assert body["applied"]["previous_level"] == "NOTSET"
    assert capture_logger.level == logging.DEBUG
    assert logging.getLogger("timelapse_manager.capture.supervisor").isEnabledFor(
        logging.DEBUG
    )
    with session_scope(get_context().session_factory) as session:
        messages = [
            event.message
            for event in session.execute(select(Event)).scalars()
            if (event.event_metadata or {}).get("action") == "debug.log_level"
        ]
    assert messages == [
        "log level for timelapse_manager.capture set to DEBUG for 30 minute(s)"
    ]


def test_log_level_override_reverts_on_shutdown(
    tmp_path: Path, alembic_cfg: Config, capture_logger: logging.Logger
) -> None:
    capture_logger.setLevel(logging.NOTSET)
    for client in _client(_debug_settings(tmp_path, enabled=True), alembic_cfg):
        resp = client.post(
            _LOG_LEVEL_ROUTE,
            json={"level": "DEBUG", "logger": "timelapse_manager.capture"},
            headers=_bearer(),
        )
        assert resp.status_code == 200
        assert capture_logger.level == logging.DEBUG
    assert capture_logger.level == logging.NOTSET


@pytest.mark.parametrize(
    "payload",
    [
        {"level": "DEBUG", "logger": "root"},
        {"level": "DEBUG", "logger": "uvicorn.access"},
        {"level": "DEBUG", "logger": "timelapse_managerX"},
        {"level": "DEBUG", "logger": "timelapse_manager..capture"},
        {"level": "TRACE"},
        {"level": "DEBUG", "minutes": 0},
        {"level": "DEBUG", "minutes": 241},
    ],
)
def test_log_level_rejects_invalid_requests(
    enabled_client: TestClient, payload: dict[str, object]
) -> None:
    resp = enabled_client.post(_LOG_LEVEL_ROUTE, json=payload, headers=_bearer())
    assert resp.status_code == 422


def test_log_level_requires_admin(enabled_client: TestClient) -> None:
    resp = enabled_client.post(_LOG_LEVEL_ROUTE, json={"level": "DEBUG"})
    assert resp.status_code == 401
