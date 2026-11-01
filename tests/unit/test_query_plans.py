"""Query-plan regression tests for the hot-path indexes (issue #66).

Each test calls the **real** query function on a migrated database, captures
the SQL it emits with a ``before_cursor_execute`` listener, and runs
``EXPLAIN QUERY PLAN`` on that exact SQL with its parameters. A hand-written
copy of the query would keep passing after the code drifted, so it is never
used here.

The table is seeded with a realistic mix and then ``ANALYZE``-d: on an empty
table the planner's choice says nothing. Plan text differs between SQLite
versions, so the checks are loose: the expected index name appears in the plan,
and the table is never read by a bare full scan (``SCAN <table>`` with no
``USING ... INDEX``).

Shared by WP-66-D (event), WP-66-E (frame) and WP-66-J (render_job): add a
seeder and a test class per table, and reuse :func:`_captured_sql`,
:func:`_plan` and :func:`_assert_uses_index`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, event, insert
from sqlalchemy.orm import Session, sessionmaker

from timelapse_manager.config import Settings
from timelapse_manager.db.models import Camera, Event, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.encode.frame_source import gather_frames
from timelapse_manager.monitoring.alerts import (
    auto_clear_for_event,
    get_active_alerts,
)
from timelapse_manager.storage.frames import project_frame_aggregates

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _engine(factory: sessionmaker[Session]) -> Engine:
    bind = factory.kw["bind"]
    assert isinstance(bind, Engine)
    return bind


def _analyze(factory: sessionmaker[Session]) -> None:
    """Gather ``sqlite_stat1`` and make every pooled connection reload it."""
    engine = _engine(factory)
    with engine.connect() as conn:
        conn.exec_driver_sql("ANALYZE")
        conn.commit()
    engine.dispose()


# Production runs with no ``sqlite_stat1`` (the app never runs ANALYZE), but an
# operator's ``ANALYZE`` or ``PRAGMA optimize`` must not turn an index off, so
# every plan is checked in both regimes.
STATS_REGIMES = pytest.mark.parametrize(
    "analyze", [False, True], ids=["nostats", "analyze"]
)


@contextmanager
def _captured_sql(engine: Engine) -> Iterator[list[tuple[str, Any]]]:
    """Collect every ``(statement, parameters)`` the engine sends to SQLite."""
    captured: list[tuple[str, Any]] = []

    def listener(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        captured.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", listener)
    try:
        yield captured
    finally:
        event.remove(engine, "before_cursor_execute", listener)


def _run_and_capture(
    factory: sessionmaker[Session], call: Callable[[Session], object]
) -> list[tuple[str, Any]]:
    with _captured_sql(_engine(factory)) as captured, session_scope(factory) as session:
        call(session)
    return captured


def _one(captured: list[tuple[str, Any]], prefix: str) -> tuple[str, Any]:
    """Return the single captured statement that starts with ``prefix``."""
    matches = [
        (sql, params)
        for sql, params in captured
        if sql.lstrip().upper().startswith(prefix.upper())
    ]
    assert len(matches) == 1, f"expected one {prefix!r} statement, got {matches}"
    return matches[0]


def _plan(factory: sessionmaker[Session], sql: str, params: Any) -> list[str]:
    """Return the ``detail`` column of ``EXPLAIN QUERY PLAN`` for ``sql``."""
    raw = _engine(factory).raw_connection()
    try:
        rows = raw.cursor().execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    finally:
        raw.close()
    return [str(row[-1]) for row in rows]


def _assert_uses_index(plan: list[str], index: str, table: str) -> None:
    bare_scan = re.compile(rf"^SCAN (TABLE )?{table}$")
    assert any(index in step for step in plan), (index, plan)
    assert not any(bare_scan.match(step.strip()) for step in plan), plan


# ---------------------------------------------------------------------------
# event (WP-66-D, migration 033)
# ---------------------------------------------------------------------------


def _seed_events(factory: sessionmaker[Session], *, analyze: bool) -> None:
    """A production-like event log: mostly info, cleared alerts, few open ones.

    About 6k rows (prod had about 5.4k) across system, camera and project
    scopes, so the planner sees realistic selectivity after ``ANALYZE``.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    rows: list[Event] = []
    for i in range(6000):
        scope, scope_id = (
            ("project", i % 25 + 1)
            if i % 3
            else (("camera", i % 10 + 1) if i % 2 else ("system", None))
        )
        when = now - timedelta(minutes=6000 - i)
        if i % 40 == 0:
            rows.append(
                Event(
                    scope=scope,
                    scope_id=scope_id,
                    level="warning",
                    message="cleared",
                    timestamp=when,
                    event_metadata={"reason": "low_disk"},
                    alert_cleared_at=when + timedelta(minutes=5),
                    alert_clear_reason="auto",
                )
            )
        elif i % 500 == 1:
            rows.append(
                Event(
                    scope=scope,
                    scope_id=scope_id,
                    level="error",
                    message="open",
                    timestamp=when,
                    event_metadata={"reason": "low_disk"},
                )
            )
        else:
            rows.append(
                Event(
                    scope=scope,
                    scope_id=scope_id,
                    level="info",
                    message="capture ok",
                    timestamp=when,
                    event_metadata={"type": "capture.frame"},
                )
            )
    with session_scope(factory) as session:
        session.add_all(rows)
    if analyze:
        _analyze(factory)


