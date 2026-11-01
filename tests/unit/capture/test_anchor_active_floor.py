"""Anchor instants before a project was (re)activated are not "missed" (#280).

A new or resumed anchor project used to write a ``skipped_missed`` fire-log row
and a warning alert for the anchor instant that passed before it existed or while
it was paused. The runner now ignores instants earlier than the project's
``active_since``; a miss while the project was active is still recorded (#6).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.unit.capture.test_anchor_retry import (
    _INSTANT,
    FakeClock,
    _active_alert_reasons,
    _events,
    _seed,
    _supervisor,
    adapter_script,  # noqa: F401
)
from timelapse_manager.db.models import Camera, ExactTimeFire, Project
from timelapse_manager.db.session import session_scope

# Two hours after the 12:05 anchor: well past the 30 minute grace window.
_LATE = _INSTANT + timedelta(hours=2)


def _fire_rows(factory: Any, project_id: int) -> list[tuple[str, str]]:
    with session_scope(factory) as session:
        return [
            (row.local_date, row.status)
            for row in session.query(ExactTimeFire).filter_by(project_id=project_id)
        ]


class TestNoFalseMiss:
    async def test_a_project_created_after_todays_anchor_records_nothing(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        clock = FakeClock(_LATE)
        supervisor = _supervisor(migrated_factory, tmp_path, clock)
        target = dataclasses.replace(
            _seed(migrated_factory, tmp_path), active_since=_LATE - timedelta(minutes=3)
        )

        await supervisor._evaluate_exact_time_anchors(target, clock.now())
        await supervisor.stop()

        assert _fire_rows(migrated_factory, target.project_id) == []
        assert _events(migrated_factory, target.project_id) == []
        assert _active_alert_reasons(migrated_factory, target.project_id) == []

    async def test_a_project_resumed_after_a_paused_anchor_records_nothing(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        """Paused across the anchor, resumed an hour after it: no warning."""
        clock = FakeClock(_LATE)
        supervisor = _supervisor(migrated_factory, tmp_path, clock)
        target = dataclasses.replace(
            _seed(migrated_factory, tmp_path),
            active_since=_INSTANT + timedelta(hours=1),
        )

        await supervisor._evaluate_exact_time_anchors(target, clock.now())
        await supervisor.stop()

        assert _fire_rows(migrated_factory, target.project_id) == []
        assert _active_alert_reasons(migrated_factory, target.project_id) == []

    @pytest.mark.usefixtures("adapter_script")
    async def test_an_anchor_that_fires_after_activation_still_captures(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        """The floor drops only older instants: a later one fires normally."""
        clock = FakeClock(_INSTANT + timedelta(minutes=1))
        supervisor = _supervisor(migrated_factory, tmp_path, clock)
        target = dataclasses.replace(
            _seed(migrated_factory, tmp_path),
            active_since=_INSTANT - timedelta(hours=1),
        )

        await supervisor._evaluate_exact_time_anchors(target, clock.now())
        await supervisor.stop()

        assert _fire_rows(migrated_factory, target.project_id) == [
            ("2026-10-14", "captured")
        ]


class TestGenuineMissIsStillRecorded:
    async def test_a_runner_stalled_across_an_anchor_while_active_alerts(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        clock = FakeClock(_LATE)
        supervisor = _supervisor(migrated_factory, tmp_path, clock)
        target = dataclasses.replace(
            _seed(migrated_factory, tmp_path), active_since=_INSTANT - timedelta(days=2)
        )

        await supervisor._evaluate_exact_time_anchors(target, clock.now())
        await supervisor.stop()

        assert _fire_rows(migrated_factory, target.project_id) == [
            ("2026-10-14", "skipped_missed")
        ]
        assert any(
            level == "warning"
            for level, _ in _events(migrated_factory, target.project_id)
        )
        assert _active_alert_reasons(migrated_factory, target.project_id) != []


class TestActiveSinceIsMaintained:
    def _project(self, factory: Any) -> int:
        with session_scope(factory) as session:
            cam = Camera(name="c", address="10.0.16.71", protocol="vapix")
            session.add(cam)
            session.flush()
            project = Project(camera_id=cam.id, name="p", lifecycle_state="active")
            session.add(project)
            session.flush()
            assert project.active_since is not None
            return project.id

    def test_resume_stamps_and_other_edits_do_not(self, migrated_factory: Any) -> None:
        pid = self._project(migrated_factory)
        old = datetime(2026, 1, 1)
        with session_scope(migrated_factory) as session:
            project = session.get(Project, pid)
            assert project is not None
            project.active_since = old
            project.lifecycle_state = "paused"
        with session_scope(migrated_factory) as session:
            project = session.get(Project, pid)
            assert project is not None
            assert project.active_since == old  # pausing does not move it
            project.name = "renamed"
        with session_scope(migrated_factory) as session:
            project = session.get(Project, pid)
            assert project is not None
            assert project.active_since == old  # an unrelated edit does not
            project.lifecycle_state = "active"
        with session_scope(migrated_factory) as session:
            project = session.get(Project, pid)
            assert project is not None
            assert project.active_since is not None
            assert project.active_since > old
            assert project.active_since > datetime.now(UTC).replace(
                tzinfo=None
            ) - timedelta(minutes=5)

    def test_target_from_rows_carries_it_and_falls_back_to_created_at(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        supervisor = _supervisor(migrated_factory, tmp_path, FakeClock(_LATE))
        pid = self._project(migrated_factory)
        with session_scope(migrated_factory) as session:
            project = session.get(Project, pid)
            assert project is not None
            camera = session.get(Camera, project.camera_id)
            assert camera is not None
            stamp = datetime(2026, 5, 5, 1, 2, 3)
            project.active_since = stamp
            session.flush()
            target = supervisor.target_from_rows(project, camera)
            assert target.active_since == stamp.replace(tzinfo=UTC)
            project.active_since = None  # a row from before the column
            target = supervisor.target_from_rows(project, camera)
            assert target.active_since == project.created_at.replace(tzinfo=UTC)
