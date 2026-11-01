"""Tests for the streaming body-size cap and security-response headers.

Two independent hardening seams are covered:

* :class:`BodyLimitMiddleware` -- a request body that exceeds the configured
  ceiling is rejected with ``413`` even when it is sent chunked with no
  ``Content-Length`` (the bypass the per-endpoint fast-reject cannot see), while
  a body at or under the ceiling reaches the handler untouched.
* :class:`SecurityHeadersMiddleware` -- every response carries the fixed
  hardening headers, with HSTS present only over effective HTTPS and the CSP
  well-formed.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import re
from collections.abc import Generator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from tests.conftest import LOOPBACK_PEER, csrf_of, seed_admin
from tests.web.test_frames_import_web import _seed_project
from timelapse_manager.app import create_app
from timelapse_manager.config.settings import (
    CaptureSettings,
    DatabaseSettings,
    LoggingSettings,
    MonitoringSettings,
    PathsSettings,
    RenderSettings,
    SecretsSettings,
    ServerSettings,
    Settings,
    SsrfSettings,
)
from timelapse_manager.runtime import get_context
from timelapse_manager.security.token import ensure_local_token
from timelapse_manager.web.body_limit import (
    AUTH_FORM_MAX_BYTES,
    URLENCODED_FORM_MAX_BYTES,
    body_limit_for,
)
from timelapse_manager.web.dependencies import FormDep
from timelapse_manager.web.multipart import report_body_limit
from timelapse_manager.web.routers._shared import _form_getlist

# A deliberately tiny cap so tests need only kilobytes, not the 512 MiB default.
_TEST_BODY_CAP = 4096


# ---------------------------------------------------------------------------
# Body-size cap
# ---------------------------------------------------------------------------


@pytest.fixture()
def capped_settings(tmp_path: Path) -> Settings:
    """Settings with a tiny request-body ceiling for the body-limit tests."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = tmp_path / "capped.db"
    return Settings(
        database=DatabaseSettings(url=f"sqlite:///{db_path}"),
        logging=LoggingSettings(level="WARNING", format="text"),
        paths=PathsSettings(
            data_dir=data_dir,
            frames_root=data_dir / "frames",
            token_file=data_dir / ".local-token",
        ),
        capture=CaptureSettings(autostart=False),
        render=RenderSettings(autostart=False),
        monitoring=MonitoringSettings(autostart=False),
        ssrf=SsrfSettings(
            allowed_private_subnets=["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
        ),
        secrets=SecretsSettings(use_os_keystore=False),
        server=ServerSettings(max_request_body_bytes=_TEST_BODY_CAP),
    )


@pytest.fixture()
def capped_client(
    capped_settings: Settings, alembic_cfg: Config
) -> Generator[TestClient, None, None]:
    """Yield a migrated TestClient whose body cap is :data:`_TEST_BODY_CAP`."""
    alembic_cfg.set_main_option("sqlalchemy.url", capped_settings.database.url)
    alembic_command.upgrade(alembic_cfg, "head")
    app = create_app(capped_settings)
    with TestClient(app, base_url="https://testserver", client=LOOPBACK_PEER) as c:
        yield c


def _chunks(total: int, chunk: int = 1024) -> Iterator[bytes]:
    """Yield ``total`` bytes in fixed-size chunks.

    Passing an iterator to httpx as the request ``content`` makes it stream the
    body with ``Transfer-Encoding: chunked`` and *no* ``Content-Length`` -- the
    exact shape the per-endpoint Content-Length check cannot police.
    """
    sent = 0
    while sent < total:
        size = min(chunk, total - sent)
        yield b"\x00" * size
        sent += size


def _upload_url() -> str:
    # The single-file upload handler reads the raw body before any project
    # lookup, so the middleware's cap is its sole guard -- ideal for the test.
    return "/api/v1/projects/1/frames/upload"


def test_oversized_chunked_body_rejected_413(
    capped_client: TestClient, capped_settings: Settings
) -> None:
    """A chunked body over the cap is rejected 413 and never buffered/handled."""
    token = ensure_local_token(capped_settings)
    resp = capped_client.post(
        _upload_url(),
        params={"capture_timestamp": datetime.now(UTC).isoformat()},
        content=_chunks(_TEST_BODY_CAP * 2),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream",
        },
    )
    # The request carried no Content-Length (it was streamed), so this 413 can
    # only have come from the streaming middleware.
    assert resp.status_code == 413
    # The middleware's message, not the handler's "not a valid image" 422 --
    # proof the handler never processed a fully-buffered body.
    assert "size limit" in resp.text.lower()


