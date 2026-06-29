"""System information and metrics endpoints.

Exposes a non-secret summary of the running process: application and ffmpeg
versions, the live database status, the network binding, and the log level.
Database credentials embedded in the connection URL are redacted; no token or
other secret is ever returned.

A second, separately mounted router serves a Prometheus metrics exposition. It
is off by default and, when enabled, is gated behind administrator
authentication -- the listener binds all interfaces and offers no unauthenticated
path, so the figures (which can reveal operational scale) are never public. The
exposition text is built by hand rather than via a metrics client library to
keep the distributed bundle lean.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..config import Settings
from ..db.models import Event, Project, RenderJob
from ..db.session import get_session
from ..runtime import get_context
from ..security.principal import require_admin_principal
from ..storage.frames import sum_project_disk_usage

router = APIRouter()

# A separate router for the Prometheus endpoint. It shares the versioned API's
# path prefix but is included on its own, so it does not inherit that API's
# blanket bearer-token dependency; its own guards -- the enable flag and the
# admin principal -- own access control end to end, letting a disabled deployment
# answer 404 (rather than the API's 401) for a scrape.
metrics_router = APIRouter()

# Prometheus text-exposition content type, pinned to the format version the
# scraper negotiates against.
_METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# Project lifecycle state counted by ``projects_active``.
_ACTIVE_LIFECYCLE = "active"

# Render job status counted by ``render_failures_total``.
_FAILED_RENDER_STATUS = "failed"

# Event types (stored under the ``type`` key of an event's JSON details) that
# represent a capture failure. The event table has no ``type`` column, so these
# are matched by extracting the JSON key -- the same access path the audit read
# uses.
_CAPTURE_FAILURE_TYPES = ("capture.gap", "capture.stalled")

# The JSON details key under which an event's type identifier is stored.
_EVENT_TYPE_KEY = "type"

# Matches the ``user:password@`` credential portion of a connection URL.
_URL_CREDENTIALS = re.compile(r"://[^/@]+@")


def _redact_db_url(url: str) -> str:
    """Mask any embedded credentials in a database URL for safe display."""
    return _URL_CREDENTIALS.sub("://***@", url)


def _db_status(session_factory: Any) -> str:
    """Return ``"ok"`` if a trivial query succeeds, else ``"error"``."""
    try:
        session = session_factory()
        try:
            session.execute(text("SELECT 1"))
        finally:
            session.close()
    except Exception:
        return "error"
    return "ok"


def _summary(settings: Settings) -> dict[str, Any]:
    """Build the non-secret configuration summary block."""
    return {
        "bind_address": settings.server.bind_address,
        "http_port": settings.server.http_port,
        "https_port": settings.server.https_port,
        "database_url": _redact_db_url(settings.database.url),
        "log_level": settings.logging.level,
        "log_format": settings.logging.format,
    }


@router.get("/system")
def system_info() -> dict[str, Any]:
    """Return application version, component versions, and non-secret config."""
    context = get_context()
    return {
        "app_version": context.app_version,
        "ffmpeg_version": context.ffmpeg_version,
        "ffmpeg_path": context.ffmpeg_path,
        "db_status": _db_status(context.session_factory),
        "config": _summary(context.settings),
    }


def _require_metrics_enabled() -> None:
    """Reject the metrics scrape with 404 unless the endpoint is enabled.

    Run before the admin gate so a disabled deployment is indistinguishable from
    one that never defined the route: an unauthenticated scrape gets 404, not a
    401 that would betray the endpoint's existence.
    """
    if not get_context().settings.observability.metrics_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


def _escape_label_value(value: str) -> str:
    """Escape a string for use as a Prometheus label value.

    The exposition format requires a backslash, double quote, and newline to be
    backslash-escaped; backslash is replaced first so an introduced backslash is
    not re-escaped.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _frames_captured_total(session: Session) -> int:
    """Return the sum of every project's recorded frame count."""
    total = session.execute(
        select(func.coalesce(func.sum(Project.frame_count), 0))
    ).scalar_one()
    return int(total or 0)


