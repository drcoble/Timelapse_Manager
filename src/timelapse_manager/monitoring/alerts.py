"""Active alerts: the threshold, the read query, manual clear, and auto-clear.

An **active alert** is an event whose severity is at or above the alert
threshold (:data:`ALERT_LEVEL_THRESHOLD`, ``warning`` by default -- so warning,
error, and critical) and that has not been cleared (``alert_cleared_at`` is
NULL). The definition is deliberately *level-primary, not type-primary*: most
alertable conditions (low disk, a frozen camera, a camera gone offline) are
emitted as untyped events that carry only a ``level`` and a ``reason``, so a
type filter would miss them. The event type may be layered on as an optional
filter in the future, but it is never required for an event to be an alert.

Clearing an alert **never deletes the event row**. It only sets the three
``alert_*`` columns, so the operational log stays append-only and complete:

* manual clear -- ``alert_cleared_at = now``, ``alert_cleared_by = <user>``,
  ``alert_clear_reason = "manual"`` (operator/admin action, attributed);
* auto clear -- ``alert_cleared_at = now``, ``alert_cleared_by = NULL``,
  ``alert_clear_reason = "auto"`` (a matching resolve signal was observed).

Auto-clear on resolve
---------------------
Some conditions emit a natural *resolve* signal once they recover. The disk pair
is the must-work example: a ``low_disk`` warning is raised when free space drops
below the watermark, and a ``disk_recovered`` info event is emitted when it comes
back. :data:`RESOLVE_TO_RAISE_REASONS` maps each **resolve** reason to the set of
**raise** reasons it clears; :func:`auto_clear_for_event` matches by ``scope`` +
``scope_id`` so a recovery for one project never clears another's alert.

The resolve signals are themselves **info** level -- below the alert threshold --
so the auto-clear evaluator must inspect *every* new event regardless of level
(see :func:`auto_clear_for_event`). The dispatcher drives it on the same poll
batch it already pulls from the ``event`` table, which is the one place both
event write paths (``log_event`` and the supervisor's ``_write_event``) are seen.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, func, update

from ..db.models import Event
from .events import _levels_at_or_above

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# The severity floor at which an uncleared event counts as an active alert.
# Single source of truth -- the read query and any future filter share it.
ALERT_LEVEL_THRESHOLD = "warning"

# Clear reasons stored on a cleared event.
CLEAR_REASON_MANUAL = "manual"
CLEAR_REASON_AUTO = "auto"

# The JSON details key carrying the untyped reason marker (e.g. "low_disk",
# "disk_recovered", "camera_offline", "camera_recovered"). The supervisor writes
# it on its events and the disk/camera resolve pairs are matched on it.
_REASON_KEY = "reason"

# Mapping from a RESOLVE reason to the set of RAISE reasons it auto-clears.
# Keyed by the resolve reason ONLY: a raise reason is never itself a trigger, so
# raising a fresh ``low_disk`` after a recovery simply re-arms the alert and is
# never mistaken for a clear. Matched by scope + scope_id in
# :func:`auto_clear_for_event`. Conditions with no natural resolve signal
# (a frozen camera, a failed render) are intentionally absent: they remain
# manual-clear / terminal -- no resolve is invented for them.
RESOLVE_TO_RAISE_REASONS: dict[str, frozenset[str]] = {
    "disk_recovered": frozenset({"low_disk"}),
    "camera_recovered": frozenset({"camera_offline"}),
}


@dataclass(frozen=True)
class ActiveAlert:
    """A detached snapshot of one active alert, safe to use after the session."""

    id: int
    scope: str
    scope_id: int | None
    level: str
    message: str
    timestamp: datetime
    event_type: str | None
    reason: str | None
    metadata: dict[str, Any] | None


def _reason_of(event: Event) -> str | None:
    """Return the ``reason`` marker from an event's JSON details, or None."""
    details = event.event_metadata or {}
    if not isinstance(details, dict):
        return None
    value = details.get(_REASON_KEY)
    return str(value) if value is not None else None


def _type_of(event: Event) -> str | None:
    """Return the dotted event type from an event's JSON details, or None."""
    details = event.event_metadata or {}
    if not isinstance(details, dict):
        return None
    value = details.get("type")
    return str(value) if value is not None else None


def _to_active_alert(event: Event) -> ActiveAlert:
    """Project an event row onto a detached :class:`ActiveAlert` snapshot."""
    return ActiveAlert(
        id=event.id,
        scope=event.scope,
        scope_id=event.scope_id,
        level=event.level,
        message=event.message,
        timestamp=event.timestamp,
        event_type=_type_of(event),
        reason=_reason_of(event),
        metadata=event.event_metadata,
    )


