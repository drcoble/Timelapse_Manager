"""API-level tests for the Prometheus metrics endpoint.

The endpoint is off by default, invisible (404) while disabled, and gated behind
administrator authentication once enabled. These tests cover all three access
states and assert that the emitted exposition reflects seeded database state,
including correct Prometheus label escaping.

Fixtures are defined locally rather than in the shared conftest so this module
can toggle the ``observability.metrics_enabled`` flag per app, which the shared
client fixtures do not expose.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from timelapse_manager.app import create_app
from timelapse_manager.config.settings import (
    CaptureSettings,
    DatabaseSettings,
    LoggingSettings,
    MonitoringSettings,
    ObservabilitySettings,
    PathsSettings,
    RenderSettings,
    SecretsSettings,
    Settings,
)
from timelapse_manager.db.engine import create_db_engine
from timelapse_manager.db.models import Camera, Event, Frame, Project, RenderJob
from timelapse_manager.db.session import create_session_factory, session_scope
from timelapse_manager.security.token import ensure_local_token

# ---------------------------------------------------------------------------
# Settings / app fixtures (local: they toggle the metrics flag per app)
# ---------------------------------------------------------------------------


def _metrics_settings(tmp_path: Path, *, enabled: bool) -> Settings:
    """Build isolated settings with the metrics flag set as requested."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    db_path = tmp_path / "metrics_test.db"
    return Settings(
        database=DatabaseSettings(url=f"sqlite:///{db_path}"),
        logging=LoggingSettings(level="WARNING", format="text"),
        paths=PathsSettings(
            data_dir=data_dir,
            frames_root=data_dir / "frames",
            token_file=data_dir / ".local-token",
        ),
        capture=CaptureSettings(autostart=False),
        render=RenderSettings(autostart=False),
        monitoring=MonitoringSettings(autostart=False),
        observability=ObservabilitySettings(metrics_enabled=enabled),
        secrets=SecretsSettings(use_os_keystore=False),
    )


def _migrate(alembic_cfg: Config, url: str) -> None:
    """Run all migrations against the database the app will open."""
    alembic_cfg.set_main_option("sqlalchemy.url", url)
    alembic_command.upgrade(alembic_cfg, "head")


@pytest.fixture()
def disabled_settings(tmp_path: Path) -> Settings:
    return _metrics_settings(tmp_path, enabled=False)


@pytest.fixture()
def enabled_settings(tmp_path: Path) -> Settings:
    return _metrics_settings(tmp_path, enabled=True)