def test_body_at_cap_reaches_handler(
    capped_client: TestClient, capped_settings: Settings
) -> None:
    """A body sized exactly at the cap passes the middleware to the handler."""
    token = ensure_local_token(capped_settings)
    resp = capped_client.post(
        _upload_url(),
        params={"capture_timestamp": datetime.now(UTC).isoformat()},
        content=_chunks(_TEST_BODY_CAP),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream",
        },
    )
    # Not rejected by the cap: the handler ran and rejected the all-zero bytes as
    # a non-image (422), proving the body reached it.
    assert resp.status_code != 413
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Security response headers
# ---------------------------------------------------------------------------

_EXPECTED_STATIC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def test_security_headers_present_over_https(web_client: TestClient) -> None:
    """Every fixed security header (incl. HSTS over HTTPS) is set on a response."""
    resp = web_client.get("/healthz")
    assert resp.status_code == 200
    for name, value in _EXPECTED_STATIC_HEADERS.items():
        assert resp.headers.get(name) == value
    assert "Permissions-Policy" in resp.headers
    assert "Content-Security-Policy" in resp.headers
    # HSTS is present because the test client uses an https base URL.
    assert resp.headers.get("Strict-Transport-Security") == (
        "max-age=31536000; includeSubDomains"
    )


def test_hsts_absent_over_http(web_client_no_redirect: TestClient) -> None:
    """HSTS must not be emitted over the plaintext HTTP fallback."""
    resp = web_client_no_redirect.get("/healthz")
    assert resp.status_code == 200
    # The other hardening headers are still present over http...
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert "Content-Security-Policy" in resp.headers
    # ...but HSTS is withheld so a plaintext client is never pinned to HTTPS.
    assert "Strict-Transport-Security" not in resp.headers


def test_csp_is_well_formed(web_client: TestClient) -> None:
    """The CSP locks the default source to self and the dangerous sinks down."""
    resp = web_client.get("/healthz")
    csp = resp.headers["Content-Security-Policy"]
    directives = {
        part.split(" ", 1)[0]: part
        for part in (chunk.strip() for chunk in csp.split(";"))
        if part
    }
    assert directives["default-src"] == "default-src 'self'"
    assert directives["frame-ancestors"] == "frame-ancestors 'none'"
    assert directives["object-src"] == "object-src 'none'"
    assert directives["base-uri"] == "base-uri 'self'"
    # Inline styles/scripts the server-rendered UI relies on are allowed only on
    # their own directives, and images may use data: URIs for the icon sprites.
    assert "data:" in directives["img-src"]
    assert "'unsafe-inline'" in directives["style-src"]
    # Scripts are external files only (#70 F-S3, #67-S17): no inline allowance
    # and no eval, whatever else the policy says.
    assert directives["script-src"] == "script-src 'self'"
    assert "'unsafe-eval'" not in csp
    assert "'unsafe-inline'" not in csp.replace(directives["style-src"], "")


def test_csp_header_is_identical_on_html_and_error_responses(
    anon_client: TestClient,
) -> None:
    """The login page, a 404 error page and a redirect carry one byte-identical CSP."""
    policies = {
        anon_client.get(path, follow_redirects=False).headers["Content-Security-Policy"]
        for path in ("/login", "/healthz", "/nope-not-a-route", "/")
    }
    assert len(policies) == 1


# ---------------------------------------------------------------------------
# Per-route body limits and the Content-Length pre-reject (#67 S6)
# ---------------------------------------------------------------------------

_MiB = 1024 * 1024


