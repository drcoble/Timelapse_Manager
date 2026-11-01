"""The diagnostic snapshot: section builders and the zip bundle.

A snapshot is a set of named members -- JSON files, ``log.txt`` and the user's
optional image -- built from explicit inputs (settings, a session, an optional
running supervisor and pre-captured loop diagnostics). Nothing here reads the
process-wide application context, so the web route, the API and the offline
CLI all build the same bundle.

Two rules hold throughout:

* **Allow-list, then redact twice.** Each section copies named fields (never a
  whole credential-bearing row), passes through
  :func:`timelapse_manager.logging.redact`, and the finished bytes of every
  member except the user's own image go through the value-based
  :class:`~.redaction.Scrubber`.
* **No frame or image data.** Frames appear as metadata rows only; the only
  image in a bundle is the one the reporter attached.

Every section builder degrades a failure to ``{"error": ...}`` so one broken
section never loses the report.
"""

from __future__ import annotations

import io
import json
import logging
import os
import platform
import shutil
import socket
import sys
import time
import urllib.parse
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db.models import (
    Camera,
    Event,
    ExactTimeFire,
    Frame,
    Project,
    RenderJob,
)
from ..logging import get_ring_buffer, redact, redact_text
from ..monitoring.diagnostics import (
    build_task_dump,
    runner_snapshots,
    threads_snapshot,
)
from ..version import get_app_version, get_build_info
from .redaction import REDACTED, Scrubber
from .store import AREAS, Attachment

if TYPE_CHECKING:
    import asyncio

    from ..capture import CaptureSupervisor
    from ..monitoring.diagnostics import LoopLagMonitor
    from .anonymize import Pseudonymizer, Verifier

logger = logging.getLogger(__name__)

# Bounds on the scoped data, per the issue.
_PROJECT_FRAMES = 20
_PROJECT_FIRE_DAYS = 7
_PROJECT_RENDER_JOBS = 50
_PROJECT_EVENTS = 100
_SYSTEM_EVENTS = 200

# How much of a log file is read, from its end, when looking for the window.
_LOG_FILE_TAIL_BYTES = 16 * 1024 * 1024

# Plain-language summary shown on the form before the user submits. Kept here,
# next to the builders, so the two cannot drift apart.
SNAPSHOT_SUMMARY: tuple[str, ...] = (
    "App version, build, database revision, ffmpeg version, uptime, and the "
    "host's OS, CPU count, memory and free disk space.",
    "The effective configuration with every password, token, key and "
    "credential removed.",
    "The state of every capture runner and event listener, and (when the "
    "service is running) which internal tasks and threads are busy.",
    "For the related project: its settings, schedule and anchors, the last 20 "
    "frames' details (no images), recent exact-time and render records, and its "
    "last 100 events.",
    "The last 200 events across the system.",
    "The app's own log from 30 minutes either side of the time you give.",
    "This page's address, your browser name, your username and role, and the "
    "ids of your last few requests.",
)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportMeta:
    """What the reporter said, plus who and when."""

    report_key: str
    title: str
    description: str
    area: str
    reported_at: datetime
    created_at: datetime
    username: str | None
    role: str | None
    project_id: int | None
    camera_id: int | None
    include_snapshot: bool
    source: str  # "web", "api" or "cli"
    # Anonymized for a public issue (#21): placeholders replace identifying
    # values and the screenshot is left out.
    anonymize: bool = False


@dataclass(frozen=True)
class RequestContext:
    """Where the report was filed from. Never headers or cookies wholesale."""

    page_url: str | None = None
    user_agent: str | None = None
    recent_requests: tuple[dict[str, Any], ...] = ()


