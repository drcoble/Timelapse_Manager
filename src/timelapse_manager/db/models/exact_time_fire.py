"""Exact-time fire-log: the durable decision record for exact-time anchors.

An exact-time anchor fires a single capture once per anchor per local day. The
guarantee that it fires *exactly once* -- surviving process restarts, clock
jitter, and concurrent evaluation -- is provided by a durable row rather than an
in-memory flag: one row per ``(project, anchor, local day)``, written at decision
time, with a unique constraint that turns a duplicate insert into the double-fire
guard. The row records *what was decided* (captured / failed / skipped) so an
operator can see whether today's shot fired and, if not, why.
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

_status_enum = Enum(
    # A frame was captured for this anchor on this day.
    "captured",
    # The capture was attempted but failed (camera unreachable, etc.).
    "failed",
    # The fire instant was missed by more than the grace window (the app was
    # down when the anchor was due), so no frame was taken.
    "skipped_missed",
    # A solar-noon anchor could not fire because the camera has no geolocation.
    "skipped_no_geo",
    name="exact_time_fire_status",
    native_enum=False,
)


class ExactTimeFire(Base):
    """A per-anchor, per-day fire record for exact-time capture.

    A row is claimed at decision time and then its ``status`` is finalised once
    the capture attempt resolves. The unique constraint on
    ``(project_id, anchor_id, local_date)`` is the idempotency guard -- a second
    attempt to fire the same anchor on the same local day hits the constraint and
    is rejected, which is exactly how a restart or clock jitter is prevented from
    double-firing.
    """

    __tablename__ = "exact_time_fire"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "anchor_id",
            "local_date",
            name="uq_exact_time_fire",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    # The anchor's stable generated id (stored in the anchor object, never a list
    # index) so a fire record stays bound to its anchor across reorders/edits.
    anchor_id: Mapped[str] = mapped_column(String, nullable=False)
    # The local calendar day (``YYYY-MM-DD`` in the project's schedule timezone)
    # the anchor fired for. Together with the anchor id this is the once-per-day
    # idempotency key.
    local_date: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(_status_enum, nullable=False)
    # When the decision row was written (naive UTC, matching the other datetime
    # columns).
    fired_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)
    # The written frame, when ``status == "captured"``; ``NULL`` for a skip/fail.
    frame_id: Mapped[int | None] = mapped_column(
        ForeignKey("frame.id", ondelete="SET NULL"), nullable=True
    )
    # A short human-readable reason for a skip or failure, ``NULL`` on success.
    detail: Mapped[str | None] = mapped_column(String, nullable=True)
