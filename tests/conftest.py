"""Shared pytest fixtures for the Timelapse Manager test suite."""

from __future__ import annotations

import re
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Generator
from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

import timelapse_manager.db.session as _db_session_mod
import timelapse_manager.runtime as _runtime_mod
import timelapse_manager.security.crypto as _crypto_mod
import timelapse_manager.web.routers.auth as _web_auth_mod
from timelapse_manager.app import create_app
from timelapse_manager.cameras.base import (
    CameraAdapter,
    CameraCapabilities,
    CapturedFrame,
    GeoLocation,
    ValidationResult,
)
from timelapse_manager.config.settings import (
    CaptureSettings,
    DatabaseSettings,
    LoggingSettings,
    MonitoringSettings,
    PathsSettings,
    RenderSettings,
    SecretsSettings,
    Settings,
    SsrfSettings,
)
from timelapse_manager.db.engine import create_db_engine
from timelapse_manager.db.models import Camera, Project
from timelapse_manager.db.session import create_session_factory, session_scope
from timelapse_manager.security.keystore import KeyFileProvider
from timelapse_manager.security.token import ensure_local_token
from timelapse_manager.storage.monitor import DiskSpaceMonitor


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers so pytest does not warn about unknown marks."""
    config.addinivalue_line(
        "markers",
        "slow: marks tests that are slow or spawn subprocesses"
        " (deselect with -m 'not slow')",
    )
    config.addinivalue_line(
        "markers",
        "live: marks tests that require real camera hardware and env-var"
        " credentials (skipped automatically when env vars are unset)",
    )
    config.addinivalue_line(
        "markers",
        "packaging: marks tests that exercise packaging/distribution concerns"
        " (resolver, pin file, SBOM, path helpers)",
    )
    config.addinivalue_line(
        "markers",
        "abuse: marks tests that probe security boundaries, denial logic, and"
        " hardening seams (SSRF, subprocess allowlists, crypto, redaction)",
    )
    config.addinivalue_line(
        "markers",
        "ldap_integration: marks tests that require a live LDAP directory"
        " (skipped automatically when the server is unreachable;"
        " set TLM_TEST_LDAP_URL to override the default ldap://127.0.0.1:3893)",
    )
    config.addinivalue_line(
        "markers",
        "ldap_live: marks tests that require real FreeIPA/LDAP directory"
        " credentials (skipped automatically when env vars are unset;"
        " requires TLM_TEST_LDAP_URL_TLS + TLM_TEST_LDAP_BIND_DN/PW etc.)",
    )


# ---------------------------------------------------------------------------
# Global-state isolation (autouse): reset the two process-wide singletons
# before and after every test so TestClient lifespans never cross-contaminate.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_globals() -> pytest.Generator[None, None, None]:
    """Reset process-level singletons before and after every test.

    The application lifespan installs two module-level globals:
    - ``timelapse_manager.runtime._context``
    - ``timelapse_manager.db.session._session_factory``

    Without this fixture, a TestClient from one test leaks its session factory
    and context into the next test, causing spurious failures or wrong results.
    """
    # Pre-test teardown: dispose any leftover context from a previous test.
    _runtime_mod.dispose()
    _db_session_mod._session_factory = None  # noqa: SLF001
    # The login throttle is a module-level singleton in web/routers/auth.py. Its
    # per-IP counters persist between tests because TestClient always uses
    # "testclient" as the client IP. Reset it so a bad-creds test in one
    # test does not throttle good-creds tests in later tests.
    _web_auth_mod._throttle = None  # noqa: SLF001

    yield

    # Post-test teardown: same cleanup in case the test installed new state.
    _runtime_mod.dispose()
    _db_session_mod._session_factory = None  # noqa: SLF001
    _web_auth_mod._throttle = None  # noqa: SLF001


# ---------------------------------------------------------------------------
# Crypto key-provider isolation (autouse): install a tmp-dir KeyFileProvider
# before every test so encryption calls never touch the real OS Keychain.
#
# This must run AFTER _reset_globals (which calls dispose() → set_key_provider
# (None)), so it depends on that fixture explicitly.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _crypto_key_provider(
    tmp_path: Path,
    _reset_globals: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    """Install an isolated file-backed key provider for every test.

    Sets TLM_SECRETS__USE_OS_KEYSTORE=false so Settings built during the test
    never attempt a keyring probe, and installs a KeyFileProvider on a
    per-test tmp path so encrypt_secret / decrypt_secret always have a working
    key without touching the real macOS Keychain.  Cleared on teardown.
    """
    monkeypatch.setenv("TLM_SECRETS__USE_OS_KEYSTORE", "false")
    key_file = tmp_path / ".secret-key"
    provider = KeyFileProvider(key_file)
    _crypto_mod.set_key_provider(provider)
    yield
    _crypto_mod.set_key_provider(None)


# ---------------------------------------------------------------------------
# Core database fixtures (retained from Phase 00; used by migration tests)
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_db_path(tmp_path: Path) -> Path:
    """Return a path inside pytest's temporary directory for a test SQLite DB."""
    return tmp_path / "test.db"


