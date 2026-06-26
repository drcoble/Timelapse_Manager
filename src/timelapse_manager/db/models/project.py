"""Project entity: a capture campaign bound to a single camera."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin

_operational_status_enum = Enum(
    "idle",
    "capturing",
    "rendering",
    "error",
    name="project_operational_status",
    native_enum=False,
)
_lifecycle_state_enum = Enum(
    "active",
    "paused",
    "archived",
    name="project_lifecycle_state",
    native_enum=False,
)


class Project(TimestampMixin, Base):
    """A timelapse capture project.

    Schedules and post-render actions are rich, nested structures and are stored
    as JSON documents rather than being normalised into their own tables.
    """

    __tablename__ = "project"
    __table_args__ = (Index("ix_project_camera_id", "camera_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    camera_id: Mapped[int] = mapped_column(
        ForeignKey("camera.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    capture_interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Campaign bounds (distinct from the recurring daily ``schedule`` window
    # below): capture does not run before ``start_date`` and is stopped when
    # ``now >= end_date`` or the active ``frame_count`` reaches
    # ``max_frame_count``. All three are optional; a project with none set runs
    # open-endedly. Stored as naive UTC, matching the other datetime columns.
    start_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    end_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    max_frame_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    schedule: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    render_schedule: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    archive_schedule: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    post_render_actions: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    storage_path: Mapped[str | None] = mapped_column(String, nullable=True)
    # Which of the camera's named streams/profiles this project captures from.
    # A null ``stream_id`` means "use the camera's default stream" -- the
    # behaviour for every project that never picks one. ``stream_label`` is the
    # human-readable name of the selected stream, kept alongside the id purely so
    # the UI can show it without re-querying the camera.
    stream_id: Mapped[str | None] = mapped_column(String, nullable=True)
    stream_label: Mapped[str | None] = mapped_column(String, nullable=True)
    # Per-project PTZ positioning: the camera is moved to this position before
    # capture. A project may select a camera-defined ``ptz_preset`` by name/id,
    # or specify a raw ``ptz_pan``/``ptz_tilt``/``ptz_zoom`` position. All are
    # nullable; a project with none set captures from wherever the camera is
    # pointing. The pan/tilt/zoom values are in the camera's own units and are
    # forwarded verbatim, so no range is enforced here.
    ptz_preset: Mapped[str | None] = mapped_column(String, nullable=True)
    ptz_pan: Mapped[float | None] = mapped_column(Float, nullable=True)
    ptz_tilt: Mapped[float | None] = mapped_column(Float, nullable=True)
    ptz_zoom: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Exact-time capture anchors: a list of anchor objects describing instants
    # (a wall-clock time, or solar noon for the camera's location) at which a
    # single frame is captured once per local day, independent of the recurring
    # ``schedule`` gate. ``NULL``/absent means no anchors -- a project that never
    # opts in keeps its plain interval-and-gate behaviour. Stored in a dedicated
    # column (not folded into ``schedule``) so the schedule form's rebuild and
    # preset detection never disturb it. Each anchor carries a stable generated
    # id so the durable fire-log can key on it across reorders/edits.
    exact_time_anchors: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    # Event-triggered capture configuration: a list of trigger objects, each
    # naming a camera event topic, an enable flag, and a per-trigger debounce
    # cooldown. A matching camera event captures a single frame, independent of
    # the ``schedule`` gate. ``NULL``/absent means no triggers -- a project that
    # never opts in runs no event listener at all.
    event_triggers: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    operational_status: Mapped[str] = mapped_column(
        _operational_status_enum, nullable=False, default="idle"
    )
    lifecycle_state: Mapped[str] = mapped_column(
        _lifecycle_state_enum, nullable=False, default="active"
    )
    frame_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