def _alert_reads(
    factory: sessionmaker[Session], **kwargs: Any
) -> tuple[tuple[str, Any], tuple[str, Any]]:
    """Run ``get_active_alerts`` and return its (count, page) statements."""
    captured = _run_and_capture(
        factory, lambda session: get_active_alerts(session, **kwargs)
    )
    count = _one(captured, "SELECT count(")
    page = _one(captured, "SELECT event.id")
    return count, page


class TestEventPlans:
    @STATS_REGIMES
    def test_active_alert_count_and_page_use_the_partial_index(
        self, migrated_factory: sessionmaker[Session], analyze: bool
    ) -> None:
        _seed_events(migrated_factory, analyze=analyze)

        count, page = _alert_reads(migrated_factory)

        for sql, params in (count, page):
            _assert_uses_index(
                _plan(migrated_factory, sql, params), "ix_event_open_alerts", "event"
            )

    @STATS_REGIMES
    def test_scoped_alert_reads_use_an_index(
        self, migrated_factory: sessionmaker[Session], analyze: bool
    ) -> None:
        """With a scope filter any event index will do; never a full scan."""
        _seed_events(migrated_factory, analyze=analyze)

        count, page = _alert_reads(migrated_factory, scope="project", scope_id=7)

        for sql, params in (count, page):
            plan = _plan(migrated_factory, sql, params)
            assert any("INDEX ix_event_" in step for step in plan), plan
            assert not any(re.match(r"^SCAN (TABLE )?event$", s) for s in plan), plan

    @STATS_REGIMES
    def test_higher_floor_still_uses_the_partial_index(
        self, migrated_factory: sessionmaker[Session], analyze: bool
    ) -> None:
        _seed_events(migrated_factory, analyze=analyze)

        count, page = _alert_reads(migrated_factory, level_floor="error")

        for sql, params in (count, page):
            _assert_uses_index(
                _plan(migrated_factory, sql, params), "ix_event_open_alerts", "event"
            )

    def test_alert_reads_repeat_the_index_predicate_as_literals(
        self, migrated_factory: sessionmaker[Session]
    ) -> None:
        """SQLite only uses a partial index when the query repeats its WHERE.

        The level list must reach SQLite as literals in the index's order; a
        bound ``?`` list would silently stop the index being used.
        """
        count, page = _alert_reads(migrated_factory)

        for sql, _params in (count, page):
            flat = " ".join(sql.split())
            assert "level IN ('warning', 'error', 'critical')" in flat, flat
            assert "FROM (" not in flat, "the count must not wrap a sub-select"

    def test_index_levels_match_the_alert_threshold(self) -> None:
        from timelapse_manager.db.models.event import OPEN_ALERT_INDEX_LEVELS
        from timelapse_manager.monitoring.alerts import ALERT_LEVEL_THRESHOLD
        from timelapse_manager.monitoring.events import _levels_at_or_above

        assert tuple(_levels_at_or_above(ALERT_LEVEL_THRESHOLD)) == (
            OPEN_ALERT_INDEX_LEVELS
        )

    @STATS_REGIMES
    @pytest.mark.parametrize(
        ("scope", "scope_id"),
        [("project", 7), ("system", None)],
        ids=["entity", "system"],
    )
    def test_auto_clear_update_uses_the_scope_index(
        self,
        migrated_factory: sessionmaker[Session],
        analyze: bool,
        scope: str,
        scope_id: int | None,
    ) -> None:
        """``scope_id IS NULL`` (system scope) is served by the index too."""
        _seed_events(migrated_factory, analyze=analyze)

        captured = _run_and_capture(
            migrated_factory,
            lambda session: auto_clear_for_event(
                session, scope=scope, scope_id=scope_id, reason="disk_recovered"
            ),
        )

        update_sql, update_params = _one(captured, "UPDATE event")
        _assert_uses_index(
            _plan(migrated_factory, update_sql, update_params),
            "ix_event_scope_scope_id",
            "event",
        )


