"""Shared fixtures for the E2E browser test suite.

All Playwright-specific imports are deferred to function scope so that these
files can be *collected* (imported) without Playwright being installed.  The
auto-skip gate ensures that a plain ``pytest -n auto`` run (with no browsers)
passes collection cleanly and skips browser tests rather than erroring.

Boot sequence
-------------
1. ``alembic upgrade head`` against a per-test SQLite file.
2. Seed the initial admin user directly via the security layer (without
   going through ``seed_admin()`` from the web conftest, which needs the
   in-process ``get_context()`` singleton — not available in the server
   subprocess).
3. Start the app subprocess via ``tests/e2e/_serve.py``.
4. Poll ``/healthz`` until the server is ready (max 10 s).
5. Yield ``http://127.0.0.1:<port>`` as the base URL.
6. Tear down the subprocess on test exit.
"""

from __future__ import annotations

import datetime
import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Generator
from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config

# ---------------------------------------------------------------------------
# Marker registration
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``ui`` marker for browser-driven E2E tests."""
    config.addinivalue_line(
        "markers",
        "ui: marks tests that require a real browser via Playwright"
        " (skipped automatically when Playwright browsers are not installed;"
        " run with -m ui to select)",
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Return a free TCP port on loopback."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _alembic_cfg_for(db_url: str) -> Config:
    """Return an Alembic Config pointed at ``db_url`` using the real scripts."""
    repo_root = Path(__file__).parent.parent.parent
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _seed_admin(db_url: str, data_dir: Path) -> None:
    """Migrate + seed the initial admin user into ``db_url``.

    Uses the same building blocks as the web-test conftest ``seed_admin()``
    helper, but without relying on ``get_context()`` (which lives in the server
    subprocess, not in the test process).
    """
    import timelapse_manager.security.crypto as _crypto_mod
    from timelapse_manager.config.settings import AuthSettings
    from timelapse_manager.db.engine import create_db_engine
    from timelapse_manager.db.session import create_session_factory, session_scope
    from timelapse_manager.security import create_initial_admin
    from timelapse_manager.security.keystore import KeyFileProvider

    # Minimal Argon2 cost so seeding is fast.
    auth_settings = AuthSettings(
        argon2_memory_kib=256,
        argon2_time_cost=1,
        argon2_parallelism=1,
        password_min_length=12,
    )

    # Install an isolated key provider so encrypt_secret never touches the OS
    # keychain during seeding.
    key_file = data_dir / ".secret-key"
    provider = KeyFileProvider(key_file)
    _crypto_mod.set_key_provider(provider)

    try:
        alembic_cfg = _alembic_cfg_for(db_url)
        alembic_command.upgrade(alembic_cfg, "head")

        engine = create_db_engine(db_url)
        factory = create_session_factory(engine)
        try:
            with session_scope(factory) as db:
                create_initial_admin(
                    db,
                    "admin",
                    "AdminP@ssw0rd1234",
                    settings=auth_settings,
                )
        finally:
            engine.dispose()
    finally:
        _crypto_mod.set_key_provider(None)


# ---------------------------------------------------------------------------
# Browser availability gate
#
# All browser-touching fixtures carry ``@pytest.mark.ui`` so they are
# deselected by default.  The ``_chromium_page`` fixture also guards
# against Playwright not being installed (importorskip) and against the
# browser binary being absent (except BrowserError → skip).  Both
# ``playwright`` and ``pytest-playwright`` are imported function-locally so
# collection succeeds even when neither package is installed.
# ---------------------------------------------------------------------------


@pytest.fixture()
def _chromium_page() -> Generator[object, None, None]:
    """Yield a Playwright Chromium page, or skip if browsers are not installed.

    Skips under two conditions:
    - ``playwright`` package is not installed (pytest.importorskip).
    - The Chromium browser binary is missing (catches playwright.sync_api.Error).

    Import is entirely deferred so this file can be collected without the
    package present.
    """
    playwright_sync_api = pytest.importorskip(
        "playwright.sync_api",
        reason="playwright package not installed; run 'playwright install chromium'",
    )
    sync_playwright = playwright_sync_api.sync_playwright
    Error = playwright_sync_api.Error  # type: ignore[attr-defined]

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(headless=True)
    except Error as exc:
        pw.stop()
        pytest.skip(f"Chromium browser not installed: {exc}")
    page = browser.new_page()
    try:
        yield page
    finally:
        page.close()
        browser.close()
        pw.stop()


# ---------------------------------------------------------------------------
# Live server fixture
# ---------------------------------------------------------------------------


def _boot_app(
    tmp_path: Path,
    extra_seed: Callable[[str, Path], None] | None = None,
) -> tuple[subprocess.Popen[bytes], str]:
    """Boot the app subprocess against a fresh seeded DB; return (proc, base_url).

    Seeds the initial admin, then optionally runs ``extra_seed(db_url, data_dir)``
    before the subprocess starts, so the server sees a ready DB on first
    lifespan init. Warms /healthz then /login (45 s budget) so the first test
    never races a not-fully-ready server. xdist-safe (per-worker tmp_path/port).
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "e2e_test.db"
    db_url = f"sqlite:///{db_path}"

    _seed_admin(db_url, data_dir)
    if extra_seed is not None:
        extra_seed(db_url, data_dir)

    port = _free_port()
    serve_script = Path(__file__).parent / "_serve.py"
    proc = subprocess.Popen(
        [
            sys.executable,
            str(serve_script),
            "--db-url",
            db_url,
            "--data-dir",
            str(data_dir),
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    base_url = f"http://127.0.0.1:{port}"
    healthz_url = f"{base_url}/healthz"
    login_url = f"{base_url}/login"
    deadline = time.monotonic() + 45.0
    healthy = False
    ready = False
    while time.monotonic() < deadline:
        try:
            if not healthy:
                with urllib.request.urlopen(healthz_url, timeout=1.0) as resp:
                    healthy = resp.status == 200
            if healthy:
                with urllib.request.urlopen(login_url, timeout=2.0) as resp:
                    if resp.status == 200 and b'name="username"' in resp.read():
                        ready = True
                        break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)

    if not ready:
        proc.terminate()
        try:
            _, err = proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            err = b""
        tail = err.decode("utf-8", "replace")[-2000:] if err else "(no stderr)"
        raise RuntimeError(
            f"E2E app server did not become ready within 45 s on port {port}.\n"
            f"--- server stderr tail ---\n{tail}"
        )
    return proc, base_url


@pytest.fixture()
def live_server(tmp_path: Path) -> Generator[str, None, None]:
    """Start the real app (admin only); yield its base URL."""
    proc, base_url = _boot_app(tmp_path)
    try:
        yield base_url
    finally:
        proc.terminate()
        proc.wait()


def _seed_demo_data(db_url: str, data_dir: Path) -> None:
    """Seed a Camera + Project + a few frames for data-gated screen tests.

    On a fresh DB this yields camera id 1, project id 1, frames seq 0-64
    (>60 so the continuous-scroll sentinel/append is exercised).
    """
    from datetime import UTC, datetime, timedelta

    from timelapse_manager.db.engine import create_db_engine
    from timelapse_manager.db.models import Camera, Event, Frame, Project, User
    from timelapse_manager.db.session import create_session_factory, session_scope

    engine = create_db_engine(db_url)
    factory = create_session_factory(engine)
    try:
        with session_scope(factory) as db:
            cam = Camera(
                name="e2e-cam",
                address="10.0.0.9",
                protocol="vapix",
                geolocation_latitude=34.0,
                geolocation_longitude=-83.0,
                geolocation_source="camera",
            )
            db.add(cam)
            db.flush()
            proj = Project(
                camera_id=cam.id,
                name="E2E Project",
                capture_interval_seconds=300,
                lifecycle_state="active",
            )
            db.add(proj)
            db.flush()
            # A second, non-self user so the Users screen renders a row with the
            # actions popover (the logged-in admin's own row shows "(you)").
            db.add(
                User(
                    username="viewer1",
                    role="viewer",
                    auth_source="ldap",
                    enabled=True,
                )
            )
            db.flush()
            for i in range(65):
                db.add(
                    Frame(
                        project_id=proj.id,
                        sequence_index=i,
                        capture_timestamp=(
                            datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=i)
                        ).replace(tzinfo=None),
                        file_path=f"/frames/{proj.id}/{i:08d}.jpg",
                        capture_status="captured",
                        origin="captured",
                        lifecycle_state="active",
                    )
                )
            # >50 operational events so the events log shows a scroll sentinel.
            for i in range(55):
                db.add(
                    Event(
                        scope="camera",
                        scope_id=cam.id,
                        level="error" if i % 10 == 0 else "info",
                        message=f"seed event {i}",
                        event_metadata=None,
                    )
                )
            db.flush()
    finally:
        engine.dispose()


