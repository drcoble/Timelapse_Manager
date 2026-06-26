"""Milestone entity: a user-placed chapter marker within a project."""

from __future__ import annotations

import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class Milestone(TimestampMixin, Base):
    """A named position in a project's timeline, set by a user.

    Positioned by frame index and/or timestamp; included in any render that
    covers its position.
    """

    __tablename__ = "milestone"
    __table_args__ = (Index("ix_milestone_project_id", "project_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    label: Mapped[str | None] = mapped_column(String, nullable=True)
    position_frame_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position_timestamp: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True
    )
