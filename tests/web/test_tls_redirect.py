"""H-suite: TLS redirect middleware and effective-scheme detection."""

from __future__ import annotations

import logging
from collections.abc import Generator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.conftest import LOOPBACK_PEER, seed_admin
from timelapse_manager.app import create_app
from timelapse_manager.config.settings import ServerSettings, Settings
from timelapse_manager.web.middleware import AllowedHostsMiddleware


class TestHttpsRedirectMiddleware:
    def test_direct_http_request_triggers_308_redirect(
        self, web_client_http: TestClient
    ) -> None:
        """A genuine effective-http request is 308'd to https when enabled."""
        resp = web_client_http.get("/", follow_redirects=False)
        assert resp.status_code == 308
        assert resp.headers["location"].startswith("https://")

    def test_forged_forwarded_proto_https_is_ignored_for_direct_client(
        self, web_client_http: TestClient
    ) -> None:
        """A direct (untrusted) client cannot use X-Forwarded-Proto to dodge TLS.

        The TestClient peer is not a configured trusted proxy, so the forwarded
        header is ignored and the real http connection still triggers the
        redirect. This is the security property: only a trusted proxy may assert
        the effective scheme.
        """
        resp = web_client_http.get(
            "/",
            headers={"X-Forwarded-Proto": "https"},
            follow_redirects=False,
        )
        assert resp.status_code == 308
        assert resp.headers["location"].startswith("https://")

    def test_real_https_request_passes_through(self, web_client: TestClient) -> None:
        """An effective-https request (real TLS base URL) is NOT redirected."""
        seed_admin(web_client)
        resp = web_client.get("/", follow_redirects=False)
        # Allowed through (303 to /first-run or 200 dashboard), never a 308.
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


# ---------------------------------------------------------------------------
# server.allowed_hosts (#67 S20)
# ---------------------------------------------------------------------------

_DEPLOYED_HOSTS = ["timelapse.example.com", "10.0.12.26", "timelapse01.example.com"]


def _client_with_hosts(
    web_settings: Settings,
    alembic_cfg: Config,
    allowed_hosts: list[str],
    *,
    base_url: str = "https://testserver",
) -> TestClient:
    settings = web_settings.model_copy(
        update={
            "server": web_settings.server.model_copy(
                update={"allowed_hosts": allowed_hosts}
            )
        }
    )
    alembic_cfg.set_main_option("sqlalchemy.url", settings.database.url)
    alembic_command.upgrade(alembic_cfg, "head")
    return TestClient(
        create_app(settings),
        base_url=base_url,
        follow_redirects=False,
        client=LOOPBACK_PEER,
    )


@pytest.fixture()
def hosts_client(
    web_settings: Settings, alembic_cfg: Config
) -> Generator[TestClient, None, None]:
    """A client whose app answers only the deployed host names."""
    with _client_with_hosts(web_settings, alembic_cfg, _DEPLOYED_HOSTS) as client:
        yield client


