"""Admin debug endpoints for inspecting a live process (issue #7).

Serves the in-process diagnostics from :mod:`..monitoring.diagnostics` as JSON:

* ``GET /api/v1/system/debug/tasks`` -- every asyncio task with its innermost
  await, plus default-executor statistics and a capture-runner summary (the
  same data ``SIGUSR2`` logs);
* ``GET /api/v1/system/debug/threads`` -- every thread's Python stack;
* ``GET /api/v1/system/debug/runners`` -- per capture runner, every
  ``CaptureState`` field, the heartbeat age, the last plan, and the latest
  exact-time fire-log row;
* ``POST /api/v1/system/debug/log-level`` -- raise (or lower) a
  ``timelapse_manager.*`` logger's level for a bounded time; it reverts on its
  own.

Access control mirrors ``/metrics``: the routes live on their own router so
they do not inherit the versioned API's blanket bearer dependency, answer 404
while ``observability.debug_endpoints_enabled`` is off, and require an
administrator (session or local bearer token) when on. Every payload passes
through the log redactor as a final pass, and the read routes are cached for one
second so they cannot be used to load the event loop.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import ExactTimeFire
from ..db.session import DbSessionDep
from ..logging import redact
from ..monitoring.diagnostics import (
    MAX_OVERRIDE_MINUTES,
    build_task_dump,
    runner_snapshots,
    threads_snapshot,
    validate_logger_name,
)
from ..monitoring.events import EventType, log_event
from ..runtime import get_context
from ..security.principal import (
    Principal,
    ensure_sentinel_admin,
    require_admin_principal,
)

debug_router = APIRouter()

# How long a read payload is reused. One second keeps a polling client (or a
# hostile admin session) from turning the dumps into load on the loop.
_CACHE_TTL_SECONDS = 1.0


def _require_debug_enabled() -> None:
    """Reject with 404 unless the debug endpoints are enabled.

    Runs before the admin gate, as for ``/metrics``, so a disabled deployment
    answers an unauthenticated probe with 404 rather than a revealing 401.
    """
    if not get_context().settings.observability.debug_endpoints_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


_GATED = [Depends(_require_debug_enabled), Depends(require_admin_principal)]


class _TtlCache:
    """A tiny per-application cache of the last payload for each route."""

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, dict[str, Any]]] = {}

    def get_or_build(
        self, key: str, build: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and now - entry[0] < self._ttl:
                return entry[1]
        payload = build()
        with self._lock:
            self._entries[key] = (time.monotonic(), payload)
        return payload


def _cache(request: Request) -> _TtlCache:
    """The cache bound to this application instance (created on first use)."""
    cache = getattr(request.app.state, "debug_cache", None)
    if cache is None:
        cache = _TtlCache(_CACHE_TTL_SECONDS)
        request.app.state.debug_cache = cache
    return cache


def _redacted(payload: dict[str, Any]) -> dict[str, Any]:
    """Final secret-scrubbing pass over a payload (URL userinfo, secret keys)."""
    scrubbed: dict[str, Any] = redact(payload)
    return scrubbed


@debug_router.get("/system/debug/tasks", dependencies=_GATED)
async def debug_tasks(request: Request) -> dict[str, Any]:
    """Every asyncio task by name with its innermost await, plus executor stats.

    ``async`` on purpose: listing the loop's tasks must happen on the loop's own
    thread. If this route hangs, the loop itself is blocked -- use ``SIGUSR1``
    or ``py-spy`` instead.
    """
    context = get_context()
    loop_monitor = context.loop_lag_monitor
    loop = asyncio.get_running_loop()

    def build() -> dict[str, Any]:
        dump = build_task_dump(loop, context.capture_supervisor)
        dump["event_loop_lag_seconds"] = (
            {
                "last": round(loop_monitor.last_lag_seconds, 6),
                "max_last_minute": round(loop_monitor.lag_seconds, 6),
            }
            if loop_monitor is not None
            else None
        )
        return _redacted(dump)

    return _cache(request).get_or_build("tasks", build)


@debug_router.get("/system/debug/threads", dependencies=_GATED)
def debug_threads(request: Request) -> dict[str, Any]:
    """Every thread's Python stack (``sys._current_frames``), with thread names."""
    return _cache(request).get_or_build(
        "threads", lambda: _redacted({"threads": threads_snapshot()})
    )


