"""Read and update the singleton notification settings row.

This is the CRUD seam over the single ``notification_settings`` row (its primary
key is constrained to ``1``). It exists to enforce one correctness rule the web
form alone cannot: the SMTP password is **masked** when read for display and
must **not** be overwritten with the mask when an unchanged form is saved.

Secret handling and encryption at rest
--------------------------------------
This service is the single persistence seam for the notification secrets, and it
encrypts them at rest. Two secrets are protected:

* **SMTP password** -- encrypted on write and never returned in plaintext from
  the display path. :func:`load_settings` still returns the mask sentinel for
  display; :func:`resolve_smtp_password` is the separate, explicit resolver that
  decrypts the stored secret for the SMTP send.
* **Webhook URLs** -- encrypted on write (a URL may embed a credential in its
  userinfo or a query token) and decrypted in the display view, because the
  settings form round-trips the URLs verbatim. The decrypted-at-use webhook
  channel is built by :func:`build_webhook_channel`, which runs the outbound-URL
  validation seam on each decrypted URL.

Encryption uses a versioned ``enc:v1:`` ciphertext prefix, so a legacy plaintext
value (written before encryption existed) is recognised on read and returned
unchanged, then silently re-encrypted the next time its field is written. No data
migration is required.

The masked write-back rule
--------------------------
:func:`load_settings` returns the password as the mask sentinel ``"***"`` when a
password is stored (and an empty string when none is). :func:`update_settings`
treats an empty / unchanged / sentinel-valued submitted password as "leave the
stored secret alone": only a genuinely new value overwrites it (and only that
new value is encrypted -- the keep-stored branch returns the already-encrypted
ciphertext untouched, so it is never double-wrapped). Without this, a naive save
of the displayed (masked) form would clobber the real password with the literal
mask and silently break delivery.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..db.models import NotificationSettings
from ..security.crypto import decrypt_secret, encrypt_secret

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from .channels import NotificationChannel

logger = logging.getLogger(__name__)

# The sentinel a stored password is masked to for display, and the value the
# update path recognises as "unchanged -- keep the stored secret".
MASK_SENTINEL = "***"

# The singleton row's fixed primary key.
_ROW_ID = 1

# SMTP security modes the form may submit; anything else falls back to "none".
_SECURITY_MODES = ("none", "tls", "starttls")


@dataclass(frozen=True)
class NotificationSettingsView:
    """A display-safe projection of the notification settings row.

    The SMTP password is never the real secret here: it is :data:`MASK_SENTINEL`
    when one is stored and an empty string when none is. Webhook URLs are kept
    verbatim (no whole-URL masking, so the form round-trips), but any credential
    embedded in a URL's userinfo is the caller's responsibility to redact for
    display via :func:`timelapse_manager.logging.redact_text`.
    """

    enabled_channels: list[str] = field(default_factory=list)
    smtp_server: str = ""
    smtp_port: int | None = None
    smtp_security: str = "none"
    smtp_username: str = ""
    smtp_password: str = ""  # masked: MASK_SENTINEL if set, "" if not
    smtp_password_set: bool = False
    smtp_from_address: str = ""
    smtp_recipients: list[str] = field(default_factory=list)
    webhook_urls: list[str] = field(default_factory=list)
    routing_rules: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class NotificationSettingsUpdate:
    """The fields an admin may submit to update notification settings.

    ``smtp_password`` follows the masked write-back rule: ``None``, an empty
    string, or :data:`MASK_SENTINEL` all mean "keep the stored secret". Only a
    different, non-empty value replaces it.
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


def load_settings(session: Session) -> NotificationSettingsView:
    """Return the current settings as a display-safe view (masking the password).

    A missing singleton row yields an all-default view, so the settings page
    renders cleanly on a fresh install with nothing configured yet.
    """
    row = session.get(NotificationSettings, _ROW_ID)
    if row is None:
        return NotificationSettingsView()
    has_password = bool(row.smtp_password)
    return NotificationSettingsView(
        enabled_channels=_as_str_list(row.enabled_channels),
        smtp_server=row.smtp_server or "",
        smtp_port=row.smtp_port,
        smtp_security=row.smtp_security or "none",
        smtp_username=row.smtp_username or "",
        smtp_password=MASK_SENTINEL if has_password else "",
        smtp_password_set=has_password,
        smtp_from_address=row.smtp_from_address or "",
        smtp_recipients=_as_str_list(row.smtp_recipients),
        # Webhook URLs are stored encrypted (a URL can embed a credential) but the
        # form round-trips them verbatim, so the display view decrypts them.
        # Legacy plaintext URLs pass through decrypt_secret unchanged.
        webhook_urls=[decrypt_secret(u) for u in _as_str_list(row.webhook_urls)],
        routing_rules=_as_dict_list(row.routing_rules),
    )


