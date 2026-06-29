"""H-suite: TLS redirect middleware and effective-scheme detection."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import seed_admin


class TestHttpsRedirectMiddleware:
    def test_x_forwarded_proto_http_triggers_308_redirect(
        self, web_client: TestClient
    ) -> None:
        """An effective-http request is 308'd to https when redirect is enabled."""
        seed_admin(web_client)
        resp = web_client.get(
            "/",
            headers={"X-Forwarded-Proto": "http"},
            follow_redirects=False,
        )
        assert resp.status_code == 308
        assert resp.headers["location"].startswith("https://")

    def test_x_forwarded_proto_https_passes_through(
        self, web_client: TestClient
    ) -> None:
        """An effective-https request is NOT redirected."""
        seed_admin(web_client)
        resp = web_client.get(
            "/",
            headers={"X-Forwarded-Proto": "https"},
            follow_redirects=False,
        )
        # Should be allowed through (either 303 to /first-run or 200 for dashboard).
        # NOT a 308.
        assert resp.status_code != 308

    def test_no_redirect_when_redirect_disabled(
        self, web_client_no_redirect: TestClient
    ) -> None:
        """When redirect_http_to_https=False, http requests pass through."""
        seed_admin(web_client_no_redirect)
        resp = web_client_no_redirect.get(
            "/login",
            headers={"X-Forwarded-Proto": "http"},
            follow_redirects=False,
        )
        # Should reach the login page (200), not a 308.
        assert resp.status_code == 200


class TestTlsCertGeneration:
    def test_ensure_tls_cert_generates_cert_and_key(self, tmp_path: Path) -> None:
        """ensure_tls_cert generates a cert/key pair into the data directory."""
        from timelapse_manager.config.settings import (
            DatabaseSettings,
            LoggingSettings,
            PathsSettings,
            Settings,
            TlsSettings,
        )
        from timelapse_manager.service.tls import ensure_tls_cert

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        settings = Settings(
            database=DatabaseSettings(url="sqlite:///./unused.db"),
            logging=LoggingSettings(level="WARNING", format="text"),
            paths=PathsSettings(
                data_dir=data_dir,
                frames_root=data_dir / "frames",
                token_file=data_dir / ".token",
            ),
            tls=TlsSettings(auto_generate=True),
        )
        cert_path, key_path = ensure_tls_cert(settings)
        assert cert_path.exists(), "Certificate file must exist after generation"
        assert key_path.exists(), "Key file must exist after generation"

    def test_generated_key_has_owner_only_permissions(self, tmp_path: Path) -> None:
        """The private key must have 0600 permissions."""
        from timelapse_manager.config.settings import (
            DatabaseSettings,
            LoggingSettings,
            PathsSettings,
            Settings,
            TlsSettings,
        )
        from timelapse_manager.service.tls import ensure_tls_cert

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        settings = Settings(
            database=DatabaseSettings(url="sqlite:///./unused.db"),
            logging=LoggingSettings(level="WARNING", format="text"),
            paths=PathsSettings(
                data_dir=data_dir,
                frames_root=data_dir / "frames",
                token_file=data_dir / ".token",
            ),
            tls=TlsSettings(auto_generate=True),
        )
        _, key_path = ensure_tls_cert(settings)
        mode = key_path.stat().st_mode & 0o777
        assert mode == 0o600, f"Key permissions {oct(mode)} should be 0o600"

    def test_ensure_tls_cert_is_idempotent(self, tmp_path: Path) -> None:
        """A second call reuses the existing cert/key without regenerating."""
        from timelapse_manager.config.settings import (
            DatabaseSettings,
            LoggingSettings,
            PathsSettings,
            Settings,
            TlsSettings,
        )
        from timelapse_manager.service.tls import ensure_tls_cert

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        settings = Settings(
            database=DatabaseSettings(url="sqlite:///./unused.db"),
            logging=LoggingSettings(level="WARNING", format="text"),
            paths=PathsSettings(
                data_dir=data_dir,
                frames_root=data_dir / "frames",
                token_file=data_dir / ".token",
            ),
            tls=TlsSettings(auto_generate=True),
        )
        cert1, key1 = ensure_tls_cert(settings)
        mtime_cert_1 = cert1.stat().st_mtime
        cert2, key2 = ensure_tls_cert(settings)
        mtime_cert_2 = cert2.stat().st_mtime
        assert cert1 == cert2
        assert mtime_cert_1 == mtime_cert_2, "Second call must not overwrite the cert"
