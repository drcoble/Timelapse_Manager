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

from ...monitoring.settings_service import (
    NotificationSettingsUpdate,
    load_settings,
    update_settings,
)
from .. import dependencies as deps
from ..dependencies import (
    AdminUser,
    DbDep,
    FormDep,
    templates,
)
from ._shared import (
    _audit,
    _parse_lines,
)

logger = logging.getLogger(__name__)

router = APIRouter()


_NOTIFY_CHANNELS = ("email", "webhook")


@router.get("/notification-settings", response_class=HTMLResponse)
def notification_settings_page(
    request: Request, db: DbDep, user: AdminUser
) -> Response:
    """Render the admin notification settings form (password shown masked)."""
    view = load_settings(db)
    return templates.TemplateResponse(
        request,
        "notification_settings.html",
        deps.base_context(
            request,
            db,
            user,
            settings=view,
            channels=_NOTIFY_CHANNELS,
            security_modes=("none", "starttls", "tls"),
        ),
    )


@router.post("/notification-settings")
def notification_settings_submit(
    request: Request, db: DbDep, user: AdminUser, form: FormDep
) -> Response:
    """Persist notification settings, honouring the masked-password keep rule.

    A blank / unchanged / masked SMTP password leaves the stored secret intact;
    only a genuinely new value overwrites it (enforced in the settings service).
    Transport configuration changes take effect on the next restart; this records
    an audited intent. The submitted password is never logged.
    """
    update = NotificationSettingsUpdate(
        enabled_channels=[c for c in _NOTIFY_CHANNELS if form.get(f"channel_{c}")],
        smtp_server=form.get("smtp_server") or None,
        smtp_port=_parse_int(form.get("smtp_port")),
        smtp_security=form.get("smtp_security", "none"),
        smtp_username=form.get("smtp_username") or None,
        smtp_password=form.get("smtp_password"),
        smtp_from_address=form.get("smtp_from_address") or None,
        smtp_recipients=_parse_lines(form.get("smtp_recipients")),
        webhook_urls=_parse_lines(form.get("webhook_urls")),
        routing_rules=_parse_routing_rules(form.get("routing_rules")),
    )
    update_settings(db, update)
    _audit(
        db,
        scope="system",
        scope_id=None,
        actor_user_id=user.id,
        message="notification settings updated",
    )
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
