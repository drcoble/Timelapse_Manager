"""Build an encoder :class:`RenderSpec` from a job, its project, and its frames.

The render layer speaks the database's language (a :class:`RenderJob` row with
JSON output/overlay settings, a :class:`Project`, and :class:`Milestone` rows);
the encoder speaks value objects. This module is the single bridge between them:
it gathers the project's active frames, resolves milestones into the encoder's
lightweight shape, computes chapters, and assembles the immutable spec.

The output is written under a per-project *render root* -- a ``renders``
sub-directory of the project's storage location -- so produced videos sit
alongside, but separate from, the captured frames. The encoder validates that the
output path stays confined within that root.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db.models import Milestone, Project, RenderJob
from ..encode import (
    Milestone as EncoderMilestone,
)
from ..encode import (
    OutputSettings,
    OverlayConfig,
    RenderSpec,
    compute_chapters,
    gather_frames,
)
from ..storage import paths

# File extension per container, used to name the output file.
_CONTAINER_EXTENSIONS: dict[str, str] = {
    "mp4": "mp4",
    "mkv": "mkv",
    "webm": "webm",
}


class SpecBuildError(Exception):
    """Raised when a renderable spec cannot be built for a job."""


def project_render_root(settings: Settings, project: Project) -> Path:
    """Return the directory a project's rendered videos are written under.

    A ``renders`` sub-directory (the configured ``output_subdir``) of the
    project's frame directory's parent: frames and renders live as siblings under
    the project's storage location. For a default-layout project that is
    ``<frames_root>/<project_id>/<output_subdir>``; for a project with an explicit
    storage path it is ``<storage_path parent>/<output_subdir>``.
    """
    frame_dir = paths.frame_dir(settings, project)
    return frame_dir.parent / settings.render.output_subdir


def _output_settings(job: RenderJob, settings: Settings) -> OutputSettings:
    """Translate the job's JSON output settings into the encoder's value object.

    Falls back to sensible defaults (the configured fps, H.264 MP4) for any field
    the stored settings omit, so a sparse request still renders.

    Width and height are only set when *both* are present in the stored settings;
    when both are absent the render keeps the source frames' native size (a
    "source" resolution -- no scaling). A partial pair still defaults the missing
    dimension so an explicit size is never silently dropped.
    """
    raw: dict[str, Any] = dict(job.output_settings or {})
    fps = _as_float(raw.get("fps"), settings.render.default_fps)
    codec = str(raw.get("codec") or "h264")
    container = str(raw.get("container") or "mp4")
    width, height = _dimensions(raw)
    return OutputSettings(
        fps=fps,
        width=width,
        height=height,
        codec=codec,
        container=container,
        bitrate_kbps=_as_opt_int(raw.get("bitrate_kbps")),
        crf=_as_opt_int(raw.get("crf")),
    )


def _dimensions(raw: dict[str, Any]) -> tuple[int | None, int | None]:
    """Resolve output width/height from stored settings.

    Both absent -> ``(None, None)`` meaning "source size" (no scaling). Otherwise
    each is coerced, defaulting a missing partner to a 1080p dimension so an
    explicit size is honoured rather than dropped.
    """
    has_width = raw.get("width") is not None
    has_height = raw.get("height") is not None
    if not has_width and not has_height:
        return None, None
    return _as_int(raw.get("width"), 1920), _as_int(raw.get("height"), 1080)


def _overlay_config(job: RenderJob) -> OverlayConfig:
    """Translate the job's JSON overlay config into the encoder's value object."""
    raw: dict[str, Any] = dict(job.overlay_config or {})
    return OverlayConfig(
        timestamp_enabled=bool(raw.get("timestamp_enabled", False)),
        timestamp_format=str(raw.get("timestamp_format") or "%Y-%m-%d %H:%M:%S"),
        timestamp_timezone=str(raw.get("timestamp_timezone") or "UTC"),
        text_enabled=bool(raw.get("text_enabled", False)),
        text_content=str(raw.get("text_content") or ""),
        image_enabled=bool(raw.get("image_enabled", False)),
        image_path=raw.get("image_path"),
        placement=str(raw.get("placement") or "top_left"),
    )


def _auto_chapter_mode(job: RenderJob) -> str | None:
    """Return the auto-chapter granularity recorded on the job, or ``None``."""
    raw: dict[str, Any] = dict(job.output_settings or {})
    mode = raw.get("auto_chapters")
    if mode in ("monthly", "weekly"):
        return str(mode)
    return None


def _deflicker(job: RenderJob) -> bool:
    """Return whether deflicker is requested for the job."""
    raw: dict[str, Any] = dict(job.output_settings or {})
    return bool(raw.get("deflicker", False))


def _load_milestones(session: Session, project_id: int) -> list[EncoderMilestone]:
    """Read a project's milestone rows as the encoder's lightweight shape."""
    rows = (
        session.execute(
            select(Milestone)
            .where(Milestone.project_id == project_id)
            .order_by(Milestone.id)
        )
        .scalars()
        .all()
    )
    return [
        EncoderMilestone(
            label=row.label or "",
            position_frame_index=row.position_frame_index,
            position_timestamp=_as_utc(row.position_timestamp),
        )
        for row in rows
    ]


def build_render_spec(
    session: Session,
    settings: Settings,
    job: RenderJob,
    project: Project,
) -> RenderSpec:
    """Assemble the immutable render spec for ``job``.

    Gathers the project's active frames in capture order, resolves milestones and
    auto-chapter boundaries into output-timeline chapters, and points the output
    at a uniquely named file inside the project's render root.

    :raises SpecBuildError: if the project has no renderable frames.
    """
    output = _output_settings(job, settings)
    overlay = _overlay_config(job)
    frames = gather_frames(session, settings, project.id)
    if len(frames) == 0:
        raise SpecBuildError(f"project {project.id} has no active frames to render")

    milestones = _load_milestones(session, project.id)
    chapters = compute_chapters(
        frames, milestones, output.fps, auto=_auto_chapter_mode(job)
    )

    render_root = project_render_root(settings, project)
    extension = _CONTAINER_EXTENSIONS.get(output.container.lower(), "mp4")
    output_path = render_root / f"render-{job.id}.{extension}"

    return RenderSpec(
        project_id=project.id,
        frames=frames,
        output_settings=output,
        overlay=overlay,
        chapters=chapters,
        deflicker=_deflicker(job),
        output_path=output_path,
        project_render_root=render_root,
    )


def _as_utc(value: datetime | None) -> datetime | None:
    """Return a stored (naive-UTC) timestamp as tz-aware UTC, or ``None``."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _as_int(value: Any, default: int) -> int:
    """Coerce ``value`` to ``int``, falling back to ``default`` on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_opt_int(value: Any) -> int | None:
    """Coerce ``value`` to ``int``, returning ``None`` when absent or invalid."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any, default: float) -> float:
    """Coerce ``value`` to ``float``, falling back to ``default`` on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