def _raw_post(
    client: TestClient,
    path: str,
    *,
    total_bytes: int,
    content_type: str,
    declared_length: int | None = None,
    scheme: str = "https",
    extra_headers: list[tuple[bytes, bytes]] | None = None,
    chunk: int = 64 * 1024,
    query: bytes = b"",
    prefix: bytes = b"",
) -> tuple[int, int, bytes]:
    """Drive one POST straight into the ASGI app; return (status, read, body).

    ``prefix`` replaces the start of the first chunk (a multipart opening, say);
    the rest of the body is filler.

    The body is produced lazily in ``chunk``-sized ``http.request`` events and
    ``read`` counts how many bytes the application actually pulled, so a test
    can prove a body was refused rather than buffered.
    """
    state: dict[str, Any] = {"read": 0, "status": 0, "body": b""}

    async def drive() -> None:
        sent = 0

        async def receive() -> dict[str, Any]:
            nonlocal sent
            if sent < total_bytes:
                size = min(chunk, total_bytes - sent)
                first = sent == 0
                sent += size
                state["read"] += size
                data = b"a" * size
                if first:
                    data = (prefix + data)[:size]
                return {
                    "type": "http.request",
                    "body": data,
                    "more_body": sent < total_bytes,
                }
            await asyncio.sleep(5)
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
            elif message["type"] == "http.response.body":
                state["body"] += message.get("body", b"")

        headers = [(b"host", b"testserver"), (b"content-type", content_type.encode())]
        if declared_length is not None:
            headers.append((b"content-length", str(declared_length).encode()))
        headers += extra_headers or []
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": scheme,
            "path": path,
            "raw_path": path.encode(),
            "query_string": query,
            "root_path": "",
            "server": ("testserver", 443 if scheme == "https" else 80),
            "client": LOOPBACK_PEER,
            "headers": headers,
        }
        await client.app(scope, receive, send)

    client.portal.call(drive)  # type: ignore[union-attr]
    return state["status"], state["read"], state["body"]


_URLENCODED = "application/x-www-form-urlencoded"


class TestUnauthenticatedFormLimits:
    @pytest.mark.parametrize("path", ["/login", "/first-run"])
    def test_8mib_streamed_form_is_413_and_cut_off(
        self, web_client: TestClient, path: str
    ) -> None:
        """No Content-Length: the stream is cut at the 64 KiB form cap."""
        seed_admin(web_client)
        total = 8 * _MiB
        status, read, body = _raw_post(
            web_client, path, total_bytes=total, content_type=_URLENCODED
        )
        assert status == 413
        assert b"size limit" in body
        # The handler never saw a buffered body: reading stopped one chunk past
        # the cap.
        assert read <= AUTH_FORM_MAX_BYTES + 64 * 1024

    def test_declared_length_over_cap_is_rejected_before_reading(
        self, web_client: TestClient
    ) -> None:
        seed_admin(web_client)
        status, read, _ = _raw_post(
            web_client,
            "/login",
            total_bytes=8 * _MiB,
            declared_length=8 * _MiB,
            content_type=_URLENCODED,
        )
        assert status == 413
        assert read == 0

    def test_pre_reject_beats_the_https_redirect(
        self, web_client_http: TestClient
    ) -> None:
        """Plain HTTP (acceptance check 29): 413, not a 308 echoing the body."""
        seed_admin(web_client_http)
        status, read, _ = _raw_post(
            web_client_http,
            "/login",
            total_bytes=2 * _MiB,
            declared_length=2 * _MiB,
            content_type=_URLENCODED,
            scheme="http",
        )
        assert status == 413
        assert read == 0

    def test_declared_length_over_cap_rejected_before_first_run_gate(
        self, web_client: TestClient
    ) -> None:
        """On a fresh install too: the cap sits outside the first-run gate."""
        status, read, _ = _raw_post(
            web_client,
            "/login",
            total_bytes=_MiB,
            declared_length=_MiB,
            content_type=_URLENCODED,
        )
        assert status == 413
        assert read == 0

    def test_malformed_content_length_still_counts_the_stream(
        self, web_client: TestClient
    ) -> None:
        """An unparseable Content-Length skips only the early reject."""
        seed_admin(web_client)
        status, read, _ = _raw_post(
            web_client,
            "/login",
            total_bytes=2 * _MiB,
            content_type=_URLENCODED,
            extra_headers=[(b"content-length", b"\xc2\xb2")],
        )
        assert status == 413
        assert read < 2 * _MiB

    def test_small_login_form_still_works(self, web_client: TestClient) -> None:
        seed_admin(web_client)
        resp = web_client.post(
            "/login",
            data={"username": "admin", "password": "AdminP@ssw0rd1234"},
            follow_redirects=False,
        )
        assert resp.status_code == 303


