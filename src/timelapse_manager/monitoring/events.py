"""Event logging: the emit helper, the public taxonomies, and read queries.

This module is the single front door for recording an operational or audit event
and for reading events back:

* :func:`log_event` persists an :class:`~timelapse_manager.db.models.Event` row
  (and emits a structured, redacted log line). It never raises into its caller
  and never delivers a notification itself -- the dispatcher does that
  asynchronously by polling for new rows.
* :class:`Level` and :class:`EventType` are the public taxonomies that callers,
  routing rules, and channels share.
* :func:`get_events` is the paginated, filterable read for the operational log.
* :func:`get_audit_events` is the admin-only read restricted to audit/security
  event types.

The event *type* is not a database column. It is stored under the ``"type"`` key
of the event's JSON details (the ORM attribute ``event_metadata``; the column is
named ``metadata``). Routing and the audit query read it back from there.
"""

from __future__ import annotations

import enum
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..db.models import Event
from ..logging import redact, redact_text

if TYPE_CHECKING:
    from ..db.models import User

logger = logging.getLogger(__name__)

# The JSON details key under which the event type is stored (there is no `type`
# column on the event table). Routing and the audit query read it back.
_TYPE_KEY = "type"


class Level(enum.Enum):
    """Severity levels exposed by the public logging API.

    These four are the levels callers and routing rules use. The underlying
    database enum additionally permits ``"debug"`` for very low-severity traces;
    that value is intentionally not part of this public surface, so a level
    floor expressed through :class:`Level` never selects debug rows.
    """

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class EventType(enum.Enum):
    """The catalogue of dotted event-type identifiers used across the system.

    Stored in event details under the ``"type"`` key and matched by routing
    rules. ``NOTIFY_DELIVERY_FAILED`` is special: it records a notification
    delivery failure and is deliberately never routed to a channel (see
    :mod:`timelapse_manager.monitoring.routing`), which prevents a delivery
    failure from triggering another notification attempt.
    """

    CAPTURE_GAP = "capture.gap"
    CAPTURE_STALLED = "capture.stalled"
    CAMERA_RECONNECT = "camera.reconnect"
    CAMERA_OFFLINE_THRESHOLD = "camera.offline_threshold"
    STORAGE_DISK_LOW = "storage.disk_low"
    RENDER_COMPLETE = "render.complete"
    RENDER_FAILED = "render.failed"
    EXPORT_COMPLETE = "export.complete"
    POSTACTION_FAILED = "postaction.failed"
    SECURITY_AUTH_EVENT = "security.auth_event"
    AUDIT_CONTROL_ACTION = "audit.control_action"
    NOTIFY_DELIVERY_FAILED = "notify.delivery_failed"


# Numeric rank for ordering severity. Mirrors the database enum (which includes
# ``debug``) so a level-floor query can translate a floor into the set of
# at-or-above level names -- the level column is a string enum, so an inequality
# comparison would compare lexically, not by severity.
_LEVEL_RANK: dict[str, int] = {
    "debug": 0,
    "info": 1,
    "warning": 2,
    "error": 3,
    "critical": 4,
}

# Event types considered audit/security records, surfaced by get_audit_events.
_AUDIT_EVENT_TYPES: frozenset[str] = frozenset(
    {
        EventType.SECURITY_AUTH_EVENT.value,
        EventType.AUDIT_CONTROL_ACTION.value,
    }
)

# The role permitted to read the audit log.
_ADMIN_ROLE = "admin"


def _levels_at_or_above(floor: str) -> list[str]:
    """Return the level names whose severity is at or above ``floor``.

    Used to translate a severity floor into an ``IN (...)`` set, because the
    level column is a string enum that cannot be compared by severity with a
    relational operator. An unknown floor name yields every level (no filtering)
    so a typo never silently hides rows.
    """
    floor_rank = _LEVEL_RANK.get(floor)
    if floor_rank is None:
        return list(_LEVEL_RANK)
    return [name for name, rank in _LEVEL_RANK.items() if rank >= floor_rank]


