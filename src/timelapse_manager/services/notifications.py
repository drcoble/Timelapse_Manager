"""Notification-settings write use-cases -- the single implementation of the rules.

The one write this domain supports is the admin notification-settings save: the
enabled channels, the SMTP transport, the webhook URLs, the routing rules, and the
two encrypted-at-rest secrets (the SMTP password and the webhook signing secret).
The web layer is a thin adapter over :func:`update_notification_settings`: it parses
its form into a :class:`NotificationSettingsSpec`, calls the service, commits, and
redirects.

Secret handling is deliberately left exactly where it already lives. The SMTP
password's masked write-back and encryption-at-rest stay inside
:func:`timelapse_manager.monitoring.settings_service.update_settings`, which this
service calls unchanged. The webhook signing secret is stored on the same singleton
row but is *not* part of that update contract, so its identical masked write-back
(kept stored on a blank / sentinel submission, encrypted only on a genuine change)
is applied here after the settings write -- never changing how the secret is
encrypted, masked, or stored.

This domain is web-only. The acting user is always a real ``AdminUser`` row, so the
audit event's actor foreign key is already satisfied and no sentinel administrator
is materialised (unlike the API-facing domains). Settings changes take effect on the
next restart, so the write implies no post-commit effect: the returned
:class:`~timelapse_manager.services._base.Result` carries empty effects.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..db.models import Event, NotificationSettings
from ..monitoring.settings_service import (
    MASK_SENTINEL,
    NotificationSettingsError,
    NotificationSettingsUpdate,
    NotificationSettingsView,
    update_settings,
)
from ..security.crypto import encrypt_secret
from ._base import Actor, Result
from .errors import ValidationError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# The notification settings singleton's fixed primary key (CheckConstraint id=1).
_ROW_ID = 1


@dataclass
class NotificationSettingsSpec:
    """Normalised, already-parsed inputs for the notification-settings save.

    The web adapter parses its form into this: the enabled channels, the SMTP
    transport fields, the recipient / webhook URL lists, the routing rules, and the
    two submitted secrets. Tolerant form parsing (blank-to-``None``, line splitting,
    routing-rule JSON) stays in the adapter; this carries the resolved values.

    ``smtp_password`` and ``webhook_signing_secret`` both follow the masked
    write-back rule: ``None``, an empty string, or :data:`MASK_SENTINEL` all mean
    "keep the stored secret"; only a different, non-empty value replaces it.
    """

    enabled_channels: list[str]
    smtp_server: str | None
    smtp_port: int | None
    smtp_security: str
    smtp_username: str | None
    smtp_password: str | None
    smtp_from_address: str | None
    smtp_recipients: list[str]
    webhook_urls: list[str]
    routing_rules: list[dict[str, Any]]
    webhook_signing_secret: str | None


def update_notification_settings(
    session: Session, actor: Actor, spec: NotificationSettingsSpec
) -> Result[NotificationSettingsView]:
    """Persist the notification settings, honouring both secrets' keep rules.

    Delegates the settings row write (including the SMTP password's masked
    write-back and encryption at rest) to the monitoring settings service, then
    applies the webhook signing secret's identical masked write-back on the same
    singleton row -- which ``update_settings`` created on first write. A blank /
    unchanged / masked secret leaves the stored ciphertext intact; only a genuinely
    new value is encrypted and overwrites it. Validates (an unknown SMTP security
    mode is a :class:`~.errors.ValidationError`, never coerced to plaintext),
    mutates, and flushes -- but
    does not commit; the caller commits. Settings take effect on the next restart,
    so no post-commit effect is implied.
    """
    try:
        view = update_settings(session, _to_update(spec))
    except NotificationSettingsError as exc:
        raise ValidationError(str(exc), field="smtp_security") from exc

    # The signing secret rides on the same row but is not part of the settings-service
    # update contract, so apply its masked write-back here -- after the settings write,
    # which creates the singleton on first save. Encryption of a genuine new value is
    # done exactly as the SMTP password's, so an unchanged secret is never re-wrapped.
    row = session.get(NotificationSettings, _ROW_ID)
    if row is not None:
        row.webhook_signing_secret = _resolve_secret(
            spec.webhook_signing_secret, row.webhook_signing_secret
        )

    _audit(session, actor, "notification settings updated")
    session.flush()
    return Result(view, ())


def _to_update(spec: NotificationSettingsSpec) -> NotificationSettingsUpdate:
    """Project the spec onto the monitoring settings-service update contract."""
    return NotificationSettingsUpdate(
        enabled_channels=spec.enabled_channels,
        smtp_server=spec.smtp_server,
        smtp_port=spec.smtp_port,
        smtp_security=spec.smtp_security,
        smtp_username=spec.smtp_username,
        smtp_password=spec.smtp_password,
        smtp_from_address=spec.smtp_from_address,
        smtp_recipients=spec.smtp_recipients,
        webhook_urls=spec.webhook_urls,
        routing_rules=spec.routing_rules,
    )


def _resolve_secret(submitted: str | None, stored: str | None) -> str | None:
    """Return the signing secret to persist, honouring the masked write-back rule.

    Mirrors the SMTP password keep-rule: a blank submission or the mask sentinel
    means the admin did not type a new secret, so the stored (already-encrypted)
    ciphertext is kept untouched. Any other value is a genuine change and is
    encrypted before storage, so an unchanged secret is never double-wrapped.
    """
    if submitted is None or submitted == "" or submitted == MASK_SENTINEL:
        return stored
    return encrypt_secret(submitted)


def _audit(session: Session, actor: Actor, message: str) -> None:
    """Append a system-scoped audit event attributed to ``actor``. Never logs secrets.

    The actor is always a real administrator on this web-only surface, so the event's
    actor foreign key is already satisfied and no sentinel user is materialised.
    """
    session.add(
        Event(
            scope="system",
            scope_id=None,
            level="info",
            message=message,
            actor_user_id=actor.user_id,
            timestamp=datetime.datetime.now(datetime.UTC).replace(tzinfo=None),
        )
    )