def _latest_fire_rows(session: Session, project_ids: list[int]) -> dict[int, Any]:
    """The most recent exact-time fire-log row for each of ``project_ids``."""
    if not project_ids:
        return {}
    rows = session.execute(
        select(ExactTimeFire)
        .where(ExactTimeFire.project_id.in_(project_ids))
        .order_by(ExactTimeFire.fired_at.desc(), ExactTimeFire.id.desc())
    ).scalars()
    latest: dict[int, Any] = {}
    for row in rows:
        if row.project_id in latest:
            continue
        latest[row.project_id] = {
            "anchor_id": row.anchor_id,
            "local_date": row.local_date,
            "status": row.status,
            "fired_at": row.fired_at.isoformat() if row.fired_at else None,
            "frame_id": row.frame_id,
            "detail": row.detail,
            "attempts": row.attempts,
            "next_attempt_at": (
                row.next_attempt_at.isoformat() if row.next_attempt_at else None
            ),
        }
    return latest


@debug_router.get("/system/debug/runners", dependencies=_GATED)
def debug_runners(request: Request, db: DbSessionDep) -> dict[str, Any]:
    """Per capture runner: state, heartbeat age, last plan, latest anchor fire.

    ``listeners`` is the same for the event listeners (#69 CAM-R7): transport,
    connected since, last event, last progress age, the redacted last error, the
    failure streak, relaunches, dropped events and downgrades.

    A plain ``def`` (served from the request threadpool, not asyncio's default
    executor), so the fire-log read cannot queue behind a saturated executor.
    """
    context = get_context()

    def build() -> dict[str, Any]:
        runners = runner_snapshots(context.capture_supervisor)
        project_ids = [
            int(runner["project_id"]) for runner in runners if "project_id" in runner
        ]
        latest = _latest_fire_rows(db, project_ids)
        for runner in runners:
            runner["latest_exact_time_fire"] = latest.get(runner.get("project_id", -1))
        supervisor = context.capture_supervisor
        listeners = supervisor.listener_snapshots() if supervisor is not None else []
        return _redacted({"runners": runners, "listeners": listeners})

    return _cache(request).get_or_build("runners", build)


class LogLevelRequest(BaseModel):
    """Body of ``POST /system/debug/log-level``."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"]
    logger: str = "timelapse_manager"
    minutes: int = Field(default=30, ge=1, le=MAX_OVERRIDE_MINUTES)

    @field_validator("logger")
    @classmethod
    def _logger_in_package(cls, value: str) -> str:
        return validate_logger_name(value)


@debug_router.post("/system/debug/log-level", dependencies=_GATED)
def debug_log_level(
    body: LogLevelRequest,
    db: DbSessionDep,
    principal: Annotated[Principal, Depends(require_admin_principal)],
) -> dict[str, Any]:
    """Set a ``timelapse_manager.*`` logger's level for ``minutes``, then revert.

    The change is immediate and process-local; it never touches the config file
    and does not survive a restart. Each change is recorded as an audit event.
    """
    overrides = get_context().log_level_overrides
    if overrides is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="log-level overrides are not available",
        )
    try:
        applied = overrides.apply(body.logger, body.level, body.minutes)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    # The audit row's actor is a foreign key; materialise the sentinel admin a
    # bearer-token caller resolves to, as the alerts API does.
    ensure_sentinel_admin(db)
    log_event(
        db,
        scope="system",
        scope_id=None,
        level="info",
        message=(
            f"log level for {body.logger} set to {body.level} for "
            f"{body.minutes} minute(s)"
        ),
        type=EventType.AUDIT_CONTROL_ACTION.value,
        metadata={
            "action": "debug.log_level",
            "logger": body.logger,
            "log_level": body.level,
            "minutes": body.minutes,
        },
        actor_user_id=principal.user_id,
    )
    return {"applied": applied, "active": overrides.active()}
