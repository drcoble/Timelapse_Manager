"""The shared same-site path rule (#67 S21), and the sites that use it."""

from __future__ import annotations

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from tests.conftest import seed_admin
from timelapse_manager.web.routers import reports as reports_router
from timelapse_manager.web.routers.auth import _safe_next
from timelapse_manager.web.safe_redirect import safe_local_path

_FORM = {"Content-Type": "application/x-www-form-urlencoded"}

# Things a browser would turn into another host, or that smuggle one past a
# later decoder. Every one must be refused.
HOSTILE = [
    "//evil.example",
    "//evil.example/x",
    "///evil.example",
    "/\\evil.example",
    "\\\\evil.example",
    "\\/evil.example",
    "/\t/evil.example",
    "/\n/evil.example",
    "/\r/evil.example",
    "/\x0b/evil.example",
    "/\x00/evil.example",
    "/ /evil.example",
    "/\x7f/evil.example",
    "/%2F%2Fevil.example",
    "/%2f%2fevil.example",
    "/%5Cevil.example",
    "/%5cevil.example",
    "/%09/evil.example",
    "/%0a/evil.example",
    "/%00",
    "https://evil.example",
    "http://evil.example/x",
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    "data:text/html,x",
    "evil.example/x",
    "projects",
    "",
    "/∕evil.example",  # division slash, non-ASCII
    "/café",
    "/./partials/projects",
    "/a/../partials/projects",
    "/%2e/partials/projects",
    "/%2E%2E/partials/projects",
    "/a/%2e%2e/b",
    "/partials/projects",
    "/alerts/summary",
    "/x" + "a" * 3000,
]


@pytest.mark.parametrize("raw", HOSTILE)
def test_hostile_values_are_refused(raw: str) -> None:
    assert safe_local_path(raw) is None


@pytest.mark.parametrize("raw", [None, ""])
def test_empty_is_refused(raw: str | None) -> None:
    assert safe_local_path(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        "/",
        "/projects",
        "/projects/3?tab=frames",
        "/projects/3?tab=a%20b&x=%2F%2F",
        "/cameras#top",
        "/a%20b",
        "/a%2Fb",
        "/a.b/c",
        "/a..b",
        "/.well-known/x",
        "/projects?next=https://evil.example",
    ],
)
def test_harmless_paths_are_kept(raw: str) -> None:
    assert safe_local_path(raw) == raw


def test_the_length_cap_is_a_parameter() -> None:
    assert safe_local_path("/" + "a" * 10, max_length=5) is None
    assert safe_local_path("/" + "a" * 10, max_length=11) == "/" + "a" * 10


@pytest.mark.parametrize("raw", HOSTILE)
def test_login_next_falls_back_to_the_dashboard(raw: str) -> None:
    assert _safe_next(raw) == "/"


@pytest.mark.parametrize("raw", HOSTILE)
def test_report_from_is_refused(raw: str) -> None:
    assert reports_router._safe_from(raw) is None


@pytest.mark.parametrize(
    "hostile", ["/\\t/evil", "/%2F%2Fevil", "/\x0b/evil", "//evil", "https://evil"]
)
def test_post_login_next_is_never_followed_off_site(
    web_client: TestClient, hostile: str
) -> None:
    seed_admin(web_client)
    resp = web_client.post(
        "/login",
        data={
            "username": "admin",
            "password": "AdminP@ssw0rd1234",
            "next": hostile,
        },
        headers=_FORM,
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_login_page_never_echoes_a_hostile_next(web_client: TestClient) -> None:
    seed_admin(web_client)
    resp = web_client.get(f"/login?next={quote('//evil.example', safe='')}")
    assert resp.status_code == 200
    assert "evil.example" not in resp.text
    assert 'name="next" value="/"' in resp.text
