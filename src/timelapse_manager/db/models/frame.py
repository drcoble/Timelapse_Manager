"""Frame entity: a single captured (or uploaded) still belonging to a project.

Image bytes live on disk; this row carries only the metadata and the file path.
"""

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
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin

_capture_status_enum = Enum(
    "pending",
    "captured",
    "failed",
    name="frame_capture_status",
    native_enum=False,
)
_origin_enum = Enum(
    "captured",
    "uploaded",
    name="frame_origin",
    native_enum=False,
)
_lifecycle_state_enum = Enum(
    "active",
    "soft_deleted",
    name="frame_lifecycle_state",
    native_enum=False,
)


class Frame(TimestampMixin, Base):
    """A still image within a project's sequence."""

    __tablename__ = "frame"
    __table_args__ = (
        UniqueConstraint("project_id", "sequence_index", name="uq_frame_project_seq"),
        Index("ix_frame_project_id", "project_id"),
        Index("ix_frame_capture_timestamp", "capture_timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    sequence_index: Mapped[int] = mapped_column(Integer, nullable=False)
    capture_timestamp: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    file_path: Mapped[str | None] = mapped_column(String, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    capture_status: Mapped[str] = mapped_column(
        _capture_status_enum, nullable=False, default="pending"
    )
    origin: Mapped[str] = mapped_column(
        _origin_enum, nullable=False, default="captured"
    )
    lifecycle_state: Mapped[str] = mapped_column(
        _lifecycle_state_enum, nullable=False, default="active"
    )
    # When set, this frame is omitted from rendered output but stays fully
    # visible in the browser. ``NULL`` means included; a non-null value is the
    # instant the frame was excluded. This is orthogonal to ``lifecycle_state``:
    # a frame can be both soft-deleted and excluded, and restoring it from
    # soft-delete leaves the exclusion bit untouched (and vice versa). Only the
    # encoder honours this flag; every browse path shows excluded frames.
    excluded_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    # ``True`` when ``capture_timestamp`` was *inferred* rather than read from the
    # image: an imported frame whose bytes carried no readable Exif capture time
    # falls back to a caller-supplied time (e.g. the upload instant) and is
    # flagged here so the browser can badge it and offer an inline correction.
    # ``False`` for a live capture or an import whose Exif time was readable. The
    # flag is cleared once the user edits the timestamp.
    capture_timestamp_inferred: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    # Denormalised snapshot of which named camera stream/profile captured this
    # frame, as the stream identifier in force at capture time. ``NULL`` for an
    # uploaded frame, or a captured frame taken from the camera's default stream.
    # Stored on the frame (rather than read from the project) so the provenance
    # is fixed at capture time and survives a later change to the project's
    # selected stream.
    stream_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Why this frame was captured, for provenance on the unified one-shot path.
    # ``NULL`` for an ordinary interval capture or an uploaded frame; set to a
    # short reason token (e.g. ``"anchor:clock"``, ``"anchor:solar_noon"``,
    # ``"event:<topic>"``) when the frame was produced by an exact-time anchor or
    # an event trigger. Free-form text rather than an enum so new one-shot
    # producers can record their own reason without a schema change.
    capture_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    # A best-effort snapshot of the camera's scene/image settings at the moment
    # this frame was captured, stored as a queryable JSON object. The envelope is
    # versioned so its shape can evolve without breaking readers::
    #
    #     {
    #       "schema_version": 1,        # int -- bumped if the shape changes
    #       "source": "vapix",          # which adapter produced the metadata
    #       "captured_resolution": "WxH",  # the frame's own pixel dimensions
    #       ... scene fields the camera exposed (e.g. brightness, contrast,
    #           saturation, sharpness, exposure), each present only when read ...
    #     }
    #
    # ``NULL`` when no metadata was collected: uploaded frames, frames from a
    # protocol that exposes no scene data (rtsp/http/onvif), or a capture whose
    # best-effort metadata read failed. Collection never fails a capture, so a
    # ``NULL`` here is always benign.
    scene_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
