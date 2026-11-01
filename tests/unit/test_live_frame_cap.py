"""A running capture loop honours an edited ``max_frame_count`` (#104).

The loop used to enforce the cap it was launched with; an edit reached it only
when the next reconcile pass restarted it. These tests edit the stored cap while
the loop runs (from inside the fake clock's ``sleep``, i.e. between two cycles)
and require the very next cycle to act on it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from tests.unit.test_campaign_bounds import (
    _INTERVAL,
    _MAX_CYCLES,
    FakeClock,
    _frame_count,
    _lifecycle,
    _make_settings,
    _permissive_disk_monitor,
    _run_one_loop,
    _seed_camera_and_project,
)
from timelapse_manager.capture.supervisor import (
    CaptureState,
    CaptureSupervisor,
    CaptureTarget,
)
from timelapse_manager.db.models import Project
from timelapse_manager.db.session import session_scope


class _EditingClock(FakeClock):
    """A fake clock that runs ``hook`` on the Nth sleep (between two cycles)."""

    def __init__(self, hook_at: int, hook: Callable[[], None]) -> None:
        super().__init__(start=datetime(2026, 7, 1, 8, 0, tzinfo=UTC))
        self._hook_at = hook_at
        self._hook = hook

    async def sleep(self, seconds: float) -> None:
        if self.cycle_count + 1 == self._hook_at:
            self._hook()
        await super().sleep(seconds)


async def _run(
    migrated_factory,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    *,
    launch_cap: int | None,
    edit: Callable[[int], int | None],
    hook_at: int = 3,
) -> tuple[CaptureState, int, int | None, _EditingClock]:
    """Run a loop launched with ``launch_cap``; at the hook set the stored cap.

    ``edit`` receives the frame count at the hook and returns the new cap.
    Returns the final state, the frame count the hook saw, the cap it set, and
    the clock.
    """
    ctx = _seed_camera_and_project(
        migrated_factory, tmp_path, name="live-cap", max_frame_count=launch_cap
    )
    seen: dict[str, int | None] = {}

    def hook() -> None:
        count = _frame_count(migrated_factory, ctx["project_id"])
        new_cap = edit(count)
        seen["count"], seen["cap"] = count, new_cap
        with session_scope(migrated_factory) as session:
            session.get(Project, ctx["project_id"]).max_frame_count = new_cap

    clock = _EditingClock(hook_at, hook)
    supervisor = CaptureSupervisor(
        _make_settings(tmp_path),
        migrated_factory,
        clock=clock,
        disk_monitor=_permissive_disk_monitor(),
    )
    target = CaptureTarget(
        project_id=ctx["project_id"],
        project_name="live-cap",
        camera_id=ctx["camera_id"],
        interval_seconds=_INTERVAL,
        storage_path=str(ctx["storage_path"]),
        max_frame_count=launch_cap,
    )
    state = CaptureState(project_id=ctx["project_id"], camera_id=ctx["camera_id"])
    await _run_one_loop(supervisor, target, state)
    await supervisor.stop()
    count = seen["count"]
    assert count is not None, "the loop ended before the cap was edited"
    return state, count, seen["cap"], clock


async def test_lowering_the_cap_below_the_count_stops_the_next_cycle(
    migrated_factory,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    state, count, _cap, clock = await _run(
        migrated_factory, tmp_path, launch_cap=1000, edit=lambda c: c - 1
    )
    project_id = state.project_id
    assert count >= 1
    # No frame was captured after the edit, and the campaign ended.
    assert _frame_count(migrated_factory, project_id) == count
    assert _lifecycle(migrated_factory, project_id) == "archived"
    assert state.state == "stopped"
    assert clock.cycle_count < _MAX_CYCLES


async def test_lowering_the_cap_to_the_count_stops_without_another_frame(
    migrated_factory,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    state, count, _cap, clock = await _run(
        migrated_factory, tmp_path, launch_cap=1000, edit=lambda c: c
    )
    assert _frame_count(migrated_factory, state.project_id) == count
    assert _lifecycle(migrated_factory, state.project_id) == "archived"
    assert clock.cycle_count < _MAX_CYCLES


async def test_a_cap_added_to_an_uncapped_loop_ends_it_at_exactly_the_cap(
    migrated_factory,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    state, count, cap, clock = await _run(
        migrated_factory, tmp_path, launch_cap=None, edit=lambda c: c + 2
    )
    assert cap == count + 2
    assert _frame_count(migrated_factory, state.project_id) == cap
    assert _lifecycle(migrated_factory, state.project_id) == "archived"
    assert clock.cycle_count < _MAX_CYCLES


async def test_removing_the_cap_lets_a_capped_loop_continue(
    migrated_factory,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    """Launched with a cap of 4, edited to none: it runs past 4 until the bound."""
    state, _count, _cap, _clock = await _run(
        migrated_factory, tmp_path, launch_cap=4, edit=lambda c: None, hook_at=2
    )
    assert _frame_count(migrated_factory, state.project_id) > 4
    assert _lifecycle(migrated_factory, state.project_id) == "active"
