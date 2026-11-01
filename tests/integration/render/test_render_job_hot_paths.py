"""Behaviour pins for the render_job hot-path rewrite (#66 C10, WP-66-J).

The worker's claim and the scheduler's due check were rewritten to read
through the 037 indexes. These tests pin that the *decisions* did not change:

* the scheduler's grouped history read enqueues exactly the jobs the previous
  per-project implementation did (kept here as an oracle), on a fixture that
  covers due, not due, the interval boundary, in-flight jobs of every kind,
  terminal jobs, inactive projects and every "off" schedule shape;
* the claim still takes the oldest pending job first, never a job in another
  status, and never a row that was settled between its read and its flip.

The query plans themselves are pinned in ``tests/unit/test_query_plans.py``.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from timelapse_manager.config.settings import Settings
from timelapse_manager.db.models import Camera, Project, RenderJob
from timelapse_manager.db.session import session_scope
from timelapse_manager.encode.encoder import (
    Encoder,
    OutputSettings,
    RenderResult,
    RenderSpec,
)
from timelapse_manager.render.queue import Clock, RenderQueue
from timelapse_manager.render.scheduler import RenderScheduler, _interval_seconds

_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
_DAY = 86400.0


class _NullEncoder(Encoder):
    """Never called: these tests only enqueue and claim rows."""

    async def validate(self, output: OutputSettings, *, has_chapters: bool) -> None:
        raise AssertionError("not used")

    async def render(self, spec: RenderSpec) -> RenderResult:
        raise AssertionError("not used")


class _FixedClock(Clock):
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


def _schedule(interval: Any = _DAY, *, enabled: bool = True) -> dict[str, Any]:
    return {"enabled": enabled, "interval_seconds": interval}


def _naive(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(tzinfo=None)


# (name, lifecycle, render_schedule, archive_schedule,
#  [(kind, status, age before _NOW)])
_FIXTURE: list[
    tuple[
        str,
        str,
        dict[str, Any] | None,
        dict[str, Any] | None,
        list[tuple[str, str, timedelta]],
    ]
] = [
    # No jobs yet: both kinds due.
    ("fresh", "active", _schedule(), _schedule(), []),
    # Newest scheduled is recent (not due); newest archive is old (due).
    (
        "recent-and-old",
        "active",
        _schedule(),
        _schedule(),
        [
            ("scheduled", "done", timedelta(days=9)),
            ("scheduled", "done", timedelta(hours=3)),
            ("archive", "done", timedelta(days=2)),
        ],
    ),
    # Exactly one interval old: due (the check is ``>=``); one second short is not.
    (
        "boundary",
        "active",
        _schedule(),
        _schedule(),
        [
            ("scheduled", "done", timedelta(seconds=_DAY)),
            ("archive", "done", timedelta(seconds=_DAY - 1)),
        ],
    ),
    # In flight blocks re-enqueue even when overdue: pending and encoding.
    (
        "in-flight",
        "active",
        _schedule(),
        _schedule(),
        [
            ("scheduled", "done", timedelta(days=30)),
            ("scheduled", "pending", timedelta(days=20)),
            ("archive", "encoding", timedelta(days=5)),
        ],
    ),
    # An in-flight job older than a newer finished one still blocks.
    (
        "in-flight-older",
        "active",
        _schedule(),
        None,
        [
            ("scheduled", "pending", timedelta(days=10)),
            ("scheduled", "done", timedelta(days=3)),
        ],
    ),
    # Only manual and export jobs (some in flight): neither counts for either kind.
    (
        "other-kinds",
        "active",
        _schedule(),
        _schedule(),
        [
            ("manual", "pending", timedelta(minutes=1)),
            ("export", "encoding", timedelta(minutes=1)),
            ("manual", "done", timedelta(minutes=5)),
        ],
    ),
    # Terminal jobs anchor the cadence like finished ones: failed recent, not due.
    (
        "failed-recent",
        "active",
        _schedule(),
        _schedule(),
        [
            ("scheduled", "failed", timedelta(hours=1)),
            ("archive", "failed", timedelta(days=3)),
        ],
    ),
    # Every "off" shape: absent, disabled, zero, negative, junk, missing interval.
    ("off-absent", "active", None, None, []),
    ("off-disabled", "active", _schedule(enabled=False), _schedule(0), []),
    ("off-negative", "active", _schedule(-5), _schedule("abc"), []),
    ("off-missing", "active", {"enabled": True}, {}, []),
    # A non-active project is never scheduled, whatever its schedules say.
    (
        "archived-project",
        "archived",
        _schedule(),
        _schedule(),
        [("scheduled", "done", timedelta(days=9))],
    ),
    # Shorter cadence, several jobs: due off the newest one only.
    (
        "hourly",
        "active",
        _schedule(3600),
        _schedule(7200),
        [
            ("scheduled", "done", timedelta(hours=5)),
            ("scheduled", "done", timedelta(minutes=59)),
            ("archive", "done", timedelta(hours=4)),
            ("archive", "done", timedelta(hours=2, seconds=1)),
        ],
    ),
]


def _seed(factory: sessionmaker[Session]) -> dict[str, int]:
    """Seed the fixture; return ``{project name: id}``."""
    ids: dict[str, int] = {}
    with session_scope(factory) as session:
        camera = Camera(name="cam", address="127.0.0.1", protocol="vapix")
        session.add(camera)
        session.flush()
        for name, lifecycle, render_schedule, archive_schedule, jobs in _FIXTURE:
            project = Project(
                camera_id=camera.id,
                name=name,
                lifecycle_state=lifecycle,
                operational_status="idle",
                render_schedule=render_schedule,
                archive_schedule=archive_schedule,
            )
            session.add(project)
            session.flush()
            ids[name] = project.id
            for kind, status, age in jobs:
                session.add(
                    RenderJob(
                        project_id=project.id,
                        kind=kind,
                        status=status,
                        created_at=_naive(_NOW - age),
                    )
                )
    return ids


def _oracle_due_pairs(
    factory: sessionmaker[Session], now: datetime
) -> list[tuple[int, str]]:
    """The pre-#66-C10 scheduler's decisions, verbatim in logic.

    Loads every active project, then per project and kind: off when the
    schedule has no live interval; not due when a pending/encoding job of the
    kind exists; due when there is no job of the kind or the newest (by
    ``created_at``) is at least an interval old.
    """
    due: list[tuple[int, str]] = []
    with session_scope(factory) as session:
        projects = (
            session.execute(
                select(Project)
                .where(Project.lifecycle_state == "active")
                .order_by(Project.id)
            )
            .scalars()
            .all()
        )
        for project in projects:
            for kind, schedule in (
                ("scheduled", project.render_schedule),
                ("archive", project.archive_schedule),
            ):
                interval = _interval_seconds(schedule)
                if interval is None:
                    continue
                in_flight = session.execute(
                    select(RenderJob.id)
                    .where(RenderJob.project_id == project.id)
                    .where(RenderJob.kind == kind)
                    .where(RenderJob.status.in_(("pending", "encoding")))
                    .limit(1)
                ).first()
                if in_flight is not None:
                    continue
                last_at = session.execute(
                    select(RenderJob.created_at)
                    .where(RenderJob.project_id == project.id)
                    .where(RenderJob.kind == kind)
                    .order_by(RenderJob.created_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if last_at is None:
                    due.append((project.id, kind))
                    continue
                if last_at.tzinfo is None:
                    last_at = last_at.replace(tzinfo=UTC)
                if (now - last_at).total_seconds() >= interval:
                    due.append((project.id, kind))
    return due


def _scheduler(
    factory: sessionmaker[Session], settings: Settings, now: datetime
) -> RenderScheduler:
    queue = RenderQueue(settings, factory, encoder=_NullEncoder())
    return RenderScheduler(settings, factory, queue, clock=_FixedClock(now))


def _created_pairs(
    factory: sessionmaker[Session], job_ids: list[int]
) -> list[tuple[int, str]]:
    with session_scope(factory) as session:
        rows = session.execute(
            select(RenderJob.id, RenderJob.project_id, RenderJob.kind, RenderJob.status)
            .where(RenderJob.id.in_(job_ids))
            .order_by(RenderJob.id)
        ).all()
    assert all(status == "pending" for *_rest, status in rows)
    return [(project_id, kind) for _id, project_id, kind, _status in rows]


class TestSchedulerDecisionsUnchanged:
    @pytest.mark.parametrize(
        "now",
        [
            _NOW,
            _NOW - timedelta(hours=1),
            _NOW + timedelta(hours=2),
            _NOW + timedelta(days=3),
        ],
        ids=["now", "hour-earlier", "two-hours-later", "three-days-later"],
    )
    def test_grouped_read_matches_the_per_project_oracle(
        self,
        migrated_factory: sessionmaker[Session],
        settings_no_autostart: Settings,
        now: datetime,
    ) -> None:
        _seed(migrated_factory)
        expected = _oracle_due_pairs(migrated_factory, now)

        created = _scheduler(
            migrated_factory, settings_no_autostart, now
        )._enqueue_due_jobs(now)  # noqa: SLF001

        # Same pairs, same order (project id, then scheduled before archive).
        assert _created_pairs(migrated_factory, created) == expected

    def test_fixture_exercises_every_branch(
        self,
        migrated_factory: sessionmaker[Session],
        settings_no_autostart: Settings,
    ) -> None:
        """The decisions at ``_NOW``, spelled out, so the oracle is not vacuous."""
        ids = _seed(migrated_factory)

        created = _scheduler(
            migrated_factory, settings_no_autostart, _NOW
        )._enqueue_due_jobs(_NOW)  # noqa: SLF001

        assert _created_pairs(migrated_factory, created) == [
            (ids["fresh"], "scheduled"),
            (ids["fresh"], "archive"),
            (ids["recent-and-old"], "archive"),
            (ids["boundary"], "scheduled"),
            (ids["other-kinds"], "scheduled"),
            (ids["other-kinds"], "archive"),
            (ids["failed-recent"], "archive"),
            (ids["hourly"], "archive"),
        ]

    def test_second_pass_enqueues_nothing(
        self,
        migrated_factory: sessionmaker[Session],
        settings_no_autostart: Settings,
    ) -> None:
        """The jobs a pass enqueues are in flight for the next pass.

        Even far past every interval, a pair whose job is still pending is not
        enqueued again (the double-enqueue guard).
        """
        _seed(migrated_factory)
        scheduler = _scheduler(migrated_factory, settings_no_autostart, _NOW)

        first = scheduler._enqueue_due_jobs(_NOW)  # noqa: SLF001
        assert first
        assert scheduler._enqueue_due_jobs(_NOW) == []  # noqa: SLF001
        later = scheduler._enqueue_due_jobs(_NOW + timedelta(days=9))  # noqa: SLF001
        assert not set(_created_pairs(migrated_factory, first)) & set(
            _created_pairs(migrated_factory, later)
        )

    def test_no_live_schedule_reads_no_render_job(
        self,
        migrated_factory: sessionmaker[Session],
        settings_no_autostart: Settings,
    ) -> None:
        with session_scope(migrated_factory) as session:
            camera = Camera(name="cam", address="127.0.0.1", protocol="vapix")
            session.add(camera)
            session.flush()
            session.add(
                Project(
                    camera_id=camera.id,
                    name="unscheduled",
                    lifecycle_state="active",
                    operational_status="idle",
                )
            )
        engine = migrated_factory.kw["bind"]
        assert isinstance(engine, Engine)
        statements: list[str] = []

        def listener(*args: Any) -> None:
            statements.append(str(args[2]))

        event.listen(engine, "before_cursor_execute", listener)
        try:
            created = _scheduler(
                migrated_factory, settings_no_autostart, _NOW
            )._enqueue_due_jobs(_NOW)  # noqa: SLF001
        finally:
            event.remove(engine, "before_cursor_execute", listener)

        assert created == []
        assert not [sql for sql in statements if "render_job" in sql]


def _add_job(
    factory: sessionmaker[Session], project_id: int, status: str, created_at: datetime
) -> int:
    with session_scope(factory) as session:
        job = RenderJob(
            project_id=project_id,
            kind="manual",
            status=status,
            created_at=created_at,
        )
        session.add(job)
        session.flush()
        return job.id


def _status(factory: sessionmaker[Session], job_id: int) -> str:
    with session_scope(factory) as session:
        return session.execute(
            select(RenderJob.status).where(RenderJob.id == job_id)
        ).scalar_one()


@pytest.fixture()
def project_id(migrated_factory: sessionmaker[Session]) -> int:
    with session_scope(migrated_factory) as session:
        camera = Camera(name="cam", address="127.0.0.1", protocol="vapix")
        session.add(camera)
        session.flush()
        project = Project(
            camera_id=camera.id,
            name="claims",
            lifecycle_state="active",
            operational_status="idle",
        )
        session.add(project)
        session.flush()
        return project.id


class TestClaimOrderUnchanged:
    def test_claims_the_oldest_pending_first_and_skips_other_statuses(
        self,
        migrated_factory: sessionmaker[Session],
        settings_no_autostart: Settings,
        project_id: int,
    ) -> None:
        """Oldest is by id (enqueue order), as before -- not by ``created_at``."""
        base = datetime(2026, 1, 1)
        done = _add_job(migrated_factory, project_id, "done", base)
        first = _add_job(migrated_factory, project_id, "pending", base + timedelta(9))
        encoding = _add_job(migrated_factory, project_id, "encoding", base)
        second = _add_job(migrated_factory, project_id, "pending", base)
        failed = _add_job(migrated_factory, project_id, "failed", base)
        third = _add_job(migrated_factory, project_id, "pending", base + timedelta(1))
        queue = RenderQueue(
            settings_no_autostart, migrated_factory, encoder=_NullEncoder()
        )

        claims = [queue._claim_next_pending() for _ in range(4)]  # noqa: SLF001

        assert claims == [first, second, third, None]
        for job_id in (first, second, third, encoding):
            assert _status(migrated_factory, job_id) == "encoding"
        assert _status(migrated_factory, done) == "done"
        assert _status(migrated_factory, failed) == "failed"
        with session_scope(migrated_factory) as session:
            started = session.execute(
                select(RenderJob.started_at).where(RenderJob.id == first)
            ).scalar_one()
        assert started is not None

    def test_a_row_settled_between_read_and_flip_is_not_claimed(
        self,
        migrated_factory: sessionmaker[Session],
        settings_no_autostart: Settings,
        project_id: int,
        tmp_db_path: Any,
    ) -> None:
        """A writer outside the queue fails the row after the claim reads it.

        The claim's flip is conditional on ``pending``, so it leaves the
        settled row alone and reports nothing claimed; the next claim takes
        the next pending job.
        """
        base = datetime(2026, 1, 1)
        target = _add_job(migrated_factory, project_id, "pending", base)
        following = _add_job(migrated_factory, project_id, "pending", base)
        engine = migrated_factory.kw["bind"]
        assert isinstance(engine, Engine)
        fired: list[bool] = []

        def settle_first(*args: Any) -> None:
            statement = str(args[2])
            if fired or not statement.lstrip().upper().startswith("UPDATE RENDER_JOB"):
                return
            fired.append(True)
            other = sqlite3.connect(str(tmp_db_path), timeout=5)
            try:
                other.execute(
                    "UPDATE render_job SET status = 'failed' WHERE id = ?", (target,)
                )
                other.commit()
            finally:
                other.close()

        queue = RenderQueue(
            settings_no_autostart, migrated_factory, encoder=_NullEncoder()
        )
        event.listen(engine, "before_cursor_execute", settle_first)
        try:
            assert queue._claim_next_pending() is None  # noqa: SLF001
        finally:
            event.remove(engine, "before_cursor_execute", settle_first)

        assert fired == [True]
        assert _status(migrated_factory, target) == "failed"
        assert queue._claim_next_pending() == following  # noqa: SLF001
        assert _status(migrated_factory, following) == "encoding"
