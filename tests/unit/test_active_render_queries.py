"""The in-flight render queries use the partial index ``ix_render_job_active`` (#160).

SQLite proves a query may use a partial index only from the *literal* terms of its
WHERE clause. These three reads used to bind their status list as parameters, so
they scanned the whole render history. Each now goes through
``active_job_filter()``; the tests pin the literal predicate in the SQL and the
``EXPLAIN QUERY PLAN`` in both statistics regimes.

Refs #160.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from tests.unit.test_query_plans import (
    STATS_REGIMES,
    _captured_sql,
    _engine,
    _plan,
    _render_job_selects,
    _seed_render_jobs,
)
from timelapse_manager.api.system import _render_rows_without_worker
from timelapse_manager.db.models.render_job import ACTIVE_JOB_INDEX_WHERE
from timelapse_manager.db.session import session_scope
from timelapse_manager.services.renders import _find_queued_duplicate
from timelapse_manager.web.routers.renders import _renders_active

_BARE_SCAN = re.compile(r"^SCAN (TABLE )?render_job$")


def _only_select(captured: list[tuple[str, Any]]) -> tuple[str, Any]:
    [(sql, params)] = _render_job_selects(captured)
    return sql, params


class TestActiveRenderQueryPlans:
    @STATS_REGIMES
    def test_render_activity_count_uses_the_partial_index(
        self, migrated_factory: sessionmaker[Session], analyze: bool
    ) -> None:
        _seed_render_jobs(migrated_factory, analyze=analyze)

        with _captured_sql(_engine(migrated_factory)) as captured:
            counts = _render_rows_without_worker(migrated_factory)

        assert counts["pending_rows"] == 3
        assert counts["encoding_rows"] == 2
        sql, params = _only_select(captured)
        assert f"render_job.{ACTIVE_JOB_INDEX_WHERE}" in sql, sql
        plan = _plan(migrated_factory, sql, params)
        assert any("ix_render_job_active" in step for step in plan), plan
        assert not any(_BARE_SCAN.match(step.strip()) for step in plan), plan

    @STATS_REGIMES
    def test_queued_duplicate_lookup_repeats_the_predicate_and_never_scans(
        self, migrated_factory: sessionmaker[Session], analyze: bool
    ) -> None:
        project_ids = _seed_render_jobs(migrated_factory, analyze=analyze)

        with (
            _captured_sql(_engine(migrated_factory)) as captured,
            session_scope(migrated_factory) as session,
        ):
            duplicate = _find_queued_duplicate(session, project_ids[4], {}, {})

        assert duplicate is not None  # the pending manual job of that project
        sql, params = _only_select(captured)
        assert f"render_job.{ACTIVE_JOB_INDEX_WHERE}" in sql, sql
        plan = _plan(migrated_factory, sql, params)
        assert not any(_BARE_SCAN.match(step.strip()) for step in plan), plan

    @STATS_REGIMES
    def test_renders_active_check_uses_the_partial_index(
        self, migrated_factory: sessionmaker[Session], analyze: bool
    ) -> None:
        _seed_render_jobs(migrated_factory, analyze=analyze)

        with (
            _captured_sql(_engine(migrated_factory)) as captured,
            session_scope(migrated_factory) as session,
        ):
            assert _renders_active(session) is True

        sql, params = _only_select(captured)
        assert f"render_job.{ACTIVE_JOB_INDEX_WHERE}" in sql, sql
        plan = _plan(migrated_factory, sql, params)
        assert any("ix_render_job_active" in step for step in plan), plan
        assert not any(_BARE_SCAN.match(step.strip()) for step in plan), plan
