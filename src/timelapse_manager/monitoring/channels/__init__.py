"""Notification channels: the delivery-side abstraction for alerts.

A *channel* is a single outbound delivery mechanism (email, webhook, ...). The
:class:`~timelapse_manager.monitoring.channels.base.NotificationChannel` abstract
base defines the contract the dispatcher relies on; concrete channels live in
sibling modules and are injected into the dispatcher at construction so this
package never depends on a particular transport.
"""

from __future__ import annotations

from .base import ChannelSendError, NotificationChannel, NotificationMessage

__all__ = [
    "ChannelSendError",
    "NotificationChannel",
    "NotificationMessage",
]