@pytest.fixture()
def live_server_with_data(tmp_path: Path) -> Generator[str, None, None]:
    """Start the app with a seeded Camera + Project (id 1) + frames."""
    proc, base_url = _boot_app(tmp_path, extra_seed=_seed_demo_data)
    try:
        yield base_url
    finally:
        proc.terminate()
        proc.wait()


def make_frame_seeder(
    n_frames: int = 70,
    *,
    project_name: str = "Scroll Project",
    interval_minutes: int = 5,
    first_capture: datetime.datetime | None = None,
) -> Callable[[str, Path], None]:
    """Build a seed callable that inserts a Camera + Project + ``n_frames`` frames.

    The default frame count (70) deliberately crosses the 60-per-batch boundary so
    that a second continuous-scroll batch is loaded on scroll, which is what makes
    the oldest-timestamp announcement meaningful. Each frame gets a distinct
    capture timestamp spaced ``interval_minutes`` apart starting at
    ``first_capture`` (default 2026-01-01T00:00 UTC), so the oldest timestamp in
    any batch is unambiguous.

    Returns a ``(db_url, data_dir) -> None`` callable suitable for passing as the
    ``extra_seed`` of :func:`_boot_app`. The seeded project is id 1 on a fresh DB.
    This helper is additive: it does not alter ``_seed_demo_data`` or any existing
    fixture.
    """
    from datetime import UTC, datetime, timedelta

    start = (
        first_capture if first_capture is not None else datetime(2026, 1, 1, tzinfo=UTC)
    )

    def _seed(db_url: str, data_dir: Path) -> None:
        from timelapse_manager.db.engine import create_db_engine
        from timelapse_manager.db.models import Camera, Frame, Project
        from timelapse_manager.db.session import create_session_factory, session_scope

        engine = create_db_engine(db_url)
        factory = create_session_factory(engine)
        try:
            with session_scope(factory) as db:
                cam = Camera(name="scroll-cam", address="10.0.0.11", protocol="vapix")
                db.add(cam)
                db.flush()
                proj = Project(
                    camera_id=cam.id,
                    name=project_name,
                    capture_interval_seconds=interval_minutes * 60,
                    lifecycle_state="active",
                )
                db.add(proj)
                db.flush()
                for i in range(n_frames):
                    ts = start + timedelta(minutes=interval_minutes * i)
                    db.add(
                        Frame(
                            project_id=proj.id,
                            sequence_index=i,
                            capture_timestamp=ts.replace(tzinfo=None),
                            file_path=f"/frames/{proj.id}/{i:08d}.jpg",
                            capture_status="captured",
                            origin="captured",
                            lifecycle_state="active",
                        )
                    )
                db.flush()
        finally:
            engine.dispose()

    return _seed