class TestAllowedHosts:
    def test_disallowed_host_is_400(self, hosts_client: TestClient) -> None:
        resp = hosts_client.get("/healthz", headers={"Host": "evil.example"})
        assert resp.status_code == 400
        assert resp.text == "Invalid host header"
        # Even the 400 carries the correlation id (outermost layer runs first).
        assert resp.headers.get("X-Request-ID")

    @pytest.mark.parametrize(
        "host",
        [
            "timelapse.example.com",
            "TIMELAPSE.example.com",
            "timelapse.example.com.",
            "timelapse.example.com:443",
            "10.0.12.26:8080",
            "timelapse01.example.com",
        ],
    )
    def test_configured_hosts_are_served(
        self, hosts_client: TestClient, host: str
    ) -> None:
        resp = hosts_client.get("/healthz", headers={"Host": host})
        assert resp.status_code == 200

    @pytest.mark.parametrize(
        "host",
        ["127.0.0.1:8080", "localhost", "localhost:8080", "[::1]:8080", "[::1]"],
    )
    def test_loopback_is_always_allowed(
        self, hosts_client: TestClient, host: str
    ) -> None:
        """The CLI, ``tlm-upgrade.sh`` and local probes keep working."""
        resp = hosts_client.get("/healthz", headers={"Host": host})
        assert resp.status_code == 200

    @pytest.mark.parametrize(
        "host",
        [
            "evil.timelapse.example.com",
            "timelapse.example.com.evil.example",
            "timelapse.example.com:notaport",
            "10.0.12.26:8080:1",
            "[::1",
            "",
        ],
    )
    def test_lookalike_and_malformed_hosts_are_400(
        self, hosts_client: TestClient, host: str
    ) -> None:
        resp = hosts_client.get("/healthz", headers={"Host": host})
        assert resp.status_code == 400

    def test_api_is_covered_too(self, hosts_client: TestClient) -> None:
        resp = hosts_client.get("/api/v1/system", headers={"Host": "evil.example"})
        assert resp.status_code == 400

    def test_bad_host_gets_400_not_a_redirect_to_it(
        self, web_settings: Settings, alembic_cfg: Config
    ) -> None:
        with _client_with_hosts(
            web_settings, alembic_cfg, _DEPLOYED_HOSTS, base_url="http://testserver"
        ) as client:
            resp = client.get("/login", headers={"Host": "evil.example"})
            assert resp.status_code == 400
            ok = client.get("/login", headers={"Host": "timelapse.example.com"})
            assert ok.status_code == 308
            target = urlsplit(ok.headers["location"])
            assert target.hostname == "timelapse.example.com"
            assert target.username is None

    def test_subdomain_wildcard(
        self, web_settings: Settings, alembic_cfg: Config
    ) -> None:
        with _client_with_hosts(web_settings, alembic_cfg, ["*.example.com"]) as client:
            assert client.get(
                "/healthz", headers={"Host": "a.example.com"}
            ).status_code == (200)
            assert (
                client.get("/healthz", headers={"Host": "example.com"}).status_code
                == 400
            )
            assert (
                client.get("/healthz", headers={"Host": "evilexample.com"}).status_code
                == 400
            )

    def test_star_default_answers_any_host(self, web_client: TestClient) -> None:
        """The default (both deployed VMs today) behaves exactly as before."""
        assert web_client.app.user_middleware  # type: ignore[attr-defined]
        assert not any(
            m.cls is AllowedHostsMiddleware
            for m in web_client.app.user_middleware  # type: ignore[attr-defined]
        )
        for host in ("anything.example", "10.9.9.9", "timelapse.example.com"):
            assert web_client.get("/healthz", headers={"Host": host}).status_code == 200

    def test_rejection_is_logged_once_per_host(
        self, hosts_client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                hosts_client.get("/healthz", headers={"Host": "evil.example"})
        hits = [r for r in caplog.records if "allowed_hosts" in r.getMessage()]
        assert len(hits) == 1


class TestRedirectHostBrackets:
    def test_ipv6_host_is_rebracketed_in_the_redirect(
        self, web_client_http: TestClient
    ) -> None:
        resp = web_client_http.get("/login", headers={"Host": "[::1]:8080"})
        assert resp.status_code == 308
        assert resp.headers["location"].startswith("https://[::1]:")
        assert urlsplit(resp.headers["location"]).hostname == "::1"


class TestAllowedHostsSetting:
    def test_default_is_star(self) -> None:
        assert ServerSettings().allowed_hosts == ["*"]

    def test_entries_are_normalised(self) -> None:
        settings = ServerSettings(
            allowed_hosts=[
                "Timelapse.example.com.",
                "[::1]",
                "10.0.12.26",
                "*.Example.com",
            ]
        )
        assert settings.allowed_hosts == [
            "timelapse.example.com",
            "::1",
            "10.0.12.26",
            "*.example.com",
        ]

    def test_star_anywhere_means_any_host(self) -> None:
        assert ServerSettings(allowed_hosts=["a.example", "*"]).allowed_hosts == ["*"]

    def test_env_string_is_split(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "TLM_SERVER__ALLOWED_HOSTS", "timelapse.example.com, 10.0.12.26"
        )
        assert Settings().server.allowed_hosts == [
            "timelapse.example.com",
            "10.0.12.26",
        ]

    @pytest.mark.parametrize(
        "entry",
        [
            "https://timelapse.example.com",
            "timelapse.example.com:443",
            "timelapse.example.com/path",
            "time*.example.com",
            "a.*.com",
        ],
    )
    def test_typos_fail_at_load(self, entry: str) -> None:
        with pytest.raises(ValidationError):
            ServerSettings(allowed_hosts=[entry])

    def test_empty_list_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ServerSettings(allowed_hosts=[])


class TestHostParserDifferentials:
    """One strict parser: the allowlist and the redirect see the same host.

    Starlette's ``request.url.hostname`` is ``urlsplit`` of ``scheme://<Host>``,
    so ``Host: timelapse.example.com@evil`` has hostname ``evil``. Any header that
    is not exactly ``name-or-ip[:port]`` is refused rather than interpreted.
    """

    @pytest.mark.parametrize(
        "host",
        [
            "timelapse.example.com@evil.example",
            "evil.example@timelapse.example.com",
            "timelapse.example.com:443@evil.example",
            "evil/timelapse.example.com",
            "timelapse.example.com/evil",
            "timelapse.example.com\\evil.example",
            "evil.example\\timelapse.example.com",
            "timelapse.example.com..",
            "timelapse.example.com?x",
            "timelapse.example.com#x",
            "timelapse.example.com:",
            "timelapse.example.com:99999999",
            "[10.0.12.26]",
            "[::1]x",
            "::1",
        ],
    )
    def test_crafted_hosts_are_refused(
        self, hosts_client: TestClient, host: str
    ) -> None:
        resp = hosts_client.get("/healthz", headers={"Host": host})
        assert resp.status_code == 400, host

    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            "127.0.0.1:8080",
            "localhost",
            "LOCALHOST:8080",
            "10.0.12.26",
            "10.0.12.26:8080",
            "[::1]",
            "[::1]:8080",
            "[0:0:0:0:0:0:0:1]:8080",
            "TIMELAPSE.EXAMPLE.COM",
            "timelapse.example.com.",
            "timelapse.example.com.:443",
        ],
    )
    def test_valid_forms_are_served(self, hosts_client: TestClient, host: str) -> None:
        resp = hosts_client.get("/healthz", headers={"Host": host})
        assert resp.status_code == 200, host

    def test_wildcard_does_not_match_lookalikes_or_the_apex(
        self, web_settings: Settings, alembic_cfg: Config
    ) -> None:
        with _client_with_hosts(web_settings, alembic_cfg, ["*.example.com"]) as client:
            for host, expected in (
                ("a.example.com", 200),
                ("A.B.Example.COM.", 200),
                ("example.com", 400),
                ("evil-example.com", 400),
                ("evilexample.com", 400),
                ("a.example.com@evil.example", 400),
                ("evil.example/a.example.com", 400),
            ):
                resp = client.get("/healthz", headers={"Host": host})
                assert resp.status_code == expected, host

    @pytest.mark.parametrize(
        "host",
        [
            "timelapse.example.com@evil.example",
            "evil.example/timelapse.example.com",
            "timelapse.example.com\\evil.example",
        ],
    )
    def test_star_default_redirect_never_targets_a_smuggled_host(
        self, web_client_http: TestClient, host: str
    ) -> None:
        resp = web_client_http.get("/login", headers={"Host": host})
        assert resp.status_code == 308
        target = urlsplit(resp.headers["location"])
        assert target.hostname == "localhost"
        assert target.username is None

    def test_star_default_malformed_bracket_is_not_a_500(
        self, web_client_http: TestClient
    ) -> None:
        resp = web_client_http.get("/login", headers={"Host": "[::1"})
        assert resp.status_code == 308
        target = urlsplit(resp.headers["location"])
        assert target.hostname == "localhost"
        assert target.username is None

    def test_redirect_keeps_path_and_query(self, web_client_http: TestClient) -> None:
        resp = web_client_http.get(
            "/login?next=%2Fprojects%2F3", headers={"Host": "Timelapse.example.com."}
        )
        assert resp.status_code == 308
        assert resp.headers["location"] == (
            "https://timelapse.example.com:8443/login?next=%2Fprojects%2F3"
        )


@pytest.mark.parametrize(
    "target",
    [b"@evil.example", b"@evil.example/login", b"http://evil.example/x", b"*"],
)
def test_non_origin_form_target_cannot_extend_the_redirect_authority(
    web_client_http: TestClient, target: bytes
) -> None:
    """``GET @evil.example`` must not become ``https://host:8443@evil.example``."""
    state: dict[str, Any] = {"status": 0, "headers": []}

    async def drive() -> None:
        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                state["headers"] = message["headers"]

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": target.decode("latin-1"),
            "raw_path": target,
            "query_string": b"",
            "root_path": "",
            "server": ("testserver", 80),
            "client": LOOPBACK_PEER,
            "headers": [(b"host", b"timelapse.example.com")],
        }
        await web_client_http.app(scope, receive, send)

    web_client_http.portal.call(drive)  # type: ignore[union-attr]
    assert state["status"] == 308
    location = dict(state["headers"])[b"location"].decode("latin-1")
    parsed = urlsplit(location)
    # Starlette's URL also folds such a path into the authority, so the host
    # cross-check fails closed to ``localhost``; either way, never evil.
    assert parsed.hostname in ("timelapse.example.com", "localhost")
    assert parsed.username is None
    assert parsed.path == "/"
