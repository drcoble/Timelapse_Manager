"""Deliver a stored report to email, a webhook, and Gitea.

:func:`deliver_report` runs after the report is stored, off the request path
(a background task for the web form and API, or an admin's resend). Each
destination is independent: one failing never stops the others, and every
outcome is written to the row's ``deliveries`` map. Recorded errors are a class
name or an HTTP status only -- never a URL, a response body or a token.

* **Email** goes through the SMTP server configured under notification
  settings, to ``reporting.email_to``. The zip is attached when it is under
  ``email_attach_max_bytes``; otherwise the message carries a download link.
* **Webhook** POSTs a JSON summary to ``reporting.webhook_url`` through the
  same SSRF guard as the notification webhooks, signed with the notification
  webhook signing secret when one is set.
* **Gitea** creates an issue in ``reporting.gitea.repo`` and uploads the zip
  as an issue attachment. The token is read from ``token_file`` at send time.
  The issue number is stored as soon as it exists, so a retry after a failed
  upload re-uploads to the same issue instead of filing a second one.

Every HTTP call carries ``User-Agent: timelapse-manager/<version>`` (some Git servers
reject the default Python user agents), a timeout, and never follows
redirects.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

import httpx
from sqlalchemy.orm import Session, sessionmaker

from .. import __version__
from ..config import Settings
from ..db.models import Project, Report
from ..db.session import session_scope
from ..logging import redact_text
from ..version import get_build_info
from .redaction import Scrubber, collect_secrets
from .store import AREAS, set_delivery, zip_path_for

logger = logging.getLogger(__name__)

USER_AGENT = f"timelapse-manager/{__version__}"

_ZIP_TYPE = "application/zip"


class DeliveryError(Exception):
    """A destination failed; ``str()`` is safe to store (no URL or token)."""


@dataclass(frozen=True)
class ReportView:
    """A detached copy of the fields delivery needs (no live ORM row)."""

    id: int
    key: str
    title: str
    description: str
    area: str
    username: str | None
    project_id: int | None
    project_name: str | None
    camera_id: int | None
    reported_at: str
    created_at: str
    zip_name: str
    zip_bytes: bytes | None
    gitea_issue_number: int | None
    gitea_issue_url: str | None
    gitea_attachment_ok: bool


def _http_client(timeout: float) -> httpx.AsyncClient:
    """Build the HTTP client deliveries use. Tests replace this."""
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
    )


def _webhook_client(timeout: float) -> httpx.AsyncClient:
    """Build the client for the report webhook. Tests replace this.

    Like :func:`_http_client`, but its transport connects only to the address
    the SSRF guard validated (a rebinding DNS answer cannot redirect the
    socket), and it ignores environment proxies, which would bypass that.
    """
    from ..security.pinned_http import pinned_async_transport

    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
        transport=pinned_async_transport(),
        trust_env=False,
    )


def download_url(settings: Settings, key: str) -> str | None:
    """The admin download link for a report, when the public URL is set."""
    base = settings.reporting.public_base_url
    if not base:
        return None
    return f"{base.rstrip('/')}/settings/reports/{key}/download"


def _load_view(
    session_factory: sessionmaker[Session], settings: Settings, report_id: int
) -> ReportView | None:
    with session_scope(session_factory) as session:
        report = session.get(Report, report_id)
        if report is None:
            return None
        project = (
            session.get(Project, report.project_id)
            if report.project_id is not None
            else None
        )
        path = zip_path_for(settings, report)
        try:
            data = path.read_bytes() if path is not None else None
        except OSError:
            data = None
        gitea = (report.deliveries or {}).get("gitea") or {}
        # The reporter's own words leave the box in the email, webhook and
        # issue body, so they get the same redaction as the zip: URL
        # credentials by pattern, then every known secret by value.
        scrubber = Scrubber(collect_secrets(session, settings))

        def scrub(text: str) -> str:
            return scrubber.scrub_text(redact_text(text))

        return ReportView(
            id=report.id,
            key=report.report_key,
            title=scrub(report.title),
            description=scrub(report.description),
            area=report.area,
            username=report.username,
            project_id=report.project_id,
            project_name=project.name if project is not None else None,
            camera_id=report.camera_id,
            reported_at=report.reported_at.isoformat(),
            created_at=report.created_at.isoformat(),
            zip_name=f"{report.report_key}.zip",
            zip_bytes=data,
            gitea_issue_number=report.gitea_issue_number,
            gitea_issue_url=report.gitea_issue_url,
            gitea_attachment_ok=gitea.get("attachment") == "ok",
        )


def summary_lines(view: ReportView, settings: Settings) -> list[str]:
    """Short "key: value" lines describing a report, shared by every channel."""
    build = get_build_info()
    project = (
        f"#{view.project_id} {view.project_name}"
        if view.project_id is not None
        else "none"
    )
    return [
        f"Report: {view.key}",
        f"Title: {view.title}",
        f"Filed by: {view.username or 'unknown'}",
        f"Area: {AREAS.get(view.area, view.area)}",
        f"Project: {project}",
        f"When: {view.reported_at} UTC",
        f"Version: {__version__} (build {build['sha']} {build['date']})",
    ]


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def _smtp_channel(session_factory: sessionmaker[Session], settings: Settings) -> Any:
    """Build an SMTP channel from notification settings, to ``email_to``."""
    from ..monitoring.channels.smtp import SMTPChannel, build_smtp_config
    from ..monitoring.settings_service import load_settings, resolve_smtp_password

    with session_scope(session_factory) as session:
        view = load_settings(session)
        config = build_smtp_config(
            server=view.smtp_server or None,
            port=view.smtp_port,
            security=view.smtp_security,
            username=view.smtp_username or None,
            password=resolve_smtp_password(session),
            from_address=view.smtp_from_address or None,
            recipients=settings.reporting.email_to,
        )
    if config is None:
        raise DeliveryError("SMTP server not configured")
    return SMTPChannel(
        config, send_timeout_seconds=settings.reporting.delivery_timeout_seconds
    )


async def send_email(
    session_factory: sessionmaker[Session], settings: Settings, view: ReportView
) -> dict[str, Any]:
    """Email the report summary, attaching the zip when it is small enough."""
    from ..monitoring.channels.base import ChannelSendError

    # The SMTP settings and password come from the database: read them in a
    # worker thread, never on the event loop (#48).
    channel = await asyncio.to_thread(_smtp_channel, session_factory, settings)
    email = EmailMessage()
    email["Subject"] = f"[Timelapse Manager] Problem report {view.key}: {view.title}"
    lines = summary_lines(view, settings)
    lines += ["", "What happened:", view.description or "(no description)", ""]
    cap = settings.reporting.email_attach_max_bytes
    attached = view.zip_bytes is not None and len(view.zip_bytes) <= cap
    link = download_url(settings, view.key)
    if attached:
        lines.append(f"The redacted diagnostic snapshot is attached ({view.zip_name}).")
    elif link:
        lines.append(f"Download the diagnostic snapshot (admin login): {link}")
    else:
        lines.append(
            "Download the diagnostic snapshot from Settings > Problem reports."
        )
    email.set_content("\n".join(lines))
    if attached and view.zip_bytes is not None:
        email.add_attachment(
            view.zip_bytes,
            maintype="application",
            subtype="zip",
            filename=view.zip_name,
        )
    try:
        await channel.send_email(email)
    except ChannelSendError as exc:
        raise DeliveryError(str(exc)) from exc
    return {"attached": attached}


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


def _webhook_signing_secret(session_factory: sessionmaker[Session]) -> str | None:
    """The notification webhook signing secret, read in its own session."""
    from ..monitoring.settings_service import resolve_webhook_signing_secret

    with session_scope(session_factory) as session:
        return resolve_webhook_signing_secret(session)


async def send_webhook(
    session_factory: sessionmaker[Session], settings: Settings, view: ReportView
) -> dict[str, Any]:
    """POST a signed JSON summary (no snapshot content) to the webhook URL."""
    from ..monitoring.channels.webhook import SIGNATURE_HEADER, sign_body
    from ..render.post_actions import validate_outbound_url_async
    from ..security.ssrf import SsrfError

    url = settings.reporting.webhook_url
    if not url:
        raise DeliveryError("webhook not configured")
    try:
        target = await validate_outbound_url_async(url)
    except SsrfError as exc:
        raise DeliveryError("webhook URL blocked by the SSRF guard") from exc
    secret = await asyncio.to_thread(_webhook_signing_secret, session_factory)
    payload = {
        "event_type": "report.filed",
        "report_id": view.key,
        "title": view.title,
        "area": view.area,
        "reporter": view.username,
        "project_id": view.project_id,
        "camera_id": view.camera_id,
        "reported_at": view.reported_at,
        "filed_at": view.created_at,
        "app_version": __version__,
        "download_url": download_url(settings, view.key),
        "gitea_issue_url": view.gitea_issue_url,
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if secret:
        headers[SIGNATURE_HEADER] = sign_body(secret, body)
    async with _webhook_client(settings.reporting.delivery_timeout_seconds) as client:
        try:
            response = await client.post(target, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise DeliveryError(f"webhook failed: {type(exc).__name__}") from exc
    if not response.is_success:
        raise DeliveryError(f"webhook returned HTTP {response.status_code}")
    return {"signed": bool(secret)}


# ---------------------------------------------------------------------------
# Gitea
# ---------------------------------------------------------------------------


def issue_body(view: ReportView, settings: Settings) -> str:
    """The issue body: the reporter's words, a short summary, the report id."""
    build = get_build_info()
    project = (
        f"#{view.project_id} {view.project_name}"
        if view.project_id is not None
        else "none"
    )
    return "\n".join(
        [
            f"Filed from the app's **Report a problem** form. Report `{view.key}`, "
            f"by `{view.username or 'unknown'}`.",
            "",
            "## What happened",
            "",
            view.description.strip() or "_No description given._",
            "",
            "## Summary",
            "",
            f"- **Version:** {__version__} (build `{build['sha']}`, {build['date']})",
            f"- **Area:** {AREAS.get(view.area, view.area)}",
            f"- **Project:** {project}",
            f"- **When:** {view.reported_at} UTC",
            f"- **Filed:** {view.created_at} UTC",
            "",
            f"The redacted diagnostic snapshot is attached as `{view.zip_name}`.",
        ]
    )


