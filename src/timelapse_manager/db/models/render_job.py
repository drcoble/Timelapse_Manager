"""RenderJob entity: a request to encode a project's frames into a video."""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    ColumnElement,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    bindparam,
    text,
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

# The statuses of a job still in flight: queued, or claimed by the worker.
# ``ix_render_job_active`` is partial on exactly these, so it holds only the
# handful of live jobs however long the history grows. SQLite uses a partial
# index only when the query repeats its WHERE terms and cannot see through bound
# parameters, so the queue and scheduler reads go through
# :func:`active_job_filter`, which renders this IN list as literals in this
# order. Migration 037 freezes the text.
ACTIVE_JOB_STATUSES: tuple[str, ...] = ("pending", "encoding")
ACTIVE_JOB_INDEX_WHERE = "status IN ({})".format(
    ", ".join(f"'{status}'" for status in ACTIVE_JOB_STATUSES)
)


class RenderJob(TimestampMixin, Base):
    """An encoding job and the settings used to produce its output video."""

    __tablename__ = "render_job"
    # AUTOINCREMENT (migration 028): ids only ever grow, so a deleted job's id
    # is never handed to a new one. The audit log ("render N ...") and the
    # render/export cards' event lookups identify a job by its id.
    __table_args__ = (
        Index("ix_render_job_project_id", "project_id"),
        # The worker's claim and watchdog reads, and the scheduler's in-flight
        # check. Partial on the live jobs only (migration 037); see
        # ``ACTIVE_JOB_STATUSES``.
        Index(
            "ix_render_job_active",
            "id",
            sqlite_where=text(ACTIVE_JOB_INDEX_WHERE),
        ),
        # The scheduler's newest-job-per-(project, kind) read (037): a covering
        # index, so it never reads a table row.
        Index("ix_render_job_project_kind_created", "project_id", "kind", "created_at"),
        {"sqlite_autoincrement": True},
    )

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
    # What the viewer's browser is asked about (migration 032): the RFC 6381
    # codec string (``av01.0.05M.08``) and the container MIME type, probed with
    # ffprobe once the render is written. ``NULL`` until probed, or when the
    # probe failed; the page then falls back to a MIME-only check.
    codec_string: Mapped[str | None] = mapped_column(String, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String, nullable=True)
    overlay_config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(_status_enum, nullable=False, default="pending")
    output_file_path: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    completed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    # The frame window the job actually used, recorded by the render worker as
    # it encodes (or, for an export, as it bundles the zip): how many frames, and
    # the capture times (naive UTC) of the first and last. ``NULL`` on jobs that
    # ran before these columns existed -- the render card says "not recorded".
    frame_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_frame_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    last_frame_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    # How many times a clean service stop put this job back to ``pending``
    # (migration 042, #178). A job is requeued once; interrupted again, it fails.
    shutdown_requeues: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )


def active_job_filter() -> ColumnElement[bool]:
    """Return the WHERE term that selects in-flight jobs via ``ix_render_job_active``.

    It repeats the index's partial predicate with the status list rendered as
    literals (``status IN ('pending', 'encoding')``): SQLite proves a query may
    use a partial index only from literal terms, never from bound parameters.
    Narrow it further with an extra term (``RenderJob.status == "pending"``);
    that only shrinks the result and keeps the index usable.
    """
    return RenderJob.status.in_(
        bindparam(
            "active_job_statuses",
            list(ACTIVE_JOB_STATUSES),
            expanding=True,
            literal_execute=True,
        )
    )