@pytest.fixture()
def tmp_db_url(tmp_db_path: Path) -> str:
    """Return a SQLite URL pointing at the temporary test database."""
    return f"sqlite:///{tmp_db_path}"


@pytest.fixture()
def alembic_cfg(tmp_db_url: str) -> Config:
    """Return an Alembic Config pointed at the temp DB and the real script dir.

    Uses absolute paths so migrations are hermetic regardless of the working
    directory pytest is invoked from.
    """
    alembic_ini = Path(__file__).parent.parent / "alembic.ini"
    alembic_dir = Path(__file__).parent.parent / "alembic"

    cfg = Config(str(alembic_ini))
    cfg.set_main_option("script_location", str(alembic_dir))
    cfg.set_main_option("sqlalchemy.url", tmp_db_url)
    return cfg


# ---------------------------------------------------------------------------
# Phase 01 fixtures: settings, fully-wired TestClient, and auth token
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    """Return a Settings instance pointed at isolated temp directories.

    Each test gets its own temp DB file, data directory, and token file so
    there is no shared filesystem state between tests.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = tmp_path / "test.db"

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
        secrets=SecretsSettings(use_os_keystore=False),
    )


@pytest.fixture()
def client(settings: Settings) -> pytest.Generator[TestClient, None, None]:
    """Yield a TestClient whose lifespan has fully executed.

    Using the context-manager form ensures the ASGI lifespan (startup +
    shutdown) runs, which installs the session factory, context, and token
    that most route tests depend on.
    """
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth_token(settings: Settings) -> str:
    """Return the local bearer token for the settings fixture.

    Calls ``ensure_local_token`` which is idempotent: it creates the token
    file on the first call and reads it back on subsequent calls, so this
    fixture and the lifespan's ``ensure_local_token`` call always agree.
    """
    return ensure_local_token(settings)


# ---------------------------------------------------------------------------
# Phase 02: settings fixture with autostart=False (prevents background tasks)
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings_no_autostart(tmp_path: Path) -> Settings:
    """Settings identical to ``settings`` but with capture.autostart=False.

    Any TestClient that exercises the camera or capture API must use this
    variant so the supervisor does not launch background tasks against the
    temp database (which has no qualifying projects anyway, but being explicit
    prevents timing surprises).

    Opts in the RFC-1918 ranges so tests that create cameras at private
    addresses (e.g. 10.0.0.x, 192.168.x.x) pass the SSRF guard. Loopback
    remains unconditionally blocked per the guard's design.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = tmp_path / "test.db"
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
        ssrf=SsrfSettings(
            allowed_private_subnets=["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
        ),
        secrets=SecretsSettings(use_os_keystore=False),
    )


# ---------------------------------------------------------------------------
# Phase 02: migrated session factory for unit tests that hit the DB directly
# ---------------------------------------------------------------------------


@pytest.fixture()
def migrated_factory(
    alembic_cfg: Config, tmp_db_url: str
) -> Generator[sessionmaker, None, None]:  # type: ignore[type-arg]
    """Yield a session factory backed by a fully-migrated temp SQLite DB."""
    alembic_command.upgrade(alembic_cfg, "head")
    engine = create_db_engine(tmp_db_url)
    factory = create_session_factory(engine)
    yield factory
    engine.dispose()


# ---------------------------------------------------------------------------
# Phase 02: migrated TestClient (camera/capture API tests)
# ---------------------------------------------------------------------------


@pytest.fixture()
def migrated_client(
    settings_no_autostart: Settings,
    alembic_cfg: Config,
) -> Generator[TestClient, None, None]:
    """Yield a TestClient whose DB has been fully migrated via Alembic.

    Runs ``alembic upgrade head`` against the same SQLite file the TestClient's
    lifespan will open, so all tables exist before the first request.
    """
    # Point alembic at the same DB URL the settings use.
    alembic_cfg.set_main_option("sqlalchemy.url", settings_no_autostart.database.url)
    alembic_command.upgrade(alembic_cfg, "head")

    app = create_app(settings_no_autostart)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def cam_auth_token(settings_no_autostart: Settings) -> str:
    """Bearer token matching the ``migrated_client`` fixture's settings."""
    return ensure_local_token(settings_no_autostart)


