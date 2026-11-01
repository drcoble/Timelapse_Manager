"""The in-memory signals behind ``/healthz`` readiness (#67 R5).

* the notification dispatcher's poll heartbeat (``last_poll_at``);
* the render queue's ``unreclaimed_orphans``;
* :func:`readiness.evaluate_readiness` edge cases that the HTTP tests in
  ``test_healthz.py`` do not reach (startup grace, clock kinds).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from timelapse_manager.capture import CaptureSupervisor
from timelapse_manager.config.settings import (
    MonitoringSettings,
    RenderSettings,
    Settings,
)
from timelapse_manager.encode.encoder import Encoder
from timelapse_manager.encode.ffmpeg_impl import render_time_ceiling
from timelapse_manager.monitoring import NotificationDispatcher, readiness
from timelapse_manager.render import RenderQueue

# ---------------------------------------------------------------------------
# Dispatcher heartbeat
# ---------------------------------------------------------------------------


def _dispatcher(interval: float = 0.01) -> NotificationDispatcher:
    dispatcher = NotificationDispatcher(
        MagicMock(),
        [],
        MonitoringSettings(autostart=False, poll_interval_seconds=interval),
    )
    dispatcher._current_max_id = lambda: 0  # type: ignore[method-assign]  # noqa: SLF001
    return dispatcher


async def _run_loop_briefly(dispatcher: NotificationDispatcher) -> None:
    await dispatcher.start()
    try:
        await asyncio.sleep(0.1)
    finally:
        await dispatcher.stop()


def test_dispatcher_never_started_has_no_age() -> None:
    dispatcher = _dispatcher()
    assert dispatcher.last_poll_at is None
    assert dispatcher.last_poll_age_seconds() is None


async def test_successful_poll_stamps_last_poll_at() -> None:
    dispatcher = _dispatcher()
    calls = 0

    async def _ok() -> None:
        nonlocal calls
        calls += 1

    dispatcher._poll_and_launch = _ok  # type: ignore[method-assign]  # noqa: SLF001
    await _run_loop_briefly(dispatcher)
    assert calls >= 1
    assert dispatcher.last_poll_at is not None
    age = dispatcher.last_poll_age_seconds()
    assert age is not None and age < 5.0


async def test_failing_poll_does_not_stamp_and_ages_from_start() -> None:
    dispatcher = _dispatcher()

    async def _fail() -> None:
        raise RuntimeError("database is locked")

    dispatcher._poll_and_launch = _fail  # type: ignore[method-assign]  # noqa: SLF001
    await _run_loop_briefly(dispatcher)
    assert dispatcher.last_poll_at is None
    # Counts from start, so a poll that never succeeds still goes stale.
    age = dispatcher.last_poll_age_seconds()
    assert age is not None and age >= 0.1
    started = dispatcher._started_monotonic  # noqa: SLF001
    assert started is not None
    assert dispatcher.last_poll_age_seconds(started + 100.0) == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Render queue: unreclaimed orphans
# ---------------------------------------------------------------------------


def _queue(settings: Settings) -> RenderQueue:
    return RenderQueue(settings, MagicMock(), encoder=MagicMock(spec=Encoder))


async def test_orphans_stay_counted_while_the_failing_write_fails(
    settings: Settings,
) -> None:
    queue = _queue(settings)
    started = datetime(2026, 10, 28, 6, 0, 0)
    queue._encoding_rows_sync = lambda: [(7, started), (8, None)]  # type: ignore[method-assign]  # noqa: SLF001

    def _write_fails(*_args: Any, **_kwargs: Any) -> list[int]:
        raise RuntimeError("database is locked")

    queue._fail_orphans_sync = _write_fails  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(RuntimeError):
        await queue._sweep_orphans()  # noqa: SLF001
    assert queue.unreclaimed_orphans == 2
    assert queue.orphans_reclaimed_total == 0
    assert queue.oldest_encoding_started_at == started


async def test_a_sweep_that_fails_its_orphans_clears_the_count(
    settings: Settings,
) -> None:
    queue = _queue(settings)
    queue._unreclaimed_orphans = 3  # noqa: SLF001 - left by an earlier sweep
    queue._encoding_rows_sync = lambda: [(7, None), (8, None)]  # type: ignore[method-assign]  # noqa: SLF001
    # Job 8 was settled by a cancel meanwhile: not reclaimed, not an orphan.
    queue._fail_orphans_sync = lambda ids, reason: [7]  # type: ignore[method-assign]  # noqa: SLF001
    await queue._sweep_orphans()  # noqa: SLF001
    assert queue.unreclaimed_orphans == 0
    assert queue.orphans_reclaimed_total == 1


async def test_no_orphans_is_zero(settings: Settings) -> None:
    queue = _queue(settings)
    assert queue.unreclaimed_orphans == 0
    queue._encoding_rows_sync = lambda: []  # type: ignore[method-assign]  # noqa: SLF001
    await queue._sweep_orphans()  # noqa: SLF001
    assert queue.unreclaimed_orphans == 0
    assert queue.oldest_encoding_started_at is None


# ---------------------------------------------------------------------------
# Render queue: a long render inside its watchdog budget is not overdue
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)


def _started(seconds_ago: float) -> datetime:
    return (_NOW - timedelta(seconds=seconds_ago)).replace(tzinfo=None)


def _running_queue(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> RenderQueue:
    queue = _queue(settings)
    monkeypatch.setattr(queue, "is_running", lambda: True)
    return queue


def _overdue_status(settings: Settings, queue: RenderQueue) -> readiness.Check:
    result = _evaluate(_context(settings, render_queue=queue), now=_NOW)
    return result.checks["render_overdue_encoding"]


def test_long_render_within_its_budget_stays_ok(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """15 min into a 5,000-frame render: far past 2 x stall timeout, inside budget."""
    queue = _running_queue(settings, monkeypatch)
    budget = render_time_ceiling(settings.render.max_job_seconds, 5000)
    assert budget == 10000  # max(3600, 2 * 5000)
    queue._encoding_rows = ((7, _started(900)),)  # noqa: SLF001
    queue._job_budget_seconds[7] = budget  # noqa: SLF001
    assert 2 * settings.render.stall_timeout_seconds < 900
    assert queue.encoding_overruns(_NOW) == [900 - budget]
    check = _overdue_status(settings, queue)
    assert check.status == "ok"
    assert check.value == 0


def test_render_past_its_budget_plus_grace_is_overdue(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _running_queue(settings, monkeypatch)
    grace = settings.render.stall_timeout_seconds
    queue._encoding_rows = (  # noqa: SLF001
        (7, _started(3600 + grace + 60)),  # overdue
        (8, _started(3600 + grace - 60)),  # inside the grace
        (9, None),  # no start time: skipped
    )
    queue._job_budget_seconds.update({7: 3600.0, 8: 3600.0})  # noqa: SLF001
    check = _overdue_status(settings, queue)
    assert check.status == "degraded"
    assert check.value == 1


def test_unprepared_job_uses_the_ceiling_floor(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before preparation the frame count is unknown: max(3600, 0) applies."""
    queue = _running_queue(settings, monkeypatch)
    queue._encoding_rows = ((7, _started(1800)),)  # noqa: SLF001
    assert queue.encoding_overruns(_NOW) == [1800 - 3600]
    assert _overdue_status(settings, queue).status == "ok"


