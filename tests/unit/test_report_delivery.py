"""Delivery tests for problem reports (#8): Gitea, email, webhook.

Gitea is exercised against an ``httpx.MockTransport`` (never the real
git.example.com): the issue and the attachment are created, the explicit
``User-Agent`` is sent, a failure leaves the report stored and flagged, and a
resend finishes the job on the same issue. Email uses a patched ``smtplib``
(as the channel tests do); the webhook a mock transport, with the signature
checked against the notification signing secret.
"""

from __future__ import annotations

import asyncio
import email
import hashlib
import hmac
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from timelapse_manager import __version__
from timelapse_manager.config.settings import (
    ReportingGiteaSettings,
    ReportingGithubSettings,
    ReportingSettings,
    Settings,
)
from timelapse_manager.db.models import NotificationSettings, Report
from timelapse_manager.db.session import session_scope
from timelapse_manager.monitoring import settings_service as notification_settings
from timelapse_manager.reports import delivery
from timelapse_manager.reports.service import Reporter, Submission, file_report
from timelapse_manager.reports.snapshot import RequestContext, SnapshotSources
from timelapse_manager.runtime import get_context
from timelapse_manager.security.crypto import encrypt_secret

_GITEA_TOKEN = "gitea-unit-token-abc123"
_REPO_API = "https://git.example.test/api/v1/repos/alice.smith/timelapse-manager"


@pytest.fixture(autouse=True)
def _machine_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fixed host name, so anonymizing never depends on the runner's (#21)."""
    import socket

    monkeypatch.setattr(socket, "gethostname", lambda: "tlm-test-host-42")
    monkeypatch.setattr(socket, "getfqdn", lambda *_: "tlm-test-host-42.example.org")


@pytest.fixture()
def app(web_client: TestClient) -> TestClient:
    """A migrated, running app (its context supplies the session factory)."""
    return web_client


def _settings(tmp_path: Path, **reporting: Any) -> Settings:
    token_file = tmp_path / "gitea-token"
    token_file.write_text(_GITEA_TOKEN, encoding="utf-8")
    gitea = reporting.pop(
        "gitea",
        ReportingGiteaSettings(
            base_url="https://git.example.test",
            repo="alice.smith/timelapse-manager",
            token_file=token_file,
            labels=["bug", "no-such-label"],
        ),
    )
    # #8's plain reports unless a test asks for the GitHub hand-off (#21).
    reporting.setdefault("github", ReportingGithubSettings(enabled=False))
    return get_context().settings.model_copy(
        update={"reporting": ReportingSettings(gitea=gitea, **reporting)}
    )


def _file(
    settings: Settings,
    description: str = "Roof camera stopped at **noon**.",
    title: str = "Capture stopped",
) -> int:
    filed = file_report(
        get_context().session_factory,
        settings,
        Submission(
            title=title,
            description=description,
            area="capture",
            reported_at=None,
            project_id=None,
            camera_id=None,
            include_snapshot=False,
            attachment=None,
            source="web",
        ),
        Reporter(user_id=None, username="viewer", role="viewer"),
        context=RequestContext(),
        sources=SnapshotSources(),
    )
    return filed.report_id


def _report(report_id: int) -> Report:
    with session_scope(get_context().session_factory) as session:
        report = session.get(Report, report_id)
        assert report is not None
        session.expunge(report)
        return report


def _use_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        return handler(request)

    real = delivery._http_client

    def client(timeout: float) -> httpx.AsyncClient:
        built = real(timeout)
        return httpx.AsyncClient(
            transport=httpx.MockTransport(recording),
            timeout=timeout,
            follow_redirects=False,
            headers=built.headers,
        )

    monkeypatch.setattr(delivery, "_http_client", client)
    monkeypatch.setattr(delivery, "_webhook_client", client)
    return seen


