"""Tests for the GET /healthz readiness endpoint (#67 S19, R5).

Two views:

* **Anyone** gets ``{"status", "db_status"}``. The raw text keeps the compact
  ``"db_status":"ok"`` the OpenNMS pollers match.
* **A loopback peer** (relaying for nobody) or a **metrics token** from an
  allowed peer gets the full body: versions, ffmpeg path, alembic revision,
  component map and readiness checks. ``tlm-upgrade.sh`` matches
  ``"app_version":"<ver>"`` in it.

``status`` is ``failed`` (503) on a database error or a dead loop, ``degraded``
(200) on a stall signal, ``ok`` (200) otherwise.
"""

from __future__ import annotations

import subprocess
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient

import timelapse_manager
from tests.conftest import LOOPBACK_PEER
from timelapse_manager.app import create_app
from timelapse_manager.capture.supervisor import (
    CaptureState,
    RunnerProblem,
    RunnerSnapshot,
)
from timelapse_manager.config.settings import (
    ObservabilitySettings,
    ServerSettings,
    Settings,
)
from timelapse_manager.monitoring import readiness
from timelapse_manager.runtime import get_context
from timelapse_manager.security.token import read_token_file

OPENNMS = ("10.0.12.104", 40000)
EDGE_PROXY = "192.0.2.10"
FULL_KEYS = {
    "status",
    "app_version",
    "ffmpeg_version",
    "ffmpeg_path",
    "db_status",
    "alembic_revision",
    "components",
    "checks",
}
MINIMAL_KEYS = {"status", "db_status"}


# ---------------------------------------------------------------------------
# Fakes for the background components (memory-only signals)
# ---------------------------------------------------------------------------


class _FakeSupervisor:
    """A running supervisor exposing just what readiness reads."""

    _started = True

    def __init__(
        self,
        *,
        last_reconcile_at: datetime | None = None,
        snapshots: list[RunnerSnapshot] | None = None,
        wedge: timedelta = timedelta(minutes=10),
    ) -> None:
        self.last_reconcile_at = (
            last_reconcile_at if last_reconcile_at is not None else datetime.now(UTC)
        )
        self._snapshots = snapshots or []
        self._wedge = wedge

    def is_running(self) -> bool:
        return True

    def runner_snapshots(self) -> list[RunnerSnapshot]:
        return list(self._snapshots)

    def runner_problems(self) -> list[RunnerProblem]:
        """Stand in for the supervisor's own definition, over the same snapshots."""
        now = datetime.now(UTC)
        problems: list[RunnerProblem] = []
        for snapshot in self._snapshots:
            state = snapshot.state
            if snapshot.task_done:
                if state.state != "stopped":
                    problems.append(RunnerProblem(state.project_id, "had exited"))
                continue
            beat = state.last_cycle_at or state.started_at
            if beat is not None and now - beat > self._wedge:
                problems.append(RunnerProblem(state.project_id, "wedged"))
        return problems


class _FakeQueue:
    _started = True

    def __init__(
        self, *, orphans: int = 0, overruns: list[float] | None = None
    ) -> None:
        self.unreclaimed_orphans = orphans
        self.oldest_encoding_started_at = None
        self._overruns = overruns or []

    def is_running(self) -> bool:
        return True

    def encoding_overruns(self, now: datetime) -> list[float]:
        return list(self._overruns)


class _FakeDispatcher:
    _started = True

    def __init__(self, *, age: float | None = 0.0) -> None:
        self._age = age

    def is_running(self) -> bool:
        return True

    def last_poll_age_seconds(self, now: float | None = None) -> float | None:
        return self._age


class _FakeLagMonitor:
    loop = None

    def __init__(self, last: float = 0.0, recent: list[float] | None = None) -> None:
        self._recent = recent if recent is not None else [last]
        self.last_lag_seconds = self._recent[-1] if self._recent else 0.0
        self.lag_seconds = max(self._recent, default=0.0)

    def recent_lags(self, count: int) -> list[float]:
        return self._recent[-count:]


