"""Tests for the GET /healthz liveness endpoint."""

from __future__ import annotations

import subprocess

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from fastapi.testclient import TestClient

import timelapse_manager
from timelapse_manager.app import create_app
from timelapse_manager.config.settings import Settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(settings: Settings) -> TestClient:
    """Return a TestClient whose lifespan has run (enters and stays entered)."""
    return TestClient(create_app(settings))


# ---------------------------------------------------------------------------
# Shape and authentication
# ---------------------------------------------------------------------------


class TestHealthzShape:
    """The endpoint returns the expected four-field JSON shape."""

    def test_returns_http_200(self, client: TestClient) -> None:
        response = client.get("/healthz")
        assert response.status_code == 200

    def test_response_is_json(self, client: TestClient) -> None:
        response = client.get("/healthz")
        response.json()  # raises if not valid JSON

    def test_response_has_exactly_five_keys(self, client: TestClient) -> None:
        body = client.get("/healthz").json()
        assert set(body.keys()) == {
            "app_version",
            "ffmpeg_version",
            "ffmpeg_path",
            "db_status",
            "alembic_revision",
        }

    def test_ffmpeg_path_is_non_empty_string(self, client: TestClient) -> None:
        body = client.get("/healthz").json()
        assert isinstance(body["ffmpeg_path"], str)
        assert body["ffmpeg_path"].strip() != ""

    def test_is_unauthenticated(self, client: TestClient) -> None:
        """The liveness endpoint must not require a bearer token."""
        response = client.get("/healthz")
        assert response.status_code == 200

    def test_app_version_matches_package_version(self, client: TestClient) -> None:
        body = client.get("/healthz").json()
        assert body["app_version"] == timelapse_manager.__version__

    def test_app_version_is_non_empty_string(self, client: TestClient) -> None:
        body = client.get("/healthz").json()
        assert isinstance(body["app_version"], str)
        assert body["app_version"].strip() != ""

    def test_ffmpeg_version_is_non_empty_string(self, client: TestClient) -> None:
        body = client.get("/healthz").json()
        assert isinstance(body["ffmpeg_version"], str)
        assert body["ffmpeg_version"].strip() != ""

    def test_db_status_is_ok_with_running_app(self, client: TestClient) -> None:
        """A fully started app should report db_status == 'ok'."""
        body = client.get("/healthz").json()
        assert body["db_status"] == "ok"

    def test_db_status_is_string(self, client: TestClient) -> None:
        body = client.get("/healthz").json()
        assert isinstance(body["db_status"], str)

    def test_alembic_revision_is_string(self, client: TestClient) -> None:
        body = client.get("/healthz").json()
        assert isinstance(body["alembic_revision"], str)


# ---------------------------------------------------------------------------
# Alembic revision reporting
# ---------------------------------------------------------------------------


class TestHealthzAlembicRevision:
    """The alembic_revision field reflects the database migration state."""

    def test_revision_is_unknown_on_unmigrated_db(self, settings: Settings) -> None:
        """A fresh (unmigrated) DB should report 'unknown', not crash."""
        with TestClient(create_app(settings)) as c:
            body = c.get("/healthz").json()
        assert body["alembic_revision"] == "unknown"

    def test_revision_is_current_head_after_migration(
        self, settings: Settings, alembic_cfg: Config
    ) -> None:
        """After upgrade head the revision field shows the current head."""
        # Override the alembic config to use this test's settings DB.
        alembic_cfg.set_main_option("sqlalchemy.url", settings.database.url)
        alembic_command.upgrade(alembic_cfg, "head")

        from alembic.script import ScriptDirectory

        head = ScriptDirectory.from_config(alembic_cfg).get_current_head()

        with TestClient(create_app(settings)) as c:
            body = c.get("/healthz").json()
        assert body["alembic_revision"] == head


# ---------------------------------------------------------------------------
# db_status degraded path
# ---------------------------------------------------------------------------


