"""Monitoring: event logging, notification routing, and asynchronous dispatch.

This package holds the backend of the monitoring and notification subsystem:

* :mod:`.events` -- the ``log_event`` emit helper, the public ``Level`` /
  ``EventType`` taxonomies, and read queries (``get_events`` and the admin-only
  ``get_audit_events``).
* :mod:`.routing` -- ``evaluate_routing_rules``, the pure function that maps an
  ``(event_type, level)`` pair to the set of channel names a notification should
  be delivered through.
* :mod:`.dispatcher` -- ``NotificationDispatcher``, a poll-based asyncio loop
  that reads newly logged events and fans them out to injected channels with
  debounce and bounded retry.
* :mod:`.channels` -- the ``NotificationChannel`` abstract base and the
  ``NotificationMessage`` dataclass that concrete channels (e.g. email, webhook)
  implement against.

The dispatcher never imports a concrete channel: channels are injected at
construction. Logging an event and delivering a notification are decoupled --
:func:`log_event` only persists a row (and emits a structured log line); the
dispatcher delivers asynchronously by polling for new rows.
"""

from __future__ import annotations

from .alerts import (
    ALERT_LEVEL_THRESHOLD,
    RESOLVE_TO_RAISE_REASONS,
    ActiveAlert,
    auto_clear_for_event,
    clear_alert,
    clear_all_alerts,
    get_active_alerts,
)
from .channels import NotificationChannel, NotificationMessage
from .dispatcher import NotificationDispatcher
from .events import (
    EventType,
    Level,
    get_audit_events,
    get_events,
    log_event,
)
from .routing import evaluate_routing_rules

__all__ = [
    "ALERT_LEVEL_THRESHOLD",
    "RESOLVE_TO_RAISE_REASONS",
    "ActiveAlert",
    "EventType",
    "Level",
    "NotificationChannel",
    "NotificationDispatcher",
    "NotificationMessage",
    "auto_clear_for_event",
    "clear_alert",
    "clear_all_alerts",
    "evaluate_routing_rules",
    "get_active_alerts",
    "get_audit_events",
    "get_events",
    "log_event",
]