def update_settings(
    session: Session, update: NotificationSettingsUpdate
) -> NotificationSettingsView:
    """Apply an update to the singleton row (creating it on first write).

    Implements the masked write-back rule for the SMTP password: the stored
    secret is preserved unless the submitted value is genuinely new. Returns the
    refreshed, masked view. The caller's surrounding transaction commits.
    """
    row = session.get(NotificationSettings, _ROW_ID)
    if row is None:
        row = NotificationSettings(id=_ROW_ID)
        session.add(row)

    row.enabled_channels = list(update.enabled_channels)
    row.smtp_server = update.smtp_server or None
    row.smtp_port = update.smtp_port
    row.smtp_security = (
        update.smtp_security if update.smtp_security in _SECURITY_MODES else "none"
    )
    row.smtp_username = update.smtp_username or None
    row.smtp_password = _resolve_password(update.smtp_password, row.smtp_password)
    row.smtp_from_address = update.smtp_from_address or None
    row.smtp_recipients = [r for r in update.smtp_recipients if r]
    # Webhook URLs always arrive as plaintext from the form (no keep-stored
    # rule), so the submitted list is always re-encrypted -- never double-wrapped.
    row.webhook_urls = [encrypt_secret(u) for u in update.webhook_urls if u]
    row.routing_rules = list(update.routing_rules)

    session.flush()
    return load_settings(session)


def _resolve_password(submitted: str | None, stored: str | None) -> str | None:
    """Return the password to persist, honouring the masked write-back rule.

    A blank submission or the mask sentinel means "the admin did not type a new
    password" -- keep whatever is stored (already-encrypted) ciphertext untouched.
    Any other value is a genuine change and is encrypted before storage. Only the
    new-value branch encrypts, so an unchanged secret is never double-wrapped.
    """
    if submitted is None or submitted == "" or submitted == MASK_SENTINEL:
        return stored
    return encrypt_secret(submitted)


def resolve_smtp_password(session: Session) -> str | None:
    """Return the decrypted stored SMTP password for transport use, or ``None``.

    This is the decrypt-at-use seam for the SMTP send: the display path
    (:func:`load_settings`) only ever returns the mask sentinel, never plaintext.
    A legacy plaintext password passes through unchanged. Returns ``None`` when no
    row or no password is stored. The result is never logged.
    """
    row = session.get(NotificationSettings, _ROW_ID)
    if row is None or not row.smtp_password:
        return None
    return decrypt_secret(row.smtp_password)


def resolve_webhook_urls(session: Session) -> list[str]:
    """Return the decrypted stored webhook URLs, or an empty list.

    The decrypt-at-use seam for the webhook channel. Legacy plaintext URLs pass
    through unchanged. Decrypted URLs are never logged; the caller
    (:func:`build_webhook_channel`) still validates each via the outbound-URL seam.
    """
    row = session.get(NotificationSettings, _ROW_ID)
    if row is None:
        return []
    return [decrypt_secret(u) for u in _as_str_list(row.webhook_urls)]


def build_webhook_channel(
    session: Session, *, send_timeout_seconds: float
) -> NotificationChannel | None:
    """Build the webhook channel from decrypted stored URLs, or ``None`` if none.

    Decrypts the stored webhook URLs at use (never logging them) and constructs a
    :class:`~timelapse_manager.monitoring.channels.webhook.WebhookChannel`, which
    runs the outbound-URL validation seam on each decrypted URL at send time.
    Returns ``None`` when no URL is configured, so a half-configured row yields no
    channel.
    """
    from .channels.webhook import WebhookChannel

    urls = [u for u in resolve_webhook_urls(session) if u]
    if not urls:
        return None
    return WebhookChannel(urls, send_timeout_seconds=send_timeout_seconds)


def _as_str_list(value: Any) -> list[str]:
    """Coerce a stored JSON value into a list of non-empty strings."""
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item]


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    """Coerce a stored JSON value into a list of dicts (routing rules)."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