class TestHealthzDbStatus:
    """The db_status field degrades gracefully when the database is broken."""

    def test_db_status_non_ok_returns_503_and_does_not_raise(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A broken DB yields 503 with the full body, and never raises.

        When the database probe reports a non-ok status the endpoint must
        signal not-ready via the HTTP status so a proxy can drain the instance,
        while still returning the same JSON shape and never propagating an
        exception.
        """
        import timelapse_manager.app as app_mod

        original_db_status = app_mod._db_status  # noqa: SLF001

        def _always_error(_factory: object) -> str:
            return "error"

        monkeypatch.setattr(app_mod, "_db_status", _always_error)
        with TestClient(create_app(settings)) as c:
            response = c.get("/healthz")
        assert response.status_code == 503
        body = response.json()
        assert body["db_status"] != "ok"
        # The body shape is unchanged from the healthy path.
        assert set(body.keys()) == {
            "app_version",
            "ffmpeg_version",
            "ffmpeg_path",
            "db_status",
            "alembic_revision",
        }
        monkeypatch.setattr(app_mod, "_db_status", original_db_status)

    def test_db_probe_exception_returns_503_and_does_not_raise(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the db probe itself raises, the handler reports 503, not 500.

        The handler catches everything: an exception inside the probe leaves
        db_status at its safe default and the route still returns the full body
        with a 503 status rather than propagating the error.
        """
        import timelapse_manager.app as app_mod

        original_db_status = app_mod._db_status  # noqa: SLF001

        def _boom(_factory: object) -> str:
            raise RuntimeError("db probe blew up")

        monkeypatch.setattr(app_mod, "_db_status", _boom)
        with TestClient(create_app(settings)) as c:
            response = c.get("/healthz")
        assert response.status_code == 503
        assert response.json()["db_status"] != "ok"
        monkeypatch.setattr(app_mod, "_db_status", original_db_status)


# ---------------------------------------------------------------------------
# ffmpeg absence / probe failure paths
# ---------------------------------------------------------------------------


class TestHealthzWithFfmpegAbsent:
    """Verify healthz gracefully handles a missing ffmpeg binary."""

    def test_ffmpeg_version_is_unavailable_when_binary_missing(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "timelapse_manager.version.subprocess.run",
            lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
        )
        with TestClient(create_app(settings)) as c:
            body = c.get("/healthz").json()
        assert body["ffmpeg_version"] == "unavailable"

    def test_endpoint_returns_200_when_ffmpeg_missing(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "timelapse_manager.version.subprocess.run",
            lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
        )
        with TestClient(create_app(settings)) as c:
            assert c.get("/healthz").status_code == 200

    def test_ffmpeg_version_is_unavailable_on_timeout(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "timelapse_manager.version.subprocess.run",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(cmd="ffmpeg", timeout=5)
            ),
        )
        with TestClient(create_app(settings)) as c:
            body = c.get("/healthz").json()
        assert body["ffmpeg_version"] == "unavailable"

    def test_ffmpeg_version_is_unavailable_on_nonzero_exit(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        completed = subprocess.CompletedProcess(
            args=["ffmpeg", "-version"], returncode=1, stdout="", stderr=""
        )
        monkeypatch.setattr(
            "timelapse_manager.version.subprocess.run",
            lambda *args, **kwargs: completed,
        )
        with TestClient(create_app(settings)) as c:
            body = c.get("/healthz").json()
        assert body["ffmpeg_version"] == "unavailable"

    def test_ffmpeg_version_is_unavailable_on_empty_stdout(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        completed = subprocess.CompletedProcess(
            args=["ffmpeg", "-version"], returncode=0, stdout="", stderr=""
        )
        monkeypatch.setattr(
            "timelapse_manager.version.subprocess.run",
            lambda *args, **kwargs: completed,
        )
        with TestClient(create_app(settings)) as c:
            body = c.get("/healthz").json()
        assert body["ffmpeg_version"] == "unavailable"

    def test_app_version_still_correct_when_ffmpeg_missing(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "timelapse_manager.version.subprocess.run",
            lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
        )
        with TestClient(create_app(settings)) as c:
            body = c.get("/healthz").json()
        assert body["app_version"] == timelapse_manager.__version__


class TestHealthzWithFfmpegPresent:
    """Verify healthz parses a real-looking ffmpeg version line."""

    def test_first_line_of_stdout_returned_as_ffmpeg_version(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        expected = "ffmpeg version 6.1.2 Copyright (c) 2000-2024 the FFmpeg developers"
        fake_stdout = expected + "\nextra line"
        completed = subprocess.CompletedProcess(
            args=["ffmpeg", "-version"], returncode=0, stdout=fake_stdout, stderr=""
        )
        monkeypatch.setattr(
            "timelapse_manager.version.subprocess.run",
            lambda *args, **kwargs: completed,
        )
        with TestClient(create_app(settings)) as c:
            body = c.get("/healthz").json()
        assert body["ffmpeg_version"] == expected