def make_event_seeder(
    n_events: int = 90,
    *,
    interval_minutes: int = 1,
    first_event: datetime.datetime | None = None,
    event_type: str | None = None,
    message_prefix: str = "scroll event",
) -> Callable[[str, Path], None]:
    """Build a seed callable that inserts a Camera + ``n_events`` events.

    The default count (90) crosses the 75-per-batch operational boundary so the
    continuous-scroll sentinel/append and the date-jump window are both
    exercised. Every event gets a distinct ``camera``-scoped timestamp spaced
    ``interval_minutes`` apart starting at ``first_event`` (default
    2026-02-01T00:00 UTC), so a mid-series ``?at=`` jump is deterministic. Levels
    cycle info/warning/error/critical so the level chips have something to filter.

    ``event_type`` defaults to ``None`` -- typeless operational rows, the original
    behaviour -- so existing callers are unchanged. Passing an audit/security type
    makes the seeded rows audit records (folded into the JSON ``type`` key, exactly
    as ``log_event`` does), for exercising the admin-only audit scroll.

    Returns a ``(db_url, data_dir) -> None`` callable for ``_boot_app``'s
    ``extra_seed``. The seeded camera is id 1 on a fresh DB.
    """
    from datetime import UTC, datetime, timedelta

    start = first_event if first_event is not None else datetime(2026, 2, 1, tzinfo=UTC)
    levels = ("info", "warning", "error", "critical")
    metadata = {"type": event_type} if event_type is not None else None

    def _seed(db_url: str, data_dir: Path) -> None:
        from timelapse_manager.db.engine import create_db_engine
        from timelapse_manager.db.models import Camera, Event
        from timelapse_manager.db.session import create_session_factory, session_scope

        engine = create_db_engine(db_url)
        factory = create_session_factory(engine)
        try:
            with session_scope(factory) as db:
                cam = Camera(name="evt-cam", address="10.0.0.21", protocol="vapix")
                db.add(cam)
                db.flush()
                for i in range(n_events):
                    ts = start + timedelta(minutes=interval_minutes * i)
                    db.add(
                        Event(
                            scope="camera",
                            scope_id=cam.id,
                            level=levels[i % len(levels)],
                            message=f"{message_prefix} {i}",
                            timestamp=ts.replace(tzinfo=None),
                            event_metadata=dict(metadata) if metadata else None,
                        )
                    )
                db.flush()
        finally:
            engine.dispose()

    return _seed


