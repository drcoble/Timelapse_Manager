"""Tests for the ffmpeg_path field on /healthz and /api/v1/system.

Verifies that:
- /healthz includes ffmpeg_path as a non-empty string.
- /api/v1/system includes ffmpeg_path as a non-empty string.
- When an explicit ffmpeg_binary knob is set, both endpoints report that
  exact path (making the assertion deterministic without touching a real binary).
- The ffmpeg_version field is still present alongside ffmpeg_path.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from timelapse_manager.app import create_app
from timelapse_manager.config.settings import (
    CaptureSettings,
    DatabaseSettings,
    LoggingSettings,
    MonitoringSettings,
    PathsSettings,
    RenderSettings,
    Settings,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings_with_knob(tmp_path: Path) -> Settings:
    """Settings with an explicit ffmpeg_binary knob.

    The resolver returns the knob verbatim without checking existence, so this
    is usable without a real binary on disk and makes endpoint assertions
    deterministic.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return Settings(
        database=DatabaseSettings(url=f"sqlite:///{tmp_path}/test.db"),
        logging=LoggingSettings(level="WARNING", format="text"),
        paths=PathsSettings(
            data_dir=data_dir,
            frames_root=data_dir / "frames",
            token_file=data_dir / ".local-token",
        ),
        capture=CaptureSettings(autostart=False),
        render=RenderSettings(ffmpeg_binary="/opt/static/ffmpeg", autostart=False),
        monitoring=MonitoringSettings(autostart=False),
    )


@pytest.fixture()
def knob_client(
    settings_with_knob: Settings,
) -> pytest.Generator[TestClient, None, None]:
    """TestClient whose lifespan uses the knob-settings fixture."""
    with TestClient(create_app(settings_with_knob)) as c:
        yield c


# ---------------------------------------------------------------------------
# /healthz – ffmpeg_path field
# ---------------------------------------------------------------------------


class TestHealthzFfmpegPath:
    def test_ffmpeg_path_present(self, client: TestClient) -> None:
        body = client.get("/healthz").json()
        assert "ffmpeg_path" in body

    def test_ffmpeg_path_is_string(self, client: TestClient) -> None:
        body = client.get("/healthz").json()
        assert isinstance(body["ffmpeg_path"], str)

    def test_ffmpeg_path_is_non_empty(self, client: TestClient) -> None:
        body = client.get("/healthz").json()
        assert body["ffmpeg_path"].strip() != ""

    def test_ffmpeg_version_still_present_alongside_path(
        self, client: TestClient
    ) -> None:
        body = client.get("/healthz").json()
        assert "ffmpeg_version" in body
        assert isinstance(body["ffmpeg_version"], str)

    def test_ffmpeg_path_equals_knob_when_set(self, knob_client: TestClient) -> None:
        body = knob_client.get("/healthz").json()
        assert body["ffmpeg_path"] == "/opt/static/ffmpeg"

    def test_ffmpeg_path_consistent_across_requests(self, client: TestClient) -> None:
        """The path is resolved once at startup and cached; it must not change."""
        path1 = client.get("/healthz").json()["ffmpeg_path"]
        path2 = client.get("/healthz").json()["ffmpeg_path"]
        assert path1 == path2


# ---------------------------------------------------------------------------
# /api/v1/system – ffmpeg_path field
# ---------------------------------------------------------------------------


class TestSystemFfmpegPath:
    def test_ffmpeg_path_present(self, client: TestClient, auth_token: str) -> None:
        body = client.get(
            "/api/v1/system",
            headers={"Authorization": f"Bearer {auth_token}"},
        ).json()
        assert "ffmpeg_path" in body

    def test_ffmpeg_path_is_string(self, client: TestClient, auth_token: str) -> None:
        body = client.get(
            "/api/v1/system",
            headers={"Authorization": f"Bearer {auth_token}"},
        ).json()
        assert isinstance(body["ffmpeg_path"], str)

    def test_ffmpeg_path_is_non_empty(
        self, client: TestClient, auth_token: str
    ) -> None:
        body = client.get(
            "/api/v1/system",
            headers={"Authorization": f"Bearer {auth_token}"},
        ).json()
        assert body["ffmpeg_path"].strip() != ""

    def test_ffmpeg_version_still_present_alongside_path(
        self, client: TestClient, auth_token: str
    ) -> None:
        body = client.get(
            "/api/v1/system",
            headers={"Authorization": f"Bearer {auth_token}"},
        ).json()
        assert "ffmpeg_version" in body
        assert isinstance(body["ffmpeg_version"], str)

    def test_ffmpeg_path_equals_knob_when_set(
        self,
        settings_with_knob: Settings,
        tmp_path: Path,
    ) -> None:
        """When the knob is set, /api/v1/system reports that exact path."""
        from timelapse_manager.security.token import ensure_local_token

        token = ensure_local_token(settings_with_knob)
        with TestClient(create_app(settings_with_knob)) as c:
            body = c.get(
                "/api/v1/system",
                headers={"Authorization": f"Bearer {token}"},
            ).json()
        assert body["ffmpeg_path"] == "/opt/static/ffmpeg"

    def test_healthz_and_system_path_agree(
        self,
        settings_with_knob: Settings,
    ) -> None:
        """The path reported by /healthz and /api/v1/system must be the same."""
        from timelapse_manager.security.token import ensure_local_token

        token = ensure_local_token(settings_with_knob)
        with TestClient(create_app(settings_with_knob)) as c:
            healthz_path = c.get("/healthz").json()["ffmpeg_path"]
            system_path = c.get(
                "/api/v1/system",
                headers={"Authorization": f"Bearer {token}"},
            ).json()["ffmpeg_path"]
        assert healthz_path == system_path