# ---------------------------------------------------------------------------
# Phase 02: mock HTTP snapshot server (subprocess)
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Return a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def mock_snapshot_server() -> Generator[str, None, None]:
    """Start the dev mock HTTP snapshot server on an ephemeral port.

    Yields the base URL ``http://127.0.0.1:<port>`` and terminates the
    subprocess on teardown.
    """
    port = _free_port()
    server_script = (
        Path(__file__).parent.parent / "dev" / "mock_cameras" / "http_snapshot.py"
    )
    proc = subprocess.Popen(
        [sys.executable, str(server_script), "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    base_url = f"http://127.0.0.1:{port}"
    # Wait until the healthz endpoint is reachable (max 5 s).
    deadline = time.monotonic() + 5.0
    import urllib.request

    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"{base_url}/healthz", timeout=0.5)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    else:
        proc.terminate()
        proc.wait()
        raise RuntimeError("mock snapshot server did not start in time")
    yield base_url
    proc.terminate()
    proc.wait()


# ---------------------------------------------------------------------------
# Phase 02: fake adapters for supervisor / isolation unit tests
# ---------------------------------------------------------------------------


class FakeAdapter(CameraAdapter):
    """A capture adapter that succeeds immediately with a tiny JPEG-shaped bytes."""

    # Minimal 1x1 grey JPEG (valid enough for _imageinfo not to choke)
    _BYTES = (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t"
        b"\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
        b"\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\x1e41=>"
        b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
        b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00"
        b"\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b"
        b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xfb\x03\xff\xd9"
    )

    def __init__(self, captured_at=None) -> None:
        from datetime import UTC, datetime

        self._captured_at = captured_at or datetime.now(UTC)

    async def capture(self) -> CapturedFrame:
        return CapturedFrame(
            image_bytes=self._BYTES,
            width=1,
            height=1,
            format="jpeg",
            captured_at=self._captured_at,
        )

    async def validate_connection(self) -> ValidationResult:
        return ValidationResult(ok=True, reason=None, message="ok")

    async def get_geolocation(self) -> GeoLocation | None:
        return None

    async def capabilities(self) -> CameraCapabilities:
        return CameraCapabilities(supported_resolutions=[])

    async def close(self) -> None:
        return None


class SlowAdapter(CameraAdapter):
    """An adapter that sleeps for longer than any reasonable timeout."""

    async def capture(self) -> CapturedFrame:
        import asyncio

        await asyncio.sleep(3600)
        raise RuntimeError("should never reach here")  # pragma: no cover

    async def validate_connection(self) -> ValidationResult:  # pragma: no cover
        return ValidationResult(ok=True, reason=None, message="ok")

    async def get_geolocation(self) -> GeoLocation | None:  # pragma: no cover
        return None

    async def capabilities(self) -> CameraCapabilities:  # pragma: no cover
        return CameraCapabilities(supported_resolutions=[])

    async def close(self) -> None:
        return None


class ErrorAdapter(CameraAdapter):
    """An adapter that always raises on capture."""

    async def capture(self) -> CapturedFrame:
        from timelapse_manager.cameras.base import OtherCaptureError

        raise OtherCaptureError("simulated camera failure")

    async def validate_connection(self) -> ValidationResult:  # pragma: no cover
        return ValidationResult(ok=True, reason=None, message="ok")

    async def get_geolocation(self) -> GeoLocation | None:  # pragma: no cover
        return None

    async def capabilities(self) -> CameraCapabilities:  # pragma: no cover
        return CameraCapabilities(supported_resolutions=[])

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Phase 02: seeded Camera + Project for capture integration tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def capture_target(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    tmp_path: Path,
) -> dict:
    """Return a dict with ids and paths for a seeded Camera + active Project.

    Inserts one Camera (vapix protocol) and one active Project with a 60s
    interval, so the CaptureSupervisor's ``_load_targets`` picks them up.
    The project's storage_path is set to a temp subdir.
    """
    storage = tmp_path / "frames"
    storage.mkdir()
    with session_scope(migrated_factory) as session:
        cam = Camera(
            name="test-cam",
            address="127.0.0.1",
            protocol="vapix",
            snapshot_uri="http://127.0.0.1/snapshot.jpg",
        )
        session.add(cam)
        session.flush()
        camera_id = cam.id
        proj = Project(
            camera_id=camera_id,
            name="test-project",
            capture_interval_seconds=60,
            lifecycle_state="active",
            operational_status="idle",
            storage_path=str(storage),
        )
        session.add(proj)
        session.flush()
        project_id = proj.id
    return {
        "camera_id": camera_id,
        "project_id": project_id,
        "storage_path": storage,
    }


# ---------------------------------------------------------------------------
# Phase 04: minimal valid image byte helpers
# ---------------------------------------------------------------------------


def make_jpeg(width: int = 640, height: int = 480) -> bytes:
    """Return a structurally-minimal JPEG with the given dimensions.

    Enough for the dimension reader and format detector; not a real image.
    Use for upload tests and anywhere a valid image is needed without Pillow.
    """
    sof = (
        b"\xff\xc0"
        + struct.pack(">H", 17)
        + b"\x08"
        + struct.pack(">H", height)
        + struct.pack(">H", width)
        + b"\x01\x01\x11\x00"
    )
    return b"\xff\xd8" + sof + b"\xff\xd9"


def make_png(width: int = 320, height: int = 240) -> bytes:
    """Return a structurally-minimal PNG IHDR with the given dimensions.

    Enough for the dimension reader and format detector; not a real image.
    """
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


# ---------------------------------------------------------------------------
# Phase 04: permissive DiskSpaceMonitor fixture
#
# Any CaptureSupervisor built WITHOUT an injected disk_monitor probes the
# REAL disk on each cycle. On a CI machine with limited free space, this can
# incorrectly trigger the low-disk pause gate and break existing supervisor/
# capture tests. Inject this monitor wherever a CaptureSupervisor is
# constructed so capture tests remain disk-independent.
# ---------------------------------------------------------------------------


@pytest.fixture()
def permissive_disk_monitor() -> DiskSpaceMonitor:
    """Return a DiskSpaceMonitor that always reports abundant free space.

    Inject this as ``disk_monitor=`` when constructing a CaptureSupervisor
    in tests that must be independent of the real disk state.
    """
    return DiskSpaceMonitor(
        low_watermark_bytes=1,
        low_watermark_percent=0.001,
        resume_watermark_bytes=1,
        resume_watermark_percent=0.001,
        check_interval_seconds=0.0,
        get_free_bytes=lambda _p: 10**15,
        get_total_bytes=lambda _p: 10**15,
    )


# ---------------------------------------------------------------------------
# Phase 06: Web UI / auth fixtures
#
# These helpers support the security test suites. They follow the same
# isolation pattern as the Phase 02 migrated client.
# ---------------------------------------------------------------------------

from timelapse_manager.config.settings import (  # noqa: E402
    AuthSettings,
    ServerSettings,
    TlsSettings,
)
from timelapse_manager.runtime import get_context  # noqa: E402
from timelapse_manager.security import create_initial_admin  # noqa: E402


@pytest.fixture()
def web_settings(tmp_path: Path) -> Settings:
    """Settings tuned for web/auth tests.

    Uses a temp DB and data directory, disables autostart background loops,
    and sets minimal Argon2 cost so password hashing in tests is fast.
    Redirect-to-https is enabled by default (matching production); tests that
    specifically verify the http redirect behaviour use this fixture as-is.
    Tests that need to POST with a cookie over http must override
    ``server.redirect_http_to_https`` themselves.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = tmp_path / "web_test.db"
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


@pytest.fixture()
def web_settings_no_redirect(web_settings: Settings) -> Settings:
    """Same as ``web_settings`` but with redirect_http_to_https disabled.

    Use this when testing cookie Secure-flag behaviour over a plain http base
    URL: with redirect enabled, a POST over http would get a 308 before the
    login handler runs, so the Set-Cookie header is never emitted.
    """
    return web_settings.model_copy(
        update={"server": ServerSettings(redirect_http_to_https=False)}
    )


@pytest.fixture()
def web_client(
    web_settings: Settings,
    alembic_cfg: Config,
) -> Generator[TestClient, None, None]:
    """Yield a TestClient (https base_url) for an authed web-UI flow.

    Runs alembic migrations against the settings DB before starting the app,
    so the session and user tables exist. The base_url is https://testserver
    so httpx does not drop Secure cookies on round-trips.
    """
    alembic_cfg.set_main_option("sqlalchemy.url", web_settings.database.url)
    alembic_command.upgrade(alembic_cfg, "head")
    app = create_app(web_settings)
    with TestClient(app, base_url="https://testserver") as c:
        yield c


@pytest.fixture()
def web_client_no_redirect(
    web_settings_no_redirect: Settings,
    alembic_cfg: Config,
) -> Generator[TestClient, None, None]:
    """Yield a TestClient with no https redirect (http base_url).

    Use for testing the Secure flag absent on http, and for the redirect test
    itself (which only needs to observe the 308 status, not follow it).
    """
    alembic_cfg.set_main_option("sqlalchemy.url", web_settings_no_redirect.database.url)
    alembic_command.upgrade(alembic_cfg, "head")
    app = create_app(web_settings_no_redirect)
    with TestClient(app, base_url="http://testserver", follow_redirects=False) as c:
        yield c


def seed_admin(
    client: TestClient,
    *,
    username: str = "admin",
    password: str = "AdminP@ssw0rd1234",
) -> None:
    """Create the initial admin user directly via the security layer.

    Writes the user into the database that the running TestClient's lifespan
    opened, then commits the transaction. Subsequent requests to the web UI
    will see this admin and the first-run gate will be satisfied.
    """
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        create_initial_admin(
            db,
            username,
            password,
            settings=ctx.settings.auth,
        )


def login(
    client: TestClient,
    username: str = "admin",
    password: str = "AdminP@ssw0rd1234",
) -> str:
    """POST to /login and return the raw session token string.

    The TestClient's cookie jar retains the session cookie so subsequent
    requests via the same client instance are authenticated.
    """
    resp = client.post(
        "/login",
        data={"username": username, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    # A successful login is a 303 redirect to /.
    assert resp.status_code == 303, (
        f"Login failed with {resp.status_code}: {resp.text[:200]}"
    )
    cookie_name = get_context().settings.session.cookie_name
    raw_token = client.cookies.get(cookie_name)
    assert raw_token is not None, "Session cookie not set after login"
    return raw_token


def csrf_of(client: TestClient, path: str) -> str:
    """GET a page and extract the CSRF token from the meta tag.

    Returns the token string so tests can include it in subsequent POST
    requests that require CSRF validation.
    """
    resp = client.get(path)
    assert resp.status_code == 200, f"GET {path} returned {resp.status_code}"
    match = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]*)"', resp.text)
    assert match is not None, f"No csrf-token meta tag found in {path}"
    return match.group(1)


@pytest.fixture()
def admin_client(web_client: TestClient) -> TestClient:
    """Return a TestClient already logged in as an admin."""
    seed_admin(web_client)
    login(web_client)
    return web_client


@pytest.fixture()
def viewer_client(web_client: TestClient) -> TestClient:
    """Return a TestClient already logged in as a viewer-role user."""
    seed_admin(web_client)
    login(web_client)
    # Create a viewer user while logged in as admin.
    csrf = csrf_of(web_client, "/users")
    web_client.post(
        "/users",
        data={
            "username": "viewer",
            "password": "ViewerPass12345!",
            "role": "viewer",
            "csrf_token": csrf,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    # Log out the admin.
    csrf = csrf_of(web_client, "/")
    web_client.post(
        "/logout",
        data={"csrf_token": csrf},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    # Log in as viewer.
    login(web_client, username="viewer", password="ViewerPass12345!")
    return web_client


@pytest.fixture()
def operator_client(web_client: TestClient) -> TestClient:
    """Return a TestClient already logged in as an operator-role user.

    Seeds an admin, uses it to create an operator account, logs the admin out,
    and logs the operator in -- so the returned client carries an operator
    session for exercising the operational mutation surface.
    """
    seed_admin(web_client)
    login(web_client)
    csrf = csrf_of(web_client, "/users")
    web_client.post(
        "/users",
        data={
            "username": "operator",
            "password": "OperatorPass12345!",
            "role": "operator",
            "csrf_token": csrf,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    csrf = csrf_of(web_client, "/")
    web_client.post(
        "/logout",
        data={"csrf_token": csrf},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    login(web_client, username="operator", password="OperatorPass12345!")
    return web_client


@pytest.fixture()
def anon_client(web_client: TestClient) -> TestClient:
    """Return a TestClient with no authentication (admin seeded but not logged in)."""
    seed_admin(web_client)
    return web_client


@pytest.fixture()
def cli_client(
    web_settings: Settings, alembic_cfg: Config
) -> Generator[TestClient, None, None]:
    """Yield a TestClient for the CLI bearer-token path (no session cookie)."""
    alembic_cfg.set_main_option("sqlalchemy.url", web_settings.database.url)
    alembic_command.upgrade(alembic_cfg, "head")
    app = create_app(web_settings)
    with TestClient(app, base_url="https://testserver") as c:
        yield c