def _snapshot(
    *,
    state: str = "running",
    task_done: bool = False,
    last_cycle_at: datetime | None = None,
    pause_reason: str | None = None,
) -> RunnerSnapshot:
    now = datetime.now(UTC)
    capture_state = CaptureState(
        project_id=1,
        camera_id=1,
        state=state,
        started_at=now,
        last_cycle_at=last_cycle_at or now,
        pause_reason=pause_reason,
    )
    return RunnerSnapshot(
        project_name="Roof",
        state=capture_state,
        plan=None,
        task_name="capture-project-1",
        task_done=task_done,
    )


def _install(monkeypatch: pytest.MonkeyPatch, **components: Any) -> None:
    """Swap healthy fakes (or the given overrides) into the live context.

    The lifespan stops its own local references at shutdown, so the fakes are
    never stopped.
    """
    context = get_context()
    healthy: dict[str, Any] = {
        "capture_supervisor": _FakeSupervisor(),
        "render_queue": _FakeQueue(),
        "notification_dispatcher": _FakeDispatcher(),
        "loop_lag_monitor": _FakeLagMonitor(),
    }
    healthy.update(components)
    for name, value in healthy.items():
        monkeypatch.setattr(context, name, value)


# ---------------------------------------------------------------------------
# Who sees what
# ---------------------------------------------------------------------------


