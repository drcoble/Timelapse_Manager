"""Runner DEBUG lines and the per-runner plan record (issue #7).

One ``runner cycle`` line per loop cycle and one ``anchor evaluation`` line per
anchor pass, both silent at INFO; each cycle also records the runner's plan so
the debug endpoint can show the next wake and decision.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from timelapse_manager.capture import supervisor as supervisor_mod
from timelapse_manager.capture.schedule import parse_schedule
from timelapse_manager.capture.supervisor import (
    CaptureState,
    CaptureSupervisor,
    CaptureTarget,
    _ProjectRunner,
)
from timelapse_manager.config.settings import (
    CaptureSettings,
    DatabaseSettings,
    PathsSettings,
    Settings,
)
from timelapse_manager.storage.monitor import DiskSpaceMonitor

_NOW = datetime(2026, 6, 1, 3, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _enabled_logger() -> Generator[None, None, None]:
    """Re-enable the logger under test for this module's tests.

    An Alembic migration run earlier in the same worker calls
    ``logging.config.fileConfig`` with ``disable_existing_loggers=True``, which
    disables already-imported loggers (a test-harness artifact, not the SUT).
    """
    target = logging.getLogger(supervisor_mod.__name__)
    was_disabled = target.disabled
    target.disabled = False
    yield
    target.disabled = was_disabled


class _FixedClock:
    def now(self) -> datetime:
        return _NOW

    def monotonic(self) -> float:
        return self.now().timestamp()

    async def sleep(self, seconds: float) -> None:  # pragma: no cover - unused
        raise AssertionError("not expected")


def _supervisor(tmp_path: Path) -> CaptureSupervisor:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    settings = Settings(
        database=DatabaseSettings(url=f"sqlite:///{tmp_path / 'x.db'}"),
        paths=PathsSettings(
            data_dir=data_dir,
            frames_root=data_dir / "frames",
            token_file=data_dir / ".local-token",
        ),
        capture=CaptureSettings(autostart=False),
    )
    monitor = DiskSpaceMonitor(
        low_watermark_bytes=1,
        low_watermark_percent=0.001,
        resume_watermark_bytes=1,
        resume_watermark_percent=0.001,
        check_interval_seconds=0.0,
        get_free_bytes=lambda _p: 10**15,
        get_total_bytes=lambda _p: 10**15,
    )
    return CaptureSupervisor(
        settings, MagicMock(), disk_monitor=monitor, clock=_FixedClock()
    )


def _target(**overrides: object) -> CaptureTarget:
    fields: dict[str, object] = {
        "project_id": 5,
        "project_name": "Roof",
        "camera_id": 1,
        "interval_seconds": 600,
    }
    fields.update(overrides)
    return CaptureTarget(**fields)  # type: ignore[arg-type]


async def _one_wait_cycle(supervisor: CaptureSupervisor, target: CaptureTarget) -> None:
    import random

    # A frame was just taken, so the cycle plans a wait (no capture, no DB).
    state = CaptureState(
        project_id=5,
        camera_id=1,
        last_capture_at=_NOW,
        next_due_mono=_NOW.timestamp() + 600.0,
    )
    supervisor._runners[5] = _ProjectRunner(state=state, target=target)  # noqa: SLF001
    sleep = await supervisor._run_cycle(  # noqa: SLF001
        target,
        state,
        interval=600.0,
        max_idle=300.0,
        rng=random.Random(5),
        schedule=parse_schedule(None),
        volume_path=Path("/"),
    )
    assert sleep == 300.0


def _cycle_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == supervisor_mod.__name__ and "runner cycle" in r.getMessage()
    ]


async def test_cycle_line_is_silent_at_info(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    supervisor = _supervisor(tmp_path)
    with caplog.at_level(logging.INFO, logger=supervisor_mod.__name__):
        await _one_wait_cycle(supervisor, _target())
    assert _cycle_lines(caplog) == []
    await supervisor.stop()


async def test_cycle_line_and_plan_at_debug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    supervisor = _supervisor(tmp_path)
    with caplog.at_level(logging.DEBUG, logger=supervisor_mod.__name__):
        await _one_wait_cycle(supervisor, _target())
    [line] = _cycle_lines(caplog)
    assert line == (
        "runner cycle project=5 open=True disk_ok=True next_change=None "
        "next_anchor_wake=None decision=wait sleep=300.0"
    )
    [snapshot] = supervisor.runner_snapshots()
    assert snapshot.plan is not None
    assert snapshot.plan.action == "wait"
    assert snapshot.plan.next_wake_at == datetime(2026, 6, 1, 3, 5, tzinfo=UTC)
    await supervisor.stop()


async def test_anchor_lines_at_debug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    supervisor = _supervisor(tmp_path)
    # Evaluated at 03:00 UTC, yesterday's noon anchor is due (and missed); the
    # claim reports it already recorded, so nothing else runs.
    target = _target(
        exact_time_anchors=[{"id": "noon", "kind": "clock", "time": "12:00"}]
    )
    with (
        patch.object(supervisor, "_claim_exact_time_fire", return_value=None),
        caplog.at_level(logging.DEBUG, logger=supervisor_mod.__name__),
    ):
        wake = await supervisor._evaluate_exact_time_anchors(target, _NOW)  # noqa: SLF001
    assert wake is not None
    messages = [r.getMessage() for r in caplog.records]
    assert messages == [
        "anchor evaluation project=5 anchors=1 due=1",
        "anchor decision project=5 anchor=noon local_date=2026-05-31 "
        "within_grace=False has_geo=True status=skipped_missed "
        "claim=already_recorded",
    ]

    caplog.clear()
    with (
        patch.object(supervisor, "_claim_exact_time_fire", return_value=None),
        caplog.at_level(logging.INFO, logger=supervisor_mod.__name__),
    ):
        await supervisor._evaluate_exact_time_anchors(target, _NOW)  # noqa: SLF001
    assert caplog.records == []
    await supervisor.stop()