@pytest.fixture()
def disabled_client(
    disabled_settings: Settings, alembic_cfg: Config
) -> Generator[TestClient, None, None]:
    _migrate(alembic_cfg, disabled_settings.database.url)
    app = create_app(disabled_settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def enabled_client(
    enabled_settings: Settings, alembic_cfg: Config
) -> Generator[TestClient, None, None]:
    _migrate(alembic_cfg, enabled_settings.database.url)
    app = create_app(enabled_settings)
    with TestClient(app) as c:
        yield c


def _admin_headers(settings: Settings) -> dict[str, str]:
    """Return a bearer header for the local token, which resolves to the admin.

    With no session cookie, the admin gate falls back to the local bearer token
    and yields the sentinel administrator -- the most faithful authenticated path
    for an automated scraper.
    """
    return {"Authorization": f"Bearer {ensure_local_token(settings)}"}


# ---------------------------------------------------------------------------
# DB seed helpers
# ---------------------------------------------------------------------------


def _factory(settings: Settings) -> sessionmaker[Session]:
    """Build a session factory against the settings' database file."""
    return create_session_factory(create_db_engine(settings.database.url))


def _seed_project(
    factory: sessionmaker[Session],
    *,
    name: str,
    frame_count: int = 0,
    lifecycle_state: str = "active",
) -> int:
    with session_scope(factory) as session:
        cam = Camera(name=f"{name}-cam", address="127.0.0.1", protocol="vapix")
        session.add(cam)
        session.flush()
        proj = Project(
            camera_id=cam.id,
            name=name,
            lifecycle_state=lifecycle_state,
            operational_status="idle",
            frame_count=frame_count,
        )
        session.add(proj)
        session.flush()
        return proj.id


def _seed_frame(
    factory: sessionmaker[Session],
    project_id: int,
    *,
    sequence_index: int,
    size_bytes: int | None,
    lifecycle_state: str = "active",
) -> None:
    with session_scope(factory) as session:
        session.add(
            Frame(
                project_id=project_id,
                sequence_index=sequence_index,
                file_size_bytes=size_bytes,
                lifecycle_state=lifecycle_state,
            )
        )


def _seed_event(
    factory: sessionmaker[Session],
    *,
    project_id: int,
    event_type: str,
    level: str = "warning",
) -> None:
    with session_scope(factory) as session:
        session.add(
            Event(
                scope="project",
                scope_id=project_id,
                level=level,
                message=f"{event_type} occurred",
                event_metadata={"type": event_type},
            )
        )


def _seed_render_job(
    factory: sessionmaker[Session],
    project_id: int,
    *,
    status: str,
) -> None:
    with session_scope(factory) as session:
        session.add(RenderJob(project_id=project_id, status=status))


def _parse_samples(body: str) -> dict[str, float]:
    """Parse exposition body into a ``{full_sample_key: value}`` map.

    The key is the metric name with any label set kept verbatim (e.g.
    ``disk_used_bytes{project="a"}``), so labelled and unlabelled series of the
    same metric are distinguishable.
    """
    samples: dict[str, float] = {}
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        key, _, value = line.rpartition(" ")
        samples[key] = float(value)
    return samples


# ---------------------------------------------------------------------------
# Access-control tests
# ---------------------------------------------------------------------------


def test_disabled_returns_404(disabled_client: TestClient) -> None:
    """Default (disabled) deployments answer 404, as if the route were absent."""
    resp = disabled_client.get("/api/v1/metrics")
    assert resp.status_code == 404


def test_disabled_unauthenticated_returns_404_not_401(
    disabled_client: TestClient,
) -> None:
    """A disabled endpoint hides behind 404 even for an unauthenticated scrape.

    This asserts the enable-guard runs before the admin gate: were the order
    reversed, an unauthenticated scrape would leak the endpoint's existence with
    a 401.
    """
    resp = disabled_client.get("/api/v1/metrics")
    assert resp.status_code == 404


def test_enabled_unauthenticated_is_rejected(enabled_client: TestClient) -> None:
    """An enabled endpoint refuses an unauthenticated scrape."""
    resp = enabled_client.get("/api/v1/metrics")
    assert resp.status_code in (401, 403)
    assert resp.status_code != 200


def test_enabled_admin_returns_exposition(
    enabled_client: TestClient, enabled_settings: Settings
) -> None:
    """An enabled endpoint serves Prometheus exposition to an admin scraper."""
    headers = _admin_headers(enabled_settings)
    resp = enabled_client.get("/api/v1/metrics", headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "version=0.0.4" in resp.headers["content-type"]

    body = resp.text
    expected_names = {
        "frames_captured_total",
        "capture_failures_total",
        "render_failures_total",
        "projects_active",
        "disk_used_bytes",
    }
    for name in expected_names:
        assert f"# HELP {name} " in body, f"missing HELP for {name}"
        assert f"# TYPE {name} " in body, f"missing TYPE for {name}"

    assert "# TYPE frames_captured_total counter" in body
    assert "# TYPE capture_failures_total counter" in body
    assert "# TYPE render_failures_total counter" in body
    assert "# TYPE projects_active gauge" in body
    assert "# TYPE disk_used_bytes gauge" in body
    assert body.endswith("\n")


# ---------------------------------------------------------------------------
# Value / correctness tests
# ---------------------------------------------------------------------------


def test_values_reflect_seeded_data(
    enabled_client: TestClient, enabled_settings: Settings
) -> None:
    """Seeded frames, failures, usage, and active projects show in the exposition."""
    factory = _factory(enabled_settings)

    # Two active projects and one archived one.
    p_active = _seed_project(factory, name="alpha", frame_count=3)
    _seed_project(factory, name="beta", frame_count=2)
    _seed_project(factory, name="gamma", frame_count=0, lifecycle_state="archived")

    # Disk usage: alpha holds 100 + 200 active bytes; a soft-deleted frame and a
    # NULL-size frame both contribute nothing.
    _seed_frame(factory, p_active, sequence_index=0, size_bytes=100)
    _seed_frame(factory, p_active, sequence_index=1, size_bytes=200)
    _seed_frame(
        factory,
        p_active,
        sequence_index=2,
        size_bytes=999,
        lifecycle_state="soft_deleted",
    )
    _seed_frame(factory, p_active, sequence_index=3, size_bytes=None)

    # Capture failures: two qualifying event types plus one unrelated event.
    _seed_event(factory, project_id=p_active, event_type="capture.gap")
    _seed_event(factory, project_id=p_active, event_type="capture.stalled")
    _seed_event(factory, project_id=p_active, event_type="render.complete")

    # Render failures: one failed job, one successful job.
    _seed_render_job(factory, p_active, status="failed")
    _seed_render_job(factory, p_active, status="done")

    headers = _admin_headers(enabled_settings)
    resp = enabled_client.get("/api/v1/metrics", headers=headers)
    assert resp.status_code == 200
    samples = _parse_samples(resp.text)

    # frames_captured_total = sum of project frame_count = 3 + 2 + 0.
    assert samples["frames_captured_total"] == 5
    # Only capture.gap + capture.stalled count.
    assert samples["capture_failures_total"] == 2
    # Only the failed render job counts.
    assert samples["render_failures_total"] == 1
    # Two active projects (archived gamma excluded).
    assert samples["projects_active"] == 2
    # Total disk usage = 100 + 200 (soft-deleted and NULL excluded).
    assert samples["disk_used_bytes"] == 300
    # Per-project series: alpha 300, beta 0, gamma 0.
    assert samples['disk_used_bytes{project="alpha"}'] == 300
    assert samples['disk_used_bytes{project="beta"}'] == 0
    assert samples['disk_used_bytes{project="gamma"}'] == 0


def test_frames_total_climbs_with_more_frames(
    enabled_client: TestClient, enabled_settings: Settings
) -> None:
    """Adding a project with frames raises ``frames_captured_total``."""
    factory = _factory(enabled_settings)
    headers = _admin_headers(enabled_settings)

    _seed_project(factory, name="first", frame_count=4)
    before = _parse_samples(enabled_client.get("/api/v1/metrics", headers=headers).text)

    _seed_project(factory, name="second", frame_count=6)
    after = _parse_samples(enabled_client.get("/api/v1/metrics", headers=headers).text)

    assert before["frames_captured_total"] == 4
    assert after["frames_captured_total"] == 10


def test_label_value_escaping(
    enabled_client: TestClient, enabled_settings: Settings
) -> None:
    """A project name with a quote and backslash is escaped per Prometheus rules."""
    factory = _factory(enabled_settings)
    # Raw name contains a double quote and a backslash.
    raw_name = 'we"ir\\d'
    pid = _seed_project(factory, name=raw_name, frame_count=1)
    _seed_frame(factory, pid, sequence_index=0, size_bytes=50)

    headers = _admin_headers(enabled_settings)
    resp = enabled_client.get("/api/v1/metrics", headers=headers)
    assert resp.status_code == 200
    body = resp.text

    # The escaped form: \" for the quote and \\ for the backslash.
    expected_line = 'disk_used_bytes{project="we\\"ir\\\\d"} 50'
    assert expected_line in body, (
        f"escaped label line not found.\nexpected: {expected_line!r}\nbody:\n{body}"
    )
