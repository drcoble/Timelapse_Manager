"""Integration tests for the versioned local API.

Tests the GET /api/v1/system endpoint for:
- 401 without a bearer token
- 401 with an incorrect bearer token
- 200 with the correct token
- Response body shape and secret redaction
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi.testclient import TestClient


class TestSystemEndpointAuth:
    def test_returns_401_without_auth_header(self, client: TestClient) -> None:
        response = client.get("/api/v1/system")
        assert response.status_code == 401

    def test_returns_401_with_wrong_token(self, client: TestClient) -> None:
        response = client.get(
            "/api/v1/system",
            headers={"Authorization": "Bearer completely-wrong-token"},
        )
        assert response.status_code == 401

    def test_returns_401_with_malformed_scheme(self, client: TestClient) -> None:
        response = client.get(
            "/api/v1/system",
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
        )
        assert response.status_code == 401

    def test_returns_200_with_valid_token(
        self, client: TestClient, auth_token: str
    ) -> None:
        response = client.get(
            "/api/v1/system",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert response.status_code == 200


class TestSystemEndpointBody:
    def test_body_is_valid_json(self, client: TestClient, auth_token: str) -> None:
        response = client.get(
            "/api/v1/system",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        response.json()  # raises if not valid JSON

    def test_body_contains_app_version(
        self, client: TestClient, auth_token: str
    ) -> None:
        body = client.get(
            "/api/v1/system",
            headers={"Authorization": f"Bearer {auth_token}"},
        ).json()
        assert "app_version" in body
        assert isinstance(body["app_version"], str)
        assert body["app_version"].strip() != ""

    def test_body_contains_ffmpeg_version(
        self, client: TestClient, auth_token: str
    ) -> None:
        body = client.get(
            "/api/v1/system",
            headers={"Authorization": f"Bearer {auth_token}"},
        ).json()
        assert "ffmpeg_version" in body
        assert isinstance(body["ffmpeg_version"], str)

    def test_body_contains_db_status(self, client: TestClient, auth_token: str) -> None:
        body = client.get(
            "/api/v1/system",
            headers={"Authorization": f"Bearer {auth_token}"},
        ).json()
        assert "db_status" in body

    def test_body_contains_config_block(
        self, client: TestClient, auth_token: str
    ) -> None:
        body = client.get(
            "/api/v1/system",
            headers={"Authorization": f"Bearer {auth_token}"},
        ).json()
        assert "config" in body
        assert isinstance(body["config"], dict)

    def test_config_block_contains_ports(
        self, client: TestClient, auth_token: str
    ) -> None:
        config = client.get(
            "/api/v1/system",
            headers={"Authorization": f"Bearer {auth_token}"},
        ).json()["config"]
        assert "http_port" in config
        assert "https_port" in config

    def test_raw_token_not_in_response_body(
        self, client: TestClient, auth_token: str
    ) -> None:
        body_text = client.get(
            "/api/v1/system",
            headers={"Authorization": f"Bearer {auth_token}"},
        ).text
        assert auth_token not in body_text, (
            "Bearer token must not appear in the response body"
        )


class TestDbUrlRedaction:
    """Verify that _redact_db_url in api/system.py masks embedded credentials.

    Uses a manually-wired AppContext (no lifespan) so we can inject a
    postgres-style URL with real credentials without needing psycopg2.
    The SQLite engine handles the db_status query; only the settings'
    database.url field carries the fake postgres URL for redaction testing.
    """

    def test_credentials_redacted_in_database_url_field(self, tmp_path: Path) -> None:
        """Raw password in the DB URL must appear as *** in the API response."""
        import timelapse_manager.db.session as _db_session_mod
        import timelapse_manager.runtime as _runtime_mod
        from timelapse_manager.app import create_app
        from timelapse_manager.config.settings import (
            DatabaseSettings,
            LoggingSettings,
            PathsSettings,
            Settings,
        )
        from timelapse_manager.db.engine import create_db_engine
        from timelapse_manager.db.session import (
            create_session_factory,
            set_session_factory,
        )
        from timelapse_manager.runtime import AppContext, set_context
        from timelapse_manager.security.token import ensure_local_token

        # Build settings whose database.url carries embedded credentials.
        # The *actual* engine uses a real SQLite file so db_status returns "ok".
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        sqlite_path = tmp_path / "redact_test.db"
        token_file = data_dir / ".local-token"

        settings = Settings(
            database=DatabaseSettings(
                url="postgresql://admin:s3cr3tpassword@dbhost:5432/timelapse"
            ),
            logging=LoggingSettings(level="WARNING", format="text"),
            paths=PathsSettings(
                data_dir=data_dir,
                frames_root=data_dir / "frames",
                token_file=token_file,
            ),
        )

        # Build a real SQLite engine for db_status — no postgres driver needed.
        sqlite_engine = create_db_engine(f"sqlite:///{sqlite_path}")
        factory = create_session_factory(sqlite_engine)

        # Install the singletons the route dependency reads.
        set_session_factory(factory)
        token = ensure_local_token(settings)
        context = AppContext(
            settings=settings,
            db_engine=sqlite_engine,
            session_factory=factory,
            logger=logging.getLogger("test"),
            app_version="0.0.0",
            ffmpeg_version="unavailable",
        )
        set_context(context)

        try:
            # Skip the lifespan so our manually-installed context stands.
            app = create_app(settings)
            c = TestClient(app, raise_server_exceptions=True)
            body = c.get(
                "/api/v1/system",
                headers={"Authorization": f"Bearer {token}"},
            ).json()
        finally:
            sqlite_engine.dispose()
            _runtime_mod.dispose()
            _db_session_mod._session_factory = None  # noqa: SLF001

        db_url = body["config"]["database_url"]
        assert "s3cr3tpassword" not in db_url, (
            f"Raw password must not appear in database_url: {db_url}"
        )
        assert "***" in db_url, (
            f"Redacted placeholder '***' must appear in database_url: {db_url}"
        )
        assert "admin" not in db_url, (
            f"Username must not appear in database_url: {db_url}"
        )
        assert "dbhost" in db_url, (
            f"Host must still appear in database_url after redaction: {db_url}"
        )
