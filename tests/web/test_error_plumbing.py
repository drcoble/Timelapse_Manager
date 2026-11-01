"""Server half of visible failures (#70 F-R1).

* Deleting a camera that a project still references is a ``409`` with an
  actionable message (an inline error for HTMX, JSON for the API) instead of
  the foreign-key ``IntegrityError`` that used to surface as a bare ``500``.
* An HTMX request that fails CSRF gets ``HX-Trigger: tlm:csrf-failed``, so the
  page can explain the refusal instead of doing nothing.
* An unhandled exception gets a ``500`` that still carries the security
  headers and the request id: a styled page for a browser, an inline error plus
  a ``tlm:server-error`` trigger for HTMX, and JSON for a machine caller. The
  exception text never reaches any of them.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from fastapi.testclient import TestClient

from tests.conftest import LOOPBACK_PEER, csrf_of, seed_admin
from timelapse_manager.app import create_app
from timelapse_manager.config import Settings
from timelapse_manager.db.models import Camera, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.security.token import ensure_local_token

_BOOM_PATH = "/__test__/boom"
_SECRET_TEXT = "secret-internal-detail-do-not-leak"


def _seed_camera(name: str, *, projects: int = 0) -> int:
    """Insert a camera plus ``projects`` projects that use it; return its id."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(name=name, address="192.168.1.60", protocol="vapix")
        db.add(cam)
        db.flush()
        for i in range(projects):
            db.add(
                Project(
                    camera_id=cam.id,
                    name=f"{name}-project-{i}",
                    capture_interval_seconds=300,
                    lifecycle_state="paused",
                )
            )
        db.flush()
        return cam.id


def _camera_exists(camera_id: int) -> bool:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        return db.get(Camera, camera_id) is not None


def _htmx_headers(csrf: str) -> dict[str, str]:
    return {"HX-Request": "true", "X-CSRF-Token": csrf}


