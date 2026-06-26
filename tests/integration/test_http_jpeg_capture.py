"""Integration tests for the HTTP/JPEG adapter against the mock snapshot server.

Starts the dev mock HTTP snapshot server as a subprocess (mock_snapshot_server
fixture) and exercises the full HttpJpegAdapter.capture() path end-to-end.
Also verifies the FrameWriter integration: captured bytes land on disk and a
Frame row is inserted into a real migrated SQLite DB.

Uses:
- mock_snapshot_server: real subprocess, ephemeral port
- migrated_factory: real temp SQLite with Alembic migrations applied
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from timelapse_manager.cameras.base import CapturedFrame
from timelapse_manager.cameras.http_jpeg import HttpJpegAdapter
from timelapse_manager.cameras.vapix import VapixAdapter
from timelapse_manager.capture.frame_writer import FrameWriter
from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def http_client():
    """Yield a real async HTTP client and close it after the test."""
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture()
def seeded_project(migrated_factory, tmp_path: Path):
    """Seed a Camera + Project and return their ids + a storage path.

    The camera address is stored as a non-loopback value in the DB. The
    mock snapshot server binds to 127.0.0.1 (loopback) and the adapter
    receives the full URL directly, so the seeded address is used only for
    the DB row (not for the actual HTTP connection in these tests).
    """
    storage = tmp_path / "frames"
    storage.mkdir()
    with session_scope(migrated_factory) as session:
        cam = Camera(
            name="http-test-cam",
            address="10.0.0.1",
            protocol="http",
            snapshot_uri="http://10.0.0.1/snapshot.jpg",
        )
        session.add(cam)
        session.flush()
        cam_id = cam.id
        proj = Project(
            camera_id=cam_id,
            name="http-test-proj",
            lifecycle_state="active",
            operational_status="idle",
            storage_path=str(storage),
        )
        session.add(proj)
        session.flush()
        proj_id = proj.id
    return {"camera_id": cam_id, "project_id": proj_id, "storage_path": storage}


@pytest.fixture(autouse=True)
def _bypass_snapshot_guard():
    """Bypass the SSRF guard for snapshot URLs in these behaviour tests.

    The mock snapshot server binds to 127.0.0.1, which is always blocked by
    the SSRF guard. These tests verify adapter behaviour (auth, frame parsing,
    FrameWriter integration), not the guard itself. The guard's denial of
    loopback targets is tested separately in the abuse test suite.
    """
    with patch("timelapse_manager.cameras.http_jpeg._guard_snapshot_url"):
        yield


# ---------------------------------------------------------------------------
# HttpJpegAdapter against mock server
# ---------------------------------------------------------------------------


class TestHttpJpegAdapterCapture:
    async def test_capture_returns_captured_frame(
        self, mock_snapshot_server: str, http_client: httpx.AsyncClient
    ) -> None:
        url = f"{mock_snapshot_server}/snapshot.jpg"
        adapter = HttpJpegAdapter(http_client, url)

        frame = await adapter.capture()

        assert isinstance(frame, CapturedFrame)
        assert len(frame.image_bytes) > 0

    async def test_captured_frame_is_jpeg_format(
        self, mock_snapshot_server: str, http_client: httpx.AsyncClient
    ) -> None:
        url = f"{mock_snapshot_server}/snapshot.jpg"
        adapter = HttpJpegAdapter(http_client, url)

        frame = await adapter.capture()

        # Bytes start with JPEG SOI marker
        assert frame.image_bytes[:2] == b"\xff\xd8"
        assert frame.format in ("jpeg", "jpg")

    async def test_captured_frame_has_timestamp(
        self, mock_snapshot_server: str, http_client: httpx.AsyncClient
    ) -> None:
        url = f"{mock_snapshot_server}/snapshot.jpg"
        adapter = HttpJpegAdapter(http_client, url)

        frame = await adapter.capture()

        assert frame.captured_at is not None
        assert frame.captured_at.tzinfo is not None  # timezone-aware

    async def test_validate_connection_returns_ok(
        self, mock_snapshot_server: str, http_client: httpx.AsyncClient
    ) -> None:
        url = f"{mock_snapshot_server}/snapshot.jpg"
        adapter = HttpJpegAdapter(http_client, url)

        result = await adapter.validate_connection()

        assert result.ok is True
        assert result.reason is None

    async def test_close_is_safe(
        self, mock_snapshot_server: str, http_client: httpx.AsyncClient
    ) -> None:
        url = f"{mock_snapshot_server}/snapshot.jpg"
        adapter = HttpJpegAdapter(http_client, url)
        await adapter.close()  # should not raise; http client is caller-owned


# ---------------------------------------------------------------------------
# VapixAdapter against mock server (uses VAPIX CGI path)
# ---------------------------------------------------------------------------


class TestVapixAdapterCapture:
    async def test_vapix_captures_from_axis_cgi_path(
        self, mock_snapshot_server: str, http_client: httpx.AsyncClient
    ) -> None:
        # mock server serves GET /axis-cgi/jpg/image.cgi
        # Extract host:port from base URL
        host_port = mock_snapshot_server.removeprefix("http://")
        adapter = VapixAdapter(http_client, address=host_port)

        frame = await adapter.capture()

        assert isinstance(frame, CapturedFrame)
        assert frame.image_bytes[:2] == b"\xff\xd8"

    async def test_vapix_with_explicit_snapshot_uri_uses_it(
        self, mock_snapshot_server: str, http_client: httpx.AsyncClient
    ) -> None:
        explicit_uri = f"{mock_snapshot_server}/snapshot.jpg"
        adapter = VapixAdapter(
            http_client, address="irrelevant", snapshot_uri=explicit_uri
        )

        frame = await adapter.capture()

        assert len(frame.image_bytes) > 0


# ---------------------------------------------------------------------------
# FrameWriter integration with HttpJpegAdapter
# ---------------------------------------------------------------------------


class TestFrameWriterIntegration:
    @pytest.mark.slow
    async def test_capture_and_write_creates_file_and_row(
        self,
        mock_snapshot_server: str,
        http_client: httpx.AsyncClient,
        migrated_factory,
        seeded_project: dict,
    ) -> None:
        url = f"{mock_snapshot_server}/snapshot.jpg"
        adapter = HttpJpegAdapter(http_client, url)
        writer = FrameWriter(migrated_factory, seeded_project["storage_path"].parent)

        frame = await adapter.capture()
        result = writer.write(seeded_project["project_id"], frame)

        # File on disk
        assert Path(result.file_path).exists()
        assert Path(result.file_path).stat().st_size == len(frame.image_bytes)

        # Row in DB
        with session_scope(migrated_factory) as session:
            db_frame = session.get(Frame, result.frame_id)
        assert db_frame is not None
        assert db_frame.project_id == seeded_project["project_id"]
        assert db_frame.sequence_index == 1

    @pytest.mark.slow
    async def test_two_captures_produce_sequential_indices(
        self,
        mock_snapshot_server: str,
        http_client: httpx.AsyncClient,
        migrated_factory,
        seeded_project: dict,
    ) -> None:
        url = f"{mock_snapshot_server}/snapshot.jpg"
        adapter = HttpJpegAdapter(http_client, url)
        writer = FrameWriter(migrated_factory, seeded_project["storage_path"].parent)
        project_id = seeded_project["project_id"]

        r1 = writer.write(project_id, await adapter.capture())
        r2 = writer.write(project_id, await adapter.capture())

        assert r1.sequence_index == 1
        assert r2.sequence_index == 2