@pytest.fixture()
def live_server_events(tmp_path: Path) -> Generator[str, None, None]:
    """Start the app seeded with 90 operational events (distinct timestamps).

    The 90-event count crosses the 75-per-operational-batch boundary, so the
    events log shows a scroll sentinel, the date-jump windows mid-series, and the
    level chips have all four severities to filter.
    """
    proc, base_url = _boot_app(tmp_path, extra_seed=make_event_seeder(90))
    try:
        yield base_url
    finally:
        proc.terminate()
        proc.wait()


@pytest.fixture()
def logged_in_events_page(
    _chromium_page: object, live_server_events: str
) -> tuple[object, str]:
    """An authenticated Chromium page on a server seeded with 90 events.

    Returns (page, base_url). All seeded events are ``camera``-scoped.
    """
    login(_chromium_page, live_server_events)
    return _chromium_page, live_server_events


@pytest.fixture()
def live_server_audit(tmp_path: Path) -> Generator[str, None, None]:
    """Start the app seeded with 90 audit/security events (distinct timestamps).

    The 90-record count crosses the audit batch boundary, so the audit log shows
    a scroll sentinel that pages to an end-cap. Records carry the audit
    control-action type so they appear only in the admin-only audit view.
    """
    from timelapse_manager.monitoring import EventType

    proc, base_url = _boot_app(
        tmp_path,
        extra_seed=make_event_seeder(
            90,
            event_type=EventType.AUDIT_CONTROL_ACTION.value,
            message_prefix="audit event",
        ),
    )
    try:
        yield base_url
    finally:
        proc.terminate()
        proc.wait()


@pytest.fixture()
def logged_in_audit_page(
    _chromium_page: object, live_server_audit: str
) -> tuple[object, str]:
    """An authenticated (admin) Chromium page on a server seeded with audit events.

    Returns (page, base_url). The seeded user is the admin, so the Audit tab and
    the admin-only audit view are reachable.
    """
    login(_chromium_page, live_server_audit)
    return _chromium_page, live_server_audit