class TestRouteLimits:
    def test_upload_above_max_image_bytes_is_413(
        self, web_client: TestClient, web_settings: Settings
    ) -> None:
        get_context().settings.capture.max_image_bytes = 128 * 1024
        token = ensure_local_token(web_settings)
        status, read, body = _raw_post(
            web_client,
            "/api/v1/projects/1/frames/upload",
            total_bytes=4 * _MiB,
            content_type="application/octet-stream",
            extra_headers=[(b"authorization", f"Bearer {token}".encode())],
            query=b"capture_timestamp=2026-10-27T12:00:00",
        )
        assert status == 413
        assert b"size limit" in body
        assert read < _MiB

    def test_import_of_100_mib_is_still_accepted(
        self, web_client: TestClient, web_settings: Settings
    ) -> None:
        """The frame import keeps the full 512 MiB ceiling."""
        project_id = _seed_project()
        token = ensure_local_token(web_settings)
        total = 100 * _MiB
        status, read, _ = _raw_post(
            web_client,
            f"/api/v1/projects/{project_id}/frames/import",
            total_bytes=total,
            declared_length=total,
            content_type="multipart/form-data; boundary=nothing-here",
            extra_headers=[(b"authorization", f"Bearer {token}".encode())],
            chunk=_MiB,
            # One unrelated file part that never closes: the streaming reader
            # takes the whole body through (dropping the part, spooling nothing)
            # and then refuses the unterminated form.
            prefix=(
                b"--nothing-here\r\n"
                b'Content-Disposition: form-data; name="other"; filename="x"\r\n'
                b"\r\n"
            ),
        )
        # The whole body reached the handler: the 512 MiB ceiling is intact.
        assert read == total
        assert status == 400

    def test_large_json_body_gets_the_default_cap(self, web_client: TestClient) -> None:
        get_context().settings.server.max_default_body_bytes = 256 * 1024
        status, read, _ = _raw_post(
            web_client,
            "/api/v1/cameras",
            total_bytes=_MiB,
            declared_length=_MiB,
            content_type="application/json",
        )
        assert status == 413
        assert read == 0


def _limit(method: str, path: str, content_type: str, settings: Settings) -> int:
    return body_limit_for(method, path, content_type, settings)


class TestBodyLimitTable:
    @pytest.fixture()
    def table_settings(self) -> Settings:
        return Settings(
            server=ServerSettings(max_default_body_bytes=12_345),
            capture=CaptureSettings(max_image_bytes=1_000_000),
        )

    @pytest.mark.parametrize("path", ["/login", "/first-run", "/logout"])
    def test_auth_forms(self, table_settings: Settings, path: str) -> None:
        for content_type in (_URLENCODED, "multipart/form-data; boundary=x", ""):
            assert _limit("POST", path, content_type, table_settings) == 64 * 1024

    @pytest.mark.parametrize(
        "path", ["/projects/7/frames/import", "/api/v1/projects/7/frames/import"]
    )
    def test_frame_import(self, table_settings: Settings, path: str) -> None:
        assert _limit("POST", path, "multipart/form-data", table_settings) == (
            512 * _MiB
        )

    def test_frame_upload(self, table_settings: Settings) -> None:
        path = "/api/v1/projects/7/frames/upload"
        assert _limit("POST", path, "image/jpeg", table_settings) == (
            1_000_000 + 64 * 1024
        )

    def test_report_form_sits_above_the_route_cap(
        self, table_settings: Settings
    ) -> None:
        route_cap = report_body_limit(table_settings)
        limit = _limit("POST", "/report", "multipart/form-data", table_settings)
        assert route_cap < limit <= route_cap + 2 * _MiB

    def test_other_urlencoded_form(self, table_settings: Settings) -> None:
        assert _limit("POST", "/cameras", _URLENCODED, table_settings) == _MiB

    def test_everything_else_gets_the_default(self, table_settings: Settings) -> None:
        assert _limit(
            "POST", "/api/v1/cameras", "application/json", table_settings
        ) == (12_345)
        # Only the exact router paths earn the large limits.
        for path in (
            "/projects/7/frames/import/",
            "/projects/7/frames/import\n",
            "/x/projects/7/frames/import",
            "/api/v1/projects/7/frames/upload/extra",
        ):
            assert _limit("POST", path, "multipart/form-data", table_settings) == 12_345
        # The upload/import limits are for POST only.
        assert _limit("PUT", "/projects/7/frames/import", "", table_settings) == 12_345

    def test_content_type_classification_matches_the_form_parser(
        self, table_settings: Settings
    ) -> None:
        """Case-sensitive prefix, exactly as ``parsed_form`` decides to parse."""
        assert (
            _limit(
                "POST", "/cameras", "APPLICATION/X-WWW-FORM-URLENCODED", table_settings
            )
            == 12_345
        )
        assert (
            _limit("POST", "/cameras", _URLENCODED + "; charset=utf-8", table_settings)
            == _MiB
        )

    def test_hard_ceiling_caps_every_route(self) -> None:
        settings = Settings(server=ServerSettings(max_request_body_bytes=1000))
        for path in ("/login", "/projects/1/frames/import", "/report", "/cameras"):
            assert _limit("POST", path, _URLENCODED, settings) == 1000


