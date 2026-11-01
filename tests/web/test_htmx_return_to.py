"""``HX-Current-URL`` is untrusted input to the login return-to (#70 F-S6).

Any client can send the header. It is used only when it names this host (or no
host) and a safe path; anything else gives a bare ``/login``.
"""

from __future__ import annotations

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient


def _redirect_for(client: TestClient, current_url: str) -> str:
    resp = client.get(
        "/alerts/summary",
        headers={"HX-Request": "true", "HX-Current-URL": current_url},
        follow_redirects=False,
    )
    assert resp.status_code == 204
    return resp.headers["HX-Redirect"]


@pytest.mark.parametrize(
    "current",
    [
        "http://testserver//evil.example/x",
        "https://testserver//evil.example/x",
        "https://evil.example/x",
        "https://evil.example/projects",
        "//evil.example/x",
        "/\\evil",
        "//testserver//evil.example",
        "javascript:alert(1)",
        "ftp://testserver/projects",
        "https://user@testserver/projects",
        "https://testserver.evil.example/projects",
        "https://testserver:8443/projects",
        "https://testserver/%2F%2Fevil.example",
        "https://testserver/%5Cevil.example",
        "https://testserver/./partials/projects",
        "https://testserver/partials/projects",
        "https://testserver/alerts/summary",
        "http://[::1",
        "evil.example/x",
    ],
)
def test_untrusted_current_url_gives_a_bare_login(
    anon_client: TestClient, current: str
) -> None:
    assert _redirect_for(anon_client, current) == "/login"


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        ("https://testserver/projects", "/projects"),
        ("http://testserver/projects?tab=frames", "/projects?tab=frames"),
        ("https://TESTSERVER/projects", "/projects"),
        ("/projects", "/projects"),
        ("https://testserver", "/"),
    ],
)
def test_a_same_host_page_is_preserved(
    anon_client: TestClient, current: str, expected: str
) -> None:
    assert _redirect_for(anon_client, current) == (
        f"/login?next={quote(expected, safe='')}"
    )
