"""Exact-time anchors are retried inside their grace window (#69, CAM-R1).

Every deployed project is anchor-only (one 12:05 clock anchor), so a single
transient camera failure used to cost the whole day's frame: the claimed
fire-log row was finalised ``failed`` and the unique constraint stopped any
second attempt. These tests drive the real anchor fire path -- claim,
``capture_one_now``, the frame writer and the fire-log -- against a migrated
SQLite database, with a fake clock and a scripted camera adapter.

Covered:

* a first-attempt success at 12:05 behaves exactly as before;
* a transient failure is retried and captured inside grace (one frame, the
  fire-log row ``captured`` with ``attempts=2``, no alert);
* every attempt failing ends in one ``anchor_failed`` alert after attempt 7;
  an auth failure stops after 2; a late start stops at the grace cut-off;
* no double capture: racing reclaims, and a timeout landing during the frame
  write;
* a restart inside the retry window resumes at ``next_attempt_at``; a restart
  after grace finalises once;
* the runner's wake is folded to the retry instant;
* the next day's success writes ``anchor_recovered`` and the alert clears;
* the new fire-log columns reach the debug reader and the report snapshot.
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from tests.conftest import FakeAdapter
from tests.unit.credential_binding_support import (  # noqa: F401
    _allowed_private_subnets,
)
from timelapse_manager.cameras.base import (
    AuthCaptureError,
    CapturedFrame,
    UnreachableCaptureError,
)
from timelapse_manager.capture import supervisor as sup_mod
from timelapse_manager.capture.anchors import EXACT_TIME_GRACE
from timelapse_manager.capture.supervisor import (
    ANCHOR_JITTER_MAX,
    CaptureState,
    CaptureSupervisor,
    CaptureTarget,
)
from timelapse_manager.config.settings import (
    CaptureSettings,
    DatabaseSettings,
    LoggingSettings,
    PathsSettings,
    Settings,
)
from timelapse_manager.db.models import Camera, Event, ExactTimeFire, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.monitoring.alerts import auto_clear_for_event, get_active_alerts
from timelapse_manager.storage.monitor import DiskSpaceMonitor

_NY = "America/New_York"
_ANCHOR = {
    "id": "noon",
    "kind": "clock",
    "time": "12:05",
    "offset_minutes": 0,
    "enabled": True,
}
# 12:05 America/New_York on 2026-10-14 (EDT) is 16:05 UTC.
_INSTANT = datetime(2026, 10, 14, 12, 5, tzinfo=ZoneInfo(_NY)).astimezone(UTC)
_DAY = "2026-10-14"


class FakeClock:
    """A settable wall clock; the tests drive the runner by hand."""

    def __init__(self, start: datetime) -> None:
        self.current = start

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.current.timestamp()

    async def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=max(0.0, seconds))
        await asyncio.sleep(0)


class ScriptedAdapter(FakeAdapter):
    """Each capture pops the next outcome: ``"ok"`` or an exception to raise."""

    def __init__(self, script: list[object], calls: list[int]) -> None:
        super().__init__()
        self._script = script
        self._calls = calls

    async def capture(self) -> CapturedFrame:
        self._calls.append(1)
        outcome = self._script.pop(0) if self._script else "ok"
        if isinstance(outcome, BaseException):
            raise outcome
        return await super().capture()


def _permissive_disk_monitor() -> DiskSpaceMonitor:
    return DiskSpaceMonitor(
        low_watermark_bytes=1,
        low_watermark_percent=0.001,
        resume_watermark_bytes=1,
        resume_watermark_percent=0.001,
        check_interval_seconds=0.0,
        get_free_bytes=lambda _p: 10**15,
        get_total_bytes=lambda _p: 10**15,
    )


def _settings(tmp_path: Path, timeout_seconds: float = 1.0) -> Settings:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    return Settings(
        database=DatabaseSettings(url=f"sqlite:///{tmp_path / 'test.db'}"),
        logging=LoggingSettings(level="WARNING", format="text"),
        paths=PathsSettings(
            data_dir=data_dir,
            frames_root=data_dir / "frames",
            token_file=data_dir / ".local-token",
        ),
        capture=CaptureSettings(autostart=False, timeout_seconds=timeout_seconds),
    )


def _supervisor(
    factory: Any, tmp_path: Path, clock: FakeClock, timeout_seconds: float = 1.0
) -> CaptureSupervisor:
    return CaptureSupervisor(
        _settings(tmp_path, timeout_seconds),
        factory,
        clock=clock,
        disk_monitor=_permissive_disk_monitor(),
    )


def _seed(factory: Any, tmp_path: Path, *, camera_exists: bool = True) -> CaptureTarget:
    """An anchor-only VAPIX project, shaped like the deployed 12:05 projects."""
    storage = tmp_path / "frames" / "anchor"
    storage.mkdir(parents=True, exist_ok=True)
    schedule = {"enabled": True, "timezone": _NY}
    with session_scope(factory) as session:
        cam = Camera(name="anchor-cam", address="10.0.16.71", protocol="vapix")
        session.add(cam)
        session.flush()
        proj = Project(
            camera_id=cam.id,
            name="anchor",
            capture_interval_seconds=None,
            lifecycle_state="active",
            operational_status="idle",
            storage_path=str(storage),
            schedule=schedule,
            exact_time_anchors=[_ANCHOR],
        )
        session.add(proj)
        session.flush()
        return CaptureTarget(
            project_id=proj.id,
            project_name=proj.name,
            camera_id=cam.id if camera_exists else cam.id + 1000,
            interval_seconds=None,
            schedule=schedule,
            storage_path=str(storage),
            exact_time_anchors=[_ANCHOR],
        )


def _fire(factory: Any, project_id: int, local_date: str = _DAY) -> ExactTimeFire:
    with session_scope(factory) as session:
        row = (
            session.query(ExactTimeFire)
            .filter_by(project_id=project_id, local_date=local_date)
            .one()
        )
        session.expunge(row)
        return row


def _frames(factory: Any, project_id: int) -> list[str | None]:
    with session_scope(factory) as session:
        return [
            reason
            for (reason,) in session.query(Frame.capture_reason)
            .filter_by(project_id=project_id)
            .all()
        ]


def _events(factory: Any, project_id: int) -> list[tuple[str, dict[str, Any]]]:
    with session_scope(factory) as session:
        rows = (
            session.query(Event)
            .filter_by(scope="project", scope_id=project_id)
            .order_by(Event.id)
            .all()
        )
        return [(row.level, dict(row.event_metadata or {})) for row in rows]


def _reasons(factory: Any, project_id: int) -> list[str]:
    return [str(meta.get("reason")) for _, meta in _events(factory, project_id)]


def _active_alert_reasons(factory: Any, project_id: int) -> list[str | None]:
    with session_scope(factory) as session:
        alerts, _ = get_active_alerts(session, scope="project", scope_id=project_id)
        return [alert.reason for alert in alerts]


@pytest.fixture
def adapter_script() -> Iterator[tuple[list[object], list[int]]]:
    """Patch the one-shot adapter factory; yield (script, capture calls)."""
    script: list[object] = []
    calls: list[int] = []
    with patch(
        "timelapse_manager.capture.one_shot.build_adapter",
        lambda *a, **k: ScriptedAdapter(script, calls),
    ):
        yield script, calls


async def _drive_until_settled(
    supervisor: CaptureSupervisor,
    clock: FakeClock,
    target: CaptureTarget,
    *,
    max_cycles: int = 20,
) -> list[datetime | None]:
    """Run anchor evaluation, jumping the clock to each returned retry wake.

    Stops when the wake is no longer today's retry (it is tomorrow's anchor).
    Returns the wakes seen.
    """
    wakes: list[datetime | None] = []
    for _ in range(max_cycles):
        wake = await supervisor._evaluate_exact_time_anchors(target, clock.now())
        wakes.append(wake)
        if wake is None or wake - clock.now() > timedelta(hours=1):
            return wakes
        clock.current = wake
    raise AssertionError(f"did not settle: {wakes}")


# ---------------------------------------------------------------------------
# The 12:05 success path is unchanged
# ---------------------------------------------------------------------------


class TestFirstAttemptSuccessUnchanged:
    async def test_first_attempt_success_at_1205(
        self, migrated_factory: Any, tmp_path: Path, adapter_script: Any
    ) -> None:
        script, calls = adapter_script
        clock = FakeClock(_INSTANT + timedelta(seconds=3))
        supervisor = _supervisor(migrated_factory, tmp_path, clock)
        target = _seed(migrated_factory, tmp_path)

        wake = await supervisor._evaluate_exact_time_anchors(target, clock.now())
        await supervisor.stop()

        fire = _fire(migrated_factory, target.project_id)
        assert (fire.status, fire.attempts, fire.next_attempt_at, fire.detail) == (
            "captured",
            1,
            None,
            None,
        )
        assert fire.frame_id is not None
        assert calls == [1]
        assert _frames(migrated_factory, target.project_id) == ["anchor:clock"]
        # One info event, with the same metadata as before #69.
        assert _events(migrated_factory, target.project_id) == [
            (
                "info",
                {
                    "reason": "anchor:clock",
                    "anchor_id": "noon",
                    "local_date": _DAY,
                    "dedup_key": f"{target.project_id}:noon:{_DAY}",
                },
            )
        ]
        # The runner next wakes for tomorrow's 12:05, as before.
        assert wake == _INSTANT + timedelta(days=1)

    async def test_later_cycles_inside_grace_do_nothing(
        self, migrated_factory: Any, tmp_path: Path, adapter_script: Any
    ) -> None:
        _, calls = adapter_script
        clock = FakeClock(_INSTANT + timedelta(seconds=3))
        supervisor = _supervisor(migrated_factory, tmp_path, clock)
        target = _seed(migrated_factory, tmp_path)
        await supervisor._evaluate_exact_time_anchors(target, clock.now())
        for minutes in (1, 10, 29, 31, 300):
            await supervisor._evaluate_exact_time_anchors(
                target, _INSTANT + timedelta(minutes=minutes)
            )
        await supervisor.stop()

        assert calls == [1]
        assert _fire(migrated_factory, target.project_id).status == "captured"
        assert len(_events(migrated_factory, target.project_id)) == 1


# ---------------------------------------------------------------------------
# Retry inside grace
# ---------------------------------------------------------------------------


class TestRetryWithinGrace:
    async def test_transient_timeout_then_success(
        self, migrated_factory: Any, tmp_path: Path, adapter_script: Any
    ) -> None:
        """(a) The first attempt times out, the retry 15 s later captures."""
        script, calls = adapter_script
        script.append(UnreachableCaptureError("camera rebooting"))
        clock = FakeClock(_INSTANT + timedelta(seconds=3))
        supervisor = _supervisor(migrated_factory, tmp_path, clock)
        target = _seed(migrated_factory, tmp_path)

        wake = await supervisor._evaluate_exact_time_anchors(target, clock.now())
        retry_at = _INSTANT + timedelta(seconds=18)
        assert wake == retry_at
        fire = _fire(migrated_factory, target.project_id)
        assert (fire.status, fire.attempts, fire.detail) == (
            "retry_pending",
            1,
            "camera rebooting",
        )
        assert fire.next_attempt_at == retry_at.replace(tzinfo=None)
        assert _frames(migrated_factory, target.project_id) == []

        # A cycle before the retry is due captures nothing and keeps the wake.
        clock.current = retry_at - timedelta(seconds=5)
        assert (
            await supervisor._evaluate_exact_time_anchors(target, clock.now())
            == retry_at
        )
        assert calls == [1]

        clock.current = retry_at
        wake = await supervisor._evaluate_exact_time_anchors(target, clock.now())
        await supervisor.stop()

        assert wake == _INSTANT + timedelta(days=1)
        assert calls == [1, 1]
        fire = _fire(migrated_factory, target.project_id)
        assert (fire.status, fire.attempts, fire.next_attempt_at, fire.detail) == (
            "captured",
            2,
            None,
            None,
        )
        assert _frames(migrated_factory, target.project_id) == ["anchor:clock"]
        assert _reasons(migrated_factory, target.project_id) == [
            "anchor_retry",
            "anchor:clock",
        ]
        assert _active_alert_reasons(migrated_factory, target.project_id) == []

    async def test_capture_timeout_is_a_retried_failure(
        self, migrated_factory: Any, tmp_path: Path, adapter_script: Any
    ) -> None:
        """A snapshot over the capture budget is a failed attempt, retried."""
        script, _ = adapter_script
        clock = FakeClock(_INSTANT + timedelta(seconds=3))
        supervisor = _supervisor(
            migrated_factory, tmp_path, clock, timeout_seconds=0.05
        )
        target = _seed(migrated_factory, tmp_path)

        class _Slow(ScriptedAdapter):
            async def capture(self) -> CapturedFrame:
                await asyncio.sleep(3600)
                raise AssertionError("unreachable")  # pragma: no cover

        with patch(
            "timelapse_manager.capture.one_shot.build_adapter",
            lambda *a, **k: _Slow([], []),
        ):
            await supervisor._evaluate_exact_time_anchors(target, clock.now())
        clock.current = _INSTANT + timedelta(seconds=18)
        await supervisor._evaluate_exact_time_anchors(target, clock.now())
        await supervisor.stop()

        fire = _fire(migrated_factory, target.project_id)
        assert (fire.status, fire.attempts) == ("captured", 2)
        assert _frames(migrated_factory, target.project_id) == ["anchor:clock"]

    async def test_every_attempt_fails_raises_one_alert(
        self, migrated_factory: Any, tmp_path: Path, adapter_script: Any
    ) -> None:
        """(b) Seven failed attempts, then one ``anchor_failed`` warning."""
        script, calls = adapter_script
        script.extend(UnreachableCaptureError(f"down {n}") for n in range(10))
        clock = FakeClock(_INSTANT + timedelta(seconds=3))
        supervisor = _supervisor(migrated_factory, tmp_path, clock)
        target = _seed(migrated_factory, tmp_path)

        wakes = await _drive_until_settled(supervisor, clock, target)
        # Later cycles inside and after grace change nothing.
        for minutes in (25, 29, 40):
            await supervisor._evaluate_exact_time_anchors(
                target, _INSTANT + timedelta(minutes=minutes)
            )
        await supervisor.stop()

        assert len(calls) == 7
        offsets = [
            round((w - _INSTANT).total_seconds()) for w in wakes[:-1] if w is not None
        ]
        assert offsets == [18, 48, 108, 228, 468, 948]
        fire = _fire(migrated_factory, target.project_id)
        assert (fire.status, fire.attempts, fire.next_attempt_at, fire.detail) == (
            "failed",
            7,
            None,
            "down 6",
        )
        assert _frames(migrated_factory, target.project_id) == []
        reasons = _reasons(migrated_factory, target.project_id)
        assert reasons == ["anchor_retry"] * 6 + ["anchor_failed"]
        level, meta = _events(migrated_factory, target.project_id)[-1]
        assert level == "warning"
        assert meta == {
            "reason": "anchor_failed",
            "anchor_id": "noon",
            "kind": "clock",
            "local_date": _DAY,
            "attempts": 7,
            "last_error": "down 6",
        }
        assert _active_alert_reasons(migrated_factory, target.project_id) == [
            "anchor_failed"
        ]

    async def test_auth_failure_stops_after_two_attempts(
        self, migrated_factory: Any, tmp_path: Path, adapter_script: Any
    ) -> None:
        script, calls = adapter_script
        script.extend(AuthCaptureError("401 Unauthorized") for _ in range(5))
        clock = FakeClock(_INSTANT + timedelta(seconds=3))
        supervisor = _supervisor(migrated_factory, tmp_path, clock)
        target = _seed(migrated_factory, tmp_path)

        await _drive_until_settled(supervisor, clock, target)
        await supervisor.stop()

        assert len(calls) == 2
        fire = _fire(migrated_factory, target.project_id)
        assert (fire.status, fire.attempts) == ("failed", 2)
        assert _reasons(migrated_factory, target.project_id) == [
            "anchor_retry",
            "anchor_failed",
        ]

    async def test_late_start_stops_at_the_grace_cut_off(
        self, migrated_factory: Any, tmp_path: Path, adapter_script: Any
    ) -> None:
        """Woken 25 minutes late, the retries stop before grace closes."""
        script, calls = adapter_script
        script.extend(UnreachableCaptureError("down") for _ in range(10))
        clock = FakeClock(_INSTANT + timedelta(minutes=25))
        supervisor = _supervisor(migrated_factory, tmp_path, clock)
        target = _seed(migrated_factory, tmp_path)

        await _drive_until_settled(supervisor, clock, target)
        await supervisor.stop()

        # 25:00, 25:15, 25:45, 26:45, 28:45; then 240 s would end past 29:30.
        assert len(calls) == 5
        assert clock.now() <= _INSTANT + EXACT_TIME_GRACE
        fire = _fire(migrated_factory, target.project_id)
        assert (fire.status, fire.attempts) == ("failed", 5)
        assert _reasons(migrated_factory, target.project_id)[-1] == "anchor_failed"

    async def test_deleted_camera_is_retried_then_finalised(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        """(g) A ``RuntimeError`` (camera gone) is an attempt like any other."""
        clock = FakeClock(_INSTANT + timedelta(seconds=3))
        supervisor = _supervisor(migrated_factory, tmp_path, clock)
        target = _seed(migrated_factory, tmp_path, camera_exists=False)

        await supervisor._evaluate_exact_time_anchors(target, clock.now())
        fire = _fire(migrated_factory, target.project_id)
        assert fire.status == "retry_pending"
        assert fire.detail == f"camera {target.camera_id} no longer exists"

        await _drive_until_settled(supervisor, clock, target)
        await supervisor.stop()
        fire = _fire(migrated_factory, target.project_id)
        assert (fire.status, fire.attempts) == ("failed", 7)
        assert _reasons(migrated_factory, target.project_id).count("anchor_failed") == 1

    async def test_error_text_is_redacted_everywhere(
        self, migrated_factory: Any, tmp_path: Path, adapter_script: Any
    ) -> None:
        script, _ = adapter_script
        script.append(
            UnreachableCaptureError("GET https://root:hunter2@10.0.16.71/x failed")
        )
        clock = FakeClock(_INSTANT + timedelta(seconds=3))
        supervisor = _supervisor(migrated_factory, tmp_path, clock)
        target = _seed(migrated_factory, tmp_path)

        await supervisor._evaluate_exact_time_anchors(target, clock.now())
        await supervisor.stop()

        fire = _fire(migrated_factory, target.project_id)
        assert "hunter2" not in (fire.detail or "")
        assert "***@10.0.16.71" in (fire.detail or "")
        assert "hunter2" not in repr(_events(migrated_factory, target.project_id))

    async def test_final_failure_shows_on_the_runner_state(
        self, migrated_factory: Any, tmp_path: Path, adapter_script: Any
    ) -> None:
        script, _ = adapter_script
        script.extend(AuthCaptureError("401 Unauthorized") for _ in range(2))
        clock = FakeClock(_INSTANT + timedelta(seconds=3))
        supervisor = _supervisor(migrated_factory, tmp_path, clock)
        target = _seed(migrated_factory, tmp_path)
        state = CaptureState(project_id=target.project_id, camera_id=target.camera_id)
        supervisor._runners[target.project_id] = sup_mod._ProjectRunner(
            state=state, target=target
        )

        await _drive_until_settled(supervisor, clock, target)
        supervisor._runners.clear()
        await supervisor.stop()

        assert state.last_error == "401 Unauthorized"
        assert state.last_error_at == clock.now()


# ---------------------------------------------------------------------------
# No double capture
# ---------------------------------------------------------------------------


def _park_retry(
    factory: Any,
    project_id: int,
    *,
    attempts: int,
    next_attempt_at: datetime,
    local_date: str = _DAY,
) -> None:
    with session_scope(factory) as session:
        session.add(
            ExactTimeFire(
                project_id=project_id,
                anchor_id="noon",
                local_date=local_date,
                status="retry_pending",
                fired_at=_INSTANT.replace(tzinfo=None),
                detail="camera rebooting",
                attempts=attempts,
                next_attempt_at=next_attempt_at.astimezone(UTC).replace(tzinfo=None),
            )
        )


class TestNoDoubleCapture:
    def test_concurrent_reclaims_have_one_winner(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        """(c) Eight threads reclaim the same due row; exactly one wins."""
        clock = FakeClock(_INSTANT + timedelta(seconds=30))
        supervisor = _supervisor(migrated_factory, tmp_path, clock)
        target = _seed(migrated_factory, tmp_path)
        _park_retry(
            migrated_factory,
            target.project_id,
            attempts=1,
            next_attempt_at=_INSTANT + timedelta(seconds=18),
        )
        barrier = threading.Barrier(8)

        def reclaim() -> str:
            barrier.wait()
            claim = supervisor._claim_anchor_attempt(
                target.project_id,
                target.project_name,
                "noon",
                "clock",
                _DAY,
                "failed",
                None,
                within_grace=True,
                now=clock.now(),
            )
            return claim.outcome

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(lambda _: reclaim(), range(8)))

        assert sorted(outcomes) == ["claimed"] + ["done"] * 7
        fire = _fire(migrated_factory, target.project_id)
        assert (fire.status, fire.attempts, fire.next_attempt_at) == ("failed", 2, None)

    async def test_two_supervisors_race_the_retry(
        self, migrated_factory: Any, tmp_path: Path, adapter_script: Any
    ) -> None:
        """(c) Two runners for the same project at the retry instant: one frame."""
        _, calls = adapter_script
        clock = FakeClock(_INSTANT + timedelta(seconds=18))
        first = _supervisor(migrated_factory, tmp_path, clock)
        second = _supervisor(migrated_factory, tmp_path, clock)
        target = _seed(migrated_factory, tmp_path)
        _park_retry(
            migrated_factory,
            target.project_id,
            attempts=1,
            next_attempt_at=_INSTANT + timedelta(seconds=18),
        )

        await asyncio.gather(
            first._evaluate_exact_time_anchors(target, clock.now()),
            second._evaluate_exact_time_anchors(target, clock.now()),
        )
        await first.stop()
        await second.stop()

        assert calls == [1]
        assert _frames(migrated_factory, target.project_id) == ["anchor:clock"]
        fire = _fire(migrated_factory, target.project_id)
        assert (fire.status, fire.attempts) == ("captured", 2)

    async def test_timeout_during_the_frame_write_captures_once(
        self, migrated_factory: Any, tmp_path: Path, adapter_script: Any
    ) -> None:
        """The attempt times out while the write hop is still running. The
        thread finishes the write and marks the row captured in the same hop,
        so the retry finds the row captured and takes no second frame."""
        _, calls = adapter_script
        clock = FakeClock(_INSTANT + timedelta(seconds=3))
        supervisor = _supervisor(migrated_factory, tmp_path, clock, 0.05)
        target = _seed(migrated_factory, tmp_path)
        real_write = supervisor.frame_writer.write
        write_done = threading.Event()

        def slow_write(*args: Any, **kwargs: Any) -> Any:
            time.sleep(2.0)  # past the 1.15 s attempt timeout
            try:
                return real_write(*args, **kwargs)
            finally:
                write_done.set()

        with (
            patch.object(sup_mod, "_ANCHOR_FIRE_MARGIN_SECONDS", 1.0),
            # The attempt timeout includes the capture-slot wait (#69 CAM-R4).
            patch.object(sup_mod, "_SLOT_WAIT_MARGIN_SECONDS", 0.0),
            patch.object(supervisor._writer, "write", slow_write),
        ):
            wake = await supervisor._evaluate_exact_time_anchors(target, clock.now())
            assert wake == _INSTANT + timedelta(seconds=18)  # retry was scheduled
            assert await asyncio.to_thread(write_done.wait, 10.0)
            # Let the write hop's mark-captured commit land.
            for _ in range(50):
                if _fire(migrated_factory, target.project_id).status == "captured":
                    break
                await asyncio.sleep(0.02)
            clock.current = _INSTANT + timedelta(seconds=18)
            await supervisor._evaluate_exact_time_anchors(target, clock.now())
        await supervisor.stop()

        assert calls == [1]
        assert _frames(migrated_factory, target.project_id) == ["anchor:clock"]
        fire = _fire(migrated_factory, target.project_id)
        assert (fire.status, fire.attempts, fire.next_attempt_at) == (
            "captured",
            1,
            None,
        )


# ---------------------------------------------------------------------------
# Restart during and after the retry window
# ---------------------------------------------------------------------------


class TestRestart:
    async def test_restart_inside_grace_resumes_at_next_attempt_at(
        self, migrated_factory: Any, tmp_path: Path, adapter_script: Any
    ) -> None:
        """(d) A fresh supervisor finds ``retry_pending`` and resumes on time."""
        _, calls = adapter_script
        target = _seed(migrated_factory, tmp_path)
        retry_at = _INSTANT + timedelta(seconds=108)
        _park_retry(
            migrated_factory, target.project_id, attempts=3, next_attempt_at=retry_at
        )
        clock = FakeClock(_INSTANT + timedelta(seconds=60))
        supervisor = _supervisor(migrated_factory, tmp_path, clock)

        wake = await supervisor._evaluate_exact_time_anchors(target, clock.now())
        assert wake == retry_at
        assert calls == []

        clock.current = retry_at
        await supervisor._evaluate_exact_time_anchors(target, clock.now())
        await supervisor.stop()

        assert calls == [1]
        fire = _fire(migrated_factory, target.project_id)
        assert (fire.status, fire.attempts) == ("captured", 4)
        assert _frames(migrated_factory, target.project_id) == ["anchor:clock"]

    async def test_restart_after_grace_finalises_once(
        self, migrated_factory: Any, tmp_path: Path, adapter_script: Any
    ) -> None:
        """(e) Down across the end of grace: finalised with one alert."""
        _, calls = adapter_script
        target = _seed(migrated_factory, tmp_path)
        _park_retry(
            migrated_factory,
            target.project_id,
            attempts=4,
            next_attempt_at=_INSTANT + timedelta(seconds=228),
        )
        clock = FakeClock(_INSTANT + timedelta(minutes=31))
        supervisor = _supervisor(migrated_factory, tmp_path, clock)

        wake = await supervisor._evaluate_exact_time_anchors(target, clock.now())
        await supervisor._evaluate_exact_time_anchors(
            target, clock.now() + timedelta(minutes=5)
        )
        await supervisor.stop()

        assert wake == _INSTANT + timedelta(days=1)
        assert calls == []
        fire = _fire(migrated_factory, target.project_id)
        assert (fire.status, fire.attempts, fire.next_attempt_at) == ("failed", 4, None)
        assert _reasons(migrated_factory, target.project_id) == ["anchor_failed"]
        assert _active_alert_reasons(migrated_factory, target.project_id) == [
            "anchor_failed"
        ]

    async def test_a_provisional_row_left_by_a_crash_is_not_retried(
        self, migrated_factory: Any, tmp_path: Path, adapter_script: Any
    ) -> None:
        """At-most-once is kept: a crash mid-attempt leaves ``failed`` with no
        ``next_attempt_at``, and nothing re-fires it."""
        _, calls = adapter_script
        target = _seed(migrated_factory, tmp_path)
        with session_scope(migrated_factory) as session:
            session.add(
                ExactTimeFire(
                    project_id=target.project_id,
                    anchor_id="noon",
                    local_date=_DAY,
                    status="failed",
                    fired_at=_INSTANT.replace(tzinfo=None),
                )
            )
        clock = FakeClock(_INSTANT + timedelta(minutes=2))
        supervisor = _supervisor(migrated_factory, tmp_path, clock)

        await supervisor._evaluate_exact_time_anchors(target, clock.now())
        await supervisor.stop()

        assert calls == []
        assert _fire(migrated_factory, target.project_id).status == "failed"
        assert _events(migrated_factory, target.project_id) == []


# ---------------------------------------------------------------------------
# Wake folding on the anchor-only runner
# ---------------------------------------------------------------------------


class TestWakeFolding:
    async def test_runner_sleeps_no_longer_than_the_retry(
        self, migrated_factory: Any, tmp_path: Path, adapter_script: Any
    ) -> None:
        """(f) After a failed attempt the runner plans to wake at the retry."""
        script, _ = adapter_script
        script.append(UnreachableCaptureError("down"))
        clock = FakeClock(_INSTANT + timedelta(seconds=3))
        supervisor = _supervisor(migrated_factory, tmp_path, clock)
        target = _seed(migrated_factory, tmp_path)
        state = CaptureState(project_id=target.project_id, camera_id=target.camera_id)

        sleep = await supervisor._run_cycle(
            target,
            state,
            interval=None,
            max_idle=3600.0,
            rng=random.Random(0),
            schedule=supervisor._parse_schedule_safely(target),
            volume_path=tmp_path,
        )
        await supervisor.stop()

        retry_at = _fire(migrated_factory, target.project_id).next_attempt_at
        assert retry_at is not None
        retry_at = retry_at.replace(tzinfo=UTC)
        plan = supervisor._plans[target.project_id]
        assert plan.next_anchor_wake is not None
        assert retry_at <= plan.next_anchor_wake <= retry_at + ANCHOR_JITTER_MAX
        assert sleep is not None
        assert sleep <= (retry_at - clock.now()).total_seconds() + (
            ANCHOR_JITTER_MAX.total_seconds()
        )


# ---------------------------------------------------------------------------
# Next day's success clears the alert
# ---------------------------------------------------------------------------


class TestRecovery:
    async def test_next_days_success_clears_anchor_failed(
        self, migrated_factory: Any, tmp_path: Path, adapter_script: Any
    ) -> None:
        """(h) Day 1 fails for good; day 2 captures, writes ``anchor_recovered``,
        and the dispatcher's auto-clear closes the alert."""
        script, _ = adapter_script
        script.extend(AuthCaptureError("401") for _ in range(2))
        clock = FakeClock(_INSTANT + timedelta(seconds=3))
        supervisor = _supervisor(migrated_factory, tmp_path, clock)
        target = _seed(migrated_factory, tmp_path)
        await _drive_until_settled(supervisor, clock, target)
        assert _active_alert_reasons(migrated_factory, target.project_id) == [
            "anchor_failed"
        ]

        clock.current = _INSTANT + timedelta(days=1, seconds=3)
        await supervisor._evaluate_exact_time_anchors(target, clock.now())
        await supervisor.stop()

        assert _fire(migrated_factory, target.project_id, "2026-10-15").status == (
            "captured"
        )
        [(level, meta)] = [
            event
            for event in _events(migrated_factory, target.project_id)
            if event[1]["reason"] == "anchor_recovered"
        ]
        assert (level, meta["local_date"], meta["previous_local_date"]) == (
            "info",
            "2026-10-15",
            _DAY,
        )
        # The dispatcher runs auto-clear for every new event it polls.
        with session_scope(migrated_factory) as session:
            cleared = auto_clear_for_event(
                session,
                scope="project",
                scope_id=target.project_id,
                reason="anchor_recovered",
            )
        assert cleared == 1
        assert _active_alert_reasons(migrated_factory, target.project_id) == []

    async def test_a_retry_success_on_the_same_day_raises_nothing(
        self, migrated_factory: Any, tmp_path: Path, adapter_script: Any
    ) -> None:
        """Yesterday captured; today's retry captures: no recovery event."""
        script, _ = adapter_script
        clock = FakeClock(_INSTANT + timedelta(seconds=3))
        supervisor = _supervisor(migrated_factory, tmp_path, clock)
        target = _seed(migrated_factory, tmp_path)
        await supervisor._evaluate_exact_time_anchors(target, clock.now())
        script.append(UnreachableCaptureError("down"))
        clock.current = _INSTANT + timedelta(days=1, seconds=3)
        await _drive_until_settled(supervisor, clock, target)
        await supervisor.stop()

        reasons = _reasons(migrated_factory, target.project_id)
        assert "anchor_recovered" not in reasons
        assert "anchor_failed" not in reasons


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