_BODY_READ_MARKERS = (
    "request.body()",
    "request.stream()",
    "request.form()",
    "parse_multipart",
    "stream_multipart",
    "stream_body_to_file",
    "read_body_limited",
)


def _endpoint_source(endpoint: Any) -> str:
    """Return the endpoint's source plus that of same-module functions it calls.

    One level deep is enough for the routes that read the body in a helper
    (``POST /report`` reads it in ``_read_submission``).
    """
    try:
        source = inspect.getsource(endpoint)
    except (OSError, TypeError):
        return ""
    module = inspect.getmodule(endpoint)
    parts = [source]
    for name in set(re.findall(r"\b(_?[a-z][a-z0-9_]*)\(", source)):
        helper = getattr(module, name, None)
        if inspect.isfunction(helper) and helper.__module__ == endpoint.__module__:
            with contextlib.suppress(OSError, TypeError):
                parts.append(inspect.getsource(helper))
    return "\n".join(parts)


def test_every_body_reading_route_has_an_explicit_limit(
    web_client: TestClient,
) -> None:
    """A route that reads a raw or multipart body must not fall to the default.

    Scans each mounted route's endpoint source for a raw body read. Any such
    route must resolve to a limit from an explicit table entry; the default is
    set to a sentinel value here so falling through to it is detectable.
    """
    settings = Settings(server=ServerSettings(max_default_body_bytes=7_777))
    found: list[str] = []
    for route in web_client.app.routes:  # type: ignore[attr-defined]
        if not isinstance(route, APIRoute):
            continue
        source = _endpoint_source(route.endpoint)
        if not any(marker in source for marker in _BODY_READ_MARKERS):
            continue
        path = re.sub(r"\{[^}]+\}", "1", route.path)
        for method in sorted(route.methods - {"GET", "HEAD"}):
            found.append(f"{method} {path}")
            limit = body_limit_for(method, path, "multipart/form-data", settings)
            assert limit != 7_777, f"{method} {route.path} has no body-limit entry"
    # The scan itself must find the known raw-body routes, or it proves nothing.
    assert "POST /report" in found
    assert "POST /projects/1/frames/import" in found
    assert "POST /api/v1/projects/1/frames/import" in found
    assert "POST /api/v1/projects/1/frames/upload" in found


def test_form_getlist_after_bounded_parsed_form() -> None:
    """``_form_getlist`` still sees every value once ``parsed_form`` read the body.

    This is the HTMX header-token path, where the CSRF layer never pre-reads the
    form: the bounded read drains the stream, so ``request.body()`` must be
    served from Starlette's cache, not raise "Stream consumed".
    """
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request, form: FormDep) -> dict[str, Any]:
        return {
            "name": form.get("name"),
            "days": await _form_getlist(request, "capture_days"),
        }

    with TestClient(app) as client:
        resp = client.post(
            "/echo",
            content=b"name=x&capture_days=mon&capture_days=tue",
            headers={"Content-Type": _URLENCODED},
        )
    assert resp.status_code == 200
    assert resp.json() == {"name": "x", "days": ["mon", "tue"]}


@pytest.mark.parametrize("path", ["/frames/offset", "/frames/export"])
def test_wp70c_large_selection_fits_the_form_cap(
    admin_client: TestClient, path: str
) -> None:
    """A WP-70-C offset/export request with a huge explicit selection is not 413.

    ``net.js`` sends these urlencoded with ``X-CSRF-Token`` and
    ``Idempotency-Key``; they fall under the 1 MiB urlencoded-form cap. 20,000
    seven-digit ids (4x the largest deployed project) is about 200 KB encoded.
    """
    ids = ",".join(str(1_000_000 + n) for n in range(20_000))
    resp = admin_client.post(
        path,
        data={"frame_ids": ids, "seconds": "60"},
        headers={
            "X-CSRF-Token": csrf_of(admin_client, "/"),
            "X-Requested-With": "fetch",
            "Accept": "application/json",
            "Idempotency-Key": "wp67g-size-check",
        },
        follow_redirects=False,
    )
    assert len(f"frame_ids={ids}".replace(",", "%2C")) < URLENCODED_FORM_MAX_BYTES
    assert resp.status_code != 413
    assert resp.status_code < 500
