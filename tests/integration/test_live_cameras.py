"""Live camera tests — require a real Axis camera and env-var credentials.

These tests are guarded by @pytest.mark.live and read all camera connection
details from environment variables. They are SKIPPED automatically when the
required variables are not set, so the CI core suite (which runs without
hardware) always passes.

Required environment variables:
    TLM_TEST_AXIS_HOST   — IP or hostname of the Axis camera
    TLM_TEST_AXIS_USER   — camera username
    TLM_TEST_AXIS_PASS   — camera password

Optional:
    TLM_TEST_DISCOVERY_RANGE  — CIDR or dash-range to scan (e.g. "192.168.1.0/24")
    TLM_TEST_RTSP_URL         — full RTSP URL; if absent, built from host

Run with:
    TLM_TEST_AXIS_HOST=... TLM_TEST_AXIS_USER=... TLM_TEST_AXIS_PASS=... \
        uv run pytest -q -m live
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

from timelapse_manager.cameras.base import CapturedFrame
from timelapse_manager.cameras.discovery import scan_range
from timelapse_manager.cameras.rtsp import RtspAdapter
from timelapse_manager.cameras.vapix import VapixAdapter
from timelapse_manager.capture.frame_writer import FrameWriter
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
from timelapse_manager.db.session import (
    create_session_factory,
    session_scope,
    set_session_factory,
)
from timelapse_manager.runtime import AppContext, set_context
from timelapse_manager.security.token import ensure_local_token

# ---------------------------------------------------------------------------
# Env-var access (never hardcoded)
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    """Return the env var or skip the test."""
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


def _rtsp_url() -> str:
    """Return the RTSP URL from env, or build a default from host."""
    url = os.environ.get("TLM_TEST_RTSP_URL")
    if url:
        return url
    host = _axis_host()
    return f"rtsp://{host}/axis-media/media.amp"


# ---------------------------------------------------------------------------
# App-context fixture — required by adapter/discovery code paths that read
# the SSRF config via get_context().settings.ssrf.
#
# Must be function-scoped so it runs AFTER the conftest _reset_globals autouse
# fixture calls dispose().  Depends on _reset_globals explicitly to guarantee
# that ordering: pytest sets up fixtures in dependency order, so set_context()
# here runs after the pre-test dispose(), and the post-test dispose() in
# _reset_globals runs after our teardown yield.  Module scope would fail
# silently — _reset_globals would wipe the context before each test body.
#
# The SSRF allow-list is derived from TLM_TEST_AXIS_HOST (the mandatory env
# var) so the guard accepts the camera subnet without hardcoding an IP.
# ---------------------------------------------------------------------------


def _camera_subnet() -> str:
    """Return the /24 CIDR that contains TLM_TEST_AXIS_HOST.

    Using strict=False lets ip_network absorb the host bits so e.g.
    192.168.10.113 becomes 192.168.10.0/24.  Falls back to the full RFC-1918
    /8 block if the var is unset (live tests skip anyway when the var is
    absent, so this branch is only exercised in conftest-level collection).
    """
    host = os.environ.get("TLM_TEST_AXIS_HOST", "")
    if not host:
        return "10.0.0.0/8"
    return str(ipaddress.ip_network(f"{host}/24", strict=False))


@pytest.fixture(autouse=True)
def _live_app_context(
    _reset_globals: None,
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    tmp_path: Path,
) -> Generator[None, None, None]:
    """Install a minimal AppContext so adapter SSRF checks can pass.

    Autouse within this module only — does not affect the rest of the suite.
    Function-scoped so it installs its context AFTER _reset_globals runs
    dispose(), and tears down before _reset_globals' post-test cleanup.
    """
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

    # A lightweight SQLite engine for the context; the migrated_factory
    # fixture provides the DB that FrameWriter writes frames into.
    ctx_engine = create_db_engine(f"sqlite:///{db_path}")
    ctx_factory = create_session_factory(ctx_engine)

    ensure_local_token(settings)
    context = AppContext(
        settings=settings,
        db_engine=ctx_engine,
        session_factory=ctx_factory,
        logger=logging.getLogger("live-test"),
        app_version="0.0.0",
        ffmpeg_version="unavailable",
    )
    set_context(context)
    set_session_factory(migrated_factory)

    yield

    ctx_engine.dispose()


# ---------------------------------------------------------------------------
# VAPIX live tests
# ---------------------------------------------------------------------------


@pytest.mark.live
class TestLiveVapixCapture:
    """Requires: TLM_TEST_AXIS_HOST, TLM_TEST_AXIS_USER, TLM_TEST_AXIS_PASS."""

    async def test_vapix_capture_returns_jpeg(self) -> None:
        host = _axis_host()
        creds = (_axis_user(), _axis_pass())
        async with httpx.AsyncClient() as client:
            adapter = VapixAdapter(client, address=host, credentials=creds)
            frame = await adapter.capture()
        assert isinstance(frame, CapturedFrame)
        assert frame.image_bytes[:2] == b"\xff\xd8", "expected JPEG SOI marker"
        assert len(frame.image_bytes) > 1000, "frame is suspiciously small"

    async def test_vapix_validate_connection_returns_ok(self) -> None:
        host = _axis_host()
        creds = (_axis_user(), _axis_pass())
        async with httpx.AsyncClient() as client:
            adapter = VapixAdapter(client, address=host, credentials=creds)
            result = await adapter.validate_connection()
        assert result.ok is True, f"validation failed: {result.message}"

    async def test_vapix_capture_produces_nonzero_dimensions(self) -> None:
        host = _axis_host()
        creds = (_axis_user(), _axis_pass())
        async with httpx.AsyncClient() as client:
            adapter = VapixAdapter(client, address=host, credentials=creds)
            frame = await adapter.capture()
        # Dimensions may be 0 if _imageinfo can't parse, but that would be a
        # sign of a real issue — assert positive values.
        assert frame.width > 0, f"expected positive width, got {frame.width}"
        assert frame.height > 0, f"expected positive height, got {frame.height}"

    async def test_vapix_wrong_password_returns_auth_failure(self) -> None:
        from timelapse_manager.cameras.base import ValidationFailure

        host = _axis_host()
        # Intentionally wrong password
        creds = (_axis_user(), "definitely-wrong-password-for-test")
        async with httpx.AsyncClient() as client:
            adapter = VapixAdapter(client, address=host, credentials=creds)
            result = await adapter.validate_connection()
        assert result.ok is False
        assert result.reason == ValidationFailure.AUTH


@pytest.mark.live
class TestLiveVapixStreamProfiles:
    """Stream-profile enumeration and profile-honoring capture against a real
    Axis camera.

    Requires: TLM_TEST_AXIS_HOST, TLM_TEST_AXIS_USER, TLM_TEST_AXIS_PASS.
    """

    async def test_list_stream_profiles_is_reachable(self) -> None:
        host = _axis_host()
        creds = (_axis_user(), _axis_pass())
        async with httpx.AsyncClient() as client:
            adapter = VapixAdapter(client, address=host, credentials=creds)
            result = await adapter.list_stream_profiles()
        # A reachable camera returns ok=True. An empty profile list is a valid
        # state (the device may have none configured) -- not a failure.
        assert result.ok is True, f"enumeration failed: {result.message}"
        for profile in result.profiles:
            assert profile.id, "profile id must be non-empty"
            assert profile.label, "profile label must be non-empty"

    async def test_each_profile_captures_a_valid_jpeg(self) -> None:
        host = _axis_host()
        creds = (_axis_user(), _axis_pass())
        async with httpx.AsyncClient() as client:
            adapter = VapixAdapter(client, address=host, credentials=creds)
            result = await adapter.list_stream_profiles()
            if not result.profiles:
                pytest.skip("camera has no configured stream profiles")
            for profile in result.profiles:
                selected = VapixAdapter(
                    client, address=host, credentials=creds, stream_id=profile.id
                )
                frame = await selected.capture()
                assert frame.image_bytes[:2] == b"\xff\xd8", (
                    f"profile {profile.id!r} did not return a JPEG"
                )
                assert frame.width > 0 and frame.height > 0


# ---------------------------------------------------------------------------
# VAPIX geolocation live tests
# ---------------------------------------------------------------------------


@pytest.mark.live
class TestLiveVapixGeolocation:
    async def test_get_geolocation_does_not_raise(self) -> None:
        host = _axis_host()
        creds = (_axis_user(), _axis_pass())
        async with httpx.AsyncClient() as client:
            adapter = VapixAdapter(client, address=host, credentials=creds)
            geo = await adapter.get_geolocation()
        # geo may be None if the camera has no location configured — that is ok
        if geo is not None:
            assert -90 <= geo.latitude <= 90
            assert -180 <= geo.longitude <= 180
            assert geo.source == "camera"


# ---------------------------------------------------------------------------
# RTSP live tests (slow — ffmpeg subprocess)
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.slow
class TestLiveRtspCapture:
    """
    Requires: TLM_TEST_AXIS_HOST (+ optional TLM_TEST_RTSP_URL),
              TLM_TEST_AXIS_USER, TLM_TEST_AXIS_PASS.
    """

    async def test_rtsp_capture_returns_jpeg(self) -> None:
        url = _rtsp_url()
        creds = (_axis_user(), _axis_pass())
        adapter = RtspAdapter(stream_url=url, credentials=creds, timeout_seconds=20.0)
        try:
            frame = await adapter.capture()
        finally:
            await adapter.close()
        assert isinstance(frame, CapturedFrame)
        assert frame.image_bytes[:2] == b"\xff\xd8"

    async def test_rtsp_validate_returns_ok(self) -> None:
        url = _rtsp_url()
        creds = (_axis_user(), _axis_pass())
        adapter = RtspAdapter(stream_url=url, credentials=creds, timeout_seconds=20.0)
        try:
            result = await adapter.validate_connection()
        finally:
            await adapter.close()
        assert result.ok is True, f"RTSP validation failed: {result.message}"


# ---------------------------------------------------------------------------
# Discovery live tests
# ---------------------------------------------------------------------------


@pytest.mark.live
class TestLiveDiscovery:
    async def test_scan_range_finds_at_least_one_camera(self) -> None:
        cidr_or_range = os.environ.get("TLM_TEST_DISCOVERY_RANGE")
        if not cidr_or_range:
            pytest.skip("TLM_TEST_DISCOVERY_RANGE not set")

        cameras = await scan_range(cidr_or_range, per_host_timeout=2.0)
        assert len(cameras) >= 1, (
            f"expected at least one camera in range {cidr_or_range!r}; found none"
        )


# ---------------------------------------------------------------------------
# Full live capture + write pipeline
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.slow
class TestLiveCaptureAndWrite:
    async def test_capture_and_write_produces_file_and_row(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        host = _axis_host()
        creds = (_axis_user(), _axis_pass())

        storage = tmp_path / "live_frames"
        storage.mkdir()
        with session_scope(migrated_factory) as session:
            cam = Camera(
                name="live-axis-cam",
                address=host,
                protocol="vapix",
            )
            session.add(cam)
            session.flush()
            cam_id = cam.id
            proj = Project(
                camera_id=cam_id,
                name="live-proj",
                lifecycle_state="active",
                operational_status="idle",
                storage_path=str(storage),
            )
            session.add(proj)
            session.flush()
            proj_id = proj.id

        writer = FrameWriter(migrated_factory, storage)
        async with httpx.AsyncClient() as client:
            adapter = VapixAdapter(client, address=host, credentials=creds)
            frame = await adapter.capture()

        result = writer.write(proj_id, frame)

        assert Path(result.file_path).exists()
        assert Path(result.file_path).stat().st_size > 0

        from timelapse_manager.db.models import Frame

        with session_scope(migrated_factory) as session:
            db_frame = session.get(Frame, result.frame_id)
        assert db_frame is not None
        assert db_frame.sequence_index == 1
