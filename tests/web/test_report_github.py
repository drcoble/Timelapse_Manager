"""Web tests for anonymized problem reports and the GitHub hand-off (#21).

With ``reporting.github.enabled`` (the default) every report is anonymized and
offered as a prefilled public GitHub issue; with it off, the reporter may tick
"Anonymize private data". These tests plant a username, a project, a camera,
an IP, a host name, a path and secrets, and prove none reaches the raw bytes of
any zip member, the stored row or the GitHub URL; that the verifier refuses and
stores nothing on a miss; that short or generic names (D16) refuse a GitHub
report but not an anonymize-only one; and the reporter download's access rules.
"""

from __future__ import annotations

import logging
import re
import socket
import urllib.parse
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.conftest import csrf_of, login, seed_admin
from tests.reports_support import (
    CONFIG_CAMERA_PASSWORD,
    DB_SECRETS,
    GITEA_TOKEN,
    PNG_BYTES,
    file_secret,
    seed_database_secrets,
    zip_members,
)
from timelapse_manager.config.settings import (
    ReportingGiteaSettings,
    ReportingGithubSettings,
    ReportingSettings,
    Settings,
)
from timelapse_manager.db.models import Camera, Event, Project, Report
from timelapse_manager.db.session import session_scope
from timelapse_manager.logging import get_ring_buffer
from timelapse_manager.reports import anonymize
from timelapse_manager.reports.github import MAX_URL_LENGTH
from timelapse_manager.runtime import get_context

_KEY = re.compile(r"TLM-\d{8}-\d{4}")
_HREF = re.compile(r'href="(https://github\.com/[^"]+)"')

ADMIN = "olivia-admin"
ADMIN_PW = "AdminP@ssw0rd1234"
REPORTER = "carol-operator"
OTHER = "dave-viewer"
VIEWER_PW = "ViewerPass12345!"

PROJECT = "Roofline Orchard"
CAMERA = "Backyard Cam 7"
CAMERA_IP = "10.0.16.71"
HOSTNAME = "tlm-box-42"
FQDN = "tlm-box-42.corp.example.com"


@pytest.fixture()
def web_settings(web_settings: Settings, tmp_path: Path) -> Settings:
    """GitHub hand-off on (the product default), a Gitea token file to keep out."""
    token_file = tmp_path / "report-gitea-token"
    token_file.write_text(GITEA_TOKEN + "\n", encoding="utf-8")
    return web_settings.model_copy(
        update={
            "reporting": ReportingSettings(
                gitea=ReportingGiteaSettings(token_file=token_file),
                github=ReportingGithubSettings(),
            ),
            "cameras": [
                {
                    "name": "Config Gate Cam",
                    "password": CONFIG_CAMERA_PASSWORD,
                    "url": f"rtsp://u:{CONFIG_CAMERA_PASSWORD}@10.0.0.9/",
                }
            ],
        }
    )


@pytest.fixture(autouse=True)
def _machine_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """A planted, known host name for this machine."""
    monkeypatch.setattr(socket, "gethostname", lambda: HOSTNAME)
    monkeypatch.setattr(socket, "getfqdn", lambda *_: FQDN)


@pytest.fixture(autouse=True)
def _fresh_log_buffer() -> None:
    """Start each test with an empty in-memory log (it is process-wide)."""
    buffer = get_ring_buffer()
    if buffer is not None:
        with buffer._lines_lock:
            buffer._lines.clear()