# ---------------------------------------------------------------------------
# frame (WP-66-E, migration 035)
# ---------------------------------------------------------------------------

_FRAME_PROJECTS = 13
_FRAMES_PER_PROJECT = 400


def _seed_frames(factory: sessionmaker[Session], *, analyze: bool) -> list[int]:
    """A production-like frame table; return the project ids.

    13 projects of 400 frames (prod had about 5,200 frames across 13 projects):
    mostly active captures, with uploads, soft-deleted and excluded frames,
    null sizes and repeated timestamps, so the planner sees realistic
    selectivity after ``ANALYZE``.
    """
    base = datetime(2026, 1, 1, 12, 0)
    with session_scope(factory) as session:
        camera = Camera(name="cam")
        session.add(camera)
        session.flush()
        projects = [
            Project(camera_id=camera.id, name=f"p{n}") for n in range(_FRAME_PROJECTS)
        ]
        session.add_all(projects)
        session.flush()
        project_ids = [project.id for project in projects]
        rows: list[dict[str, Any]] = []
        for project_id in project_ids:
            for seq in range(1, _FRAMES_PER_PROJECT + 1):
                rows.append(
                    {
                        "project_id": project_id,
                        "sequence_index": seq,
                        # Every 50th frame repeats its neighbour's time.
                        "capture_timestamp": base
                        + timedelta(days=seq - seq % 50 // 49),
                        "file_path": f"{seq:08d}.jpg",
                        "width": 1920,
                        "height": 1080,
                        "file_size_bytes": None if seq % 17 == 0 else 250_000 + seq,
                        "capture_status": "captured",
                        "origin": "uploaded" if seq % 9 == 0 else "captured",
                        "lifecycle_state": "soft_deleted"
                        if seq % 23 == 0
                        else "active",
                        "excluded_at": base if seq % 31 == 0 else None,
                    }
                )
        session.execute(insert(Frame), rows)
    if analyze:
        _analyze(factory)
    return project_ids


class TestFramePlans:
    @STATS_REGIMES
    def test_dashboard_aggregate_reads_only_the_covering_index(
        self, migrated_factory: sessionmaker[Session], analyze: bool
    ) -> None:
        project_ids = _seed_frames(migrated_factory, analyze=analyze)

        captured = _run_and_capture(
            migrated_factory,
            lambda session: project_frame_aggregates(session, project_ids),
        )

        sql, params = _one(captured, "SELECT frame.project_id")
        plan = _plan(migrated_factory, sql, params)
        _assert_uses_index(plan, "ix_frame_project_state_origin_ts_size", "frame")
        assert any(
            "COVERING INDEX ix_frame_project_state_origin_ts_size" in step
            for step in plan
        ), plan

    @STATS_REGIMES
    def test_preview_lookup_is_an_index_search_per_project(
        self, migrated_factory: sessionmaker[Session], analyze: bool
    ) -> None:
        """Newest active frame per project: an index walk, never a scan or sort."""
        project_ids = _seed_frames(migrated_factory, analyze=analyze)

        captured = _run_and_capture(
            migrated_factory,
            lambda session: project_frame_aggregates(session, project_ids),
        )

        sql, params = _one(captured, "SELECT project.id")
        plan = _plan(migrated_factory, sql, params)
        assert any(
            step.startswith("SEARCH frame USING") and "INDEX" in step for step in plan
        ), plan
        assert not any(re.match(r"^SCAN (TABLE )?(frame|project)$", s) for s in plan)
        assert not any("TEMP B-TREE" in step for step in plan), plan

    @STATS_REGIMES
    def test_render_gather_streams_in_index_order(
        self,
        migrated_factory: sessionmaker[Session],
        settings: Settings,
        analyze: bool,
    ) -> None:
        """The gather's ORDER BY is served by the index: no sort of any kind."""
        project_ids = _seed_frames(migrated_factory, analyze=analyze)

        captured = _run_and_capture(
            migrated_factory,
            lambda session: gather_frames(session, settings, project_ids[3]),
        )

        sql, params = _one(captured, "SELECT frame.sequence_index")
        plan = _plan(migrated_factory, sql, params)
        _assert_uses_index(plan, "ix_frame_project_ts", "frame")
        assert not any("TEMP B-TREE" in step for step in plan), plan


# ---------------------------------------------------------------------------
# frame: import resequence, gap jump, dimensions, bulk loads (WP-66-F, #66 C2/C7)
# ---------------------------------------------------------------------------

# Imported here, not at the top, so this section stays an append-only addition
# to a file other work packages also extend.
from timelapse_manager.storage.frames import (  # noqa: E402
    _resequence_after_import,
    active_frame_time_bounds,
    list_capture_gaps,
    load_frames_by_id,
    predominant_dimensions,
)


def _frame_statements(captured: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    """Single-row statements on ``frame`` (executemany by primary key excluded)."""
    return [
        (" ".join(sql.split()), params)
        for sql, params in captured
        if re.search(r"\bframe\b", sql)
        and not sql.lstrip().upper().startswith("INSERT")
        and not (isinstance(params, list) and params and isinstance(params[0], tuple))
    ]


def _matching(statements: list[tuple[str, Any]], needle: str) -> tuple[str, Any]:
    matches = [(sql, params) for sql, params in statements if needle in sql]
    assert len(matches) == 1, (needle, [sql for sql, _ in statements])
    return matches[0]


def _assert_no_bare_frame_scan(plan: list[str]) -> None:
    assert any("INDEX" in step or "PRIMARY KEY" in step for step in plan), plan
    assert not any(re.match(r"^SCAN (TABLE )?frame$", s.strip()) for s in plan), plan


class TestFrameImportAndBulkPlans:
    @STATS_REGIMES
    def test_suffix_resequence_reads_and_writes_through_frame_indexes(
        self, migrated_factory: sessionmaker[Session], analyze: bool
    ) -> None:
        """Every statement of an import's resequence is served by an index.

        The plans, as of SQLite 3.53 (``nostats`` / ``analyze``):

        * imported-frames bound: ``SEARCH frame USING INTEGER PRIMARY KEY``;
        * order scan: ``SEARCH frame USING INDEX sqlite_autoindex_frame_1``
          (``uq_frame_project_seq``) -- a walk in sequence order, no sort;
        * prefix bound: ``SEARCH frame USING COVERING INDEX ix_frame_project_ts
          (project_id=? AND capture_timestamp<?)``;
        * suffix ids: ``ix_frame_project_ts`` (covering; ``MULTI-INDEX OR`` once
          analysed) plus ``USE TEMP B-TREE FOR ORDER BY`` -- a sort of the
          suffix rows only, for the ``(ts IS NULL, ts, id)`` order;
        * park UPDATE: ``SEARCH frame USING [COVERING] INDEX ix_frame_project_ts``;
        * assign: executemany ``WHERE frame.id = ?`` (primary key).
        """
        project_ids = _seed_frames(migrated_factory, analyze=analyze)
        project_id = project_ids[3]
        # One "imported" historic frame at the end of the sequence.
        with session_scope(migrated_factory) as session:
            imported_id = session.execute(
                insert(Frame).returning(Frame.id),
                [
                    {
                        "project_id": project_id,
                        "sequence_index": 10_000,
                        "capture_timestamp": datetime(2026, 6, 1, 8, 0),
                        "file_path": "imported.jpg",
                        "capture_status": "captured",
                        "origin": "uploaded",
                        "lifecycle_state": "active",
                    }
                ],
            ).scalar_one()

        captured = _run_and_capture(
            migrated_factory,
            lambda session: _resequence_after_import(
                session, project_id, [imported_id]
            ),
        )
        statements = _frame_statements(captured)

        bound_sql, bound_params = _matching(statements, "WHERE frame.id IN")
        assert any(
            "PRIMARY KEY" in s for s in _plan(migrated_factory, bound_sql, bound_params)
        )

        scan_sql, scan_params = _matching(statements, "lag(frame.capture_timestamp)")
        scan_plan = _plan(migrated_factory, scan_sql, scan_params)
        _assert_uses_index(scan_plan, "sqlite_autoindex_frame_1", "frame")
        assert not any("TEMP B-TREE" in step for step in scan_plan), scan_plan

        prefix_sql, prefix_params = _matching(statements, "capture_timestamp < ?")
        prefix_plan = _plan(migrated_factory, prefix_sql, prefix_params)
        assert any(
            "COVERING INDEX ix_frame_project_ts" in step for step in prefix_plan
        ), prefix_plan

        suffix_sql, suffix_params = _matching(statements, "SELECT frame.id FROM frame")
        _assert_uses_index(
            _plan(migrated_factory, suffix_sql, suffix_params),
            "ix_frame_project_ts",
            "frame",
        )

        park_sql, park_params = _matching(statements, "UPDATE frame SET")
        _assert_uses_index(
            _plan(migrated_factory, park_sql, park_params),
            "ix_frame_project_ts",
            "frame",
        )

    @STATS_REGIMES
    def test_gap_jump_streams_ix_frame_project_ts_without_sorting(
        self, migrated_factory: sessionmaker[Session], analyze: bool
    ) -> None:
        """Bounds and gaps walk ``ix_frame_project_ts`` in order: no sort step."""
        project_ids = _seed_frames(migrated_factory, analyze=analyze)
        project_id = project_ids[5]
        start = datetime(2026, 1, 1, tzinfo=UTC)

        captured = _run_and_capture(
            migrated_factory,
            lambda session: (
                active_frame_time_bounds(session, project_id),
                list_capture_gaps(
                    session, project_id, start, start + timedelta(days=500)
                ),
            ),
        )

        for needle in ("count(*)", "lag("):
            sql, params = _matching(_frame_statements(captured), needle)
            plan = _plan(migrated_factory, sql, params)
            _assert_uses_index(plan, "ix_frame_project_ts", "frame")
            assert not any("TEMP B-TREE" in step for step in plan), plan

    @STATS_REGIMES
    def test_dimension_cache_key_and_increment_are_index_reads(
        self, migrated_factory: sessionmaker[Session], analyze: bool
    ) -> None:
        project_ids = _seed_frames(migrated_factory, analyze=analyze)
        project_id = project_ids[7]

        captured = _run_and_capture(
            migrated_factory,
            lambda session: predominant_dimensions(session, project_id),
        )

        key_sql, key_params = _matching(
            _frame_statements(captured), "SELECT project.frame_count"
        )
        key_plan = _plan(migrated_factory, key_sql, key_params)
        assert any("COVERING INDEX ix_frame_project_id" in s for s in key_plan), (
            key_plan
        )
        histogram_sql, histogram_params = _matching(
            _frame_statements(captured), "GROUP BY frame.width"
        )
        _assert_no_bare_frame_scan(
            _plan(migrated_factory, histogram_sql, histogram_params)
        )

    def test_bulk_load_is_a_primary_key_search(
        self, migrated_factory: sessionmaker[Session]
    ) -> None:
        _seed_frames(migrated_factory, analyze=False)

        captured = _run_and_capture(
            migrated_factory,
            lambda session: load_frames_by_id(session, range(1, 701)),
        )

        loads = [
            (sql, params)
            for sql, params in _frame_statements(captured)
            if "WHERE frame.id IN" in sql
        ]
        assert len(loads) == 2  # 700 ids: chunks of 500 and 200
        for sql, params in loads:
            plan = _plan(migrated_factory, sql, params)
            assert plan == ["SEARCH frame USING INTEGER PRIMARY KEY (rowid=?)"], plan


# ---------------------------------------------------------------------------
# render_job: worker claim, watchdog reads, scheduler pass (WP-66-J, #66 C10)
# ---------------------------------------------------------------------------

# Imported here, not at the top, so this section stays an append-only addition
# to a file other work packages also extend.
from timelapse_manager.db.models import RenderJob  # noqa: E402
from timelapse_manager.db.models.render_job import ACTIVE_JOB_INDEX_WHERE  # noqa: E402
from timelapse_manager.encode import Encoder, RenderResult, RenderSpec  # noqa: E402
from timelapse_manager.encode.encoder import OutputSettings  # noqa: E402
from timelapse_manager.render.queue import RenderQueue  # noqa: E402
from timelapse_manager.render.scheduler import RenderScheduler  # noqa: E402

_JOB_PROJECTS = 13
_JOBS_PER_PROJECT = 400
_LIVE_SCHEDULE = {"enabled": True, "interval_seconds": 86400}


class _NullEncoder(Encoder):
    """Never called: the plan tests only read and claim rows."""

    async def validate(self, output: OutputSettings, *, has_chapters: bool) -> None:
        raise AssertionError("not used")

    async def render(self, spec: RenderSpec) -> RenderResult:
        raise AssertionError("not used")


def _seed_render_jobs(factory: sessionmaker[Session], *, analyze: bool) -> list[int]:
    """A long render_job history with a handful of live jobs; return project ids.

    13 projects with live render and archive schedules, 400 finished jobs each
    across all four kinds (a year of daily scheduled and archive renders plus
    manual renders and exports), and only five in flight: the shape the
    partial index is for.
    """
    base = datetime(2025, 1, 1, 3, 0)
    kinds = ("scheduled", "archive", "manual", "export")
    with session_scope(factory) as session:
        camera = Camera(name="cam")
        session.add(camera)
        session.flush()
        projects = [
            Project(
                camera_id=camera.id,
                name=f"p{n}",
                render_schedule=_LIVE_SCHEDULE,
                archive_schedule=_LIVE_SCHEDULE,
            )
            for n in range(_JOB_PROJECTS)
        ]
        session.add_all(projects)
        session.flush()
        project_ids = [project.id for project in projects]
        rows: list[dict[str, Any]] = []
        for project_id in project_ids:
            for n in range(_JOBS_PER_PROJECT):
                rows.append(
                    {
                        "project_id": project_id,
                        "encoder_engine": "ffmpeg",
                        "kind": kinds[n % 4],
                        "status": "failed" if n % 17 == 0 else "done",
                        "output_settings": {"frame_ids": list(range(50))}
                        if n % 4 == 3
                        else None,
                        "created_at": base + timedelta(hours=n * 22),
                    }
                )
        for project_id, kind, status in (
            (project_ids[0], "scheduled", "pending"),
            (project_ids[2], "archive", "pending"),
            (project_ids[4], "manual", "pending"),
            (project_ids[6], "export", "encoding"),
            (project_ids[8], "scheduled", "encoding"),
        ):
            rows.append(
                {
                    "project_id": project_id,
                    "encoder_engine": "ffmpeg",
                    "kind": kind,
                    "status": status,
                    "created_at": base + timedelta(days=400),
                }
            )
        session.execute(insert(RenderJob), rows)
    if analyze:
        _analyze(factory)
    return project_ids


def _queue(factory: sessionmaker[Session], settings: Settings) -> RenderQueue:
    return RenderQueue(settings, factory, encoder=_NullEncoder())


def _render_job_selects(captured: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    return [
        (" ".join(sql.split()), params)
        for sql, params in captured
        if sql.lstrip().upper().startswith("SELECT") and "render_job" in sql
    ]


class TestRenderJobPlans:
    @STATS_REGIMES
    def test_claim_walks_the_partial_index_in_id_order(
        self,
        migrated_factory: sessionmaker[Session],
        settings_no_autostart: Settings,
        analyze: bool,
    ) -> None:
        """The oldest-pending claim reads the live jobs only, with no sort.

        As of SQLite 3.53: ``SCAN render_job USING INDEX ix_render_job_active``
        in both stats regimes. The index is ordered by id, so ``ORDER BY id``
        needs no temp B-tree.
        """
        _seed_render_jobs(migrated_factory, analyze=analyze)
        queue = _queue(migrated_factory, settings_no_autostart)

        with _captured_sql(_engine(migrated_factory)) as captured:
            claimed = queue._claim_next_pending()  # noqa: SLF001

        assert claimed is not None
        (sql, params), *rest = _render_job_selects(captured)
        assert rest == []
        plan = _plan(migrated_factory, sql, params)
        _assert_uses_index(plan, "ix_render_job_active", "render_job")
        assert not any("TEMP B-TREE" in step for step in plan), plan

    def test_claim_selects_only_id_and_repeats_the_predicate_as_literals(
        self,
        migrated_factory: sessionmaker[Session],
        settings_no_autostart: Settings,
    ) -> None:
        """A bound ``?`` status list would silently stop the index being used.

        Selecting only ``id`` also keeps the claim from loading an export's
        ``output_settings`` frame-id list (X-D1).
        """
        _seed_render_jobs(migrated_factory, analyze=False)
        queue = _queue(migrated_factory, settings_no_autostart)

        with _captured_sql(_engine(migrated_factory)) as captured:
            queue._claim_next_pending()  # noqa: SLF001

        [(sql, _params)] = _render_job_selects(captured)
        assert sql.startswith("SELECT render_job.id FROM render_job WHERE"), sql
        assert f"render_job.{ACTIVE_JOB_INDEX_WHERE}" in sql, sql
        assert "output_settings" not in sql

    @STATS_REGIMES
    def test_watchdog_reads_use_the_partial_index(
        self,
        migrated_factory: sessionmaker[Session],
        settings_no_autostart: Settings,
        analyze: bool,
    ) -> None:
        """The activity count and the encoding-row reads skip the history too."""
        _seed_render_jobs(migrated_factory, analyze=analyze)
        queue = _queue(migrated_factory, settings_no_autostart)

        with _captured_sql(_engine(migrated_factory)) as captured:
            pending, encoding_ids = queue._row_activity_sync()  # noqa: SLF001
            encoding_rows = queue._encoding_rows_sync()  # noqa: SLF001

        assert (pending, len(encoding_ids), len(encoding_rows)) == (3, 2, 2)
        selects = _render_job_selects(captured)
        assert len(selects) == 3
        for sql, params in selects:
            _assert_uses_index(
                _plan(migrated_factory, sql, params),
                "ix_render_job_active",
                "render_job",
            )

    @STATS_REGIMES
    def test_scheduler_pass_is_one_grouped_read_over_both_indexes(
        self,
        migrated_factory: sessionmaker[Session],
        settings_no_autostart: Settings,
        analyze: bool,
    ) -> None:
        """One render_job read per pass, never a table row of the history.

        As of SQLite 3.53 (both regimes): the newest-per-pair arm is ``SEARCH
        render_job USING COVERING INDEX ix_render_job_project_kind_created
        (project_id=? AND kind=?)`` and the in-flight arm is ``SCAN render_job
        USING INDEX ix_render_job_active``; the outer ``GROUP BY`` sorts only
        the arms' few rows.
        """
        _seed_render_jobs(migrated_factory, analyze=analyze)
        queue = _queue(migrated_factory, settings_no_autostart)
        scheduler = RenderScheduler(settings_no_autostart, migrated_factory, queue)

        with _captured_sql(_engine(migrated_factory)) as captured:
            created = scheduler._enqueue_due_jobs(  # noqa: SLF001
                datetime(2026, 6, 1, tzinfo=UTC)
            )

        # Every pair is overdue except the three scheduled/archive pairs with a
        # job in flight; the in-flight manual and export jobs do not count.
        assert len(created) == 2 * _JOB_PROJECTS - 3
        [(sql, params)] = _render_job_selects(captured)
        plan = _plan(migrated_factory, sql, params)
        assert any(
            "COVERING INDEX ix_render_job_project_kind_created" in step for step in plan
        ), plan
        assert any("ix_render_job_active" in step for step in plan), plan
        assert not any(
            re.match(r"^SCAN (TABLE )?render_job$", step.strip()) for step in plan
        ), plan
        assert f"render_job.{ACTIVE_JOB_INDEX_WHERE}" in sql, sql
        assert "output_settings" not in sql

    def test_scheduler_reads_only_the_project_columns_it_needs(
        self,
        migrated_factory: sessionmaker[Session],
        settings_no_autostart: Settings,
    ) -> None:
        _seed_render_jobs(migrated_factory, analyze=False)
        queue = _queue(migrated_factory, settings_no_autostart)
        scheduler = RenderScheduler(settings_no_autostart, migrated_factory, queue)

        with _captured_sql(_engine(migrated_factory)) as captured:
            scheduler._enqueue_due_jobs(datetime(2026, 6, 1, tzinfo=UTC))  # noqa: SLF001

        [project_sql] = [
            " ".join(sql.split())
            for sql, _params in captured
            if sql.lstrip().upper().startswith("SELECT") and "FROM project" in sql
        ]
        assert project_sql.startswith(
            "SELECT project.id, project.render_schedule, project.archive_schedule "
            "FROM project"
        ), project_sql
