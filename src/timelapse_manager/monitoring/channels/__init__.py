"""Notification channels: the delivery-side abstraction for alerts.

A *channel* is a single outbound delivery mechanism (email, webhook, ...). The
:class:`~timelapse_manager.monitoring.channels.base.NotificationChannel` abstract
base defines the contract the dispatcher relies on; concrete channels live in
sibling modules and are injected into the dispatcher at construction so the
dispatcher never depends on a particular transport.

:func:`build_channels` constructs the configured channels from the stored
notification settings row, and :class:`StoredChannelsProvider` lets the
dispatcher rebuild them when that row changes, so editing SMTP or webhook
settings takes effect without a restart (#67 R8/R17). The concrete channel
modules are imported inside the functions: ``webhook`` imports the render
package, which imports this package's parent, so a module-level import would be
circular.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Protocol

from .base import ChannelSendError, NotificationChannel, NotificationMessage

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# The stored columns that determine the channels. Routing rules are not among
# them: the dispatcher re-reads those every poll on its own.
_TRANSPORT_COLUMNS = (
    "enabled_channels",
    "smtp_server",
    "smtp_port",
    "smtp_security",
    "smtp_username",
    "smtp_password",
    "smtp_from_address",
    "smtp_recipients",
    "webhook_urls",
    "webhook_signing_secret",
)

# The version token of an absent settings row (fresh database).
_ABSENT_VERSION = "absent"


def build_channels(
    session: Session, *, send_timeout_seconds: float
) -> list[NotificationChannel]:
    """Construct the enabled channels from the stored notification settings row.

    The channels' transport configuration (SMTP server and credentials, webhook
    URLs) lives in the singleton ``notification_settings`` row; only the per-send
    timeout comes from ``settings.monitoring``. A channel is included only when
    its name is in the row's ``enabled_channels`` list, so an admin can disable a
    channel without clearing its configuration. A half-configured channel is
    left out rather than built to fail every send. Secrets are decrypted here,
    at use, and never logged.

    Synchronous: call it from a worker thread.

    :raises Exception: any database or decryption failure. The caller decides
        what to keep (the dispatcher keeps its current channels).
    """
    from ..settings_service import (
        build_webhook_channel,
        load_settings,
        resolve_smtp_password,
    )
    from .smtp import SMTPChannel, build_smtp_config

    channels: list[NotificationChannel] = []
    view = load_settings(session)
    enabled = set(view.enabled_channels)
    if "email" in enabled:
        smtp_config = build_smtp_config(
            server=view.smtp_server or None,
            port=view.smtp_port,
            security=view.smtp_security,
            username=view.smtp_username or None,
            # The view masks the password; decrypt the stored secret at use.
            password=resolve_smtp_password(session),
            from_address=view.smtp_from_address or None,
            recipients=view.smtp_recipients,
        )
        if smtp_config is not None:
            channels.append(
                SMTPChannel(smtp_config, send_timeout_seconds=send_timeout_seconds)
            )
    if "webhook" in enabled:
        # Each decrypted URL is validated by the channel's outbound-URL seam at
        # send time, not here.
        webhook = build_webhook_channel(
            session, send_timeout_seconds=send_timeout_seconds
        )
        if webhook is not None:
            channels.append(webhook)
    return channels


def channels_version(session: Session) -> str:
    """Return a token that changes whenever the stored transport settings change.

    A SHA-256 over the raw stored transport columns (ciphertexts as stored, so
    nothing is decrypted). ``updated_at`` is not used: SQLite's
    ``CURRENT_TIMESTAMP`` has one-second resolution, so two saves in the same
    second would look identical. A save that re-encrypts an unchanged secret
    changes the token too; that costs one harmless rebuild. Synchronous.
    """
    from ...db.models import NotificationSettings

    row = session.get(NotificationSettings, 1)
    if row is None:
        return _ABSENT_VERSION
    snapshot = {column: getattr(row, column) for column in _TRANSPORT_COLUMNS}
    encoded = json.dumps(snapshot, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ChannelsProvider(Protocol):
    """Supplies the dispatcher's channels and tells it when to rebuild them.

    Both methods are synchronous and run inside the dispatcher's worker-thread
    poll, in the poll's own session.
    """

    def version(self, session: Session) -> str:
        """Return a token that changes when the channels must be rebuilt."""
        ...

    def build(self, session: Session) -> list[NotificationChannel]:
        """Build the channels; raising keeps the dispatcher's current ones."""
        ...


class StoredChannelsProvider:
    """A :class:`ChannelsProvider` backed by the notification settings row."""

    def __init__(self, *, send_timeout_seconds: float) -> None:
        """Create the provider.

        :param send_timeout_seconds: the per-send transport timeout every built
            channel is given (``monitoring.channel_send_timeout_seconds``).
        """
        self._send_timeout_seconds = send_timeout_seconds

    def version(self, session: Session) -> str:
        """Return the stored transport settings' version token."""
        return channels_version(session)

    def build(self, session: Session) -> list[NotificationChannel]:
        """Build the enabled channels from the stored settings row."""
        return build_channels(session, send_timeout_seconds=self._send_timeout_seconds)


__all__ = [
    "ChannelSendError",
    "ChannelsProvider",
    "NotificationChannel",
    "NotificationMessage",
    "StoredChannelsProvider",
    "build_channels",
    "channels_version",
]
