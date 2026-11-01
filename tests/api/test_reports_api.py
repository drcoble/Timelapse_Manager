"""API tests for ``POST /api/v1/reports`` (#8).

The bearer-token twin of the web form: 201 with the report id, 401 without
the token, 429 past the hourly limit, 422 for bad input, and the same redacted
bundle on disk.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.reports_support import zip_members
from timelapse_manager.db.models import Report
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.security.token import ensure_local_token

_ROUTE = "/api/v1/reports"


@pytest.fixture()
def auth_token(web_client: TestClient) -> str:
    """The local bearer token of the migrated ``web_client`` app."""
    return ensure_local_token(get_context().settings)


@pytest.fixture()
def client(web_client: TestClient) -> TestClient:
    """The migrated app (``web_client``) under the name the tests use."""
    return web_client


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_requires_the_bearer_token(client: TestClient) -> None:
    assert client.post(_ROUTE, json={"title": "x"}).status_code == 401


def test_files_report_and_returns_its_id(client: TestClient, auth_token: str) -> None:
    resp = client.post(
        _ROUTE,
        json={"title": "Monitor: capture lag", "description": "lag > 60 s"},
        headers=_headers(auth_token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["saved"] is True
    assert body["delivering_to"] == []
    key = body["report_id"]
    with session_scope(get_context().session_factory) as session:
        report = session.execute(select(Report)).scalar_one()
        assert report.report_key == key
        assert report.user_id == 1  # the sentinel administrator
    data = (
        get_context().settings.paths.data_dir / "reports" / f"{key}.zip"
    ).read_bytes()
    members = zip_members(data)
    assert json.loads(members["report.json"])["source"] == "api"
    assert json.loads(members["diagnostics.json"])["available"] is True


def test_rate_limited_with_429(client: TestClient, auth_token: str) -> None:
    get_context().settings.reporting.rate_limit_per_hour = 2
    for _ in range(2):
        resp = client.post(_ROUTE, json={"title": "x"}, headers=_headers(auth_token))
        assert resp.status_code == 201
    resp = client.post(_ROUTE, json={"title": "x"}, headers=_headers(auth_token))
    assert resp.status_code == 429


def test_bad_input_is_422(client: TestClient, auth_token: str) -> None:
    headers = _headers(auth_token)
    assert client.post(_ROUTE, json={"title": ""}, headers=headers).status_code == 422
    resp = client.post(
        _ROUTE,
        json={"title": "x", "reported_at": "2020-01-01T00:00:00Z"},
        headers=headers,
    )
    assert resp.status_code == 422


def test_oversize_text_is_413(client: TestClient, auth_token: str) -> None:
    resp = client.post(
        _ROUTE,
        json={"title": "x", "description": "y" * 20_001},
        headers=_headers(auth_token),
    )
    assert resp.status_code == 413
