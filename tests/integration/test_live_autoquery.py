"""Live tests for camera auto-query: query_camera() and get_device_hostname().

These tests require a real Axis camera reachable from the test machine and are
guarded by @pytest.mark.live.  They skip automatically when the required
environment variables are absent, so the CI core suite (no hardware) always passes.

Required environment variables:
    TLM_TEST_AXIS_HOST   — IP or hostname of the Axis camera
    TLM_TEST_AXIS_USER   — camera username
    TLM_TEST_AXIS_PASS   — camera password

Run with:
    TLM_TEST_AXIS_HOST=... TLM_TEST_AXIS_USER=... TLM_TEST_AXIS_PASS=... \\
        uv run pytest -q -m live tests/integration/test_live_autoquery.py
"""

from __future__ import annotations

import ipaddress
import logging
import os
from collections.abc import Generator
from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import sessionmaker

from timelapse_manager.cameras.autoquery import query_camera
from timelapse_manager.cameras.vapix import VapixAdapter
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
from timelapse_manager.db.session import (
    create_session_factory,
    set_session_factory,
)
from timelapse_manager.runtime import AppContext, set_context
from timelapse_manager.security.token import ensure_local_token

# ---------------------------------------------------------------------------
# Env-var access — never hardcoded
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    """Return the env var value or skip the current test."""
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"env var {name!r} not set — skipping live test")
    return value


def _axis_host() -> str:
    return _require_env("TLM_TEST_AXIS_HOST")


def _axis_user() -> str:
    return _require_env("TLM_TEST_AXIS_USER")


def _axis_pass() -> str:
    return _require_env("TLM_TEST_AXIS_PASS")


# ---------------------------------------------------------------------------
# Camera subnet helper — derives /24 from TLM_TEST_AXIS_HOST
# ---------------------------------------------------------------------------


def _camera_subnet() -> str:
    """Return the /24 CIDR that contains TLM_TEST_AXIS_HOST.

    Falls back to the full RFC-1918 /8 block when the var is absent (live
    tests skip anyway in that case; this prevents an import-time error).
    """
    host = os.environ.get("TLM_TEST_AXIS_HOST", "")
    if not host:
        return "10.0.0.0/8"
    return str(ipaddress.ip_network(f"{host}/24", strict=False))


# ---------------------------------------------------------------------------
# App-context fixture — installs AppContext with SSRF allow for camera subnet.
#
# Function-scoped so it installs AFTER _reset_globals's dispose() and tears
# down before _reset_globals's post-test cleanup.  See test_live_cameras.py
# for the full reasoning on scope and ordering.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _live_app_context(
    _reset_globals: None,
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    tmp_path: Path,
) -> Generator[None, None, None]:
    """Install a minimal AppContext so adapter SSRF checks pass."""
    data_dir = tmp_path / "live_data"
    data_dir.mkdir()
    db_path = tmp_path / "live_context.db"

    settings = Settings(
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
        ssrf=SsrfSettings(allowed_private_subnets=[_camera_subnet()]),
        secrets=SecretsSettings(use_os_keystore=False),
    )

    ctx_engine = create_db_engine(f"sqlite:///{db_path}")
    ctx_factory = create_session_factory(ctx_engine)

    ensure_local_token(settings)
    context = AppContext(
        settings=settings,
        db_engine=ctx_engine,
        session_factory=ctx_factory,
        logger=logging.getLogger("live-autoquery-test"),
        app_version="0.0.0",
        ffmpeg_version="unavailable",
    )
    set_context(context)
    set_session_factory(migrated_factory)

    yield

    ctx_engine.dispose()


# ---------------------------------------------------------------------------
# Live tests: query_camera()
# ---------------------------------------------------------------------------