def _create_user(client: TestClient, username: str, role: str = "viewer") -> None:
    csrf = csrf_of(client, "/users")
    resp = client.post(
        "/users",
        data={
            "username": username,
            "password": VIEWER_PW,
            "role": role,
            "csrf_token": csrf,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert resp.status_code in (200, 303), resp.text[:300]


def _switch(client: TestClient, username: str, password: str = VIEWER_PW) -> None:
    csrf = csrf_of(client, "/")
    client.post(
        "/logout",
        data={"csrf_token": csrf},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    login(client, username=username, password=password)


@pytest.fixture()
def reporter(web_client: TestClient) -> TestClient:
    """Logged in as ``carol-operator``; ``olivia-admin`` and ``dave-viewer`` exist.

    An operator, not a viewer: a viewer's report never carries the snapshot
    (#67 S12), and these tests prove the snapshot members stay clean. An
    operator is still a non-admin, so refusals name only the kind.
    """
    seed_admin(web_client, username=ADMIN, password=ADMIN_PW)
    login(web_client, username=ADMIN, password=ADMIN_PW)
    _create_user(web_client, REPORTER, role="operator")
    _create_user(web_client, OTHER)
    _switch(web_client, REPORTER)
    return web_client


def _seed(*, camera_name: str = CAMERA) -> int:
    """The #8 secrets plus a planted project, camera and IP; return the project id."""
    with session_scope(get_context().session_factory) as session:
        seed_database_secrets(session)
        camera = Camera(name=camera_name, address=CAMERA_IP, protocol="vapix")
        session.add(camera)
        session.flush()
        project = Project(
            camera_id=camera.id,
            name=PROJECT,
            lifecycle_state="active",
            operational_status="idle",
        )
        session.add(project)
        session.flush()
        return int(project.id)


def _plant_log(data_dir: Path, *, extra: str = "") -> None:
    logging.getLogger("timelapse_manager.tests.reports").warning(
        "user %s: project %s camera %s at http://%s/axis on %s (%s) wrote %s %s",
        REPORTER,
        PROJECT,
        CAMERA,
        CAMERA_IP,
        HOSTNAME,
        FQDN,
        data_dir / "frames" / "x.jpg",
        extra,
    )


def _secrets() -> list[str]:
    settings = get_context().settings
    found = [
        *DB_SECRETS,
        CONFIG_CAMERA_PASSWORD,
        GITEA_TOKEN,
        file_secret(settings.paths.token_file),
        file_secret(settings.paths.data_dir / ".secret-key"),
    ]
    return [value for value in found if value]


def _planted(data_dir: Path) -> list[str]:
    return [
        REPORTER,
        ADMIN,
        OTHER,
        PROJECT,
        CAMERA,
        CAMERA_IP,
        HOSTNAME,
        FQDN,
        "corp.example.com",
        str(data_dir),
        "Roof cam",
        "auth.example.test",
        "smtp.example.test",
        "tlm@example.test",
        "Config Gate Cam",
        "10.0.0.9",
        "10.0.0.5",
    ]


def _submit(
    client: TestClient,
    *,
    htmx: bool = True,
    attachment: tuple[str, bytes, str] | None = None,
    **fields: Any,
) -> Any:
    data = {"title": "Capture stopped", "description": "It stopped at noon."}
    data.update({key: str(value) for key, value in fields.items()})
    headers: dict[str, str] = {}
    token = csrf_of(client, "/about")
    if htmx:
        headers.update({"HX-Request": "true", "X-CSRF-Token": token})
    else:
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


def _zip_files() -> list[Path]:
    folder = get_context().settings.paths.data_dir / "reports"
    return sorted(folder.glob("*.zip")) if folder.is_dir() else []


def _github_url(html: str) -> str:
    match = _HREF.search(html)
    assert match is not None, "no Open on GitHub link"
    return match.group(1).replace("&amp;", "&")


def _assert_absent(value: str, blob: bytes | str, where: str) -> None:
    text = blob.decode("utf-8", errors="replace") if isinstance(blob, bytes) else blob
    assert value.casefold() not in text.casefold(), f"{value!r} leaked in {where}"


# ---------------------------------------------------------------------------
# The form
# ---------------------------------------------------------------------------


def test_form_explains_the_hand_off_and_has_no_box_or_upload(
    reporter: TestClient,
) -> None:
    body = reporter.get("/report").text
    assert 'id="report-github-notice"' in body
    assert "github.com/drcoble/Timelapse_Manager" in body
    assert 'name="anonymize"' not in body
    assert 'name="attachment"' not in body
    assert "Screenshots are not included" in body


def test_form_offers_the_box_when_github_is_off(reporter: TestClient) -> None:
    get_context().settings.reporting.github.enabled = False
    body = reporter.get("/report").text
    assert 'name="anonymize"' in body
    assert 'name="attachment"' in body
    assert "report-github-notice" not in body


# ---------------------------------------------------------------------------
# Nothing private leaves
# ---------------------------------------------------------------------------


def test_planted_values_never_reach_zip_row_or_url(reporter: TestClient) -> None:
    project_id = _seed()
    data_dir = get_context().settings.paths.data_dir
    _plant_log(data_dir)
    resp = _submit(
        reporter,
        project_id=project_id,
        area="project",
        include_snapshot="on",
        title=f"{CAMERA} at {CAMERA_IP} stopped",
        description=(
            f"Since noon {PROJECT} has no frames. {REPORTER} saw it on {FQDN}; "
            f"files are in {data_dir}. Password was {DB_SECRETS[0]}."
        ),
    )
    assert resp.status_code == 200, resp.text
    url = _github_url(resp.text)
    [report] = _reports()
    assert report.anonymized is True
    raw = _zip_files()[0].read_bytes()
    members = zip_members(raw)
    assert {"report.json", "log.txt", "app.json", "events.json"} <= set(members)
    # The planted log line really was in the window (as placeholders).
    assert "project-" in members["log.txt"].decode()
    stored = f"{report.title}\n{report.description}"
    decoded = urllib.parse.unquote(url)
    for value in [*_planted(data_dir), *_secrets()]:
        for name, blob in members.items():
            _assert_absent(value, blob, name)
        _assert_absent(value, stored, "the stored row")
        _assert_absent(value, url, "the GitHub URL")
        _assert_absent(value, decoded, "the decoded GitHub URL")


def test_report_is_marked_anonymized_and_screenshot_left_out(
    reporter: TestClient,
) -> None:
    # A no-JS post can still send a file; it is ignored.
    resp = _submit(
        reporter, htmx=False, attachment=("shot.png", PNG_BYTES, "image/png")
    )
    assert resp.status_code == 200, resp.text
    members = zip_members(_zip_files()[0].read_bytes())
    assert not any(name.startswith("attachment") for name in members)
    report_json = members["report.json"].decode()
    assert '"anonymized": true' in report_json
    assert '"attachment": null' in report_json
    assert "Anonymized" in members["manifest.json"].decode()


def test_result_offers_github_download_and_is_audited(reporter: TestClient) -> None:
    resp = _submit(reporter)
    assert resp.status_code == 200, resp.text
    key = _KEY.search(resp.text)
    assert key is not None
    url = _github_url(resp.text)
    assert url.startswith("https://github.com/drcoble/Timelapse_Manager/issues/new?")
    assert "labels=" not in url
    assert len(url) <= MAX_URL_LENGTH
    assert 'target="_blank" rel="noopener noreferrer"' in resp.text
    assert f'href="/report/{key.group(0)}/download"' in resp.text
    assert "The GitHub issue is public" in resp.text
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert query["title"] == ["Capture stopped"]
    assert key.group(0) in query["body"][0]
    with session_scope(get_context().session_factory) as session:
        actions = [
            (event.event_metadata or {}).get("action")
            for event in session.execute(select(Event)).scalars()
        ]
    assert "report.github_prepared" in actions


def test_long_description_is_trimmed_in_the_url(reporter: TestClient) -> None:
    get_context().settings.reporting.max_text_bytes = 200_000
    resp = _submit(reporter, description="😀 frames missing " * 3000)
    assert resp.status_code == 200, resp.text
    url = _github_url(resp.text)
    assert len(url) <= MAX_URL_LENGTH
    assert "Description truncated" in urllib.parse.unquote(url)
    # The stored description is the whole (anonymized) text.
    [report] = _reports()
    assert len(report.description) > 10_000


def test_configured_labels_are_prefilled(reporter: TestClient) -> None:
    get_context().settings.reporting.github.labels = ["from-app"]
    url = _github_url(_submit(reporter).text)
    assert "labels=from-app" in url


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------


def _alert(html: str) -> str:
    match = re.search(r'<div class="alert error"[^>]*>(.*?)</div>\s*</div>', html, re.S)
    assert match is not None, "no inline error"
    return match.group(1)


def _assert_nothing_stored(resp: Any) -> None:
    assert resp.status_code == 200
    assert 'class="alert error"' in resp.text
    assert "nothing was saved" in resp.text
    assert _reports() == []
    assert _zip_files() == []


def test_forced_miss_is_refused_and_nothing_stored(
    reporter: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = _seed()
    _plant_log(get_context().settings.paths.data_dir)
    # The anonymizer "misses" everything; the verifier must catch it.
    monkeypatch.setattr(anonymize.Pseudonymizer, "text", lambda self, value: value)
    resp = _submit(reporter, project_id=project_id, include_snapshot="on")
    _assert_nothing_stored(resp)
    alert = _alert(resp.text)
    assert "could not be anonymized safely" in alert
    # The refusal message never repeats a private value.
    for value in (PROJECT, CAMERA_IP, HOSTNAME):
        assert value not in alert


def test_anonymizer_error_is_refused_and_nothing_stored(
    reporter: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: object, name: str, data: bytes) -> bytes:
        raise RuntimeError("boom")

    monkeypatch.setattr(anonymize.Pseudonymizer, "member", broken)
    resp = _submit(reporter, include_snapshot="on")
    _assert_nothing_stored(resp)


def test_secret_left_in_the_url_is_refused(
    reporter: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from timelapse_manager.reports import service

    monkeypatch.setattr(
        service,
        "build_issue_url",
        lambda *a, **kw: "https://github.com/o/r/issues/new?title=" + DB_SECRETS[0],
    )
    _seed()
    resp = _submit(reporter)
    _assert_nothing_stored(resp)


def test_refusals_count_toward_the_hourly_limit(
    reporter: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused attempts still build a snapshot, so they use up the limit."""
    get_context().settings.reporting.rate_limit_per_hour = 3
    assert _submit(reporter).status_code == 200  # accepted (1)
    with monkeypatch.context() as patch:
        patch.setattr(anonymize.Pseudonymizer, "text", lambda self, value: value)
        for _ in range(2):  # refused (2, 3)
            resp = _submit(reporter, description=f"see {CAMERA_IP}")
            assert "could not be anonymized safely" in _alert(resp.text)
    resp = _submit(reporter)
    assert "3 reports an hour" in resp.text
    assert len(_reports()) == 1
    with session_scope(get_context().session_factory) as session:
        refused = [
            event
            for event in session.execute(select(Event)).scalars()
            if (event.event_metadata or {}).get("action") == "report.refused"
        ]
    assert len(refused) == 2
    # The refusal record names nothing private.
    assert all(CAMERA_IP not in event.message for event in refused)
    # Another user is not affected by this user's refusals.
    _switch(reporter, OTHER)
    assert _submit(reporter).status_code == 200
    assert "Open on GitHub" in _submit(reporter).text


def test_refused_key_is_never_issued_again(
    reporter: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(anonymize.Pseudonymizer, "text", lambda self, value: value)
        _submit(reporter, description=f"see {CAMERA_IP}")
    with session_scope(get_context().session_factory) as session:
        [refused_key] = [
            (event.event_metadata or {}).get("report_id")
            for event in session.execute(select(Event)).scalars()
            if (event.event_metadata or {}).get("action") == "report.refused"
        ]
    resp = _submit(reporter)
    key = _KEY.search(resp.text)
    assert key is not None
    assert key.group(0) > refused_key


def test_plain_no_js_refusal_is_422(
    reporter: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(anonymize.Pseudonymizer, "text", lambda self, value: value)
    _seed()
    resp = _submit(reporter, htmx=False, description=f"see {CAMERA_IP}")
    assert resp.status_code == 422
    assert _reports() == []


# ---------------------------------------------------------------------------
# Short or generic names (D16)
# ---------------------------------------------------------------------------


def test_generic_public_host_label_does_not_refuse(reporter: TestClient) -> None:
    """``timelapse.<domain>`` must not turn the app's own name into a refusal."""
    get_context().settings.reporting.public_base_url = "https://timelapse.example.com"
    logging.getLogger("timelapse_manager.tests.reports").warning(
        "timelapse-manager served https://timelapse.example.com/x"
    )
    resp = _submit(reporter, include_snapshot="on")
    assert resp.status_code == 200, resp.text
    assert "Open on GitHub" in resp.text
    members = zip_members(_zip_files()[0].read_bytes())
    for name, blob in members.items():
        _assert_absent("timelapse.example.com", blob, name)


def test_short_camera_name_refusal_names_only_the_kind_for_a_non_admin(
    reporter: TestClient,
) -> None:
    _seed(camera_name="Pi")
    logging.getLogger("timelapse_manager.tests.reports").warning("camera Pi stalled")
    resp = _submit(reporter, include_snapshot="on")
    _assert_nothing_stored(resp)
    alert = _alert(resp.text)
    assert "A camera name on this server is too short or too generic" in alert
    assert "Ask an administrator to rename them" in alert
    assert "without the diagnostic snapshot" in alert
    assert "Pi" not in alert
    # Administrators find the value in the log.
    buffer = get_ring_buffer()
    assert buffer is not None
    [line] = [
        entry.text for entry in buffer.lines() if "refused for GitHub" in entry.text
    ]
    # Review finding 7: kinds and a count only. The value itself would land
    # in the next report's log and refuse it again.
    assert "1 camera name" in line
    assert "Pi" not in line


def test_short_names_are_named_for_an_admin(web_client: TestClient) -> None:
    seed_admin(web_client, username=ADMIN, password=ADMIN_PW)
    login(web_client, username=ADMIN, password=ADMIN_PW)
    _create_user(web_client, "bo")  # a short user name the admin can see
    _seed(camera_name="Pi")
    logging.getLogger("timelapse_manager.tests.reports").warning(
        "camera Pi stalled; bo looked"
    )
    resp = _submit(web_client, include_snapshot="on")
    _assert_nothing_stored(resp)
    alert = _alert(resp.text)
    assert "camera name “Pi”" in alert
    assert "user name “bo”" in alert
    assert "rename that camera" in alert


def test_non_admin_never_learns_another_users_name(reporter: TestClient) -> None:
    with session_scope(get_context().session_factory) as session:
        from timelapse_manager.db.models import User

        session.add(User(username="jo", role="viewer", auth_source="local"))
    logging.getLogger("timelapse_manager.tests.reports").warning("jo signed in")
    resp = _submit(reporter, include_snapshot="on")
    _assert_nothing_stored(resp)
    alert = _alert(resp.text)
    assert "A user name on this server" in alert
    assert "jo" not in alert


def test_short_name_only_in_structured_fields_files_fine(
    reporter: TestClient,
) -> None:
    project_id = _seed(camera_name="Pi")
    resp = _submit(
        reporter, project_id=project_id, area="project", include_snapshot="on"
    )
    assert resp.status_code == 200, resp.text
    assert "Open on GitHub" in resp.text
    members = zip_members(_zip_files()[0].read_bytes())
    camera_json = members["camera.json"].decode()
    assert '"name": "camera-' in camera_json
    assert '"Pi"' not in camera_json


def test_generic_reporter_name_in_own_text_is_refused(web_client: TestClient) -> None:
    seed_admin(web_client, username=ADMIN, password=ADMIN_PW)
    login(web_client, username=ADMIN, password=ADMIN_PW)
    _create_user(web_client, "pi")
    _switch(web_client, "pi")
    resp = _submit(web_client, description="pi cannot see renders")
    _assert_nothing_stored(resp)
    alert = _alert(resp.text)
    # A name in the reporter's own text: not even its kind is told.
    assert "Your title or description contains a short or generic name" in alert
    assert "user name" not in alert


def test_anonymize_only_keeps_short_names_and_files(reporter: TestClient) -> None:
    get_context().settings.reporting.github.enabled = False
    project_id = _seed(camera_name="Pi")
    logging.getLogger("timelapse_manager.tests.reports").warning(
        "camera Pi stalled at %s", CAMERA_IP
    )
    resp = _submit(
        reporter,
        project_id=project_id,
        include_snapshot="on",
        anonymize="on",
        attachment=("shot.png", PNG_BYTES, "image/png"),
    )
    assert resp.status_code == 200, resp.text
    assert "Open on GitHub" not in resp.text
    assert "Download anonymized zip" in resp.text
    [report] = _reports()
    assert report.anonymized is True
    members = zip_members(_zip_files()[0].read_bytes())
    assert not any(name.startswith("attachment") for name in members)
    log = members["log.txt"].decode()
    assert "camera Pi stalled" in log  # left in free text (D16 a)
    assert CAMERA_IP not in log
    assert '"name": "camera-' in members["camera.json"].decode()
    with session_scope(get_context().session_factory) as session:
        actions = [
            (event.event_metadata or {}).get("action")
            for event in session.execute(select(Event)).scalars()
        ]
    assert "report.github_prepared" not in actions


def test_github_off_without_the_box_is_plain(reporter: TestClient) -> None:
    get_context().settings.reporting.github.enabled = False
    project_id = _seed()
    resp = _submit(reporter, project_id=project_id, include_snapshot="on")
    assert resp.status_code == 200, resp.text
    [report] = _reports()
    assert report.anonymized is False
    assert "Download anonymized zip" not in resp.text
    assert PROJECT in zip_members(_zip_files()[0].read_bytes())["project.json"].decode()


# ---------------------------------------------------------------------------
# Reporter download
# ---------------------------------------------------------------------------


def _file_as_reporter(client: TestClient) -> str:
    resp = _submit(client)
    assert resp.status_code == 200, resp.text
    match = _KEY.search(resp.text)
    assert match is not None
    return match.group(0)


def test_reporter_download_rules(reporter: TestClient) -> None:
    key = _file_as_reporter(reporter)

    mine = reporter.get(f"/report/{key}/download")
    assert mine.status_code == 200
    assert mine.headers["content-type"] == "application/zip"
    assert f'filename="{key}.zip"' in mine.headers["content-disposition"]
    assert "report.json" in zip_members(mine.content)

    _switch(reporter, OTHER)
    assert reporter.get(f"/report/{key}/download").status_code == 403

    _switch(reporter, ADMIN, ADMIN_PW)
    assert reporter.get(f"/report/{key}/download").status_code == 200


def test_reporter_download_404_for_plain_reports(reporter: TestClient) -> None:
    get_context().settings.reporting.github.enabled = False
    key = _file_as_reporter(reporter)
    assert reporter.get(f"/report/{key}/download").status_code == 404
    _switch(reporter, ADMIN, ADMIN_PW)
    assert reporter.get(f"/report/{key}/download").status_code == 404
    # The admin route still serves it.
    assert reporter.get(f"/settings/reports/{key}/download").status_code == 200


def test_reporter_download_404_once_not_anonymized(reporter: TestClient) -> None:
    """The route can only ever serve a zip whose row says it is anonymized."""
    key = _file_as_reporter(reporter)
    assert reporter.get(f"/report/{key}/download").status_code == 200
    with session_scope(get_context().session_factory) as session:
        row = session.execute(
            select(Report).where(Report.report_key == key)
        ).scalar_one()
        row.anonymized = False
    assert reporter.get(f"/report/{key}/download").status_code == 404
    _switch(reporter, ADMIN, ADMIN_PW)
    assert reporter.get(f"/report/{key}/download").status_code == 404


def test_result_says_the_zip_is_what_becomes_public(reporter: TestClient) -> None:
    text = _submit(reporter).text
    assert "this zip is what becomes public" in text


def test_reporter_download_rejects_bad_keys(reporter: TestClient) -> None:
    for key in ("TLM-20261023-9999", "../etc", "x"):
        assert reporter.get(f"/report/{key}/download").status_code == 404


def test_admin_list_shows_anonymized(reporter: TestClient) -> None:
    _file_as_reporter(reporter)
    _switch(reporter, ADMIN, ADMIN_PW)
    assert "Anonymized" in reporter.get("/settings/reports").text


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def test_api_reports_are_anonymized_with_a_url(web_client: TestClient) -> None:
    from timelapse_manager.security.token import ensure_local_token

    token = ensure_local_token(get_context().settings)
    _seed()
    resp = web_client.post(
        "/api/v1/reports",
        json={"title": f"lag on {CAMERA}", "description": f"{PROJECT} late"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["anonymized"] is True
    url = body["github_issue_url"]
    assert url.startswith("https://github.com/")
    for value in (CAMERA, PROJECT):
        _assert_absent(value, urllib.parse.unquote(url), "the API URL")
    [report] = _reports()
    assert report.anonymized is True
    assert CAMERA not in report.title


# ---------------------------------------------------------------------------
# PR #31 security review (findings 1, 2, 8 and real install data)
# ---------------------------------------------------------------------------

STORAGE = "/mnt/nas-media/SmithFamily/roofline"
EXPORT = "/media/alice-usb/timelapses"


def test_paths_from_projects_and_settings_never_leak(
    reporter: TestClient, tmp_path: Path
) -> None:
    """Finding 1: storage, export, render, cert, font and CA paths."""
    settings = get_context().settings
    settings.tls.cert_path = "/srv/certs/alice-home.pem"
    settings.tls.key_path = "/srv/certs/alice-home.key"
    settings.render.font_path = "/home/alice/fonts/Inter.ttf"
    project_id = _seed()
    with session_scope(get_context().session_factory) as session:
        from timelapse_manager.db.models import Frame, LdapSettings, RenderJob

        project = session.get(Project, project_id)
        assert project is not None
        project.storage_path = STORAGE
        project.post_render_actions = [{"type": "export", "destination": EXPORT}]
        session.add(
            Frame(
                project_id=project_id,
                sequence_index=1,
                capture_timestamp=project.created_at,
                file_path=f"{STORAGE}/2026/1.jpg",
                capture_status="captured",
                origin="captured",
                lifecycle_state="active",
            )
        )
        session.add(
            RenderJob(
                project_id=project_id,
                encoder_engine="ffmpeg",
                kind="manual",
                status="done",
                output_file_path=f"{STORAGE}/renders/out.mp4",
            )
        )
        ldap = session.get(LdapSettings, 1)
        assert ldap is not None
        ldap.tls_ca_cert_path = "/etc/ssl/smith-ca.pem"
    logging.getLogger("timelapse_manager.tests.reports").warning(
        "copied %s/renders/out.mp4 to %s; home /var/home/alice", STORAGE, EXPORT
    )
    resp = _submit(
        reporter, project_id=project_id, area="project", include_snapshot="on"
    )
    assert resp.status_code == 200, resp.text
    url = _github_url(resp.text)
    members = zip_members(_zip_files()[0].read_bytes())
    for value in (
        "nas-media",
        "SmithFamily",
        "alice-usb",
        "alice-home",
        "smith-ca",
        "/var/home/alice",
        "Inter.ttf",
    ):
        for name, blob in members.items():
            _assert_absent(value, blob, name)
        _assert_absent(value, urllib.parse.unquote(url), "the GitHub URL")


def test_entity_source_failure_refuses(
    reporter: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 2: an unreadable entity source must not let its names through."""

    def broken(session: object) -> object:
        raise RuntimeError("user table unreadable")

    monkeypatch.setattr(anonymize, "_users", broken)
    _plant_log(get_context().settings.paths.data_dir)
    resp = _submit(reporter, include_snapshot="on")
    _assert_nothing_stored(resp)
    assert "could not be anonymized safely" in _alert(resp.text)
    with session_scope(get_context().session_factory) as session:
        actions = [
            (event.event_metadata or {}).get("action")
            for event in session.execute(select(Event)).scalars()
        ]
    assert "report.refused" in actions


def test_stored_and_delivered_reporter_name_is_a_placeholder(
    reporter: TestClient,
) -> None:
    """Finding 8: the row's username is the pseudonymized reporter name."""
    _file_as_reporter(reporter)
    [report] = _reports()
    assert report.username is not None
    assert report.username.startswith("user-")
    assert REPORTER not in report.username
    _switch(reporter, ADMIN, ADMIN_PW)
    table = reporter.get("/settings/reports").text
    assert f"user #{report.user_id}" in table
    assert REPORTER not in table


REAL_USERS = ("alice.smith", "svc_bot")
REAL_CAMERAS = (
    ("Porch", "https://axis-b8a44fb48e95"),
    ("Driveway view 1 (camera=5)", "https://axis-b8a44f17384a"),
)
REAL_PROJECTS = (
    "North Side",
    "Front Landscaping",
    "Garage",
    "Garden",
    "Patio",
    "W Yard",
    "Side Yard",
)


def test_real_install_names_file_cleanly_and_never_leak(
    web_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prod-shaped data files a GitHub report (no D16 refusal) and leaks none."""
    monkeypatch.setattr(socket, "gethostname", lambda: "timelapse01")
    monkeypatch.setattr(socket, "getfqdn", lambda *_: "timelapse01.example.com")
    seed_admin(web_client, username="site-admin", password=ADMIN_PW)
    login(web_client, username="site-admin", password=ADMIN_PW)
    for user in REAL_USERS:
        _create_user(web_client, user)
    with session_scope(get_context().session_factory) as session:
        cameras = []
        for name, address in REAL_CAMERAS:
            camera = Camera(name=name, address=address, protocol="vapix")
            session.add(camera)
            session.flush()
            cameras.append(camera.id)
        ids = []
        for index, name in enumerate(REAL_PROJECTS):
            project = Project(
                camera_id=cameras[index % 2],
                name=name,
                lifecycle_state="active",
                operational_status="idle",
            )
            session.add(project)
            session.flush()
            ids.append(project.id)
    log = logging.getLogger("timelapse_manager.tests.reports")
    log.warning("alice.smith paused North Side and Side Yard on Porch")
    log.warning(
        "svc_bot fetched https://axis-b8a44f17384a/axis-cgi/jpg for "
        "Driveway view 1 (camera=5); W Yard, Garage, Garden, Patio ok"
    )
    log.warning(
        "bind uid=svc_bot,cn=users,cn=accounts,dc=example,dc=com "
        "via timelapse01.example.com"
    )
    _switch(web_client, "alice.smith")
    resp = _submit(
        web_client,
        project_id=ids[0],
        area="project",
        include_snapshot="on",
        title="North Side stalled on Porch",
        description="Front Landscaping too; svc_bot saw it on timelapse01.example.com",
    )
    assert resp.status_code == 200, resp.text
    assert "Open on GitHub" in resp.text
    url = _github_url(resp.text)
    [report] = _reports()
    members = zip_members(_zip_files()[0].read_bytes())
    leaks = [
        "alice.smith",
        "alice",
        "svc_bot",
        "site-admin",
        "timelapse01",
        "example.com",
        "b8a44fb48e95",
        "b8a44f17384a",
        "Porch",
        "Driveway",
        *REAL_PROJECTS,
    ]
    stored = f"{report.title}\n{report.description}\n{report.username}"
    for value in leaks:
        for name, blob in members.items():
            _assert_absent(value, blob, name)
        _assert_absent(value, stored, "the stored row")
        _assert_absent(value, urllib.parse.unquote(url), "the GitHub URL")


def test_viewer_github_report_has_no_snapshot_and_no_leak(
    web_client: TestClient,
) -> None:
    """#67 S12: a viewer's anonymized report holds only their words, still clean."""
    seed_admin(web_client, username=ADMIN, password=ADMIN_PW)
    login(web_client, username=ADMIN, password=ADMIN_PW)
    _create_user(web_client, OTHER)
    _switch(web_client, OTHER)
    project_id = _seed()
    data_dir = get_context().settings.paths.data_dir
    _plant_log(data_dir)
    resp = _submit(
        web_client,
        project_id=project_id,
        area="project",
        include_snapshot="on",
        title=f"{CAMERA} at {CAMERA_IP} stopped",
        description=f"Since noon {PROJECT} has no frames, files in {data_dir}.",
    )
    assert resp.status_code == 200, resp.text
    url = _github_url(resp.text)
    [report] = _reports()
    assert report.anonymized is True
    members = zip_members(_zip_files()[0].read_bytes())
    assert set(members) == {"manifest.json", "report.json", "request.json"}
    stored = f"{report.title}\n{report.description}"
    for value in [*_planted(data_dir), *_secrets()]:
        for name, blob in members.items():
            _assert_absent(value, blob, name)
        _assert_absent(value, stored, "the stored row")
        _assert_absent(value, urllib.parse.unquote(url), "the decoded GitHub URL")
