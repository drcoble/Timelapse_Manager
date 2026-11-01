"""Tests for the web AppError exception handler: JSON vs. browser responses."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from timelapse_manager.services.errors import (
    NotFoundError,
    PermissionError_,
    ValidationError,
)
from timelapse_manager.web.errors import install_error_handlers


@pytest.fixture()
def client() -> TestClient:
    """A minimal app whose routes raise AppError, with the handlers installed."""
    app = FastAPI()

    @app.get("/api/thing")
    def api_thing() -> None:
        raise NotFoundError("no such thing", field="id")

    @app.get("/page")
    def page() -> None:
        raise ValidationError("that value is not allowed")

    @app.get("/forbidden")
    def forbidden() -> None:
        raise PermissionError_("you may not do that")

    install_error_handlers(app)
    return TestClient(app)


class TestMachineResponse:
    """Machine callers (the API, JSON clients) get a JSON body at the status."""

    def test_api_path_returns_json_with_code_and_detail(
        self, client: TestClient
    ) -> None:
        response = client.get("/api/thing")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == {"detail": "no such thing", "code": "not_found"}

    def test_non_api_path_default_accept_is_machine(self, client: TestClient) -> None:
        """A programmatic ``Accept: */*`` caller keeps the JSON body."""
        response = client.get("/page", headers={"accept": "*/*"})
        assert response.status_code == 422
        assert response.json() == {
            "detail": "that value is not allowed",
            "code": "unprocessable",
        }


class TestBrowserResponse:
    """Browsers get HTML: a 403 page, an HTMX fragment, or a generic page."""

    def test_browser_403_renders_access_denied_page(self, client: TestClient) -> None:
        response = client.get("/forbidden", headers={"accept": "text/html"})
        assert response.status_code == 403
        assert response.headers["content-type"].startswith("text/html")
        assert "Access denied" in response.text
        assert "you may not do that" in response.text

    def test_browser_generic_error_page(self, client: TestClient) -> None:
        response = client.get("/page", headers={"accept": "text/html"})
        assert response.status_code == 422
        assert response.headers["content-type"].startswith("text/html")
        assert "Something went wrong" in response.text
        assert "that value is not allowed" in response.text

    def test_htmx_request_returns_inline_fragment(self, client: TestClient) -> None:
        response = client.get("/page", headers={"HX-Request": "true"})
        assert response.status_code == 422
        assert 'class="inline-error"' in response.text
        assert "that value is not allowed" in response.text
        # An inline fragment is not a whole page.
        assert "<html" not in response.text.lower()

    def test_htmx_detail_is_html_escaped(self, client: TestClient) -> None:
        """The inline fragment escapes the detail so it cannot inject markup."""
        app = FastAPI()

        @app.get("/x")
        def x() -> None:
            raise ValidationError("<script>alert(1)</script>")

        install_error_handlers(app)
        response = TestClient(app).get("/x", headers={"HX-Request": "true"})
        assert "<script>" not in response.text
        assert "&lt;script&gt;" in response.text
