"""Declarative base and shared column conventions for the ORM models.

All persistence models inherit from :class:`Base`. The :class:`TimestampMixin`
supplies the ``created_at`` / ``updated_at`` columns that nearly every entity
carries, populated by the database clock so values are consistent regardless of
which process writes the row.
"""

from __future__ import annotations

import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Root declarative base shared by every persistence model."""


class TimestampMixin:
    """Adds database-managed ``created_at`` and ``updated_at`` columns.

    Both default to the current time on insert; ``updated_at`` is additionally
    refreshed on update. Times are stored in UTC.
    """

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )
