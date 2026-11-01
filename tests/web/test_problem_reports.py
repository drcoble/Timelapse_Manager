"""Web tests for "Report a problem" (#8).

Covers the link on every page for every role, the prefilled form, filing a
report (row, zip, sections, audit event), permissions on the admin list,
rate limiting, upload and text validation, CSRF on the multipart form, the
no-secret guarantee over every zip member, and graceful degradation when the
#7 task dump fails.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Generator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.conftest import csrf_of, login, seed_admin
from tests.presession_support import session_cookie
from tests.reports_support import (
    CONFIG_CAMERA_PASSWORD,
    DB_SECRETS,
    GITEA_TOKEN,
    PNG_BYTES,
    assert_no_secrets,
    file_secret,
    log_secrets,
    seed_database_secrets,
    zip_members,
)
from timelapse_manager.config.settings import (
    ReportingGiteaSettings,
    ReportingGithubSettings,
    ReportingSettings,
    Settings,
)
from timelapse_manager.db.models import Event, Report
from timelapse_manager.db.session import session_scope
from timelapse_manager.reports import delivery
from timelapse_manager.reports import snapshot as snapshot_module
from timelapse_manager.runtime import get_context
from timelapse_manager.web.routers import reports as reports_router

_NAV = {"Accept": "text/html,application/xhtml+xml"}
_KEY = re.compile(r"TLM-\d{8}-\d{4}")


@pytest.fixture()
def web_settings(web_settings: Settings, tmp_path: Path) -> Settings:
    """The shared web settings plus a Gitea token file and a config camera.

    Gitea is left unconfigured (no base_url) so no delivery is attempted, but
    the token file exists so the redaction pass must still keep it out. The
    GitHub hand-off (#21) is off: these are #8's plain, un-anonymized reports.
    """
    token_file = tmp_path / "report-gitea-token"
    token_file.write_text(GITEA_TOKEN + "\n", encoding="utf-8")
    return web_settings.model_copy(
        update={
            "reporting": ReportingSettings(
                gitea=ReportingGiteaSettings(token_file=token_file),
                github=ReportingGithubSettings(enabled=False),
            ),
            "cameras": [
                {
                    "name": "cfg",
                    "password": CONFIG_CAMERA_PASSWORD,
                    "url": f"rtsp://u:{CONFIG_CAMERA_PASSWORD}@10.0.0.9/",
                }
            ],
        }
    )


def _seed() -> int:
    with session_scope(get_context().session_factory) as session:
        return seed_database_secrets(session)


def _submit(
    client: TestClient,
    *,
    htmx: bool = True,
    attachment: tuple[str, bytes, str] | None = None,
    csrf: bool = True,
    **fields: Any,
) -> Any:
    data = {"title": "Capture stopped", "description": "It stopped at noon."}
    data.update({key: str(value) for key, value in fields.items()})
    headers: dict[str, str] = {}
    if htmx:
        headers["HX-Request"] = "true"
    token = csrf_of(client, "/about") if csrf else None
    if token and htmx:
        headers["X-CSRF-Token"] = token
    elif token:
        data["csrf_token"] = token
    files = {"attachment": attachment} if attachment else {"attachment": ("", b"")}
    return client.post(
        "/report", data=data, files=files, headers=headers, follow_redirects=False
    )


def _reports() -> list[Report]:
    with session_scope(get_context().session_factory) as session:
        rows = session.execute(select(Report).order_by(Report.id)).scalars().all()
        session.expunge_all()
        return list(rows)


def _zip_bytes(report: Report) -> bytes:
    path = (
        get_context().settings.paths.data_dir / "reports" / f"{report.report_key}.zip"
    )
    return path.read_bytes()


# ---------------------------------------------------------------------------
# Entry points and prefill
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture", ["viewer_client", "operator_client", "admin_client"]
)
@pytest.mark.parametrize("path", ["/", "/projects", "/events", "/about"])
def test_link_on_every_page_for_every_role(
    request: pytest.FixtureRequest, fixture: str, path: str
) -> None:
    client: TestClient = request.getfixturevalue(fixture)
    body = client.get(path).text
    assert 'hx-get="/drawers/report?page=' in body
    assert "Report a problem" in body


def test_link_hidden_when_disabled(admin_client: TestClient) -> None:
    get_context().settings.reporting.enabled = False
    try:
        assert "/drawers/report" not in admin_client.get("/").text
        assert admin_client.get("/report").status_code == 404
    finally:
        get_context().settings.reporting.enabled = True


def test_form_prefills_project_and_camera(viewer_client: TestClient) -> None:
    project_id = _seed()
    resp = viewer_client.get(
        f"/drawers/report?page=/projects/{project_id}", headers={"HX-Request": "true"}
    )
    assert resp.status_code == 200
    body = resp.text
    assert 'data-drawer-title="Report a problem"' in body
    assert f'name="project_id" value="{project_id}"' in body
    assert 'name="camera_id" value="1"' in body
    assert '<option value="project" selected>' in body
    assert "What the snapshot contains" in body


def test_form_ignores_absolute_from_url(viewer_client: TestClient) -> None:
    for bad in ("https://evil.example/x", "//evil.example/x"):
        body = viewer_client.get(f"/report?page={bad}").text
        assert 'name="page_url" value=""' in body


def test_full_page_fallback_without_htmx(viewer_client: TestClient) -> None:
    resp = viewer_client.get("/drawers/report?page=/renders")
    assert "<h1>Report a problem</h1>" in resp.text
    assert '<option value="renders" selected>' in resp.text


# ---------------------------------------------------------------------------
# Filing
# ---------------------------------------------------------------------------


def test_operator_files_report_with_all_sections(
    operator_client: TestClient,
) -> None:
    project_id = _seed()
    resp = _submit(
        operator_client, project_id=project_id, area="project", include_snapshot="on"
    )
    assert resp.status_code == 200, resp.text
    match = _KEY.search(resp.text)
    assert match is not None
    [report] = _reports()
    assert report.report_key == match.group(0)
    assert report.username == "operator"
    assert report.project_id == project_id
    assert report.camera_id is not None
    assert report.delivery_state == "ok"
    assert report.deliveries["local"]["status"] == "ok"

    members = zip_members(_zip_bytes(report))
    assert set(members) >= {
        "manifest.json",
        "report.json",
        "request.json",
        "app.json",
        "config.json",
        "supervisor.json",
        "diagnostics.json",
        "project.json",
        "camera.json",
        "events.json",
        "log.txt",
    }
    meta = json.loads(members["report.json"])
    assert meta["report_id"] == report.report_key
    assert meta["reporter"] == {"username": "operator", "role": "operator"}
    project = json.loads(members["project.json"])
    assert project["project"]["name"] == "Roof"
    assert project["project"]["exact_time_anchors"][0]["id"] == "noon"
    assert len(project["latest_frames"]) == 3
    assert project["exact_time_fires_last_7_days"][0]["status"] == "failed"
    assert len(project["render_jobs"]) == 1
    assert project["events"]
    app = json.loads(members["app.json"])
    assert app["alembic_revision"] == _alembic_head()
    assert app["host"]["cpu_count"]
    diagnostics = json.loads(members["diagnostics.json"])
    assert diagnostics["available"] is True
    assert diagnostics["tasks"]
    assert diagnostics["threads"]
    supervisor = json.loads(members["supervisor.json"])
    assert supervisor["available"] is True
    assert supervisor["active_project_count"] == 1
    request = json.loads(members["request.json"])
    assert request["username"] == "operator"
    assert request["recent_requests"], "the reporter's request ids are listed"
    assert "Cookie" not in members["request.json"].decode()


def test_filing_writes_audit_event(viewer_client: TestClient) -> None:
    _submit(viewer_client)
    [report] = _reports()
    with session_scope(get_context().session_factory) as session:
        messages = [event.message for event in session.execute(select(Event)).scalars()]
    assert f"user 'viewer' filed problem report {report.report_key}" in messages


def test_without_snapshot_only_the_reporters_words(viewer_client: TestClient) -> None:
    resp = _submit(viewer_client)  # include_snapshot unchecked (absent)
    assert resp.status_code == 200
    [report] = _reports()
    members = zip_members(_zip_bytes(report))
    assert set(members) == {"manifest.json", "report.json", "request.json"}


def test_no_js_submit_renders_full_page(viewer_client: TestClient) -> None:
    resp = _submit(viewer_client, htmx=False, include_snapshot="on")
    assert resp.status_code == 200
    assert "<h1>Report filed</h1>" in resp.text


def test_report_keys_are_sequential(viewer_client: TestClient) -> None:
    _submit(viewer_client)
    _submit(viewer_client)
    keys = [report.report_key for report in _reports()]
    assert [key[-4:] for key in keys] == ["0001", "0002"]


def test_reporter_sees_own_keys_on_form(viewer_client: TestClient) -> None:
    _submit(viewer_client)
    [report] = _reports()
    assert report.report_key in viewer_client.get("/report").text


def test_missing_task_dump_degrades_gracefully(
    operator_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("no dump")

    monkeypatch.setattr(snapshot_module, "build_task_dump", broken)
    resp = _submit(operator_client, include_snapshot="on")
    assert resp.status_code == 200
    [report] = _reports()
    members = zip_members(_zip_bytes(report))
    diagnostics = json.loads(members["diagnostics.json"])
    assert diagnostics["available"] is False
    assert "app.json" in members


# ---------------------------------------------------------------------------
# Redaction (web path)
# ---------------------------------------------------------------------------


def test_no_secret_appears_in_any_member(operator_client: TestClient) -> None:
    project_id = _seed()
    log_secrets()
    settings = get_context().settings
    cookie = session_cookie(operator_client)
    secrets = [
        *DB_SECRETS,
        CONFIG_CAMERA_PASSWORD,
        GITEA_TOKEN,
        cookie,
        file_secret(settings.paths.token_file),
        file_secret(settings.paths.data_dir / ".secret-key"),
    ]
    assert all(secrets), "every secret was seeded"
    resp = _submit(
        operator_client,
        project_id=project_id,
        include_snapshot="on",
        attachment=("shot.png", PNG_BYTES, "image/png"),
    )
    assert resp.status_code == 200, resp.text
    [report] = _reports()
    members = assert_no_secrets(_zip_bytes(report), [s for s in secrets if s])
    # The value scrub really ran (the log carried the bind password).
    assert "«redacted»" in members["log.txt"].decode()
    # The user's own image is the only image, byte for byte.
    assert members["attachment.png"] == PNG_BYTES
    assert not any(name.endswith((".jpg", ".jpeg")) for name in members)


# ---------------------------------------------------------------------------
# Validation and limits
# ---------------------------------------------------------------------------


def test_non_image_upload_rejected(viewer_client: TestClient) -> None:
    resp = _submit(
        viewer_client, htmx=False, attachment=("x.png", b"#!/bin/sh\necho", "image/png")
    )
    assert resp.status_code == 422
    assert "Only PNG, JPEG, WebP or GIF" in resp.text
    assert _reports() == []


def test_mismatched_type_rejected(viewer_client: TestClient) -> None:
    resp = _submit(
        viewer_client, htmx=False, attachment=("x.jpg", PNG_BYTES, "image/jpeg")
    )
    assert resp.status_code == 422


def test_oversize_upload_rejected(viewer_client: TestClient) -> None:
    get_context().settings.reporting.max_upload_bytes = 1024
    big = PNG_BYTES + b"\x00" * 4096
    resp = _submit(viewer_client, htmx=False, attachment=("big.png", big, "image/png"))
    assert resp.status_code == 413
    assert _reports() == []


def test_oversize_text_rejected(viewer_client: TestClient) -> None:
    resp = _submit(viewer_client, htmx=False, description="x" * 20_001)
    assert resp.status_code == 413
    assert _reports() == []


def test_htmx_error_is_inline_at_200(viewer_client: TestClient) -> None:
    resp = _submit(viewer_client, title="")
    assert resp.status_code == 200
    assert "Give the report a title." in resp.text
    assert 'id="report-form-container"' in resp.text


def test_time_outside_last_24h_rejected(viewer_client: TestClient) -> None:
    resp = _submit(viewer_client, htmx=False, when="2020-01-01T00:00")
    assert resp.status_code == 422
    assert "last 24 hours" in resp.text


def test_rate_limit_five_per_hour(viewer_client: TestClient) -> None:
    for _ in range(5):
        assert _submit(viewer_client).status_code == 200
    resp = _submit(viewer_client, htmx=False)
    assert resp.status_code == 429
    htmx = _submit(viewer_client)
    assert htmx.status_code == 200
    assert "5 reports an hour" in htmx.text
    assert len(_reports()) == 5


def test_multipart_without_csrf_is_403(viewer_client: TestClient) -> None:
    resp = _submit(viewer_client, htmx=False, csrf=False)
    assert resp.status_code == 403
    assert _reports() == []


def test_multipart_with_csrf_part_is_accepted(viewer_client: TestClient) -> None:
    resp = _submit(viewer_client, htmx=False)
    assert resp.status_code == 200
    assert len(_reports()) == 1


# ---------------------------------------------------------------------------
# Permissions and admin actions
# ---------------------------------------------------------------------------


def test_anonymous_is_redirected(anon_client: TestClient) -> None:
    resp = anon_client.get("/report", headers=_NAV, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")
    assert anon_client.post("/report", data={"title": "x"}).status_code == 401


def test_viewer_cannot_list_download_or_delete(viewer_client: TestClient) -> None:
    _submit(viewer_client)
    [report] = _reports()
    key = report.report_key
    assert viewer_client.get("/settings/reports").status_code == 403
    assert viewer_client.get("/settings/reports/table").status_code == 403
    assert viewer_client.get(f"/settings/reports/{key}/download").status_code == 403
    csrf = csrf_of(viewer_client, "/about")
    resp = viewer_client.post(
        f"/settings/reports/{key}/delete", headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 403
    assert len(_reports()) == 1


@pytest.fixture()
def filed_by_viewer(viewer_client: TestClient) -> Generator[TestClient, None, None]:
    """A viewer files a report, then the admin logs in on the same client."""
    _submit(viewer_client, include_snapshot="on")
    csrf = csrf_of(viewer_client, "/")
    viewer_client.post("/logout", data={"csrf_token": csrf}, follow_redirects=False)
    login(viewer_client)
    yield viewer_client


def test_admin_lists_downloads_and_deletes(filed_by_viewer: TestClient) -> None:
    client = filed_by_viewer
    [report] = _reports()
    key = report.report_key
    page = client.get("/settings/reports")
    assert page.status_code == 200
    assert key in page.text
    assert "viewer" in page.text
    assert key in client.get("/settings/reports/table").text
    assert 'data-tab-target="#tab-reports"' in client.get("/settings").text

    download = client.get(f"/settings/reports/{key}/download")
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"
    assert "report.json" in zip_members(download.content)

    confirm = client.get(f"/settings/reports/{key}/delete-confirm")
    assert f"/settings/reports/{key}/delete" in confirm.text
    csrf = csrf_of(client, "/settings/reports")
    resp = client.post(
        f"/settings/reports/{key}/delete",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert _reports() == []
    zip_path = get_context().settings.paths.data_dir / "reports" / f"{key}.zip"
    assert not zip_path.exists()
    with session_scope(get_context().session_factory) as session:
        messages = [event.message for event in session.execute(select(Event)).scalars()]
    assert f"problem report {key} deleted" in messages


def test_deleted_newest_key_is_never_issued_again(
    filed_by_viewer: TestClient,
) -> None:
    """Delete the day's newest report, file another: it gets a higher key."""
    client = filed_by_viewer
    [report] = _reports()
    deleted_key = report.report_key
    csrf = csrf_of(client, "/settings/reports")
    client.post(
        f"/settings/reports/{deleted_key}/delete",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert _reports() == []

    _submit(client)

    [new_report] = _reports()
    assert new_report.report_key > deleted_key
    assert new_report.id > report.id


def test_download_rejects_bad_keys(admin_client: TestClient) -> None:
    for key in ("..%2F..%2Fetc", "TLM-1-1", "TLM-20261023-0001"):
        assert admin_client.get(f"/settings/reports/{key}/download").status_code == 404


def test_resend_unconfigured_destination_is_422(filed_by_viewer: TestClient) -> None:
    [report] = _reports()
    csrf = csrf_of(filed_by_viewer, "/settings/reports")
    resp = filed_by_viewer.post(
        f"/settings/reports/{report.report_key}/resend",
        data={"csrf_token": csrf, "destination": "gitea"},
        follow_redirects=False,
    )
    assert resp.status_code == 422


def test_admin_list_needs_no_seed_admin_twice(web_client: TestClient) -> None:
    """Sanity: the admin list renders empty before any report exists."""
    seed_admin(web_client)
    login(web_client)
    assert (
        "No problem reports have been filed."
        in web_client.get("/settings/reports").text
    )


# ---------------------------------------------------------------------------
# Background delivery, resend and create-issue (Gitea mocked)
# ---------------------------------------------------------------------------


def _mock_gitea(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, asset_status: list[int]
) -> list[httpx.Request]:
    """Point the running app at a mocked Gitea; return the requests it sees."""
    token_file = tmp_path / "gitea-web-token"
    token_file.write_text("web-token-123456", encoding="utf-8")
    get_context().settings.reporting.gitea = ReportingGiteaSettings(
        base_url="https://git.example.test",
        repo="alice.smith/timelapse-manager",
        token_file=token_file,
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        if request.url.path.endswith("/issues"):
            return httpx.Response(
                201, json={"number": 7, "html_url": "javascript:alert(document.domain)"}
            )
        if request.url.path.endswith("/issues/7/assets"):
            return httpx.Response(asset_status.pop(0), json={})
        return httpx.Response(404)

    def client(timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={"User-Agent": delivery.USER_AGENT},
        )

    monkeypatch.setattr(delivery, "_http_client", client)
    return seen


def test_submit_delivers_to_gitea_in_background(
    viewer_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_gitea(monkeypatch, tmp_path, asset_status=[201])
    resp = _submit(viewer_client)
    assert resp.status_code == 200
    assert "Issue tracker" in resp.text
    [report] = _reports()
    assert report.gitea_issue_number == 7
    assert report.deliveries["gitea"]["status"] == "ok"
    assert report.delivery_state == "ok"


def test_admin_resend_after_failed_delivery(
    filed_by_viewer: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = filed_by_viewer
    [report] = _reports()
    key = report.report_key
    seen = _mock_gitea(monkeypatch, tmp_path, asset_status=[500, 201])

    csrf = csrf_of(client, "/settings/reports")
    resp = client.post(
        f"/settings/reports/{key}/create-issue",
        headers={"X-CSRF-Token": csrf, "HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert 'id="report-admin"' in resp.text
    [failed] = _reports()
    assert failed.delivery_state == "failed"
    assert failed.gitea_issue_number == 7
    page = client.get("/settings/reports").text
    assert "Gitea attachment upload returned HTTP 500" in page
    # The rendered link is built from config, not the response's html_url.
    assert "javascript:" not in page
    assert (
        'href="https://git.example.test/alice.smith/timelapse-manager/issues/7"' in page
    )

    resp = client.post(
        f"/settings/reports/{key}/resend",
        data={"csrf_token": csrf, "destination": "gitea"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    [done] = _reports()
    assert done.delivery_state == "ok"
    issue_posts = [r for r in seen if r.url.path.endswith("/issues")]
    assert len(issue_posts) == 1
    with session_scope(get_context().session_factory) as session:
        messages = [event.message for event in session.execute(select(Event)).scalars()]
    assert f"problem report {key} resent to gitea" in messages
    assert f"Gitea issue requested for problem report {key}" in messages


def test_resend_to_gitea_files_the_issue_when_none_exists(
    filed_by_viewer: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resend to Gitea for a report with no issue yet creates and records it.

    Covers the resend route's issue-create path (the create-issue route's twin)
    so the loop-DB guard gates it too (#48).
    """
    client = filed_by_viewer
    [report] = _reports()
    key = report.report_key
    assert report.gitea_issue_number is None
    _mock_gitea(monkeypatch, tmp_path, asset_status=[201])

    csrf = csrf_of(client, "/settings/reports")
    resp = client.post(
        f"/settings/reports/{key}/resend",
        data={"csrf_token": csrf, "destination": "gitea"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    [done] = _reports()
    assert done.gitea_issue_number == 7
    assert done.deliveries["gitea"]["status"] == "ok"


# ---------------------------------------------------------------------------
# The page the form came from is only ever a site-relative path
# ---------------------------------------------------------------------------

_HOSTILE_FROM = [
    "/\t/evil.com",
    "/\n/evil.com",
    "/\r/evil.com",
    "/ /evil.com",
    "//evil.com",
    "/\\evil.com",
    "\\\\evil.com",
    "https://evil.com",
    "javascript:alert(1)",
    "evil.com/x",
    "/\x7f/evil.com",
]


@pytest.mark.parametrize("raw", _HOSTILE_FROM)
def test_safe_from_rejects_off_site_paths(raw: str) -> None:
    assert reports_router._safe_from(raw) is None


def test_safe_from_keeps_harmless_paths() -> None:
    assert reports_router._safe_from("/projects/3?tab=frames") == (
        "/projects/3?tab=frames"
    )
    # A percent-encoded control character is refused too (#67 S21): the path is
    # checked again after one round of decoding, so nothing downstream that
    # decodes it can turn it into "//".
    assert reports_router._safe_from("/%09/evil.com") is None


@pytest.mark.parametrize("raw", _HOSTILE_FROM)
def test_hostile_from_never_reaches_cancel_href(
    viewer_client: TestClient, raw: str
) -> None:
    body = viewer_client.get("/report", params={"page": raw}).text
    assert 'name="page_url" value=""' in body
    assert '<a href="/" class="btn btn-ghost">Cancel</a>' in body
    resp = _submit(viewer_client, htmx=False, page_url=raw)
    assert resp.status_code == 200
    assert 'href="/" class="btn btn-secondary">Back</a>' in resp.text


def test_delivery_runs_after_the_response_is_sent(
    viewer_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow destination cannot hold the submit open (raw ASGI, no TestClient).

    TestClient waits for background tasks either way, so this drives the app
    directly and records when the final response body is sent versus when the
    (deliberately slow) delivery finishes.
    """
    token_file = tmp_path / "t"
    token_file.write_text("token-123456")
    get_context().settings.reporting.gitea = ReportingGiteaSettings(
        base_url="https://git.example.test", repo="a/b", token_file=token_file
    )
    order: list[str] = []

    async def slow_delivery(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.sleep(0.5)
        order.append("delivered")

    monkeypatch.setattr(reports_router, "deliver_report", slow_delivery)
    csrf = csrf_of(viewer_client, "/about")
    cookie = "; ".join(f"{k}={v}" for k, v in viewer_client.cookies.items())
    body = b"title=slow&description=d"

    async def drive() -> None:
        delivered = False

        async def receive() -> dict[str, Any]:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.sleep(5)
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.body" and not message.get("more_body"):
                order.append("response complete")

        headers = [
            (b"host", b"testserver"),
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"content-length", str(len(body)).encode()),
            (b"x-csrf-token", csrf.encode()),
            (b"hx-request", b"true"),
            (b"cookie", cookie.encode()),
        ]
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/report",
            "raw_path": b"/report",
            "query_string": b"",
            "root_path": "",
            "server": ("testserver", 443),
            "client": ("192.0.2.10", 50000),
            "headers": headers,
        }
        await viewer_client.app(scope, receive, send)

    viewer_client.portal.call(drive)  # type: ignore[union-attr]
    assert order == ["response complete", "delivered"]


# ---------------------------------------------------------------------------
# The upload cap holds on the bytes actually received
# ---------------------------------------------------------------------------


def _raw_multipart_post(
    client: TestClient,
    *,
    total_bytes: int,
    declared_length: int | None,
    csrf_in_header: bool,
) -> tuple[int, int]:
    """POST a large multipart body in 64 KiB chunks; return (status, bytes read).

    ``declared_length`` ``None`` sends no Content-Length (a chunked upload);
    otherwise the header states that (understated) size.
    """
    csrf = csrf_of(client, "/about")
    cookie = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
    boundary = "tlmboundary"
    head = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="csrf_token"\r\n\r\n'
        f"{csrf}\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="title"\r\n\r\nbig\r\n'
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="attachment"; filename="a.png"\r\n'
        "Content-Type: image/png\r\n\r\n"
    ).encode()
    payload = head + PNG_BYTES + b"\x00" * total_bytes
    chunk = 64 * 1024
    pieces = [payload[i : i + chunk] for i in range(0, len(payload), chunk)]
    state = {"read": 0, "status": 0}

    async def drive() -> None:
        index = 0

        async def receive() -> dict[str, Any]:
            nonlocal index
            if index < len(pieces):
                piece = pieces[index]
                index += 1
                state["read"] += len(piece)
                return {
                    "type": "http.request",
                    "body": piece,
                    "more_body": index < len(pieces),
                }
            await asyncio.sleep(5)
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                state["status"] = message["status"]

        headers = [
            (b"host", b"testserver"),
            (
                b"content-type",
                f"multipart/form-data; boundary={boundary}".encode(),
            ),
            (b"cookie", cookie.encode()),
        ]
        if declared_length is not None:
            headers.append((b"content-length", str(declared_length).encode()))
        if csrf_in_header:
            headers += [(b"x-csrf-token", csrf.encode()), (b"hx-request", b"true")]
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/report",
            "raw_path": b"/report",
            "query_string": b"",
            "root_path": "",
            "server": ("testserver", 443),
            "client": ("192.0.2.10", 50000),
            "headers": headers,
        }
        await client.app(scope, receive, send)

    client.portal.call(drive)  # type: ignore[union-attr]
    return state["status"], state["read"]


@pytest.mark.parametrize("csrf_in_header", [True, False], ids=["htmx", "no-js"])
@pytest.mark.parametrize("declared", [None, 1000], ids=["chunked", "understated"])
def test_upload_cap_enforced_on_streamed_bytes(
    viewer_client: TestClient, csrf_in_header: bool, declared: int | None
) -> None:
    get_context().settings.reporting.max_upload_bytes = 1024 * 1024
    total = 8 * 1024 * 1024
    status, read = _raw_multipart_post(
        viewer_client,
        total_bytes=total,
        declared_length=declared,
        csrf_in_header=csrf_in_header,
    )
    # HTMX gets the inline error at 200 (repo convention); no-JS a real 413.
    assert status in ((200,) if csrf_in_header else (413,))
    assert read < total // 2, "the body was cut off, not buffered to the end"
    assert _reports() == []


def test_filing_never_prunes_other_reports(viewer_client: TestClient) -> None:
    """A viewer filing reports cannot evict anyone's report via the count cap."""
    get_context().settings.reporting.retention_max_reports = 1
    _submit(viewer_client)
    _submit(viewer_client)
    _submit(viewer_client)
    assert len(_reports()) == 3


def _alembic_head() -> str:
    """The newest migration revision (what a fully migrated database reports)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    repo_root = Path(__file__).resolve().parents[2]
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option("script_location", str(repo_root / "alembic"))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return head


# ---------------------------------------------------------------------------
# Viewers never attach the system snapshot (#67 S12)
# ---------------------------------------------------------------------------

_SNAPSHOT_ONLY = {"log.txt", "events.json", "diagnostics.json", "app.json"}


def test_viewer_report_never_carries_the_snapshot(
    viewer_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ticking the box (or forging the field) still files only the words."""

    def no_dumps() -> Any:
        raise AssertionError("live diagnostics taken for a viewer")

    monkeypatch.setattr(reports_router, "_live_diagnostics", no_dumps)
    project_id = _seed()
    resp = _submit(
        viewer_client, project_id=project_id, area="project", include_snapshot="on"
    )
    assert resp.status_code == 200, resp.text
    [report] = _reports()
    members = zip_members(_zip_bytes(report))
    assert set(members) == {"manifest.json", "report.json", "request.json"}
    assert not _SNAPSHOT_ONLY & set(members)
    meta = json.loads(members["report.json"])
    assert meta["snapshot_included"] is False


def test_operator_report_keeps_the_snapshot(operator_client: TestClient) -> None:
    resp = _submit(operator_client, include_snapshot="on")
    assert resp.status_code == 200, resp.text
    [report] = _reports()
    members = set(zip_members(_zip_bytes(report)))
    assert members >= _SNAPSHOT_ONLY


@pytest.mark.parametrize(
    ("fixture", "checked"),
    [("viewer_client", False), ("operator_client", True), ("admin_client", True)],
)
def test_snapshot_box_default_follows_role(
    request: pytest.FixtureRequest, fixture: str, checked: bool
) -> None:
    client: TestClient = request.getfixturevalue(fixture)
    text = client.get("/report").text
    box = re.search(r'<input type="checkbox" name="include_snapshot"[^>]*>', text)
    assert box is not None
    assert ("checked" in box.group(0)) is checked


def test_viewer_form_says_diagnostics_come_from_operators(
    viewer_client: TestClient,
) -> None:
    note = "Diagnostics are attached only to reports filed by operators"
    viewer_text = viewer_client.get("/report").text
    assert note in viewer_text
    assert "The last 200 events across the system." not in viewer_text


def test_operator_form_lists_the_snapshot_contents(operator_client: TestClient) -> None:
    text = operator_client.get("/report").text
    assert "The last 200 events across the system." in text
    assert "Diagnostics are attached only to reports filed by operators" not in text


def test_submit_reads_only_a_plain_copy_of_the_user(
    operator_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The viewer's columns are copied in the pool; expiring the row afterwards
    cannot make the loop refresh it (#66 L3). The loop guard fails any query on
    the loop, so a regression shows as a violation, not a pass."""
    from sqlalchemy.orm import object_session

    real_of = reports_router.UserView.of

    def of_then_expire(user: Any) -> Any:
        view = real_of(user)
        session = object_session(user)
        assert session is not None
        session.expire_all()
        return view

    monkeypatch.setattr(reports_router.UserView, "of", staticmethod(of_then_expire))
    project_id = _seed()
    resp = _submit(
        operator_client,
        project_id=project_id,
        area="project",
        include_snapshot="on",
        when="",
    )
    assert resp.status_code == 200, resp.text
    [report] = _reports()
    assert report.username == "operator"
