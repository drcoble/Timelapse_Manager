"""System information and metrics endpoints.

Exposes a non-secret summary of the running process: application and ffmpeg
versions, the live database status, the network binding, and the log level.
Database credentials embedded in the connection URL are redacted; no token or
other secret is ever returned.

``GET /api/v1/system/render-activity`` reports the render worker's live tasks
against the queued rows, so an upgrade script can tell running work from rows
stranded by a dead task (#67 R1).

A second, separately mounted router serves a Prometheus metrics exposition. It
is off by default and, when enabled, is gated behind administrator
authentication -- the listener binds all interfaces and offers no unauthenticated
path, so the figures (which can reveal operational scale) are never public. The
exposition text is built by hand rather than via a metrics client library to
keep the distributed bundle lean.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from ..config import Settings
from ..db.models import Event, Project, RenderJob
from ..db.models.render_job import active_job_filter
from ..db.session import DbSessionDep, session_scope
from ..monitoring import readiness
from ..monitoring.diagnostics import executor_snapshot
from ..runtime import AppContext, get_context
from ..security.principal import require_metrics_reader
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

# Capture-runner states reported one-hot by ``tlm_runner_state``, so a stalled or
# failing runner shows as a 0 -> 1 flip on a fixed series. A state outside this
# set (should one be added) is still reported, as an extra series.
_RUNNER_STATES = ("running", "idle", "error", "stopped")

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


def _render_rows_without_worker(
    session_factory: sessionmaker[Session],
) -> dict[str, int]:
    """Count queued render rows when no render worker runs. Synchronous.

    With no worker there is no live task, so every ``encoding`` row is an
    orphan.
    """
    with session_scope(session_factory) as session:
        counts = dict(
            session.execute(
                select(RenderJob.status, func.count())
                .where(active_job_filter())
                .group_by(RenderJob.status)
            )
            .tuples()
            .all()
        )
    encoding = int(counts.get("encoding", 0))
    return {
        "live_tasks": 0,
        "pending_rows": int(counts.get("pending", 0)),
        "encoding_rows": encoding,
        "orphans": encoding,
    }


@router.get("/system/render-activity")
async def render_activity() -> dict[str, int]:
    """Report render work in progress, for the upgrade gate (#67 R1, O1).

    ``live_tasks`` is the jobs with a running task in this process,
    ``pending_rows`` and ``encoding_rows`` the rows in those states, and
    ``orphans`` the ``encoding`` rows with no live task (dead work the worker
    sweeps to ``failed`` on its next cycle). An upgrade is safe when
    ``live_tasks + pending_rows`` is zero; counting rows alone, as the old gate
    did, also refuses on orphans.

    ``async`` because the live task ids are read under the worker's claim lock
    on the event loop; it only awaits, and the row counts run in a worker
    thread (#62).
    """
    context = get_context()
    queue = context.render_queue
    if queue is not None:
        return (await queue.activity()).as_dict()
    return await run_in_threadpool(_render_rows_without_worker, context.session_factory)


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

    lines.extend(_runtime_metric_lines())

    # A trailing newline terminates the final sample line, as the exposition
    # format expects.
    return "\n".join(lines) + "\n"


def _runtime_metric_lines() -> list[str]:
    """Stall-detection series read from the live process, not the database (#7).

    ``tlm_runner_last_cycle_age_seconds`` is the age of each capture runner's
    heartbeat (alert well above ``capture.max_idle_sleep_seconds``);
    ``tlm_runner_state`` is one-hot per runner and state; the executor gauges
    describe asyncio's default executor (what ``to_thread`` uses); and
    ``tlm_event_loop_lag_seconds`` is the worst ``loop-lag`` tick of the last
    minute. Runners are
    labelled by project name (as ``disk_used_bytes`` is) plus the stable id.
    """
    context = get_context()
    now = datetime.now(UTC)
    supervisor = context.capture_supervisor
    snapshots = supervisor.runner_snapshots() if supervisor is not None else []
    monitor = context.loop_lag_monitor
    executor = executor_snapshot(monitor.loop if monitor is not None else None)

    lines: list[str] = []
    lines.append(
        "# HELP tlm_runner_last_cycle_age_seconds Seconds since each capture "
        "runner's last loop cycle."
    )
    lines.append("# TYPE tlm_runner_last_cycle_age_seconds gauge")
    for snapshot in snapshots:
        beat = snapshot.state.last_cycle_at or snapshot.state.started_at
        if beat is None:
            continue
        age = max(0.0, (now - beat).total_seconds())
        labels = _runner_labels(snapshot.project_name, snapshot.state.project_id)
        lines.append(f"tlm_runner_last_cycle_age_seconds{{{labels}}} {age:.3f}")

    lines.append("# HELP tlm_runner_state Capture runner state (1 = current).")
    lines.append("# TYPE tlm_runner_state gauge")
    for snapshot in snapshots:
        labels = _runner_labels(snapshot.project_name, snapshot.state.project_id)
        current = snapshot.state.state
        states = _RUNNER_STATES + ((current,) if current not in _RUNNER_STATES else ())
        for state in states:
            value = 1 if state == current else 0
            lines.append(
                f'tlm_runner_state{{{labels},state="{_escape_label_value(state)}"}} '
                f"{value}"
            )

    lines.append(
        "# HELP tlm_executor_queue_depth Work items queued on the default executor."
    )
    lines.append("# TYPE tlm_executor_queue_depth gauge")
    lines.append(f"tlm_executor_queue_depth {int(executor.get('queue_depth') or 0)}")

    lines.append("# HELP tlm_executor_threads Worker threads of the default executor.")
    lines.append("# TYPE tlm_executor_threads gauge")
    lines.append(f"tlm_executor_threads {int(executor.get('threads') or 0)}")

    lines.append(
        "# HELP tlm_event_loop_lag_seconds Worst lateness of the 1 s loop-lag "
        "tick over the last minute."
    )
    lines.append("# TYPE tlm_event_loop_lag_seconds gauge")
    lag = monitor.lag_seconds if monitor is not None else 0.0
    lines.append(f"tlm_event_loop_lag_seconds {lag:.6f}")

    lines.extend(_readiness_metric_lines(context, now))
    return lines


def _readiness_metric_lines(context: AppContext, now: datetime) -> list[str]:
    """The ``/healthz`` readiness verdict and its render and poll inputs (#67 R5).

    ``tlm_health_status`` is one-hot over ``ok``/``degraded``/``failed``, from
    the same evaluation ``/healthz`` serves. The other three are the raw
    figures behind three of its checks; a series whose component is not running
    is omitted rather than reported as zero.
    """
    database = readiness.db_status(context.session_factory)
    result = readiness.evaluate_readiness(
        context,
        database=database,
        components=readiness.component_statuses(context),
        now=now,
    )
    lines: list[str] = []
    lines.append(
        "# HELP tlm_health_status Readiness status reported by /healthz (1 = current)."
    )
    lines.append("# TYPE tlm_health_status gauge")
    for state in readiness.STATUSES:
        value = 1 if state == result.status else 0
        lines.append(f'tlm_health_status{{status="{state}"}} {value}')

    queue = context.render_queue
    queue_running = readiness.component_status(queue) == "ok"
    lines.append(
        "# HELP tlm_render_orphans Encoding render jobs with no live task that "
        "the last sweep could not fail."
    )
    lines.append("# TYPE tlm_render_orphans gauge")
    if queue is not None and queue_running:
        lines.append(f"tlm_render_orphans {queue.unreclaimed_orphans}")

    lines.append(
        "# HELP tlm_render_oldest_encoding_age_seconds Age of the oldest "
        "encoding render job at the last sweep (0 when none)."
    )
    lines.append("# TYPE tlm_render_oldest_encoding_age_seconds gauge")
    if queue is not None and queue_running:
        age = readiness.oldest_encoding_age_seconds(
            queue.oldest_encoding_started_at, now
        )
        lines.append(f"tlm_render_oldest_encoding_age_seconds {age or 0.0:.3f}")

    dispatcher = context.notification_dispatcher
    lines.append(
        "# HELP tlm_dispatcher_last_poll_age_seconds Seconds since the "
        "notification dispatcher's last successful poll."
    )
    lines.append("# TYPE tlm_dispatcher_last_poll_age_seconds gauge")
    if dispatcher is not None and readiness.component_status(dispatcher) == "ok":
        poll_age = dispatcher.last_poll_age_seconds()
        if poll_age is not None:
            lines.append(f"tlm_dispatcher_last_poll_age_seconds {poll_age:.3f}")
    return lines


def _runner_labels(project_name: str, project_id: int) -> str:
    """The ``project``/``project_id`` label pair for a runner series."""
    return f'project="{_escape_label_value(project_name)}",project_id="{project_id}"'


@metrics_router.get(
    "/metrics",
    dependencies=[Depends(_require_metrics_enabled), Depends(require_metrics_reader)],
)
def metrics(db: DbSessionDep) -> Response:
    """Serve the Prometheus exposition, computed on scrape from the database.

    Two dependencies gate this route, in order: the first answers 404 while the
    endpoint is disabled (so it is invisible by default), and only if enabled
    does the second require an administrator session, the local admin token, or
    the read-only metrics token (see :func:`require_metrics_reader`). The handler
    body runs solely for an enabled endpoint reached by one of those.
    """
    body = _render_metrics(db)
    return Response(content=body, media_type=_METRICS_CONTENT_TYPE)