def _gitea_handler(*, asset_status: list[int], create_status: int = 201) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/labels"):
            return httpx.Response(
                200, json=[{"id": 7, "name": "bug"}, {"id": 9, "name": "other"}]
            )
        if request.method == "POST" and path.endswith("/issues"):
            return httpx.Response(
                create_status,
                json={
                    "number": 42,
                    "html_url": "https://git.example.test/alice.smith/"
                    "timelapse-manager/issues/42",
                },
            )
        if request.method == "POST" and path.endswith("/issues/42/assets"):
            return httpx.Response(asset_status.pop(0), json={"id": 1})
        return httpx.Response(404)

    return handler


def _deliver(settings: Settings, report_id: int, *destinations: str) -> dict[str, str]:
    return _run_private_loop(
        delivery.deliver_report(
            get_context().session_factory, settings, report_id, destinations
        )
    )


# ---------------------------------------------------------------------------
# Gitea
# ---------------------------------------------------------------------------


def test_gitea_creates_issue_and_uploads_zip(
    app: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    report_id = _file(settings)
    key = _report(report_id).report_key
    seen = _use_transport(monkeypatch, _gitea_handler(asset_status=[201]))

    assert _deliver(settings, report_id, "gitea") == {"gitea": "ok"}

    assert all(
        r.headers["user-agent"] == f"timelapse-manager/{__version__}" for r in seen
    )
    assert all(r.headers["authorization"] == f"token {_GITEA_TOKEN}" for r in seen)
    create = next(r for r in seen if r.url.path.endswith("/issues"))
    payload = json.loads(create.content)
    assert payload["title"] == f"[{key}] Capture stopped"
    assert payload["labels"] == [7]
    assert key in payload["body"]
    assert "Roof camera stopped at **noon**." in payload["body"]
    assert "## Summary" in payload["body"]
    asset = next(r for r in seen if r.url.path.endswith("/assets"))
    assert asset.url.params["name"] == f"{key}.zip"
    assert b'name="attachment"' in asset.content
    assert f'filename="{key}.zip"'.encode() in asset.content

    report = _report(report_id)
    assert report.gitea_issue_number == 42
    assert report.gitea_issue_url.endswith("/issues/42")
    assert report.deliveries["gitea"]["status"] == "ok"
    assert report.delivery_state == "ok"


def test_gitea_failure_keeps_report_and_resend_finishes(
    app: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    report_id = _file(settings)
    seen = _use_transport(monkeypatch, _gitea_handler(asset_status=[500, 201]))

    assert _deliver(settings, report_id, "gitea") == {"gitea": "failed"}
    report = _report(report_id)
    assert report.delivery_state == "failed"
    assert report.deliveries["gitea"]["error"] == (
        "Gitea attachment upload returned HTTP 500"
    )
    assert report.deliveries["local"]["status"] == "ok"
    assert report.gitea_issue_number == 42
    assert _GITEA_TOKEN not in json.dumps(report.deliveries)

    assert _deliver(settings, report_id, "gitea") == {"gitea": "ok"}
    creates = [r for r in seen if r.method == "POST" and r.url.path.endswith("/issues")]
    assert len(creates) == 1, "the retry reuses the existing issue"
    assert _report(report_id).delivery_state == "ok"


def test_gitea_create_failure_is_flagged(
    app: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    report_id = _file(settings)
    _use_transport(monkeypatch, _gitea_handler(asset_status=[], create_status=401))
    assert _deliver(settings, report_id, "gitea") == {"gitea": "failed"}
    report = _report(report_id)
    assert report.gitea_issue_number is None
    assert report.deliveries["gitea"]["error"] == "Gitea issue create returned HTTP 401"
    assert report.zip_path is not None


def test_gitea_unreadable_token_is_flagged(
    app: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gitea = ReportingGiteaSettings(
        base_url="https://git.example.test",
        repo="alice.smith/timelapse-manager",
        token_file=tmp_path / "missing",
    )
    settings = _settings(tmp_path, gitea=gitea)
    report_id = _file(settings)
    seen = _use_transport(monkeypatch, _gitea_handler(asset_status=[]))
    assert _deliver(settings, report_id, "gitea") == {"gitea": "failed"}
    assert seen == []
    assert "token file unreadable" in _report(report_id).deliveries["gitea"]["error"]


def test_issue_body_is_plain_markdown(app: TestClient, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    view = delivery._load_view(get_context().session_factory, settings, _file(settings))
    assert view is not None
    body = delivery.issue_body(view, settings)
    assert body.splitlines()[2] == "## What happened"
    assert f"- **Version:** {__version__}" in body
    assert "- **Area:** Capture" in body


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def _configure_smtp() -> None:
    with session_scope(get_context().session_factory) as session:
        notification_settings.update_settings(
            session,
            notification_settings.NotificationSettingsUpdate(
                enabled_channels=["email"],
                smtp_server="smtp.example.test",
                smtp_port=587,
                smtp_security="none",
                smtp_username=None,
                smtp_password=None,
                smtp_from_address="tlm@example.test",
                smtp_recipients=["alerts@example.test"],
                webhook_urls=[],
                routing_rules=[],
            ),
        )


def _sent_email(smtp: MagicMock) -> email.message.EmailMessage:
    [call] = smtp.send_message.call_args_list
    message: email.message.EmailMessage = call.args[0]
    return message


def test_email_attaches_small_zip(app: TestClient, tmp_path: Path) -> None:
    _configure_smtp()
    settings = _settings(
        tmp_path, email_to=["maint@example.test"], gitea=ReportingGiteaSettings()
    )
    report_id = _file(settings)
    key = _report(report_id).report_key
    smtp = MagicMock()
    with patch("smtplib.SMTP", return_value=smtp) as smtp_cls:
        assert _deliver(settings, report_id, "email") == {"email": "ok"}
    smtp_cls.assert_called_once_with("smtp.example.test", 587, timeout=30.0)
    message = _sent_email(smtp)
    assert message["To"] == "maint@example.test"
    assert key in message["Subject"]
    attachments = list(message.iter_attachments())
    assert [part.get_filename() for part in attachments] == [f"{key}.zip"]
    assert _report(report_id).deliveries["email"]["attached"] is True


def test_email_links_large_zip(app: TestClient, tmp_path: Path) -> None:
    _configure_smtp()
    settings = _settings(
        tmp_path,
        email_to=["maint@example.test"],
        email_attach_max_bytes=10,
        public_base_url="https://timelapse.example.test/",
        gitea=ReportingGiteaSettings(),
    )
    report_id = _file(settings)
    key = _report(report_id).report_key
    smtp = MagicMock()
    with patch("smtplib.SMTP", return_value=smtp):
        assert _deliver(settings, report_id, "email") == {"email": "ok"}
    message = _sent_email(smtp)
    assert list(message.iter_attachments()) == []
    assert (
        f"https://timelapse.example.test/settings/reports/{key}/download"
        in message.get_content()
    )


def test_email_without_smtp_server_is_flagged(app: TestClient, tmp_path: Path) -> None:
    settings = _settings(
        tmp_path, email_to=["maint@example.test"], gitea=ReportingGiteaSettings()
    )
    report_id = _file(settings)
    assert _deliver(settings, report_id, "email") == {"email": "failed"}
    assert (
        _report(report_id).deliveries["email"]["error"] == "SMTP server not configured"
    )


def test_email_transport_failure_is_flagged(app: TestClient, tmp_path: Path) -> None:
    _configure_smtp()
    settings = _settings(
        tmp_path, email_to=["maint@example.test"], gitea=ReportingGiteaSettings()
    )
    report_id = _file(settings)
    with patch("smtplib.SMTP", side_effect=ConnectionRefusedError()):
        assert _deliver(settings, report_id, "email") == {"email": "failed"}
    report = _report(report_id)
    assert report.deliveries["email"]["error"] == (
        "SMTP delivery failed: ConnectionRefusedError"
    )
    assert report.delivery_state == "failed"


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


def test_webhook_posts_signed_summary(
    app: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with session_scope(get_context().session_factory) as session:
        session.add(
            NotificationSettings(
                id=1, webhook_signing_secret=encrypt_secret("sign-me-please")
            )
        )
    monkeypatch.setattr(
        "timelapse_manager.render.post_actions.validate_outbound_url_async",
        _pass_through_url,
    )
    settings = _settings(
        tmp_path,
        webhook_url="https://hooks.example.test/report",
        gitea=ReportingGiteaSettings(),
    )
    report_id = _file(settings)
    key = _report(report_id).report_key
    seen = _use_transport(monkeypatch, lambda request: httpx.Response(204))

    assert _deliver(settings, report_id, "webhook") == {"webhook": "ok"}
    [request] = seen
    body = json.loads(request.content)
    assert body["report_id"] == key
    assert body["event_type"] == "report.filed"
    expected = hmac.new(b"sign-me-please", request.content, hashlib.sha256).hexdigest()
    assert request.headers["x-signature-256"] == f"sha256={expected}"
    assert request.headers["user-agent"] == f"timelapse-manager/{__version__}"


def test_webhook_to_loopback_is_blocked(app: TestClient, tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        webhook_url="http://127.0.0.1:9/hook",
        gitea=ReportingGiteaSettings(),
    )
    report_id = _file(settings)
    assert _deliver(settings, report_id, "webhook") == {"webhook": "failed"}
    assert "SSRF" in _report(report_id).deliveries["webhook"]["error"]


def test_one_failure_does_not_stop_the_others(
    app: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "timelapse_manager.render.post_actions.validate_outbound_url_async",
        _pass_through_url,
    )
    settings = _settings(
        tmp_path,
        email_to=["maint@example.test"],
        webhook_url="https://hooks.example.test/report",
    )
    report_id = _file(settings)

    def handler(request: httpx.Request) -> httpx.Response:
        if "hooks.example.test" in str(request.url):
            return httpx.Response(204)
        return _gitea_handler(asset_status=[201])(request)

    _use_transport(monkeypatch, handler)
    outcomes = _deliver(settings, report_id, "email", "webhook", "gitea")
    assert outcomes == {"email": "failed", "webhook": "ok", "gitea": "ok"}
    assert _report(report_id).delivery_state == "failed"


def test_reporter_text_is_redacted_before_it_leaves(
    app: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pasted credentials never reach the Gitea issue or the email."""
    _configure_smtp()
    with session_scope(get_context().session_factory) as session:
        row = session.get(NotificationSettings, 1)
        assert row is not None
        row.smtp_password = encrypt_secret("Smtp-Pasted-S3cret-11")
    settings = _settings(tmp_path, email_to=["maint@example.test"])
    text = (
        "error: rtsp://admin:Uri-Pasted-S3cret-22@10.0.0.5/ failed; "
        "relay said Smtp-Pasted-S3cret-11"
    )
    report_id = _file(settings, description=text, title="pw Smtp-Pasted-S3cret-11")
    seen = _use_transport(monkeypatch, _gitea_handler(asset_status=[201]))
    smtp = MagicMock()
    with patch("smtplib.SMTP", return_value=smtp):
        assert _deliver(settings, report_id, "email", "gitea") == {
            "email": "ok",
            "gitea": "ok",
        }
    create = next(r for r in seen if r.url.path.endswith("/issues"))
    issue = create.content.decode()
    mail = _sent_email(smtp).as_string()
    for leaked in ("Uri-Pasted-S3cret-22", "Smtp-Pasted-S3cret-11"):
        assert leaked not in issue
        assert leaked not in mail
    assert "rtsp://***@10.0.0.5/" in json.loads(issue)["body"]


def test_issue_link_comes_from_config_not_the_response(
    app: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hostile ``html_url`` (e.g. ``javascript:``) is never stored or rendered."""
    settings = _settings(tmp_path)
    report_id = _file(settings)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/issues"):
            return httpx.Response(
                201, json={"number": 42, "html_url": "javascript:alert(1)"}
            )
        if request.url.path.endswith("/assets"):
            return httpx.Response(201, json={})
        return httpx.Response(200, json=[])

    _use_transport(monkeypatch, handler)
    assert _deliver(settings, report_id, "gitea") == {"gitea": "ok"}
    report = _report(report_id)
    assert report.gitea_issue_url == (
        "https://git.example.test/alice.smith/timelapse-manager/issues/42"
    )
    assert "javascript" not in json.dumps(report.deliveries)


@pytest.mark.parametrize("url", ["javascript:alert(1)", "ftp://x", "https://"])
def test_gitea_base_url_must_be_http(url: str) -> None:
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError):
        ReportingGiteaSettings(base_url=url)


def _run_private_loop(coro: Any) -> Any:
    """Run ``coro`` on a fresh loop without touching the thread's default loop.

    ``asyncio.run`` clears the default loop afterwards, which breaks later
    tests in the same worker that call ``asyncio.get_event_loop()``.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_anonymized_report_delivers_the_anonymized_text(
    app: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#21: email and Gitea send what is stored, which is the anonymized text."""
    from timelapse_manager.db.models import Camera, Project

    _configure_smtp()
    with session_scope(get_context().session_factory) as session:
        camera = Camera(name="Backyard Cam 7", address="10.0.16.71")
        session.add(camera)
        session.flush()
        session.add(Project(camera_id=camera.id, name="Roofline Orchard"))
    settings = _settings(
        tmp_path,
        email_to=["maint@example.test"],
        github=ReportingGithubSettings(enabled=True),
    )
    report_id = _file(
        settings,
        title="Backyard Cam 7 is down",
        description="Roofline Orchard stopped; camera at 10.0.16.71.",
    )
    report = _report(report_id)
    assert report.anonymized is True
    seen = _use_transport(monkeypatch, _gitea_handler(asset_status=[201]))
    smtp = MagicMock()
    with patch("smtplib.SMTP", return_value=smtp):
        assert _deliver(settings, report_id, "email", "gitea") == {
            "email": "ok",
            "gitea": "ok",
        }
    issue = next(r for r in seen if r.url.path.endswith("/issues")).content.decode()
    mail = _sent_email(smtp).as_string()
    for leaked in ("Backyard Cam 7", "Roofline Orchard", "10.0.16.71"):
        assert leaked not in issue
        assert leaked not in mail
    # The reporter's own words carry kind-less placeholders (review finding 4).
    assert "name-1" in json.loads(issue)["title"]
    # The reporter is their placeholder in every delivery (review finding 8).
    assert "by `user-" in json.loads(issue)["body"]
    assert "Filed by: user-" in mail
    assert _report(report_id).username.startswith("user-")


# ---------------------------------------------------------------------------
# Event loop (#48)
# ---------------------------------------------------------------------------


def test_delivery_runs_no_database_work_on_the_event_loop(
    app: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every destination reads and records in worker threads, never on the loop.

    Delivery runs as a background task on the event loop, so a statement there
    (the Gitea issue record, the SMTP settings, the webhook signing secret)
    would stall every request while SQLite's write lock is held.
    """
    _configure_smtp()
    monkeypatch.setattr(
        "timelapse_manager.render.post_actions.validate_outbound_url_async",
        _pass_through_url,
    )
    settings = _settings(
        tmp_path,
        email_to=["maint@example.test"],
        webhook_url="https://hooks.example.test/report",
    )
    report_id = _file(settings)

    def handler(request: httpx.Request) -> httpx.Response:
        if "hooks.example.test" in str(request.url):
            return httpx.Response(204)
        return _gitea_handler(asset_status=[201])(request)

    _use_transport(monkeypatch, handler)
    on_loop: list[str] = []

    def record_loop_statement(*args: Any) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        on_loop.append(str(args[2]))

    engine = get_context().session_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", record_loop_statement)
    try:
        with patch("smtplib.SMTP", return_value=MagicMock()):
            outcomes = _deliver(settings, report_id, "email", "webhook", "gitea")
    finally:
        event.remove(engine, "before_cursor_execute", record_loop_statement)

    assert outcomes == {"email": "ok", "webhook": "ok", "gitea": "ok"}
    assert on_loop == []
    assert _report(report_id).gitea_issue_number == 42


async def _pass_through_url(url: str) -> str:
    """Stand-in for the async SSRF seam: the tests post to example hosts."""
    return url