def test_configured_max_job_seconds_is_the_budget(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = settings.model_copy(
        update={"render": RenderSettings(autostart=False, max_job_seconds=600)}
    )
    queue = _running_queue(configured, monkeypatch)
    queue._encoding_rows = ((7, _started(1000)),)  # noqa: SLF001
    # An unprepared job: the floor is the configured ceiling.
    assert queue.encoding_overruns(_NOW) == [400.0]
    assert _overdue_status(configured, queue).status == "degraded"


async def test_running_job_records_its_watchdog_budget(
    migrated_factory: Any,
    settings_no_autostart: Settings,
) -> None:
    """``_run_job`` stores the spec's ``render_time_ceiling``, then drops it."""
    from tests.integration.render.test_queue import (
        FakeEncoder,
        _enqueue_job,
        _seed_project,
        _wait_active,
    )

    project_id = _seed_project(migrated_factory, settings_no_autostart, frame_count=4)
    job_id = _enqueue_job(migrated_factory, project_id)
    encoder = FakeEncoder(delay=9999.0)
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=encoder)
    await queue.start()
    try:
        await _wait_active(encoder)
        budgets = queue._job_budget_seconds  # noqa: SLF001
        assert budgets == {
            job_id: render_time_ceiling(settings_no_autostart.render.max_job_seconds, 4)
        }
        await queue._sweep_orphans()  # noqa: SLF001
        (overrun,) = queue.encoding_overruns(datetime.now(UTC))
        assert overrun < 0
        assert await queue.cancel_job(job_id, user_requested=True)
    finally:
        await queue.stop()
    assert queue._job_budget_seconds == {}  # noqa: SLF001


