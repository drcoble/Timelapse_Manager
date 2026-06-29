"""Unit tests for the project operational-status presentation mapping.

``_project_operational_status`` translates a project's lifecycle flag and its
live capture state into the single status word the templates branch on. These
exercise the pure mapping directly (no app, no DB) so the paused surface is
pinned independently of the route wiring.
"""

from __future__ import annotations

from dataclasses import dataclass

from timelapse_manager.web.routers import _project_operational_status


@dataclass
class _FakeProject:
    """Minimal stand-in carrying only the field the mapping reads."""

    lifecycle_state: str


@dataclass
class _FakeState:
    """Minimal stand-in for a live CaptureState (only the read fields)."""

    state: str
    pause_reason: str | None = None


class TestOperationalStatusMapping:
    def test_paused_lifecycle_reads_paused_with_no_live_state(self) -> None:
        # A paused project has had its runner stopped, so there is no live state;
        # it must still read "paused" rather than falling through to "stopped".
        project = _FakeProject(lifecycle_state="paused")
        assert _project_operational_status(project, None) == "paused"

    def test_paused_lifecycle_wins_over_live_state(self) -> None:
        # Even if a stale live state lingered, the paused lifecycle is
        # authoritative for the surface.
        project = _FakeProject(lifecycle_state="paused")
        state = _FakeState(state="running")
        assert _project_operational_status(project, state) == "paused"

    def test_active_with_no_state_reads_stopped(self) -> None:
        project = _FakeProject(lifecycle_state="active")
        assert _project_operational_status(project, None) == "stopped"

    def test_active_with_running_state_reads_running(self) -> None:
        project = _FakeProject(lifecycle_state="active")
        state = _FakeState(state="running")
        assert _project_operational_status(project, state) == "running"

    def test_active_idle_window_pause_reads_paused(self) -> None:
        # An active project whose schedule window is closed reads "paused" from the
        # live-state branch (distinct from the lifecycle-paused branch above).
        project = _FakeProject(lifecycle_state="active")
        state = _FakeState(state="idle", pause_reason="window")
        assert _project_operational_status(project, state) == "paused"