class TestReaders:
    def test_debug_and_report_rows_carry_the_retry_columns(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        from timelapse_manager.api.debug import _latest_fire_rows
        from timelapse_manager.reports.snapshot import project_section

        target = _seed(migrated_factory, tmp_path)
        retry_at = _INSTANT + timedelta(seconds=48)
        _park_retry(
            migrated_factory, target.project_id, attempts=2, next_attempt_at=retry_at
        )
        with session_scope(migrated_factory) as session:
            latest = _latest_fire_rows(session, [target.project_id])
            section = project_section(
                session, target.project_id, _INSTANT + timedelta(minutes=1)
            )

        row = latest[target.project_id]
        assert row["status"] == "retry_pending"
        assert row["attempts"] == 2
        assert row["next_attempt_at"] == retry_at.replace(tzinfo=None).isoformat()
        assert section is not None
        [fire] = section["exact_time_fires_last_7_days"]
        assert fire["attempts"] == 2
        assert fire["next_attempt_at"] is not None


# ---------------------------------------------------------------------------
# The retry helpers stay off the event loop
# ---------------------------------------------------------------------------


class TestOffTheLoop:
    async def test_retry_reclaim_and_finalise_run_in_threads(
        self, migrated_factory: Any, tmp_path: Path, adapter_script: Any
    ) -> None:
        """Every fire-log hop of a failing anchor (claim, schedule, reclaim,
        finalise) runs in a background task with no loop-DB violation, and goes
        through ``_run_blocking``."""
        from tests import loop_guard_support as guard

        script, _ = adapter_script
        script.extend(AuthCaptureError("401") for _ in range(2))
        clock = FakeClock(_INSTANT + timedelta(seconds=3))
        supervisor = _supervisor(migrated_factory, tmp_path, clock)
        target = _seed(migrated_factory, tmp_path)
        hopped: list[str] = []
        real = supervisor._run_blocking

        async def spy(fn: Any, /, *args: Any, **kwargs: Any) -> Any:
            hopped.append(getattr(fn, "__name__", repr(fn)))
            return await real(fn, *args, **kwargs)

        with patch.object(supervisor, "_run_blocking", side_effect=spy):
            # Its own task, as the supervisor runs a project runner.
            await asyncio.create_task(_drive_until_settled(supervisor, clock, target))
        await supervisor.stop()

        assert {
            "_claim_anchor_attempt",
            "_schedule_anchor_retry",
            "_finalize_anchor_failure",
        } <= set(hopped)
        assert _fire(migrated_factory, target.project_id).status == "failed"
        origins = {
            v.key
            for v in guard.collect()
            if v.background and v.key.startswith("timelapse_manager.capture.")
        }
        assert origins == set()


# ---------------------------------------------------------------------------
# capture_one_now records failed attempts (for its other callers)
# ---------------------------------------------------------------------------


class TestOneShotFailureEvent:
    async def test_failure_writes_one_redacted_warning_event(
        self, migrated_factory: Any, tmp_path: Path, adapter_script: Any
    ) -> None:
        from timelapse_manager.capture.one_shot import capture_one_now

        script, _ = adapter_script
        script.append(UnreachableCaptureError("GET https://u:secret@cam/x refused"))
        supervisor = _supervisor(migrated_factory, tmp_path, FakeClock(_INSTANT))
        target = _seed(migrated_factory, tmp_path)

        with pytest.raises(UnreachableCaptureError):
            await capture_one_now(
                supervisor,
                target,
                reason="event:tns1:Device/IO/Port",
                dedup_key="k",
            )
        await supervisor.stop()

        [(level, meta)] = _events(migrated_factory, target.project_id)
        assert level == "warning"
        assert meta["reason"] == "event:tns1:Device/IO/Port"
        assert meta["dedup_key"] == "k"
        assert meta["failed"] == "UnreachableCaptureError"
        assert "secret" not in meta["error"]

    async def test_anchor_callers_can_turn_the_event_off(
        self, migrated_factory: Any, tmp_path: Path, adapter_script: Any
    ) -> None:
        from timelapse_manager.capture.one_shot import capture_one_now

        script, _ = adapter_script
        script.append(UnreachableCaptureError("down"))
        supervisor = _supervisor(migrated_factory, tmp_path, FakeClock(_INSTANT))
        target = _seed(migrated_factory, tmp_path)

        with pytest.raises(UnreachableCaptureError):
            await capture_one_now(
                supervisor, target, reason="anchor:clock", on_failure_event=False
            )
        await supervisor.stop()

        assert _events(migrated_factory, target.project_id) == []