def get_active_alerts(
    session: Session,
    *,
    scope: str | None = None,
    scope_id: int | None = None,
    level_floor: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[ActiveAlert], int]:
    """Return the active alerts (newest first) and the total active count.

    An active alert is an uncleared event at or above ``level_floor`` (defaulting
    to :data:`ALERT_LEVEL_THRESHOLD`). Cleared events and events below the floor
    are excluded.

    :param session: an open ORM session.
    :param scope: restrict to this scope, or ``None`` for all scopes.
    :param scope_id: restrict to this scoped entity id, or ``None``.
    :param level_floor: severity floor; defaults to :data:`ALERT_LEVEL_THRESHOLD`.
        An unknown name applies no level filter (matching ``get_events``).
    :param limit: maximum rows to return (clamped to at least 1).
    :param offset: rows to skip from the start of the ordered result.
    :returns: a ``(rows, total)`` tuple where ``total`` is the full active count
        ignoring ``limit``/``offset``.
    """
    floor = level_floor if level_floor is not None else ALERT_LEVEL_THRESHOLD
    query = session.query(Event).filter(
        Event.alert_cleared_at.is_(None),
        Event.level.in_(_levels_at_or_above(floor)),
    )
    if scope is not None:
        query = query.filter(Event.scope == scope)
    if scope_id is not None:
        query = query.filter(Event.scope_id == scope_id)
    total = query.count()
    rows = (
        query.order_by(Event.timestamp.desc(), Event.id.desc())
        .limit(max(1, limit))
        .offset(max(0, offset))
        .all()
    )
    return [_to_active_alert(row) for row in rows], total


def clear_alert(session: Session, *, event_id: int, user_id: int) -> int:
    """Manually clear one active alert by event id; attributed to ``user_id``.

    Idempotent and total: clearing an already-cleared event, an event below the
    alert threshold (not an alert), or a non-existent id is a no-op that returns
    ``0`` rather than raising. A real active alert is cleared and ``1`` returned.

    The clear is attributed (``alert_cleared_by = user_id``,
    ``alert_clear_reason = "manual"``). ``alert_cleared_by`` is a foreign key to
    ``user.id``; the caller is responsible for ensuring that user row exists
    (the web/API layer materialises the sentinel administrator before calling).

    :returns: the number of rows cleared (``0`` or ``1``).
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(Event)
            .where(
                Event.id == event_id,
                Event.alert_cleared_at.is_(None),
                Event.level.in_(_levels_at_or_above(ALERT_LEVEL_THRESHOLD)),
            )
            .values(
                alert_cleared_at=now,
                alert_cleared_by=user_id,
                alert_clear_reason=CLEAR_REASON_MANUAL,
            )
        ),
    )
    return int(result.rowcount or 0)


def clear_all_alerts(session: Session, *, user_id: int) -> int:
    """Manually clear every active alert; attributed to ``user_id``.

    Bulk counterpart to :func:`clear_alert`. Clears all uncleared events at or
    above the alert threshold in one statement and returns the count cleared
    (``0`` when there are no active alerts). See :func:`clear_alert` for the
    foreign-key requirement on ``user_id``.

    :returns: the number of alerts cleared.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(Event)
            .where(
                Event.alert_cleared_at.is_(None),
                Event.level.in_(_levels_at_or_above(ALERT_LEVEL_THRESHOLD)),
            )
            .values(
                alert_cleared_at=now,
                alert_cleared_by=user_id,
                alert_clear_reason=CLEAR_REASON_MANUAL,
            )
        ),
    )
    return int(result.rowcount or 0)


def auto_clear_for_event(
    session: Session,
    *,
    scope: str,
    scope_id: int | None,
    reason: str | None,
) -> int:
    """Auto-clear active alerts matching a resolve signal, by scope + scope_id.

    Called once per new event the dispatcher pulls, for events *of any level* --
    the resolve signals (``disk_recovered``, ``camera_recovered``) are info
    level, below the alert threshold, so a level filter here would never see them
    and nothing would ever auto-clear. When ``reason`` is a key in
    :data:`RESOLVE_TO_RAISE_REASONS`, every active alert for the same scope and
    scope_id whose own ``reason`` is one of the mapped raise reasons is cleared
    with ``alert_clear_reason = "auto"`` and ``alert_cleared_by = NULL``.

    Matching is independent of the event's type and of notification routing, so
    it works for both event write paths (``log_event`` and the supervisor's
    untyped ``_write_event``). A later recurrence of the condition simply logs a
    fresh event (re-raising the alert); a cleared alert is never un-cleared.

    :returns: the number of alerts auto-cleared (``0`` when ``reason`` is not a
        resolve signal or nothing matched).
    """
    if reason is None:
        return 0
    raise_reasons = RESOLVE_TO_RAISE_REASONS.get(reason)
    if not raise_reasons:
        return 0
    now = datetime.now(UTC).replace(tzinfo=None)
    stmt = (
        update(Event)
        .where(
            Event.alert_cleared_at.is_(None),
            Event.scope == scope,
            func.json_extract(Event.event_metadata, f"$.{_REASON_KEY}").in_(
                sorted(raise_reasons)
            ),
        )
        .values(
            alert_cleared_at=now,
            alert_cleared_by=None,
            alert_clear_reason=CLEAR_REASON_AUTO,
        )
    )
    # scope_id may legitimately be NULL (system scope); ``== None`` would never
    # match in SQL, so use ``IS NULL`` semantics via the typed comparison.
    if scope_id is None:
        stmt = stmt.where(Event.scope_id.is_(None))
    else:
        stmt = stmt.where(Event.scope_id == scope_id)
    result = cast("CursorResult[Any]", session.execute(stmt))
    cleared = int(result.rowcount or 0)
    if cleared:
        logger.info(
            "auto-cleared %d alert(s) on resolve reason=%s scope=%s scope_id=%s",
            cleared,
            reason,
            scope,
            scope_id,
        )
    return cleared
