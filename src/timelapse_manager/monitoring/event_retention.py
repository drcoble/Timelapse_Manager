"""Opt-in event retention: delete old event rows in bounded batches (issue #66, C4).

The ``event`` table is the operational log and only grows. Retention is **off by
default** (``monitoring.event_retention.*_days = 0``, keep forever), so an
upgrade changes nothing until the operator opts in. When enabled, the render
scheduler's hourly housekeeping pass calls :func:`prune_events`, which deletes:

* ``debug`` and ``info`` events older than ``info_days``;
* alerts cleared more than ``cleared_alert_days`` ago (by ``alert_cleared_at``).

Whatever the windows, it always keeps:

* audit and security events (:data:`~.events.AUDIT_EVENT_TYPES`), so the audit
  log stays complete;
* uncleared alerts (neither rule can match one: the first takes only ``debug``
  and ``info``, which are below the alert threshold, and the second only cleared
  rows);
* the newest event row. ``event`` ids are not ``AUTOINCREMENT``, so deleting the
  row with the highest id would let SQLite hand that id out again. The
  notification dispatcher tracks the highest id it has seen in memory, and would
  silently skip a new event that reused an id at or below that mark.

Nothing references ``event.id`` by foreign key, so a delete cascades nowhere.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, Delete, and_, delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from ..config.settings import EventRetentionSettings
from ..db.batched_delete import prune_in_batches
from ..db.models import Event
from .events import AUDIT_EVENT_TYPES

logger = logging.getLogger(__name__)

# Most batches one housekeeping pass runs. With the default batch size this
# bounds a pass to 100k rows; a larger backlog drains over later passes, so one
# pass never holds the write lock for long.
EVENT_PRUNE_MAX_BATCHES = 20

# Levels the ``info_days`` window applies to. Both are below the alert
# threshold, so this rule can never delete an alert.
_INFO_LEVELS = ("debug", "info")


def event_prune_predicate(
    retention: EventRetentionSettings, now: datetime
) -> ColumnElement[bool] | None:
    """Return the WHERE clause selecting prunable events, or ``None`` if off.

    :param retention: the retention windows; a ``0`` window disables its rule.
    :param now: the current instant as **naive UTC**, the form event timestamps
        are stored in.
    :returns: the predicate, or ``None`` when both windows are ``0`` (nothing
        may be deleted).
    """
    rules: list[ColumnElement[bool]] = []
    if retention.info_days > 0:
        cutoff = now - timedelta(days=retention.info_days)
        rules.append(and_(Event.level.in_(_INFO_LEVELS), Event.timestamp < cutoff))
    if retention.cleared_alert_days > 0:
        cutoff = now - timedelta(days=retention.cleared_alert_days)
        rules.append(
            and_(
                Event.alert_cleared_at.is_not(None),
                Event.alert_cleared_at < cutoff,
            )
        )
    if not rules:
        return None
    event_type = func.json_extract(Event.event_metadata, "$.type")
    newest_id = select(func.max(Event.id)).scalar_subquery()
    return and_(
        or_(*rules),
        # NULL-safe: an untyped event has no ``type`` and ``NULL NOT IN (...)``
        # is NULL, which would wrongly keep every untyped row.
        or_(event_type.is_(None), event_type.not_in(sorted(AUDIT_EVENT_TYPES))),
        Event.id < newest_id,
    )


def prune_events(
    session_factory: sessionmaker[Session],
    retention: EventRetentionSettings,
    now: datetime,
    *,
    max_batches: int = EVENT_PRUNE_MAX_BATCHES,
) -> int:
    """Delete prunable events, oldest first, in bounded batches.

    Returns ``0`` without opening a session when retention is off. Synchronous:
    call it off the event loop.

    :param session_factory: factory for the short per-batch sessions.
    :param retention: the configured windows and batch size.
    :param now: the current instant as naive UTC.
    :param max_batches: per-pass cap on batches (a test lowers it).
    :returns: the number of events deleted.
    """
    predicate = event_prune_predicate(retention, now)
    if predicate is None:
        return 0

    def build(limit: int) -> Delete:
        batch_ids = select(Event.id).where(predicate).order_by(Event.id).limit(limit)
        return (
            delete(Event)
            .where(Event.id.in_(batch_ids))
            .execution_options(synchronize_session=False)
        )

    deleted = prune_in_batches(
        session_factory, build, retention.batch_size, max_batches
    )
    if deleted:
        logger.info("event retention deleted %d event(s)", deleted)
    return deleted