def issue_url(settings: Settings, number: int) -> str:
    """The web link of issue ``number``, from the configured base URL and repo."""
    gitea = settings.reporting.gitea
    base = (gitea.base_url or "").rstrip("/")
    return f"{base}/{gitea.repo}/issues/{int(number)}"


def _read_token(settings: Settings) -> str:
    path = settings.reporting.gitea.token_file
    if path is None:
        raise DeliveryError("Gitea token file not configured")
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise DeliveryError(
            f"Gitea token file unreadable: {type(exc).__name__}"
        ) from exc
    if not token:
        raise DeliveryError("Gitea token file is empty")
    return token


async def _label_ids(
    client: httpx.AsyncClient, api: str, headers: dict[str, str], names: list[str]
) -> list[int]:
    """Map label names to the repository's label ids (unknown names skipped)."""
    if not names:
        return []
    try:
        response = await client.get(
            f"{api}/labels", headers=headers, params={"limit": 50}
        )
    except httpx.HTTPError:
        logger.warning("could not list Gitea labels; filing without labels")
        return []
    if not response.is_success:
        logger.warning("Gitea label lookup returned HTTP %s", response.status_code)
        return []
    by_name = {
        str(label.get("name")): int(label["id"])
        for label in response.json()
        if isinstance(label, dict) and "id" in label
    }
    return [by_name[name] for name in names if name in by_name]