# ---------------------------------------------------------------------------
# evaluate_readiness edge cases
# ---------------------------------------------------------------------------


class _Supervisor:
    _started = True
    last_reconcile_at: datetime | None = None

    def is_running(self) -> bool:
        return True

    def runner_snapshots(self) -> list[Any]:
        return []

    def runner_problems(self) -> list[Any]:
        return []


def _context(settings: Settings, **components: Any) -> Any:
    fields: dict[str, Any] = {
        "settings": settings,
        "capture_supervisor": None,
        "render_queue": None,
        "render_scheduler": None,
        "notification_dispatcher": None,
        "loop_lag_monitor": None,
    }
    fields.update(components)
    return SimpleNamespace(**fields)


def _evaluate(context: Any, **kwargs: Any) -> readiness.Readiness:
    return readiness.evaluate_readiness(
        context,
        database="ok",
        components=readiness.component_statuses(context),
        **kwargs,
    )


def test_everything_absent_is_ok(settings: Settings) -> None:
    result = _evaluate(_context(settings))
    assert result.status == "ok"
    assert result.http_status == 200
    assert result.checks["capture_reconcile"].status == "absent"


def test_first_reconcile_has_a_startup_grace(settings: Settings) -> None:
    context = _context(settings, capture_supervisor=_Supervisor())
    limit = 3 * settings.capture.reconcile_interval_seconds
    early = _evaluate(
        context, now_monotonic=100.0 + limit - 1, process_started_monotonic=100.0
    )
    assert early.checks["capture_reconcile"].status == "ok"
    late = _evaluate(
        context, now_monotonic=100.0 + limit + 1, process_started_monotonic=100.0
    )
    assert late.checks["capture_reconcile"].status == "degraded"
    assert late.status == "degraded"


def test_failed_outranks_degraded(settings: Settings) -> None:
    stale = _Supervisor()
    stale.last_reconcile_at = datetime.now(UTC) - timedelta(days=1)
    result = readiness.evaluate_readiness(
        _context(settings, capture_supervisor=stale),
        database="error",
        components={"capture_supervisor": "ok"},
    )
    assert result.status == "failed"
    assert result.http_status == 503


def test_oldest_encoding_age_accepts_naive_and_aware() -> None:
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)
    naive = datetime(2026, 10, 28, 11, 59, 0)
    aware = datetime(2026, 10, 28, 11, 59, 0, tzinfo=UTC)
    assert readiness.oldest_encoding_age_seconds(naive, now) == 60.0
    assert readiness.oldest_encoding_age_seconds(aware, now) == 60.0
    assert readiness.oldest_encoding_age_seconds(None, now) is None


def test_the_supervisor_still_exposes_what_readiness_reads() -> None:
    """Guard against a rename in capture/ silently degrading the probe."""
    assert callable(CaptureSupervisor.runner_problems)
    assert callable(CaptureSupervisor.runner_snapshots)
    # Set in __init__ as an instance attribute.
    assert "last_reconcile_at" in CaptureSupervisor.__init__.__code__.co_names