class TestDisclosure:
    """The full body is for this host or the metrics token; others get two keys."""

    def test_loopback_gets_the_full_body(self, client: TestClient) -> None:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert set(response.json()) == FULL_KEYS
        # tlm-upgrade.sh's exact match, on the raw text.
        assert f'"app_version":"{timelapse_manager.__version__}"' in response.text
        assert '"db_status":"ok"' in response.text

    @pytest.mark.parametrize("host", ["::1", "::ffff:127.0.0.1", "127.0.0.2"])
    def test_other_loopback_forms_get_the_full_body(
        self, settings: Settings, host: str
    ) -> None:
        with TestClient(create_app(settings), client=(host, 50000)) as c:
            body = c.get("/healthz").json()
        assert set(body) == FULL_KEYS

    def test_opennms_poller_gets_the_minimal_body(self, settings: Settings) -> None:
        with TestClient(create_app(settings), client=OPENNMS) as c:
            response = c.get("/healthz")
        assert response.status_code == 200
        assert set(response.json()) == MINIMAL_KEYS
        # The literal both OpenNMS pollers match (compact JSON).
        assert '"db_status":"ok"' in response.text
        assert '"status":"ok"' in response.text
        for leak in ("ffmpeg_path", "app_version", "alembic_revision", "components"):
            assert leak not in response.text

    def test_proxied_request_gets_the_minimal_body(self, settings: Settings) -> None:
        """Through EDGE_PROXY nginx (a trusted proxy) the caller is remote."""
        configured = settings.model_copy(
            update={"server": ServerSettings(trusted_proxies=[EDGE_PROXY])}
        )
        with TestClient(create_app(configured), client=(EDGE_PROXY, 50000)) as c:
            response = c.get(
                "/healthz",
                headers={
                    "X-Forwarded-For": "203.0.113.9",
                    "X-Forwarded-Proto": "https",
                },
            )
        assert set(response.json()) == MINIMAL_KEYS

    @pytest.mark.parametrize(
        "header", ["X-Forwarded-For", "X-Forwarded-Proto", "Forwarded"]
    )
    def test_loopback_relaying_for_someone_gets_the_minimal_body(
        self, client: TestClient, header: str
    ) -> None:
        """A same-host proxy forwarding a remote caller does not count as local."""
        value = "for=203.0.113.9" if header == "Forwarded" else "203.0.113.9"
        response = client.get("/healthz", headers={header: value})
        assert set(response.json()) == MINIMAL_KEYS

    def test_non_ip_peer_gets_the_minimal_body(self, settings: Settings) -> None:
        # Starlette's default peer is the literal "testclient".
        with TestClient(create_app(settings)) as c:
            response = c.get("/healthz")
        assert response.status_code == 200
        assert set(response.json()) == MINIMAL_KEYS

    def test_remote_failure_is_a_minimal_503(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(readiness, "db_status", lambda _factory: "error")
        with TestClient(create_app(settings), client=OPENNMS) as c:
            response = c.get("/healthz")
        assert response.status_code == 503
        assert response.json() == {"status": "failed", "db_status": "error"}


# ---------------------------------------------------------------------------
# The metrics token unlocks the full body
# ---------------------------------------------------------------------------


@pytest.fixture()
def metrics_app(
    settings_no_autostart: Settings, alembic_cfg: Config
) -> Generator[tuple[FastAPI, Settings], None, None]:
    configured = settings_no_autostart.model_copy(
        update={
            "observability": ObservabilitySettings(
                metrics_enabled=True, metrics_allowed_clients=["10.0.12.104/32"]
            ),
            "server": ServerSettings(trusted_proxies=[EDGE_PROXY]),
        }
    )
    alembic_cfg.set_main_option("sqlalchemy.url", configured.database.url)
    alembic_command.upgrade(alembic_cfg, "head")
    app = create_app(configured)
    with TestClient(app, client=LOOPBACK_PEER):
        yield app, configured


def _metrics_token(settings: Settings) -> str:
    return read_token_file(settings.paths.data_dir / ".metrics-token")


class TestMetricsToken:
    def test_valid_token_from_allowed_peer_gets_the_full_body(
        self, metrics_app: tuple[FastAPI, Settings]
    ) -> None:
        app, configured = metrics_app
        token = _metrics_token(configured)
        with TestClient(app, client=OPENNMS) as c:
            response = c.get("/healthz", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert set(response.json()) == FULL_KEYS

    def test_wrong_token_is_not_an_error(
        self, metrics_app: tuple[FastAPI, Settings]
    ) -> None:
        app, _ = metrics_app
        with TestClient(app, client=OPENNMS) as c:
            response = c.get("/healthz", headers={"Authorization": "Bearer nope"})
        assert response.status_code == 200
        assert set(response.json()) == MINIMAL_KEYS
        assert "www-authenticate" not in response.headers

    def test_valid_token_from_other_peer_gets_the_minimal_body(
        self, metrics_app: tuple[FastAPI, Settings]
    ) -> None:
        app, configured = metrics_app
        token = _metrics_token(configured)
        with TestClient(app, client=("10.0.12.50", 40000)) as c:
            response = c.get("/healthz", headers={"Authorization": f"Bearer {token}"})
        assert set(response.json()) == MINIMAL_KEYS

    def test_local_admin_token_does_not_unlock_it_remotely(
        self, metrics_app: tuple[FastAPI, Settings]
    ) -> None:
        app, configured = metrics_app
        local = read_token_file(configured.paths.data_dir / ".local-token")
        with TestClient(app, client=OPENNMS) as c:
            response = c.get("/healthz", headers={"Authorization": f"Bearer {local}"})
        assert set(response.json()) == MINIMAL_KEYS

    def test_unusable_token_file_gives_the_minimal_body(
        self, metrics_app: tuple[FastAPI, Settings]
    ) -> None:
        app, configured = metrics_app
        token = _metrics_token(configured)
        path = configured.paths.data_dir / ".metrics-token"
        path.write_text("")  # an empty token file is unusable
        with TestClient(app, client=OPENNMS) as c:
            response = c.get("/healthz", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert set(response.json()) == MINIMAL_KEYS

    def test_non_ascii_token_is_refused_quietly(
        self,
        metrics_app: tuple[FastAPI, Settings],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``compare_digest`` raises on non-ASCII text; it must not get there."""
        import timelapse_manager.app as app_mod

        logged = MagicMock()
        monkeypatch.setattr(app_mod, "logger", logged)
        app, _ = metrics_app
        token = "Bearer \xe9t\xe9".encode("latin-1")
        with TestClient(app, client=OPENNMS) as c:
            response = c.get("/healthz", headers={"Authorization": token})
        assert response.status_code == 200
        assert set(response.json()) == MINIMAL_KEYS
        logged.exception.assert_not_called()

    def test_metrics_disabled_ignores_the_token_and_creates_no_file(
        self, settings: Settings
    ) -> None:
        with TestClient(create_app(settings), client=OPENNMS) as c:
            response = c.get("/healthz", headers={"Authorization": "Bearer anything"})
        assert set(response.json()) == MINIMAL_KEYS
        assert not (settings.paths.data_dir / ".metrics-token").exists()


# ---------------------------------------------------------------------------
# Full-body shape
# ---------------------------------------------------------------------------


class TestHealthzShape:
    def test_full_body_fields(self, client: TestClient) -> None:
        body = client.get("/healthz").json()
        assert body["status"] == "ok"
        assert body["app_version"] == timelapse_manager.__version__
        assert isinstance(body["ffmpeg_version"], str) and body["ffmpeg_version"]
        assert isinstance(body["ffmpeg_path"], str) and body["ffmpeg_path"].strip()
        assert body["db_status"] == "ok"
        assert isinstance(body["alembic_revision"], str)

    def test_checks_have_status_and_value(self, client: TestClient) -> None:
        checks = client.get("/healthz").json()["checks"]
        assert {
            "database",
            "capture_reconcile",
            "capture_runners",
            "capture_low_disk",
            "render_orphans",
            "render_overdue_encoding",
            "notification_poll",
            "event_loop_lag",
        } <= set(checks)
        for check in checks.values():
            assert set(check) == {"status", "value"}
        assert checks["database"] == {"status": "ok", "value": "ok"}

    def test_status_is_the_first_key_of_both_bodies(
        self, client: TestClient, settings: Settings
    ) -> None:
        """A text probe can anchor on the top-level status with a prefix match.

        Every check also carries ``"status":"ok"``, so a substring match on the
        full body cannot tell a degraded instance; ``{"status":"ok"`` can.
        """
        assert client.get("/healthz").text.startswith('{"status":"ok",')
        with TestClient(create_app(settings), client=OPENNMS) as c:
            assert c.get("/healthz").text == '{"status":"ok","db_status":"ok"}'

    def test_components_map_is_unchanged(self, client: TestClient) -> None:
        components = client.get("/healthz").json()["components"]
        assert set(components) == {
            "capture_supervisor",
            "render_queue",
            "render_scheduler",
            "notification_dispatcher",
        }
        # Not autostarted: absent, not dead, and absent never degrades.
        assert all(word == "absent" for word in components.values())

    def test_absent_components_keep_status_ok(self, client: TestClient) -> None:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_is_unauthenticated(self, client: TestClient) -> None:
        assert client.get("/healthz").status_code == 200


# ---------------------------------------------------------------------------
# failed (503)
# ---------------------------------------------------------------------------


class TestFailed:
    def test_db_error_is_503_failed(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(readiness, "db_status", lambda _factory: "error")
        response = client.get("/healthz")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "failed"
        assert body["db_status"] == "error"
        assert set(body) == FULL_KEYS
        assert body["checks"]["database"]["status"] == "failed"

    def test_db_probe_exception_is_503_not_500(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(_factory: object) -> str:
            raise RuntimeError("db probe blew up")

        monkeypatch.setattr(readiness, "db_status", _boom)
        response = client.get("/healthz")
        assert response.status_code == 503
        assert response.json()["status"] == "failed"
        assert response.json()["db_status"] != "ok"

    def test_dead_component_is_503_failed(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Dead:
            _started = True

            def is_running(self) -> bool:
                return False

        monkeypatch.setattr(get_context(), "render_queue", _Dead())
        response = client.get("/healthz")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "failed"
        assert body["components"]["render_queue"] == "dead"
        assert body["checks"]["component_render_queue"]["status"] == "failed"


# ---------------------------------------------------------------------------
# degraded (200): one test per signal
# ---------------------------------------------------------------------------


def _degraded(client: TestClient, check: str) -> dict[str, Any]:
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded", body["checks"]
    assert body["checks"][check]["status"] == "degraded"
    others = [
        name
        for name, value in body["checks"].items()
        if value["status"] == "degraded" and name != check
    ]
    assert others == []
    return body


class TestDegraded:
    def test_all_healthy_fakes_are_ok(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch)
        body = client.get("/healthz").json()
        assert body["status"] == "ok", body["checks"]

    def test_stale_reconcile(
        self, client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        interval = settings.capture.reconcile_interval_seconds
        stale = datetime.now(UTC) - timedelta(seconds=3 * interval + 5)
        _install(
            monkeypatch, capture_supervisor=_FakeSupervisor(last_reconcile_at=stale)
        )
        _degraded(client, "capture_reconcile")

    def test_reconcile_just_inside_threshold_is_ok(
        self, client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        interval = settings.capture.reconcile_interval_seconds
        recent = datetime.now(UTC) - timedelta(seconds=3 * interval - 5)
        _install(
            monkeypatch, capture_supervisor=_FakeSupervisor(last_reconcile_at=recent)
        )
        assert client.get("/healthz").json()["status"] == "ok"

    def test_wedged_runner(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old = datetime.now(UTC) - timedelta(minutes=30)
        supervisor = _FakeSupervisor(
            snapshots=[_snapshot(last_cycle_at=old)], wedge=timedelta(minutes=10)
        )
        _install(monkeypatch, capture_supervisor=supervisor)
        body = _degraded(client, "capture_runners")
        assert body["checks"]["capture_runners"]["value"] == 1

    def test_exited_runner(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        supervisor = _FakeSupervisor(snapshots=[_snapshot(task_done=True)])
        _install(monkeypatch, capture_supervisor=supervisor)
        _degraded(client, "capture_runners")

    def test_stopped_or_erroring_runner_is_not_a_problem(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A finished campaign, or a camera that is offline, is not a stall."""
        supervisor = _FakeSupervisor(
            snapshots=[
                _snapshot(state="stopped", task_done=True),
                _snapshot(state="error"),
            ]
        )
        _install(monkeypatch, capture_supervisor=supervisor)
        assert client.get("/healthz").json()["status"] == "ok"

    def test_low_disk_pause(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        supervisor = _FakeSupervisor(snapshots=[_snapshot(pause_reason="low_disk")])
        _install(monkeypatch, capture_supervisor=supervisor)
        _degraded(client, "capture_low_disk")

    def test_window_pause_is_ok(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        supervisor = _FakeSupervisor(snapshots=[_snapshot(pause_reason="window")])
        _install(monkeypatch, capture_supervisor=supervisor)
        assert client.get("/healthz").json()["status"] == "ok"

    def test_unreclaimed_orphans(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, render_queue=_FakeQueue(orphans=2))
        body = _degraded(client, "render_orphans")
        assert body["checks"]["render_orphans"]["value"] == 2

    def test_encoding_job_past_its_watchdog_budget(
        self, client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        grace = settings.render.stall_timeout_seconds
        _install(monkeypatch, render_queue=_FakeQueue(overruns=[grace + 1, -50.0]))
        body = _degraded(client, "render_overdue_encoding")
        assert body["checks"]["render_overdue_encoding"]["value"] == 1

    def test_encoding_job_within_its_budget_is_ok(
        self, client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Inside the budget, or past it by less than one stall timeout."""
        grace = settings.render.stall_timeout_seconds
        _install(monkeypatch, render_queue=_FakeQueue(overruns=[-2700.0, grace - 1]))
        assert client.get("/healthz").json()["status"] == "ok"

    def test_stale_notification_poll(
        self, client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        age = 6 * settings.monitoring.poll_interval_seconds + 1
        _install(monkeypatch, notification_dispatcher=_FakeDispatcher(age=age))
        _degraded(client, "notification_poll")

    def test_loop_lag(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(
            monkeypatch,
            loop_lag_monitor=_FakeLagMonitor(recent=[0.0, 1.5, 1.2, 1.5]),
        )
        _degraded(client, "event_loop_lag")

    def test_loop_lag_single_late_tick_does_not_flap(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#117: one slow tick between healthy ones keeps /healthz ``ok``."""
        _install(
            monkeypatch,
            loop_lag_monitor=_FakeLagMonitor(recent=[0.0, 0.0, 2.0, 0.0]),
        )
        assert client.get("/healthz").json()["status"] == "ok"
        _install(
            monkeypatch,
            loop_lag_monitor=_FakeLagMonitor(recent=[0.0, 2.0, 0.0, 2.0]),
        )
        assert client.get("/healthz").json()["status"] == "ok"
        _install(monkeypatch, loop_lag_monitor=_FakeLagMonitor(recent=[2.0, 2.0, 0.0]))
        assert client.get("/healthz").json()["status"] == "ok"

    def test_loop_lag_two_late_ticks_are_still_ok(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, loop_lag_monitor=_FakeLagMonitor(recent=[2.0, 2.0]))
        assert client.get("/healthz").json()["status"] == "ok"

    def test_loop_lag_one_severe_tick_degrades_at_once(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, loop_lag_monitor=_FakeLagMonitor(recent=[0.0, 0.0, 5.0]))
        _degraded(client, "event_loop_lag")

    def test_degraded_body_for_opennms_still_matches(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Decision D4: OpenNMS keeps matching ``"db_status":"ok"`` when degraded."""
        with TestClient(create_app(settings), client=OPENNMS) as c:
            _install(monkeypatch, loop_lag_monitor=_FakeLagMonitor(last=7.0))
            response = c.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "degraded", "db_status": "ok"}
        assert '"db_status":"ok"' in response.text

    def test_a_broken_reader_degrades_instead_of_raising(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Broken(_FakeQueue):
            @property
            def unreclaimed_orphans(self) -> int:  # type: ignore[override]
                raise RuntimeError("boom")

            @unreclaimed_orphans.setter
            def unreclaimed_orphans(self, _value: int) -> None:
                pass

        _install(monkeypatch, render_queue=_Broken())
        body = _degraded(client, "render_orphans")
        assert body["checks"]["render_orphans"]["value"] == "error"


# ---------------------------------------------------------------------------
# Alembic revision and ffmpeg (full body)
# ---------------------------------------------------------------------------


class TestHealthzAlembicRevision:
    def test_revision_is_unknown_on_unmigrated_db(self, client: TestClient) -> None:
        assert client.get("/healthz").json()["alembic_revision"] == "unknown"

    def test_revision_is_current_head_after_migration(
        self, settings: Settings, alembic_cfg: Config
    ) -> None:
        alembic_cfg.set_main_option("sqlalchemy.url", settings.database.url)
        alembic_command.upgrade(alembic_cfg, "head")

        from alembic.script import ScriptDirectory

        head = ScriptDirectory.from_config(alembic_cfg).get_current_head()
        with TestClient(create_app(settings), client=LOOPBACK_PEER) as c:
            body = c.get("/healthz").json()
        assert body["alembic_revision"] == head


def _ffmpeg_body(settings: Settings) -> dict[str, Any]:
    with TestClient(create_app(settings), client=LOOPBACK_PEER) as c:
        response = c.get("/healthz")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


class TestHealthzFfmpeg:
    def test_unavailable_when_binary_missing(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "timelapse_manager.version.subprocess.run",
            lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
        )
        body = _ffmpeg_body(settings)
        assert body["ffmpeg_version"] == "unavailable"
        assert body["app_version"] == timelapse_manager.__version__

    def test_unavailable_on_timeout(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "timelapse_manager.version.subprocess.run",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(cmd="ffmpeg", timeout=5)
            ),
        )
        assert _ffmpeg_body(settings)["ffmpeg_version"] == "unavailable"

    @pytest.mark.parametrize(("code", "stdout"), [(1, ""), (0, "")])
    def test_unavailable_on_bad_output(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        code: int,
        stdout: str,
    ) -> None:
        completed = subprocess.CompletedProcess(
            args=["ffmpeg", "-version"], returncode=code, stdout=stdout, stderr=""
        )
        monkeypatch.setattr(
            "timelapse_manager.version.subprocess.run",
            lambda *args, **kwargs: completed,
        )
        assert _ffmpeg_body(settings)["ffmpeg_version"] == "unavailable"

    def test_first_line_of_stdout_is_the_version(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        expected = "ffmpeg version 6.1.2 Copyright (c) 2000-2024 the FFmpeg developers"
        completed = subprocess.CompletedProcess(
            args=["ffmpeg", "-version"],
            returncode=0,
            stdout=expected + "\nextra line",
            stderr="",
        )
        monkeypatch.setattr(
            "timelapse_manager.version.subprocess.run",
            lambda *args, **kwargs: completed,
        )
        assert _ffmpeg_body(settings)["ffmpeg_version"] == expected