def _record_issue(
    session_factory: sessionmaker[Session], report_id: int, number: int, url: str
) -> None:
    with session_scope(session_factory) as session:
        report = session.get(Report, report_id)
        if report is not None:
            report.gitea_issue_number = number
            report.gitea_issue_url = url


async def send_gitea(
    session_factory: sessionmaker[Session], settings: Settings, view: ReportView
) -> dict[str, Any]:
    """Create the issue (once) and upload the zip as an issue attachment."""
    gitea = settings.reporting.gitea
    if not gitea.configured or gitea.base_url is None or gitea.repo is None:
        raise DeliveryError("Gitea delivery not configured")
    token = _read_token(settings)
    api = f"{gitea.base_url.rstrip('/')}/api/v1/repos/{gitea.repo}"
    headers = {"Authorization": f"token {token}", "Accept": "application/json"}
    number = view.gitea_issue_number
    url = view.gitea_issue_url
    async with _http_client(gitea.timeout_seconds) as client:
        try:
            if number is None:
                labels = await _label_ids(client, api, headers, gitea.labels)
                response = await client.post(
                    f"{api}/issues",
                    headers=headers,
                    json={
                        "title": f"[{view.key}] {view.title}",
                        "body": issue_body(view, settings),
                        "labels": labels,
                    },
                )
                if response.status_code != httpx.codes.CREATED:
                    raise DeliveryError(
                        f"Gitea issue create returned HTTP {response.status_code}"
                    )
                number = int(response.json()["number"])
                # The link is built from trusted config, never taken from the
                # response's ``html_url``: admins click it from the report list.
                url = issue_url(settings, number)
                # Stored before the upload (a retry reuses this issue), from a
                # worker thread rather than the event loop (#48).
                await asyncio.to_thread(
                    _record_issue, session_factory, view.id, number, url
                )
            if view.zip_bytes is None:
                raise DeliveryError("report zip missing; issue created without it")
            if not view.gitea_attachment_ok:
                response = await client.post(
                    f"{api}/issues/{number}/assets",
                    headers=headers,
                    params={"name": view.zip_name},
                    files={"attachment": (view.zip_name, view.zip_bytes, _ZIP_TYPE)},
                )
                if response.status_code != httpx.codes.CREATED:
                    raise DeliveryError(
                        f"Gitea attachment upload returned HTTP {response.status_code}"
                    )
        except httpx.HTTPError as exc:
            raise DeliveryError(f"Gitea request failed: {type(exc).__name__}") from exc
        except (KeyError, ValueError, TypeError) as exc:
            raise DeliveryError("Gitea returned an unexpected response") from exc
    return {"issue_number": number, "issue_url": url, "attachment": "ok"}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