@pytest.fixture()
def live_server_scroll_frames(tmp_path: Path) -> Generator[str, None, None]:
    """Start the app seeded with a Project (id 1) + 70 frames for scroll tests.

    The 70-frame count crosses the 60-per-batch boundary, so scrolling the
    sentinel into view loads a second (10-frame) batch.
    """
    proc, base_url = _boot_app(tmp_path, extra_seed=make_frame_seeder(70))
    try:
        yield base_url
    finally:
        proc.terminate()
        proc.wait()


@pytest.fixture()
def logged_in_scroll_page(
    _chromium_page: object, live_server_scroll_frames: str
) -> tuple[object, str]:
    """An authenticated Chromium page on a server seeded with 70 frames.

    Returns (page, base_url). The seeded project is id 1.
    """
    login(_chromium_page, live_server_scroll_frames)
    return _chromium_page, live_server_scroll_frames


# ---------------------------------------------------------------------------
# Live-camera scaffold
#
# A ``live``-marked fixture for future camera E2E phases.  Reads Axis camera
# addresses and credentials from environment variables only; skips the test
# automatically when any required variable is absent.  No real camera
# interaction happens here -- this is a placeholder that later phases
# replace with a real camera session.
# ---------------------------------------------------------------------------

_CAMERA_ENV_VARS = (
    "TLM_TEST_AXIS_HOST",
    "TLM_TEST_AXIS_USER",
    "TLM_TEST_AXIS_PASS",
)


@pytest.fixture()
def live_camera_env() -> dict[str, str]:
    """Return Axis camera env vars, or skip if any are unset.

    Later camera E2E phases build on this fixture to obtain connection
    details without hardcoding addresses or credentials.

    Required environment variables:
        TLM_TEST_AXIS_HOST  — IP or hostname of the Axis camera
        TLM_TEST_AXIS_USER  — camera username
        TLM_TEST_AXIS_PASS  — camera password
    """
    missing = [v for v in _CAMERA_ENV_VARS if not os.environ.get(v)]
    if missing:
        pytest.skip(f"live camera E2E requires env vars: {', '.join(missing)}")
    return {v: os.environ[v] for v in _CAMERA_ENV_VARS}


# ---------------------------------------------------------------------------
# Shared browser login — robust to a cold-start first request. Reused by every
# authenticated screen e2e test. (The live_server fixture already warms /login,
# so the retry is belt-and-suspenders.)
# ---------------------------------------------------------------------------

_E2E_ADMIN_USER = "admin"
_E2E_ADMIN_PASS = "AdminP@ssw0rd1234"  # seeded by _seed_admin


def login(page: object, base_url: str) -> None:
    """Log in as the seeded admin and land on the authenticated shell.

    Surfaces the underlying Playwright error on final failure so a real
    regression is diagnosable; the browser-e2e CI job also auto-reruns to
    absorb a transient cold-start flake.
    """
    page.set_default_timeout(60000)
    last_err: Exception | None = None

    def _attempt(timeout: int) -> bool:
        nonlocal last_err
        try:
            page.goto(f"{base_url}/login", wait_until="domcontentloaded")
            if page.locator(".app-shell").count() >= 1:
                return True  # already authenticated
            page.wait_for_selector("input[name='username']", timeout=timeout)
            page.fill("input[name='username']", _E2E_ADMIN_USER)
            page.fill("input[name='password']", _E2E_ADMIN_PASS)
            page.click("button[type='submit']")
            page.wait_for_selector(".app-shell", timeout=timeout)
            return True
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            return False

    if not _attempt(30000) and not _attempt(30000):
        raise AssertionError(
            f"login did not reach the authenticated shell: {last_err!r}"
        )


@pytest.fixture()
def logged_in_page(_chromium_page: object, live_server: str) -> object:
    """A Chromium page already authenticated as admin against the live server."""
    login(_chromium_page, live_server)
    return _chromium_page


@pytest.fixture()
def logged_in_data_page(
    _chromium_page: object, live_server_with_data: str
) -> tuple[object, str]:
    """An authenticated Chromium page on a server seeded with a Camera+Project.

    Returns (page, base_url). The seeded project is id 1.
    """
    login(_chromium_page, live_server_with_data)
    return _chromium_page, live_server_with_data
