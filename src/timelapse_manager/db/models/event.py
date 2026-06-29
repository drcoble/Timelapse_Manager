"""Event entity: an audit/log record scoped to the system, a camera, or project."""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

_scope_enum = Enum(
    "system",
    "camera",
    "project",
    name="event_scope",
    native_enum=False,
)
_level_enum = Enum(
    "debug",
    "info",
    "warning",
    "error",
    "critical",
    name="event_level",
    native_enum=False,
)


class Event(Base):
    """A scoped log/audit event, optionally attributed to a user.

    This is an append-only log, so it carries a single ``timestamp`` rather than
    the shared created/updated mixin. The free-form details column is mapped to
    the Python attribute ``event_metadata`` because ``metadata`` is reserved by
    the declarative base; the underlying database column is still named
    ``metadata``.
    """

    __tablename__ = "event"
    __table_args__ = (
        Index("ix_event_scope_timestamp", "scope", "timestamp"),
        Index("ix_event_actor_user_id", "actor_user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(_scope_enum, nullable=False)
    scope_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    level: Mapped[str] = mapped_column(_level_enum, nullable=False, default="info")
    timestamp: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    message: Mapped[str] = mapped_column(String, nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    event_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSON, nullable=True
    )
    # Active-alert lifecycle. An event is an "active alert" while its severity is
    # at or above the alert threshold AND ``alert_cleared_at`` is NULL. Clearing
    # only sets these columns -- the event row itself is never deleted, so the
    # operational log stays append-only and complete.
    #
    # ``alert_cleared_at`` -- when the alert left the active list (NULL = active).
    # ``alert_cleared_by`` -- the user who manually cleared it; NULL when the
    #   alert was auto-cleared by a matching resolve signal (or still active).
    # ``alert_clear_reason`` -- ``"manual"`` or ``"auto"``; NULL while active.
    alert_cleared_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    alert_cleared_by: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    alert_clear_reason: Mapped[str | None] = mapped_column(String, nullable=True)
