"""F-suite: CLI bearer-token path coexistence with the web session layer."""

from __future__ import annotations

from fastapi.testclient import TestClient

from timelapse_manager.runtime import get_context
from timelapse_manager.security.token import ensure_local_token


class TestCliBearerWorks:
    def test_api_system_with_valid_bearer_returns_200(
        self, cli_client: TestClient
    ) -> None:
        ctx = get_context()
        token = ensure_local_token(ctx.settings)
        resp = cli_client.get(
            "/api/v1/system",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

    def test_api_with_no_bearer_returns_401(self, cli_client: TestClient) -> None:
        resp = cli_client.get("/api/v1/system")
        assert resp.status_code == 401

    def test_api_with_wrong_bearer_returns_401(self, cli_client: TestClient) -> None:
        resp = cli_client.get(
            "/api/v1/system",
            headers={"Authorization": "Bearer wrong-token-value"},
        )
        assert resp.status_code == 401

    def test_api_with_malformed_bearer_returns_401(
        self, cli_client: TestClient
    ) -> None:
        resp = cli_client.get(
            "/api/v1/system",
            headers={"Authorization": "NotBearer something"},
        )
        assert resp.status_code == 401


class TestCliBearerCsrfExempt:
    def test_api_post_with_bearer_no_csrf_header_succeeds(
        self, cli_client: TestClient
    ) -> None:
        """The CLI bearer path has no session cookie, so CSRF is exempt."""
        ctx = get_context()
        token = ensure_local_token(ctx.settings)
        # POST to the camera discovery endpoint via API (bearer auth, no CSRF).
        # We only need to verify it reaches the route (not get a CSRF 403).
        resp = cli_client.post(
            "/api/v1/cameras/discover",
            headers={"Authorization": f"Bearer {token}"},
            json={"range": None},
        )
        # Any non-403 response means CSRF did not block it.
        assert resp.status_code != 403

    def test_bearer_path_ignores_cookie_csrf_constraint(
        self, cli_client: TestClient
    ) -> None:
        """A bearer request with a bogus CSRF header is still processed."""
        ctx = get_context()
        token = ensure_local_token(ctx.settings)
        resp = cli_client.get(
            "/api/v1/system",
            headers={
                "Authorization": f"Bearer {token}",
                "X-CSRF-Token": "bogus-csrf-value",
            },
        )
        # The CSRF middleware only fires for cookie-bearing requests, not bearer.
        assert resp.status_code == 200
