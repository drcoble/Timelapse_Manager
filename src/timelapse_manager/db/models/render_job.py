"""RenderJob entity: a request to encode a project's frames into a video."""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin

_kind_enum = Enum(
    "manual",
    "scheduled",
    "archive",
    "export",
    name="render_job_kind",
    native_enum=False,
)
_status_enum = Enum(
    "pending",
    "encoding",
    "done",
    "failed",
    name="render_job_status",
    native_enum=False,
)


class RenderJob(TimestampMixin, Base):
    """An encoding job and the settings used to produce its output video."""

    __tablename__ = "render_job"
    __table_args__ = (Index("ix_render_job_project_id", "project_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    encoder_engine: Mapped[str] = mapped_column(
        String, nullable=False, default="ffmpeg"
    )
    # ``manual``/``scheduled``/``archive`` are encode jobs; ``export`` is a
    # bundle job that zips a selection of frame image files instead of encoding a
    # video. An export job reuses this row's queue/status machinery but carries no
    # encode settings: its frame-id set rides in ``output_settings`` under the
    # ``frame_ids`` key, and its produced ``.zip`` path lands in
    # ``output_file_path`` exactly as a render's video does.
    kind: Mapped[str] = mapped_column(_kind_enum, nullable=False, default="manual")
    output_settings: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    chapters: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    browser_streamable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    overlay_config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(_status_enum, nullable=False, default="pending")
    output_file_path: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    completed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True
    )
