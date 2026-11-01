"""Notification delivery cursor: a single-row table the dispatcher persists.

The notification dispatcher (:mod:`timelapse_manager.monitoring.dispatcher`)
records here how far through the event log it has got, so a restart resumes
where the previous process stopped instead of skipping everything logged in
between (#67 R8).
"""

from __future__ import annotations

import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class NotificationCursor(Base):
    """The dispatcher's persisted position in the event log.

    A single-row table: the primary key is constrained to ``1``. Two marks are
    kept because the dispatcher does two different things with each event:

    * ``last_event_id`` is the **delivery cursor**. Every event with an id at or
      below it has settled: delivered, failed after its retries, debounced or
      routed nowhere. An event above it may be delivered again after a restart
      (at-least-once).
    * ``last_read_event_id`` is the **read mark**. Every event at or below it
      has been read and run through the active-alert auto-clear. Auto-clear is
      not idempotent across later events (a replayed resolve would clear an
      alert raised after it), so a restart never re-runs it below this mark.

    The delivery cursor never exceeds the read mark. Both only move forward:
    the dispatcher writes them with SQL ``max()``. No secret is stored here.
    """

    __tablename__ = "notification_cursor"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_notification_cursor_singleton"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    last_event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    last_read_event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )
