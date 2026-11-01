"""Notification-settings routes."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
)
from sqlalchemy.orm import Session as DbSession

from ...db.models import NotificationSettings, User
from ...monitoring.settings_service import (
    MASK_SENTINEL,
    load_settings,
)
from ...services import notifications as notification_service
from ...services._base import Actor
from ...services.errors import AppError
from .. import dependencies as deps
from ..dependencies import (
    AdminUser,
    DbDep,
    FormDep,
    templates,
)
from ._shared import (
    _parse_lines,
)

logger = logging.getLogger(__name__)

router = APIRouter()


_NOTIFY_CHANNELS = ("email", "webhook")

# The notification settings singleton's fixed primary key (CheckConstraint id=1).
_ROW_ID = 1


def _settings_page_context(
    request: Request, db: DbSession, user: User, **extra: Any
) -> dict[str, Any]:
    """Build the notification-settings page context (secrets shown masked).

    Shared by the GET render and the (unreachable-by-construction) error re-render,
    so both surface an identical, always-masked view of the settings. The webhook
    signing secret is not carried by the display view, so its presence is read
    directly here to mask the field and tell the operator whether one is stored --
    never echoing the plaintext.
    """
    view = load_settings(db)
    row = db.get(NotificationSettings, _ROW_ID)
    webhook_signing_secret_set = bool(row and row.webhook_signing_secret)
    return deps.base_context(
        request,
        db,
        user,
        settings=view,
        channels=_NOTIFY_CHANNELS,
        security_modes=("none", "starttls", "tls"),
        webhook_signing_secret_set=webhook_signing_secret_set,
        webhook_signing_secret=MASK_SENTINEL if webhook_signing_secret_set else "",
        **extra,
    )


@router.get("/notification-settings", response_class=HTMLResponse)
def notification_settings_page(
    request: Request, db: DbDep, user: AdminUser
) -> Response:
    """Render the admin notification settings form (secrets shown masked)."""
    return templates.TemplateResponse(
        request,
        "notification_settings.html",
        _settings_page_context(request, db, user),
    )


@router.post("/notification-settings")
def notification_settings_submit(
    request: Request, db: DbDep, user: AdminUser, form: FormDep
) -> Response:
    """Persist notification settings, honouring the masked-password keep rule.

    A blank / unchanged / masked SMTP password or webhook signing secret leaves
    the stored secret intact; only a genuinely new value overwrites it. The
    notification dispatcher notices the change on its next poll and rebuilds its
    channels, so no restart is needed; this records an audited intent. The
    submitted secrets are never logged. Parses the form, calls the
    service, then commits and redirects; a service-layer error rolls back the
    partial write and re-renders the form inline at the error's status.
    """
    spec = notification_service.NotificationSettingsSpec(
        enabled_channels=[c for c in _NOTIFY_CHANNELS if form.get(f"channel_{c}")],
        smtp_server=form.get("smtp_server") or None,
        smtp_port=_parse_int(form.get("smtp_port")),
        smtp_security=form.get("smtp_security", ""),
        smtp_username=form.get("smtp_username") or None,
        smtp_password=form.get("smtp_password"),
        smtp_from_address=form.get("smtp_from_address") or None,
        smtp_recipients=_parse_lines(form.get("smtp_recipients")),
        webhook_urls=_parse_lines(form.get("webhook_urls")),
        routing_rules=_parse_routing_rules(form.get("routing_rules")),
        webhook_signing_secret=form.get("webhook_signing_secret"),
    )
    try:
        notification_service.update_notification_settings(db, Actor.of(user), spec)
    except AppError as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "notification_settings.html",
            _settings_page_context(
                request,
                db,
                user,
                flash_messages=[{"type": "error", "message": exc.detail}],
            ),
            status_code=exc.status,
        )
    db.commit()
    return RedirectResponse(url="/notification-settings", status_code=303)


def _parse_int(value: str | None) -> int | None:
    """Parse an optional integer form field, tolerating blanks and junk."""
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_routing_rules(value: str | None) -> list[dict[str, Any]]:
    """Parse the routing-rules JSON textarea, tolerating an empty/invalid body.

    An empty or malformed value yields an empty rule list rather than raising, so
    a save with no rules simply routes nothing. Only list-of-dict shapes are kept.
    """
    if not value or not value.strip():
        return []

    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [rule for rule in parsed if isinstance(rule, dict)]