_SENDERS = {
    "email": send_email,
    "webhook": send_webhook,
    "gitea": send_gitea,
}


def _record(
    session_factory: sessionmaker[Session],
    report_id: int,
    destination: str,
    status: str,
    detail: dict[str, Any],
) -> None:
    with session_scope(session_factory) as session:
        report = session.get(Report, report_id)
        if report is None:
            return
        if destination == "gitea" and status == "failed":
            # Keep the issue link from a partly successful attempt.
            previous = (report.deliveries or {}).get("gitea") or {}
            if report.gitea_issue_number is not None:
                detail.setdefault("issue_number", report.gitea_issue_number)
                detail.setdefault("attachment", previous.get("attachment", "failed"))
        set_delivery(report, destination, status, **detail)


async def deliver_report(
    session_factory: sessionmaker[Session],
    settings: Settings,
    report_id: int,
    destinations: Iterable[str],
) -> dict[str, str]:
    """Send a stored report to each destination; record and return outcomes.

    Never raises: a failure is recorded on the row (status ``failed`` plus a
    short error) and the report stays stored for an admin to resend.
    """
    outcomes: dict[str, str] = {}
    wanted = [name for name in destinations if name in _SENDERS]
    if not wanted:
        return outcomes
    view = await asyncio.to_thread(_load_view, session_factory, settings, report_id)
    if view is None:
        return outcomes
    for destination in wanted:
        sender = _SENDERS[destination]
        try:
            detail = await asyncio.wait_for(
                sender(session_factory, settings, view),
                timeout=settings.reporting.delivery_timeout_seconds * 3,
            )
            status, info = "ok", dict(detail)
        except DeliveryError as exc:
            status, info = "failed", {"error": str(exc)}
        except TimeoutError:
            status, info = "failed", {"error": "timed out"}
        except Exception as exc:  # noqa: BLE001 - one destination must not stop the rest
            logger.exception("report %s delivery to %s failed", view.key, destination)
            status, info = "failed", {"error": type(exc).__name__}
        if status == "failed":
            logger.warning(
                "report %s delivery to %s failed: %s",
                view.key,
                destination,
                info["error"],
            )
        await asyncio.to_thread(
            _record, session_factory, report_id, destination, status, info
        )
        outcomes[destination] = status
    return outcomes