class TestCameraDeleteInUse:
    def test_htmx_delete_of_camera_in_use_is_409_inline_error(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera("busy-cam", projects=2)
        csrf = csrf_of(admin_client, "/cameras")

        resp = admin_client.delete(f"/cameras/{camera_id}", headers=_htmx_headers(csrf))

        assert resp.status_code == 409
        assert 'class="inline-error"' in resp.text
        assert "Camera is used by 2 project(s)" in resp.text
        assert _camera_exists(camera_id)

    def test_htmx_delete_of_unused_camera_still_succeeds(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera("idle-cam")
        csrf = csrf_of(admin_client, "/cameras")

        resp = admin_client.delete(f"/cameras/{camera_id}", headers=_htmx_headers(csrf))

        assert resp.status_code == 200
        assert not _camera_exists(camera_id)

    def test_api_delete_of_camera_in_use_is_json_409(
        self, cli_client: TestClient
    ) -> None:
        camera_id = _seed_camera("api-busy-cam", projects=1)
        token = ensure_local_token(get_context().settings)

        resp = cli_client.delete(
            f"/api/v1/cameras/{camera_id}",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 409
        body = resp.json()
        assert body["code"] == "conflict"
        assert body["detail"] == (
            "Camera is used by 1 project(s); move or delete them first."
        )
        assert _camera_exists(camera_id)


class TestCsrfFailureSignal:
    def test_htmx_csrf_failure_carries_hx_trigger(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera("csrf-cam")

        resp = admin_client.delete(
            f"/cameras/{camera_id}", headers=_htmx_headers("not-the-token")
        )

        assert resp.status_code == 403
        assert resp.text == "CSRF validation failed."
        assert "tlm:csrf-failed" in json.loads(resp.headers["HX-Trigger"])
        assert _camera_exists(camera_id)

    def test_plain_csrf_failure_has_no_trigger(self, admin_client: TestClient) -> None:
        camera_id = _seed_camera("csrf-plain-cam")

        resp = admin_client.delete(
            f"/cameras/{camera_id}", headers={"X-CSRF-Token": "not-the-token"}
        )

        assert resp.status_code == 403
        assert "HX-Trigger" not in resp.headers


@pytest.fixture()
def boom_client(
    web_settings: Settings, alembic_cfg: Config, tmp_path: Path
) -> Generator[TestClient, None, None]:
    """A client on an app with one extra route that raises an unhandled error.

    ``raise_server_exceptions=False`` lets the test see the response Starlette's
    ``ServerErrorMiddleware`` sends instead of the re-raised exception.
    """
    alembic_cfg.set_main_option("sqlalchemy.url", web_settings.database.url)
    alembic_command.upgrade(alembic_cfg, "head")
    app = create_app(web_settings)

    def boom() -> None:
        raise RuntimeError(_SECRET_TEXT)

    app.add_api_route(_BOOM_PATH, boom, methods=["GET"])
    with TestClient(
        app,
        base_url="https://testserver",
        client=LOOPBACK_PEER,
        raise_server_exceptions=False,
    ) as client:
        seed_admin(client)
        yield client


class TestUnhandledError:
    def test_browser_gets_styled_page_with_security_headers(
        self, boom_client: TestClient
    ) -> None:
        resp = boom_client.get(_BOOM_PATH, headers={"Accept": "text/html"})

        assert resp.status_code == 500
        assert resp.headers["content-type"].startswith("text/html")
        request_id = resp.headers["X-Request-ID"]
        assert request_id
        assert "Content-Security-Policy" in resp.headers
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        # HSTS: the test client's base URL is https.
        assert "Strict-Transport-Security" in resp.headers
        assert resp.headers["Cache-Control"] == "no-store"
        # The styled page: the shared stylesheet and the request id to quote.
        assert "/static/css/app" in resp.text
        assert f"Server error (request id {request_id})" in resp.text
        assert _SECRET_TEXT not in resp.text

    def test_htmx_gets_inline_error_and_trigger(self, boom_client: TestClient) -> None:
        resp = boom_client.get(_BOOM_PATH, headers={"HX-Request": "true"})

        assert resp.status_code == 500
        request_id = resp.headers["X-Request-ID"]
        assert resp.text == (
            '<div class="inline-error" role="alert">'
            f"Server error (request id {request_id})</div>"
        )
        trigger = json.loads(resp.headers["HX-Trigger"])
        assert trigger == {"tlm:server-error": {"requestId": request_id}}
        assert "Content-Security-Policy" in resp.headers

    def test_machine_caller_gets_json(self, boom_client: TestClient) -> None:
        resp = boom_client.get(_BOOM_PATH, headers={"Accept": "application/json"})

        assert resp.status_code == 500
        request_id = resp.headers["X-Request-ID"]
        assert resp.json() == {
            "detail": "Internal Server Error",
            "request_id": request_id,
        }
        assert "Content-Security-Policy" in resp.headers
        assert _SECRET_TEXT not in resp.text

    def test_supplied_request_id_is_echoed(self, boom_client: TestClient) -> None:
        resp = boom_client.get(
            _BOOM_PATH,
            headers={"Accept": "application/json", "X-Request-ID": "trace-abc.123"},
        )

        assert resp.headers["X-Request-ID"] == "trace-abc.123"
        assert resp.json()["request_id"] == "trace-abc.123"

    def test_hostile_request_id_is_replaced_not_reflected(
        self, boom_client: TestClient
    ) -> None:
        hostile = "<script>alert(1)</script>"
        resp = boom_client.get(
            _BOOM_PATH, headers={"Accept": "text/html", "X-Request-ID": hostile}
        )

        assert resp.status_code == 500
        assert hostile not in resp.text
        assert resp.headers["X-Request-ID"] != hostile


class TestLongRequestTimeouts:
    """Requests that legitimately outlive htmx's 30 s default carry an override."""

    def test_cameras_page_scan_waits_for_the_proxy_limit(
        self, admin_client: TestClient
    ) -> None:
        html = admin_client.get("/cameras").text
        assert 'hx-post="/cameras/discover"\n' in html
        assert "hx-request='{\"timeout\":300000}'" in html

    def test_camera_edit_form_query_and_poll_overrides(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera("timeout-cam")
        html = admin_client.get(f"/cameras/{camera_id}/edit-form").text
        query = html.index('hx-post="/cameras/query"')
        assert "hx-request='{\"timeout\":300000}'" in html[query : query + 120]
        poll = html.index(f'hx-get="/cameras/{camera_id}/geolocation"')
        assert "hx-request='{\"timeout\":45000}'" in html[poll : poll + 120]

    def test_ldap_test_connection_override(self, admin_client: TestClient) -> None:
        html = admin_client.get("/settings").text
        test_conn = html.index('hx-post="/settings/ldap/test-connection"')
        assert "hx-request='{\"timeout\":300000}'" in html[test_conn : test_conn + 120]

    def test_uploads_have_no_client_timeout(self, admin_client: TestClient) -> None:
        camera_id = _seed_camera("upload-cam", projects=1)
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            project_id = db.query(Project.id).filter_by(camera_id=camera_id).scalar()
        frames = admin_client.get(f"/frames?project_id={project_id}").text
        assert 'data-htmx-timeout="none"' in frames
        report = admin_client.get("/report?page=%2F").text
        assert 'data-htmx-timeout="none"' in report