def log_event(
    session: Session,
    *,
    scope: str,
    scope_id: int | None,
    level: str,
    message: str,
    type: str | None = None,  # noqa: A002 - matches the documented public API
    metadata: dict[str, Any] | None = None,
    actor_user_id: int | None = None,
) -> None:
    """Record an event row and emit a redacted structured log line.

    This is the producer-side entry point. It is intentionally cheap and total:
    it persists one row and returns. It does **not** call any channel -- the
    dispatcher delivers notifications asynchronously by polling for new rows --
    and it never raises into its caller, so a logging failure can never abort the
    operation that triggered it.

    The event ``type`` is folded into the JSON details under the ``"type"`` key
    (there is no ``type`` column); routing and the audit query read it back from
    there. Any caller-supplied ``"type"`` key in ``metadata`` is overwritten when
    ``type`` is given.

    Secrets are redacted from ``message`` and ``metadata`` before the row is
    persisted, so credentials embedded in a URL or stored under a secret-looking
    key never reach the database or the log.

    :param session: an open ORM session; the new row is added to it and the
        caller's surrounding transaction commits it.
    :param scope: ``"system"``, ``"camera"``, or ``"project"``.
    :param scope_id: id of the scoped entity, or ``None`` for system scope.
    :param level: severity name (``"info"``/``"warning"``/``"error"``/
        ``"critical"``; ``"debug"`` is also accepted by storage).
    :param message: human-readable description.
    :param type: dotted event-type identifier, or ``None``.
    :param metadata: free-form details, or ``None``.
    :param actor_user_id: id of the human user who performed the action, or
        ``None`` for a system/operational event. ``actor_user_id`` is a foreign
        key, so a non-human event MUST pass ``None`` (the default) -- passing a
        fabricated id would violate the constraint.
    """
    try:
        safe_message = redact_text(message)
        details: dict[str, Any] | None = _build_details(type, metadata)
        event = Event(
            scope=scope,
            scope_id=scope_id,
            level=level,
            message=safe_message,
            timestamp=datetime.now(UTC).replace(tzinfo=None),
            actor_user_id=actor_user_id,
            event_metadata=details,
        )
        session.add(event)
        session.flush()
    except Exception:  # noqa: BLE001 - logging must never abort the caller
        logger.exception("failed to record event", extra={"event_scope": scope})
        return
    logger.log(
        _python_log_level(level),
        safe_message,
        extra={
            "event_scope": scope,
            "event_scope_id": scope_id,
            "event_type": type,
            "event_level": level,
            "event_details": details,
        },
    )


def _build_details(
    type: str | None,  # noqa: A002 - mirrors the public parameter name
    metadata: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Merge the event type into a redacted copy of the details, or None.

    Returns ``None`` only when there is neither a type nor any metadata, so the
    stored ``metadata`` column stays null for a bare event.
    """
    if type is None and metadata is None:
        return None
    redacted = redact(metadata) if metadata is not None else {}
    details: dict[str, Any] = dict(redacted) if isinstance(redacted, dict) else {}
    if type is not None:
        details[_TYPE_KEY] = type
    return details


def _python_log_level(level: str) -> int:
    """Map an event severity name to a :mod:`logging` level constant."""
    return {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
        "critical": logging.CRITICAL,
    }.get(level, logging.INFO)


def get_events(
    session: Session,
    *,
    scope: str | None = None,
    scope_id: int | None = None,
    level_floor: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Event], int]:
    """Return a page of events (newest first) and the total matching count.

    :param session: an open ORM session.
    :param scope: restrict to this scope, or ``None`` for all scopes.
    :param scope_id: restrict to this scoped entity id, or ``None``.
    :param level_floor: include only events at or above this severity, or
        ``None`` for all severities. An unknown name applies no level filter.
    :param limit: maximum rows to return (clamped to at least 1).
    :param offset: rows to skip from the start of the ordered result.
    :returns: a ``(rows, total)`` tuple, where ``total`` is the full count
        ignoring ``limit``/``offset``.
    """
    query = session.query(Event)
    query = _apply_event_filters(
        query, scope=scope, scope_id=scope_id, level_floor=level_floor
    )
    total = query.count()
    rows = (
        query.order_by(Event.timestamp.desc(), Event.id.desc())
        .limit(max(1, limit))
        .offset(max(0, offset))
        .all()
    )
    return rows, total


def get_audit_events(
    session: Session,
    user: User,
    *,
    scope: str | None = None,
    scope_id: int | None = None,
    level_floor: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Event], int]:
    """Return a page of audit/security events; admin-only.

    Restricts results to the audit and security event types regardless of the
    other filters, so this read can never surface unrelated operational events.

    :param session: an open ORM session.
    :param user: the requesting user; must have the admin role.
    :raises PermissionError: if ``user`` is not an admin. A domain error is
        raised rather than an HTTP error so this module stays independent of the
        web framework; the web layer maps it to a ``403``.
    """
    if getattr(user, "role", None) != _ADMIN_ROLE:
        raise PermissionError("Admin role required to read the audit log.")
    query = session.query(Event).filter(
        func.json_extract(Event.event_metadata, f"$.{_TYPE_KEY}").in_(
            sorted(_AUDIT_EVENT_TYPES)
        )
    )
    query = _apply_event_filters(
        query, scope=scope, scope_id=scope_id, level_floor=level_floor
    )
    total = query.count()
    rows = (
        query.order_by(Event.timestamp.desc(), Event.id.desc())
        .limit(max(1, limit))
        .offset(max(0, offset))
        .all()
    )
    return rows, total


def _apply_event_filters(
    query: Any,
    *,
    scope: str | None,
    scope_id: int | None,
    level_floor: str | None,
) -> Any:
    """Apply the shared scope/scope_id/level-floor filters to an event query."""
    if scope is not None:
        query = query.filter(Event.scope == scope)
    if scope_id is not None:
        query = query.filter(Event.scope_id == scope_id)
    if level_floor is not None:
        query = query.filter(Event.level.in_(_levels_at_or_above(level_floor)))
    return query