@dataclass
class SnapshotSources:
    """The live inputs a snapshot reads beyond the database.

    :param supervisor: the running capture supervisor, or ``None`` offline.
    :param live_diagnostics: the task/thread dump captured on the event loop's
        own thread, or ``None`` when there is no loop (the CLI).
    :param ffmpeg_version: the probed ffmpeg version string.
    :param process_started_at: when the reporting process started (UTC), for
        uptime; ``None`` when unknown.
    :param offline_reason: why live sections are unavailable (CLI builds).
    """

    supervisor: CaptureSupervisor | None = None
    live_diagnostics: dict[str, Any] | None = None
    ffmpeg_version: str = "unknown"
    process_started_at: datetime | None = None
    offline_reason: str | None = None
    extra_app_fields: dict[str, Any] = field(default_factory=dict)


def capture_live_diagnostics(
    loop: asyncio.AbstractEventLoop,
    supervisor: CaptureSupervisor | None,
    loop_lag_monitor: LoopLagMonitor | None,
) -> dict[str, Any]:
    """The #7 task and thread dumps plus loop lag, for ``diagnostics.json``.

    Must be called on ``loop``'s own thread (it lists the loop's tasks), which
    is why the web and API routes that file a report are ``async``. Never
    raises: a failed dump becomes ``available: False`` with the reason.
    """
    try:
        dump = build_task_dump(loop, supervisor)
        dump["threads"] = threads_snapshot()
    except Exception as exc:  # noqa: BLE001 - a broken dump must not block a report
        logger.warning("report task dump failed: %s", type(exc).__name__)
        return {"available": False, "reason": f"task dump failed: {type(exc).__name__}"}
    dump["event_loop_lag_seconds"] = (
        {
            "last": round(loop_lag_monitor.last_lag_seconds, 6),
            "max_last_minute": round(loop_lag_monitor.lag_seconds, 6),
        }
        if loop_lag_monitor is not None
        else None
    )
    return dump


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def _row(obj: Any, *, exclude: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Copy an ORM row's mapped columns into a JSON-friendly dict."""
    mapper = inspect(obj).mapper
    return {
        attr.key: _jsonable(getattr(obj, attr.key))
        for attr in mapper.column_attrs
        if attr.key not in exclude
    }


def _guard(name: str, build: Callable[[], Any]) -> Any:
    """Run a section builder; turn a failure into an ``error`` entry."""
    try:
        return build()
    except Exception as exc:  # noqa: BLE001 - one bad section must not lose the report
        logger.warning("report section %s failed: %s", name, type(exc).__name__)
        return {"error": f"{name} unavailable: {type(exc).__name__}"}


def _mask_url(value: str) -> str:
    """Reduce a URL to scheme and host, dropping a path or query secret."""
    parsed = urllib.parse.urlsplit(value)
    if not parsed.scheme or not parsed.hostname:
        return REDACTED
    return f"{parsed.scheme}://{parsed.hostname}/{REDACTED}"


def _mask_urls(value: Any) -> Any:
    """Mask every http(s) URL inside a structured value (post-render actions)."""
    if isinstance(value, dict):
        return {key: _mask_urls(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_mask_urls(item) for item in value]
    if isinstance(value, str) and value.lower().startswith(("http://", "https://")):
        return _mask_url(value)
    return value


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _process_start_time() -> datetime | None:
    """When this process started, from ``/proc`` on Linux; else ``None``."""
    try:
        stat = Path("/proc/self/stat").read_text()
        # Field 22 (starttime, in clock ticks since boot) follows the
        # parenthesised command name, which may itself contain spaces.
        fields = stat.rsplit(")", 1)[1].split()
        ticks = int(fields[19])
        boot = next(
            int(line.split()[1])
            for line in Path("/proc/stat").read_text().splitlines()
            if line.startswith("btime ")
        )
        return datetime.fromtimestamp(boot + ticks / os.sysconf("SC_CLK_TCK"), UTC)
    except (OSError, ValueError, IndexError, StopIteration, AttributeError):
        return None


def _memory() -> dict[str, Any]:
    """Host memory from ``/proc/meminfo`` (Linux) or ``sysconf``."""
    info: dict[str, Any] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                info[f"{key}_bytes"] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        try:
            pages = os.sysconf("SC_PHYS_PAGES")
            info["MemTotal_bytes"] = pages * os.sysconf("SC_PAGE_SIZE")
        except (ValueError, OSError, AttributeError):
            pass
    try:
        import resource

        # ru_maxrss is KiB on Linux and bytes on macOS.
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        info["process_max_rss_bytes"] = rss if sys.platform == "darwin" else rss * 1024
    except (ImportError, OSError, ValueError):
        pass
    return info


def _disk(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"error": "not configured"}
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        return {"path": str(path), "error": type(exc).__name__}
    return {
        "path": str(path),
        "total_bytes": usage.total,
        "free_bytes": usage.free,
        "free_percent": round(usage.free / usage.total * 100, 2)
        if usage.total
        else None,
    }


def _alembic_revision(session: Session) -> str:
    from alembic.migration import MigrationContext

    revision = MigrationContext.configure(session.connection()).get_current_revision()
    return revision or "unknown"


def app_section(
    session: Session, settings: Settings, sources: SnapshotSources, now: datetime
) -> dict[str, Any]:
    """Version, build, database revision, ffmpeg, uptime and host resources."""
    started = sources.process_started_at or _process_start_time()
    section: dict[str, Any] = {
        "app_version": get_app_version(),
        "build": get_build_info(),
        "alembic_revision": _guard("alembic", lambda: _alembic_revision(session)),
        "ffmpeg_version": sources.ffmpeg_version,
        "pid": os.getpid(),
        "process_started_at": _iso(started),
        "uptime_seconds": (
            round((now.replace(tzinfo=UTC) - started).total_seconds(), 1)
            if started is not None
            else None
        ),
        "python": sys.version.split()[0],
        "frozen": bool(getattr(sys, "frozen", False)),
        "host": {
            "hostname": socket.gethostname(),
            "os": platform.system(),
            "kernel": platform.release(),
            "os_version": platform.version(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
            "load_average": list(os.getloadavg())
            if hasattr(os, "getloadavg")
            else None,
        },
        "memory": _memory(),
        "disk": {
            "state": _disk(settings.paths.data_dir),
            "frames": _disk(settings.paths.frames_root),
        },
    }
    section.update(sources.extra_app_fields)
    return section


def config_section(settings: Settings) -> dict[str, Any]:
    """The effective configuration with secrets masked.

    The same pattern redaction as the logs and the debug endpoints: any key
    naming a password/token/secret/key is masked and URL credentials are
    stripped. ``cameras``/``projects`` config entries go through it as well.
    """
    dumped = settings.model_dump(mode="json")
    redacted: dict[str, Any] = redact(dumped)
    return redacted


def supervisor_section(
    session: Session, sources: SnapshotSources, now: datetime
) -> dict[str, Any]:
    """Every runner's state, runner vs active-project count, listeners."""
    active_projects = session.execute(
        select(func.count(Project.id)).where(Project.lifecycle_state == "active")
    ).scalar_one()
    supervisor = sources.supervisor
    if supervisor is None:
        return {
            "available": False,
            "reason": sources.offline_reason or "capture supervisor not running",
            "active_project_count": active_projects,
        }
    runners = runner_snapshots(supervisor, now.replace(tzinfo=UTC))
    return {
        "available": True,
        "running": supervisor.is_running(),
        "runner_count": len(runners),
        "active_project_count": active_projects,
        "last_reconcile_at": _iso(getattr(supervisor, "last_reconcile_at", None)),
        "runners": runners,
        "listeners": supervisor.listener_snapshots(),
    }


def diagnostics_section(sources: SnapshotSources) -> dict[str, Any]:
    """The #7 task and thread dumps, captured on the loop's own thread."""
    if sources.live_diagnostics is None:
        return {
            "available": False,
            "reason": sources.offline_reason or "no running event loop",
        }
    # A dump that failed carries its own ``available: False`` and reason,
    # which override the default here.
    return {"available": True, **sources.live_diagnostics}


_CAMERA_FIELDS = (
    "id",
    "name",
    "address",
    "protocol",
    "credentials_inherit_default",
    "default_resolution",
    "geolocation_latitude",
    "geolocation_longitude",
    "geolocation_source",
    "device_hostname",
    "device_hostname_source",
)


def camera_section(session: Session, camera_id: int) -> dict[str, Any] | None:
    """The related camera's non-secret fields. Credentials are never copied."""
    camera = session.get(Camera, camera_id)
    if camera is None:
        return None
    data = {name: _jsonable(getattr(camera, name)) for name in _CAMERA_FIELDS}
    data["has_credentials"] = bool(camera.credentials)
    data["snapshot_uri"] = redact_text(camera.snapshot_uri or "") or None
    data["stream_uri"] = redact_text(camera.stream_uri or "") or None
    return data


def project_section(
    session: Session, project_id: int, now: datetime
) -> dict[str, Any] | None:
    """The related project's settings and recent history (metadata only)."""
    project = session.get(Project, project_id)
    if project is None:
        return None
    settings_row = _row(project)
    settings_row["post_render_actions"] = _mask_urls(project.post_render_actions)
    frames = session.execute(
        select(Frame)
        .where(Frame.project_id == project_id)
        .order_by(Frame.sequence_index.desc())
        .limit(_PROJECT_FRAMES)
    ).scalars()
    fires = session.execute(
        select(ExactTimeFire)
        .where(ExactTimeFire.project_id == project_id)
        .where(ExactTimeFire.fired_at >= now - timedelta(days=_PROJECT_FIRE_DAYS))
        .order_by(ExactTimeFire.fired_at.desc())
    ).scalars()
    jobs = session.execute(
        select(RenderJob)
        .where(RenderJob.project_id == project_id)
        .order_by(RenderJob.id.desc())
        .limit(_PROJECT_RENDER_JOBS)
    ).scalars()
    events = session.execute(
        select(Event)
        .where(Event.scope == "project")
        .where(Event.scope_id == project_id)
        .order_by(Event.id.desc())
        .limit(_PROJECT_EVENTS)
    ).scalars()
    return {
        "project": settings_row,
        "latest_frames": [_row(frame) for frame in frames],
        "exact_time_fires_last_7_days": [_row(fire) for fire in fires],
        "render_jobs": [
            {**_row(job), "output_settings": _mask_urls(job.output_settings)}
            for job in jobs
        ],
        "events": [_event(event) for event in events],
    }


def _event(event: Event) -> dict[str, Any]:
    return {
        "id": event.id,
        "timestamp": _iso(event.timestamp),
        "scope": event.scope,
        "scope_id": event.scope_id,
        "level": event.level,
        "message": event.message,
        "metadata": event.event_metadata,
        "actor_user_id": event.actor_user_id,
        "alert_cleared_at": _iso(event.alert_cleared_at),
    }


def events_section(session: Session) -> list[dict[str, Any]]:
    """The last 200 events across every scope, newest first."""
    rows = session.execute(
        select(Event).order_by(Event.id.desc()).limit(_SYSTEM_EVENTS)
    ).scalars()
    return [_event(event) for event in rows]


def request_section(meta: ReportMeta, context: RequestContext) -> dict[str, Any]:
    """Where the report came from: page, browser, reporter, recent requests."""
    return {
        "source": meta.source,
        "page_url": context.page_url,
        "user_agent": context.user_agent,
        "username": meta.username,
        "role": meta.role,
        "recent_requests": list(context.recent_requests),
    }


# ---------------------------------------------------------------------------
# Log excerpt
# ---------------------------------------------------------------------------


def _parse_file_timestamp(line: str) -> datetime | None:
    """Read the leading timestamp of a log-file line (local time) as UTC.

    The JSON format carries ``"timestamp"``; the text format starts with
    ``YYYY-MM-DD HH:MM:SS,mmm``. Both use the process's local time (the
    ``logging`` default), so the parsed value is treated as local.
    """
    stamp: str | None = None
    if line.startswith("{"):
        try:
            value = json.loads(line).get("timestamp")
            stamp = value if isinstance(value, str) else None
        except ValueError:
            stamp = None
    else:
        stamp = line[:23]
    if not stamp:
        return None
    try:
        parsed = datetime.strptime(stamp[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return parsed.astimezone(UTC)


def _file_log_lines(
    path: Path, start: datetime, end: datetime, max_lines: int
) -> list[str]:
    """Lines of a log file inside the window (continuation lines kept)."""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - _LOG_FILE_TAIL_BYTES))
        raw = handle.read()
    lines = raw.decode("utf-8", errors="replace").splitlines()
    kept: list[str] = []
    inside = False
    for line in lines:
        stamp = _parse_file_timestamp(line)
        if stamp is not None:
            inside = start <= stamp <= end
        if inside:
            kept.append(redact_text(line))
    return kept[-max_lines:]


def log_text(settings: Settings, reported_at: datetime, *, offline: bool) -> str:
    """The app's log within the window around ``reported_at``, as text.

    Reads the configured log file when one is set, otherwise the in-memory
    ring buffer. Every line passes :func:`redact_text`; the bundle's value
    scrub runs over the result afterwards.
    """
    reporting = settings.reporting
    window = timedelta(minutes=reporting.log_window_minutes)
    centre = reported_at.replace(tzinfo=UTC)
    start, end = centre - window, centre + window
    header = (
        f"# Log lines from {start.isoformat()} to {end.isoformat()} "
        f"(at most {reporting.log_max_lines})\n"
    )
    sink = settings.logging.file_sink
    if sink is not None:
        try:
            lines = _file_log_lines(Path(sink), start, end, reporting.log_max_lines)
        except OSError as exc:
            return header + f"# log file unreadable: {type(exc).__name__}\n"
        return header + f"# source: log file {sink}\n" + "\n".join(lines) + "\n"
    if offline:
        return header + (
            "# No log file is configured (logging.file_sink) and the in-memory "
            "buffer lives in the service process; read the journal instead.\n"
        )
    buffer = get_ring_buffer()
    if buffer is None:
        return header + "# The in-memory log buffer is disabled.\n"
    start_ts, end_ts = start.timestamp(), end.timestamp()
    texts = [line.text for line in buffer.lines() if start_ts <= line.created <= end_ts]
    texts = texts[-reporting.log_max_lines :]
    return header + "# source: in-memory buffer\n" + "\n".join(texts) + "\n"


# ---------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------


def report_section(meta: ReportMeta, attachment: Attachment | None) -> dict[str, Any]:
    """What the reporter wrote. Uses ``report_id`` (``key`` would be masked).

    The free text gets URL-credential redaction too: users paste error
    messages, and those often carry ``rtsp://user:pass@...``.
    """
    return {
        "report_id": meta.report_key,
        "title": redact_text(meta.title),
        "description": redact_text(meta.description),
        "area": meta.area,
        "area_label": AREAS.get(meta.area, meta.area),
        "reported_at": _iso(meta.reported_at),
        "filed_at": _iso(meta.created_at),
        "reporter": {"username": meta.username, "role": meta.role},
        "project_id": meta.project_id,
        "camera_id": meta.camera_id,
        "snapshot_included": meta.include_snapshot,
        "attachment": attachment.member_name if attachment else None,
        "source": meta.source,
        "anonymized": meta.anonymize,
    }


def build_members(
    session: Session,
    settings: Settings,
    meta: ReportMeta,
    context: RequestContext,
    sources: SnapshotSources,
) -> dict[str, Any]:
    """Build every JSON/text member of the bundle (before the value scrub)."""
    now = meta.created_at
    members: dict[str, Any] = {
        "report.json": report_section(meta, None),
        "request.json": _guard("request", lambda: request_section(meta, context)),
    }
    if not meta.include_snapshot:
        return members
    members["app.json"] = _guard(
        "app", lambda: app_section(session, settings, sources, now)
    )
    members["config.json"] = _guard("config", lambda: config_section(settings))
    members["supervisor.json"] = _guard(
        "supervisor", lambda: supervisor_section(session, sources, now)
    )
    members["diagnostics.json"] = _guard(
        "diagnostics", lambda: diagnostics_section(sources)
    )
    if meta.project_id is not None:
        project_id = meta.project_id
        members["project.json"] = _guard(
            "project", lambda: project_section(session, project_id, now)
        )
    if meta.camera_id is not None:
        camera_id = meta.camera_id
        members["camera.json"] = _guard(
            "camera", lambda: camera_section(session, camera_id)
        )
    members["events.json"] = _guard("events", lambda: events_section(session))
    members["log.txt"] = _guard(
        "log",
        lambda: log_text(
            settings, meta.reported_at, offline=sources.offline_reason is not None
        ),
    )
    return members


def _encode(name: str, value: Any) -> bytes:
    if name.endswith(".txt"):
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        return text.encode("utf-8")
    # Pattern redaction as a pass over everything (the sections already
    # allow-list their fields); ``report.json`` is skipped because its
    # ``report_id``/free text are the user's own words, not config.
    payload = value if name == "report.json" else redact(value)
    return json.dumps(payload, indent=2, default=str, ensure_ascii=False).encode(
        "utf-8"
    )


ANONYMIZED_NOTE = (
    "Anonymized: user, project and camera names, host names, addresses and "
    "paths are replaced with placeholders (user-1, camera-1, host-1, "
    "/path/data); the screenshot is left out. See "
    "docs/operations/problem-reports.md."
)


def build_bundle(
    members: dict[str, Any],
    meta: ReportMeta,
    attachment: Attachment | None,
    scrubber: Scrubber,
    *,
    anonymizer: Pseudonymizer | None = None,
    verifier: Verifier | None = None,
    extra_checks: Iterable[Callable[[], None]] = (),
) -> bytes:
    """Encode, scrub and zip the members (plus the attachment) into bytes.

    With ``anonymizer`` (``meta.anonymize``), every member is anonymized after
    the secret scrub -- after, so a secret URL is masked whole before a host
    inside it is replaced -- the attachment is dropped, and ``verifier`` checks
    every finished member before anything is zipped; it raises to refuse.
    ``extra_checks`` (the stored title and description) run in the same pass,
    so a refusal names every short or generic name at once.
    """
    if anonymizer is not None:
        attachment = None
    members = dict(members)
    members["report.json"] = report_section(meta, attachment)
    encoded = {
        name: scrubber.scrub(_encode(name, value)) for name, value in members.items()
    }
    manifest: dict[str, Any] = {
        "report_id": meta.report_key,
        "generated_at": _iso(meta.created_at),
        "files": sorted([*encoded, *([attachment.member_name] if attachment else [])]),
        "redaction": (
            "Secrets are masked as *** by pattern and as "
            f"{REDACTED} by value; see docs/operations/problem-reports.md."
        ),
    }
    if anonymizer is not None:
        manifest["anonymized"] = ANONYMIZED_NOTE
    encoded["manifest.json"] = scrubber.scrub(
        json.dumps(manifest, indent=2).encode("utf-8")
    )
    if anonymizer is not None:
        encoded = {
            name: anonymizer.member(name, data) for name, data in encoded.items()
        }
    if verifier is not None:
        from .anonymize import verify_all

        checks: list[Callable[[], None]] = list(extra_checks)
        checks += [
            partial(verifier.check_member, name, data) for name, data in encoded.items()
        ]
        verify_all(checks, snapshot_optional=meta.source == "web")
    buffer = io.BytesIO()
    stamp = time.gmtime()[:6]
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(encoded.items()):
            archive.writestr(
                zipfile.ZipInfo(name, date_time=stamp), data, zipfile.ZIP_DEFLATED
            )
        if attachment is not None:
            # The user's own upload, stored as-is (images are already compressed).
            archive.writestr(
                zipfile.ZipInfo(attachment.member_name, date_time=stamp),
                attachment.data,
                zipfile.ZIP_STORED,
            )
    return buffer.getvalue()