def _event_type_count(session: Session, types: tuple[str, ...]) -> int:
    """Return the number of events whose stored type is in ``types``."""
    total = session.execute(
        select(func.count())
        .select_from(Event)
        .where(
            func.json_extract(Event.event_metadata, f"$.{_EVENT_TYPE_KEY}").in_(types)
        )
    ).scalar_one()
    return int(total or 0)


def _render_failures_total(session: Session) -> int:
    """Return the number of render jobs that ended in failure."""
    total = session.execute(
        select(func.count())
        .select_from(RenderJob)
        .where(RenderJob.status == _FAILED_RENDER_STATUS)
    ).scalar_one()
    return int(total or 0)


def _projects_active(session: Session) -> int:
    """Return the number of projects in the active lifecycle state."""
    total = session.execute(
        select(func.count())
        .select_from(Project)
        .where(Project.lifecycle_state == _ACTIVE_LIFECYCLE)
    ).scalar_one()
    return int(total or 0)


def _disk_usage_by_project(session: Session) -> list[tuple[str, int]]:
    """Return ``(project_name, active_bytes)`` for every project.

    Delegates each project's footprint to the shared frame-storage aggregate, so
    this series uses the single canonical definition of a project's on-disk size
    (active frames only; an unknown size counts as zero) and never drifts from it.
    A project with no active frames reports zero.
    """
    projects = session.execute(select(Project.id, Project.name)).all()
    return [
        (str(name), sum_project_disk_usage(session, project_id))
        for project_id, name in projects
    ]


def _render_metrics(session: Session) -> str:
    """Build the Prometheus exposition text from current database state.

    Every metric is computed on this scrape; nothing is cached. The disk-usage
    family carries a bare total sample and one labelled sample per project under
    the same metric name, which the exposition format permits because each sample
    has a distinct label set.
    """
    frames_total = _frames_captured_total(session)
    capture_failures = _event_type_count(session, _CAPTURE_FAILURE_TYPES)
    render_failures = _render_failures_total(session)
    projects_active = _projects_active(session)
    disk_by_project = _disk_usage_by_project(session)
    disk_total = sum(used for _, used in disk_by_project)

    lines: list[str] = []

    lines.append("# HELP frames_captured_total Frames captured across all projects.")
    lines.append("# TYPE frames_captured_total counter")
    lines.append(f"frames_captured_total {frames_total}")

    lines.append("# HELP capture_failures_total Capture failures recorded in events.")
    lines.append("# TYPE capture_failures_total counter")
    lines.append(f"capture_failures_total {capture_failures}")

    lines.append("# HELP render_failures_total Render jobs that ended in failure.")
    lines.append("# TYPE render_failures_total counter")
    lines.append(f"render_failures_total {render_failures}")

    lines.append("# HELP projects_active Projects in the active lifecycle state.")
    lines.append("# TYPE projects_active gauge")
    lines.append(f"projects_active {projects_active}")

    lines.append("# HELP disk_used_bytes On-disk size of active frames, in bytes.")
    lines.append("# TYPE disk_used_bytes gauge")
    lines.append(f"disk_used_bytes {disk_total}")
    for name, used in disk_by_project:
        label = _escape_label_value(name)
        lines.append(f'disk_used_bytes{{project="{label}"}} {used}')

    # A trailing newline terminates the final sample line, as the exposition
    # format expects.
    return "\n".join(lines) + "\n"


@metrics_router.get(
    "/metrics",
    dependencies=[Depends(_require_metrics_enabled), Depends(require_admin_principal)],
)
def metrics(db: Annotated[Session, Depends(get_session)]) -> Response:
    """Serve the Prometheus exposition, computed on scrape from the database.

    Two dependencies gate this route, in order: the first answers 404 while the
    endpoint is disabled (so it is invisible by default), and only if enabled
    does the second require an administrator. The handler body runs solely for an
    enabled endpoint reached by an authenticated administrator.
    """
    body = _render_metrics(db)
    return Response(content=body, media_type=_METRICS_CONTENT_TYPE)
