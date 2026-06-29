"""SMTP email notification channel.

Delivers a notification as a plain-text email through a configured SMTP server.
The channel is constructed once at startup from the stored notification settings;
changing the server configuration in the UI takes effect on the next restart (the
dispatcher does not hot-reload channel transport configuration -- only the routing
rules are re-read per poll cycle).

Two timeout layers protect shutdown (see the channel contract): the dispatcher
wraps :meth:`send` in an outer ``asyncio.wait_for``, but the blocking
``smtplib`` calls run inside a worker thread and are *not* interruptible by that
cancellation. The channel therefore sets its own socket ``timeout`` on every
connection, which is the only bound on a wedged synchronous send.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import TYPE_CHECKING

from .base import ChannelSendError, NotificationChannel, NotificationMessage

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

# The stable channel identifier matched against routing rules.
_CHANNEL_NAME = "email"


@dataclass(frozen=True)
class SMTPConfig:
    """Immutable SMTP transport configuration read once from settings.

    The password is held in memory for the channel's lifetime but is never
    logged: every log line in this module names the server/recipients only, and
    the password is passed straight to ``smtplib`` without interpolation into a
    message string.
    """

    server: str
    port: int
    security: str
    username: str | None
    password: str | None
    from_address: str
    recipients: tuple[str, ...]


class SMTPChannel(NotificationChannel):
    """Sends notifications as plain-text email over SMTP.

    Recoverable transport failures (connection refused, timeout, an SMTP error
    response) are surfaced as :class:`ChannelSendError` so the dispatcher retries
    with backoff. The blocking ``smtplib`` work runs in a worker thread via
    :func:`asyncio.to_thread` so it does not block the event loop, and each
    connection carries the configured socket timeout so a hung send cannot wedge
    shutdown.
    """

    def __init__(self, config: SMTPConfig, *, send_timeout_seconds: float) -> None:
        """Create the channel from a resolved configuration.

        :param config: the SMTP transport configuration.
        :param send_timeout_seconds: the per-connection socket timeout. This is
            the only bound on a blocking synchronous send (the dispatcher's outer
            ``wait_for`` cannot interrupt a thread), so it is mandatory.
        """
        self._config = config
        self._timeout = max(0.1, send_timeout_seconds)

    @property
    def name(self) -> str:
        """Return the stable channel identifier used by routing rules."""
        return _CHANNEL_NAME

    async def send(self, message: NotificationMessage) -> None:
        """Deliver ``message`` as an email; offloads blocking I/O to a thread.

        :param message: the alert to deliver.
        :raises ChannelSendError: on any recoverable SMTP/transport failure.
        """
        email = self._build_email(message)
        try:
            await asyncio.to_thread(self._send_blocking, email)
        except (OSError, smtplib.SMTPException) as exc:
            # Surface only the exception class name -- never the message body or
            # any credential -- to the dispatcher's failure record.
            raise ChannelSendError(
                f"SMTP delivery failed: {exc.__class__.__name__}"
            ) from exc

    def _build_email(self, message: NotificationMessage) -> EmailMessage:
        """Render a notification into a plain-text :class:`EmailMessage`."""
        email = EmailMessage()
        email["Subject"] = self._subject(message)
        email["From"] = self._config.from_address
        email["To"] = ", ".join(self._config.recipients)
        email.set_content(self._body(message))
        return email

    @staticmethod
    def _subject(message: NotificationMessage) -> str:
        """Build a concise subject line from the event level and type."""
        kind = message.event_type or "event"
        return f"[Timelapse Manager] {message.level.upper()}: {kind}"

    @staticmethod
    def _body(message: NotificationMessage) -> str:
        """Build the plain-text email body.

        The message is already redacted at the source (``log_event`` scrubs it
        before persisting), so it is safe to include verbatim.
        """
        lines = [
            message.message,
            "",
            f"Level: {message.level}",
            f"Scope: {message.scope}"
            + (f" (id {message.scope_id})" if message.scope_id is not None else ""),
            f"Time: {message.timestamp.isoformat()} UTC",
        ]
        if message.event_type:
            lines.insert(1, f"Type: {message.event_type}")
        return "\n".join(lines)

    def _send_blocking(self, email: EmailMessage) -> None:
        """Connect, optionally authenticate, and send. Runs in a worker thread.

        Every connection is opened with the channel's socket timeout. STARTTLS
        and implicit-TLS (SMTPS) variants are selected by the configured
        security mode; ``none`` connects in the clear (for a local relay).
        """
        if self._config.security == "tls":
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                self._config.server, self._config.port, timeout=self._timeout
            )
        else:
            client = smtplib.SMTP(
                self._config.server, self._config.port, timeout=self._timeout
            )
        try:
            if self._config.security == "starttls":
                client.starttls()
            if self._config.username and self._config.password:
                client.login(self._config.username, self._config.password)
            client.send_message(email)
        finally:
            try:
                client.quit()
            except smtplib.SMTPException:
                # A best-effort close; the message was already sent (or the send
                # raised and is the error we care about).
                client.close()


def build_smtp_config(
    *,
    server: str | None,
    port: int | None,
    security: str | None,
    username: str | None,
    password: str | None,
    from_address: str | None,
    recipients: Sequence[str] | None,
) -> SMTPConfig | None:
    """Build an :class:`SMTPConfig` from stored settings, or ``None`` if unusable.

    Returns ``None`` when the minimum required fields (server, from address, at
    least one recipient) are missing, so a half-configured row yields no channel
    rather than a channel that always fails. The port defaults to the standard
    SMTPS/submission port for the selected security mode.
    """
    if not server or not from_address:
        return None
    clean_recipients = tuple(r for r in (recipients or []) if r)
    if not clean_recipients:
        return None
    mode = security if security in ("none", "tls", "starttls") else "none"
    resolved_port = port if port else (465 if mode == "tls" else 587)
    return SMTPConfig(
        server=server,
        port=resolved_port,
        security=mode,
        username=username or None,
        password=password or None,
        from_address=from_address,
        recipients=clean_recipients,
    )