@pytest.mark.live
class TestLiveQueryCamera:
    """Requires: TLM_TEST_AXIS_HOST, TLM_TEST_AXIS_USER, TLM_TEST_AXIS_PASS."""

    async def test_query_camera_returns_recommended_protocol(self) -> None:
        """query_camera reports a usable recommended_primary for the Axis camera."""
        host = _axis_host()
        creds = {"username": _axis_user(), "password": _axis_pass()}

        async with httpx.AsyncClient() as client:
            result = await query_camera(
                address=host,
                credentials=creds,
                http_client=client,
            )

        assert result.recommended_primary is not None, (
            f"expected a recommended protocol; error_protocol={result.error_protocol!r}"
        )
        assert result.ok_count >= 1, (
            f"expected at least one responding protocol; ok_count={result.ok_count}"
        )
        assert result.error_protocol is None, (
            f"expected no protocol error; got {result.error_protocol!r}"
        )

    async def test_query_camera_discovers_hostname(self) -> None:
        """query_camera returns a non-empty device hostname for the Axis camera."""
        host = _axis_host()
        creds = {"username": _axis_user(), "password": _axis_pass()}

        async with httpx.AsyncClient() as client:
            result = await query_camera(
                address=host,
                credentials=creds,
                http_client=client,
            )

        assert result.discovered_hostname is not None, (
            f"expected a device hostname; error_hostname={result.error_hostname!r}"
        )
        assert result.discovered_hostname.strip() != "", (
            "discovered_hostname must be non-empty"
        )
        assert result.error_hostname is None, (
            f"expected no hostname error; got {result.error_hostname!r}"
        )

    async def test_query_camera_fetches_geolocation(self) -> None:
        """query_camera returns latitude and longitude from the Axis camera.

        These test cameras are configured with geolocation — assert both
        coordinates are present and plausible rather than softening to
        None-tolerant assertions.
        """
        host = _axis_host()
        creds = {"username": _axis_user(), "password": _axis_pass()}

        async with httpx.AsyncClient() as client:
            result = await query_camera(
                address=host,
                credentials=creds,
                http_client=client,
            )

        assert result.fetched_lat is not None, (
            f"expected a latitude; error_geo={result.error_geo!r}"
        )
        assert result.fetched_lon is not None, (
            f"expected a longitude; error_geo={result.error_geo!r}"
        )
        assert result.error_geo is None, (
            f"expected no geo error; got {result.error_geo!r}"
        )
        # Plausibility: valid lat/lon ranges
        assert -90.0 <= result.fetched_lat <= 90.0, (
            f"latitude {result.fetched_lat} out of range"
        )
        assert -180.0 <= result.fetched_lon <= 180.0, (
            f"longitude {result.fetched_lon} out of range"
        )

    async def test_query_camera_wrong_credentials_returns_auth_error(self) -> None:
        """query_camera with bad credentials returns error_protocol='auth_failed'."""
        host = _axis_host()
        bad_creds = {"username": "nobody", "password": "wrong-password-intentional"}

        async with httpx.AsyncClient() as client:
            result = await query_camera(
                address=host,
                credentials=bad_creds,
                http_client=client,
            )

        assert result.recommended_primary is None, (
            "expected no recommended protocol for bad credentials"
        )
        assert result.error_protocol == "auth_failed", (
            f"expected 'auth_failed', got {result.error_protocol!r}"
        )


# ---------------------------------------------------------------------------
# Live tests: VapixAdapter.get_device_hostname()
# ---------------------------------------------------------------------------


@pytest.mark.live
class TestLiveVapixHostname:
    """Requires: TLM_TEST_AXIS_HOST, TLM_TEST_AXIS_USER, TLM_TEST_AXIS_PASS."""

    async def test_get_device_hostname_returns_nonempty_string(self) -> None:
        """VapixAdapter.get_device_hostname() returns a non-empty string."""
        host = _axis_host()
        creds = (_axis_user(), _axis_pass())

        async with httpx.AsyncClient() as client:
            adapter = VapixAdapter(client, address=host, credentials=creds)
            hostname = await adapter.get_device_hostname()

        assert hostname is not None, "expected a hostname, got None"
        assert isinstance(hostname, str), f"expected str, got {type(hostname)}"
        assert hostname.strip() != "", "hostname must not be blank"

    async def test_get_device_hostname_is_not_an_axis_placeholder(self) -> None:
        """Device hostname must not be the Axis factory placeholder '<hostname>'."""
        host = _axis_host()
        creds = (_axis_user(), _axis_pass())

        async with httpx.AsyncClient() as client:
            adapter = VapixAdapter(client, address=host, credentials=creds)
            hostname = await adapter.get_device_hostname()

        # The VapixAdapter filters out placeholder values and returns None for them.
        # If hostname is None here the camera reports an unset hostname — which
        # fails the previous test and is a configuration issue on the test camera.
        # If it arrives here as a placeholder string that would be a bug in the adapter.
        if hostname is not None:
            assert hostname.lower() not in {"<hostname>", "set hostname", "axis"}, (
                f"adapter returned Axis placeholder value {hostname!r}"
            )
