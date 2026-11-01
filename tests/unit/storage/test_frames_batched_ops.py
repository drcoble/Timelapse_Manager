"""Batched bulk frame operations, cached predominant dimensions, SQL gaps (#66 C7).

Pins the WP-66-G contracts (merged into WP-66-F):

* ``*_many`` and ``offset_timestamps_many`` load their rows in chunked ``IN``
  queries -- a constant number of SELECTs per 500 ids -- and keep the exact
  result partition, order and audit events of the per-id loop.
* The export source resolver does the same and keeps the selection order.
* ``predominant_dimensions`` is cached: a repeat call reads no frame rows, a
  call after new frames reads only those, and soft-delete/restore/permanent
  delete invalidate it on commit. A scripted upload of N frames is O(N).
* ``list_capture_gaps`` returns exactly what ``find_capture_gaps`` did over the
  same (capped) timestamp list, including gaps right at the threshold.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, event, insert, select, update
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.frames.test_import import _make_plain_jpeg
from timelapse_manager.config import Settings
from timelapse_manager.db.models import Camera, Event, Frame, Project, RenderJob
from timelapse_manager.db.session import session_scope
from timelapse_manager.render.export import _resolve_job_sources
from timelapse_manager.storage import frames as frame_service

_ACTOR = 1
_BASE = datetime(2026, 1, 1, 12, 0)


def _engine(factory: sessionmaker[Session]) -> Engine:
    bind = factory.kw["bind"]
    assert isinstance(bind, Engine)
    return bind


@contextmanager
def _statements(engine: Engine) -> Iterator[list[str]]:
    """Collect the SQL text of every statement the engine runs."""
    seen: list[str] = []

    def listener(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        seen.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", listener)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", listener)


def _frame_selects(statements: list[str]) -> list[str]:
    return [s for s in statements if re.match(r"SELECT .* FROM frame\b", s)]


def _seed(
    factory: sessionmaker[Session],
    count: int,
    *,
    name: str = "c7",
    size: tuple[int, int] = (640, 480),
) -> tuple[int, list[int]]:
    """A project with ``count`` active frames, one a minute; returns the ids."""
    with session_scope(factory) as session:
        camera = Camera(name=f"{name}-cam", address="127.0.0.1", protocol="vapix")
        session.add(camera)
        session.flush()
        project = Project(camera_id=camera.id, name=name, frame_count=count)
        session.add(project)
        session.flush()
        project_id = project.id
        if count == 0:
            return project_id, []
        session.execute(
            insert(Frame),
            [
                {
                    "project_id": project_id,
                    "sequence_index": i + 1,
                    "capture_timestamp": _BASE + timedelta(minutes=i),
                    "file_path": f"{i:08d}.jpg",
                    "width": size[0],
                    "height": size[1],
                    "capture_status": "captured",
                    "origin": "captured",
                    "lifecycle_state": "active",
                }
                for i in range(count)
            ],
        )
        ids = list(
            session.execute(
                select(Frame.id)
                .where(Frame.project_id == project_id)
                .order_by(Frame.sequence_index)
            ).scalars()
        )
    return project_id, ids


def _events(factory: sessionmaker[Session], project_id: int) -> list[tuple[Any, ...]]:
    with session_scope(factory) as session:
        return [
            (e.message, e.event_metadata, e.actor_user_id, e.level)
            for e in session.execute(
                select(Event)
                .where(Event.scope == "project", Event.scope_id == project_id)
                .order_by(Event.id)
            ).scalars()
        ]


# ---------------------------------------------------------------------------
# Bulk lifecycle and offset: chunked loads, same results
# ---------------------------------------------------------------------------


class TestBatchedBulkOps:
    @pytest.mark.parametrize(("count", "chunks"), [(500, 1), (1200, 3)])
    def test_soft_delete_many_selects_once_per_chunk(
        self, migrated_factory: sessionmaker[Session], count: int, chunks: int
    ) -> None:
        pid, ids = _seed(migrated_factory, count)

        with (
            _statements(_engine(migrated_factory)) as seen,
            session_scope(migrated_factory) as session,
        ):
            result = frame_service.soft_delete_many(session, ids, _ACTOR)

        assert result.succeeded == ids and result.failed == []
        assert len(_frame_selects(seen)) == chunks, _frame_selects(seen)
        with session_scope(migrated_factory) as session:
            states = set(
                session.execute(
                    select(Frame.lifecycle_state).where(Frame.project_id == pid)
                ).scalars()
            )
        assert states == {"soft_deleted"}

    def test_partition_order_and_events_match_the_per_id_loop(
        self, migrated_factory: sessionmaker[Session]
    ) -> None:
        """Missing ids fail in place; duplicates act twice, as before."""
        pid, ids = _seed(migrated_factory, 6)
        missing = 9_999_999
        request = [ids[3], missing, ids[0], ids[3], ids[5]]

        with session_scope(migrated_factory) as session:
            result = frame_service.exclude_many(session, request, _ACTOR)

        assert result.succeeded == [ids[3], ids[0], ids[3], ids[5]]
        assert result.failed == [missing]
        assert _events(migrated_factory, pid) == [
            (
                f"frame {fid} excluded from render",
                {"frame_id": fid, "action": "exclude"},
                _ACTOR,
                "info",
            )
            for fid in result.succeeded
        ]

    def test_ids_from_another_project_still_apply(
        self, migrated_factory: sessionmaker[Session]
    ) -> None:
        """The loader has no project filter: same as ``session.get``."""
        _, ids_a = _seed(migrated_factory, 2, name="a")
        _, ids_b = _seed(migrated_factory, 2, name="b")

        with session_scope(migrated_factory) as session:
            result = frame_service.restore_many(session, ids_a + ids_b, _ACTOR)

        assert result.succeeded == ids_a + ids_b

    def test_offset_many_selects_per_chunk_and_partitions(
        self, migrated_factory: sessionmaker[Session]
    ) -> None:
        pid, ids = _seed(migrated_factory, 500)
        with session_scope(migrated_factory) as session:
            session.execute(
                update(Frame).where(Frame.id == ids[7]).values(capture_timestamp=None)
            )
        missing = 9_999_999

        # Measured inside the session: the commit then runs the order repair
        # (#144), whose own reads are not what this test is about.
        with (
            session_scope(migrated_factory) as session,
            _statements(_engine(migrated_factory)) as seen,
        ):
            result = frame_service.offset_timestamps_many(
                session, [*ids, missing], 90, _ACTOR
            )

        # 501 distinct ids: one chunk of 500 and one of 1.
        assert len(_frame_selects(seen)) == 2, _frame_selects(seen)
        assert result.skipped_null == [ids[7]]
        assert result.failed == [missing]
        assert result.shifted == [i for i in ids if i != ids[7]]
        events = _events(migrated_factory, pid)
        assert len(events) == 499
        first = events[0]
        assert first[1]["frame_id"] == ids[0]
        assert first[1]["delta_seconds"] == 90
        assert first[1]["new"] == (_BASE + timedelta(seconds=90)).isoformat()


# ---------------------------------------------------------------------------
# Export source resolution
# ---------------------------------------------------------------------------


class TestExportSources:
    def test_three_thousand_frames_resolve_in_selection_order(
        self, migrated_factory: sessionmaker[Session], settings: Settings
    ) -> None:
        pid, ids = _seed(migrated_factory, 3000)
        _, other_ids = _seed(migrated_factory, 1, name="other")
        selection = list(reversed(ids)) + [other_ids[0], 9_999_999, ids[10]]
        with session_scope(migrated_factory) as session:
            job = RenderJob(
                project_id=pid,
                kind="export",
                status="pending",
                output_settings={"frame_ids": selection},
            )
            session.add(job)
            session.flush()
            job_id = job.id

        with _statements(_engine(migrated_factory)) as seen:
            resolved = _resolve_job_sources(settings, migrated_factory, job_id)

        assert resolved is not None
        _, _, sources = resolved
        # The off-project and unknown ids are dropped; order and the repeated
        # id are kept.
        assert [s.frame_id for s in sources] == list(reversed(ids)) + [ids[10]]
        # 3,002 distinct ids: seven chunks of at most 500.
        assert len(_frame_selects(seen)) == 7, _frame_selects(seen)


# ---------------------------------------------------------------------------
# Predominant dimensions cache
# ---------------------------------------------------------------------------


def _grouped_scans(statements: list[str]) -> list[str]:
    """Full-project dimension scans (no lower id bound)."""
    return [
        s
        for s in statements
        if "GROUP BY frame.width, frame.height" in s and "frame.id >" not in s
    ]


def _incremental_scans(statements: list[str]) -> list[str]:
    return [
        s
        for s in statements
        if "GROUP BY frame.width, frame.height" in s and "frame.id >" in s
    ]


def _dims(factory: sessionmaker[Session], pid: int) -> tuple[int, int] | None:
    with session_scope(factory) as session:
        return frame_service.predominant_dimensions(session, pid)


class TestPredominantDimensionsCache:
    def test_repeat_call_reads_no_frame_rows(
        self, migrated_factory: sessionmaker[Session]
    ) -> None:
        pid, _ = _seed(migrated_factory, 50)
        assert _dims(migrated_factory, pid) == (640, 480)

        with _statements(_engine(migrated_factory)) as seen:
            assert _dims(migrated_factory, pid) == (640, 480)

        assert _grouped_scans(seen) == [] and _incremental_scans(seen) == []

    def test_scripted_uploads_are_linear(
        self, migrated_factory: sessionmaker[Session], settings: Settings
    ) -> None:
        """Each upload folds in only its new frame: one full scan in total."""
        pid, _ = _seed(migrated_factory, 40)
        with _statements(_engine(migrated_factory)) as seen:
            for n in range(25):
                frame_service.upload_frame(
                    migrated_factory,
                    settings,
                    pid,
                    _make_plain_jpeg(1280, 720),
                    "jpeg",
                    datetime(2026, 3, 1, tzinfo=UTC) + timedelta(minutes=n),
                    _ACTOR,
                )
                _dims(migrated_factory, pid)

        assert len(_grouped_scans(seen)) == 1, _grouped_scans(seen)
        assert len(_incremental_scans(seen)) == 24
        # 40 frames at 640x480 still outnumber 25 at 1280x720.
        assert _dims(migrated_factory, pid) == (640, 480)
        for n in range(20):
            frame_service.upload_frame(
                migrated_factory,
                settings,
                pid,
                _make_plain_jpeg(1280, 720),
                "jpeg",
                datetime(2026, 4, 1, tzinfo=UTC) + timedelta(minutes=n),
                _ACTOR,
            )
        assert _dims(migrated_factory, pid) == (1280, 720)

    def test_soft_delete_and_restore_invalidate_on_commit(
        self, migrated_factory: sessionmaker[Session]
    ) -> None:
        pid, small = _seed(migrated_factory, 3, size=(320, 240))
        with session_scope(migrated_factory) as session:
            session.execute(
                insert(Frame),
                [
                    {
                        "project_id": pid,
                        "sequence_index": 100 + i,
                        "capture_timestamp": _BASE + timedelta(days=1, minutes=i),
                        "file_path": f"big{i}.jpg",
                        "width": 1920,
                        "height": 1080,
                        "capture_status": "captured",
                        "origin": "captured",
                        "lifecycle_state": "active",
                    }
                    for i in range(2)
                ],
            )
        assert _dims(migrated_factory, pid) == (320, 240)

        with session_scope(migrated_factory) as session:
            frame_service.soft_delete_many(session, small[:2], _ACTOR)
            # The mutating session sees its own change before commit.
            assert frame_service.predominant_dimensions(session, pid) == (1920, 1080)
        assert _dims(migrated_factory, pid) == (1920, 1080)

        with session_scope(migrated_factory) as session:
            frame_service.restore(session, small[0], _ACTOR)
            frame_service.restore(session, small[1], _ACTOR)
        assert _dims(migrated_factory, pid) == (320, 240)

    def test_rollback_keeps_the_cached_value(
        self, migrated_factory: sessionmaker[Session]
    ) -> None:
        pid, ids = _seed(migrated_factory, 3)
        assert _dims(migrated_factory, pid) == (640, 480)
        session = migrated_factory()
        try:
            frame_service.soft_delete_many(session, ids, _ACTOR)
            session.rollback()
            assert frame_service._DIMS_DIRTY_KEY not in session.info
        finally:
            session.close()
        assert _dims(migrated_factory, pid) == (640, 480)

    def test_permanent_delete_recomputes(
        self, migrated_factory: sessionmaker[Session], settings: Settings
    ) -> None:
        pid, ids = _seed(migrated_factory, 3, size=(800, 600))
        with session_scope(migrated_factory) as session:
            session.execute(
                insert(Frame),
                [
                    {
                        "project_id": pid,
                        "sequence_index": 50 + i,
                        "capture_timestamp": _BASE + timedelta(days=2, minutes=i),
                        "file_path": f"w{i}.jpg",
                        "width": 1024,
                        "height": 768,
                        "capture_status": "captured",
                        "origin": "captured",
                        "lifecycle_state": "active",
                    }
                    for i in range(2)
                ],
            )
        assert _dims(migrated_factory, pid) == (800, 600)
        for fid in ids[:2]:
            with session_scope(migrated_factory) as session:
                frame_service.permanent_delete(
                    session, fid, _ACTOR, confirm=True, settings=settings
                )
        assert _dims(migrated_factory, pid) == (1024, 768)

    def test_caches_are_per_database(
        self, migrated_factory: sessionmaker[Session], tmp_path: Path
    ) -> None:
        """Two databases with the same project id never share an entry."""
        from alembic import command as alembic_command
        from alembic.config import Config

        from timelapse_manager.db.engine import create_db_engine
        from timelapse_manager.db.session import create_session_factory

        pid, _ = _seed(migrated_factory, 4)
        assert _dims(migrated_factory, pid) == (640, 480)

        url = f"sqlite:///{tmp_path / 'second.db'}"
        repo = Path(__file__).resolve().parents[3]
        cfg = Config(str(repo / "alembic.ini"))
        cfg.set_main_option("script_location", str(repo / "alembic"))
        cfg.set_main_option("sqlalchemy.url", url)
        alembic_command.upgrade(cfg, "head")
        engine = create_db_engine(url)
        try:
            other = create_session_factory(engine)
            other_pid, _ = _seed(other, 4, size=(100, 100))
            assert other_pid == pid
            assert _dims(other, other_pid) == (100, 100)
        finally:
            engine.dispose()

    def test_invalidation_during_a_compute_is_not_overwritten(
        self,
        migrated_factory: sessionmaker[Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A histogram computed before an invalidation is never stored."""
        pid, _ = _seed(migrated_factory, 3)
        real = frame_service._dimension_histogram
        engine = _engine(migrated_factory)

        def racing(*args: Any, **kwargs: Any) -> Any:
            histogram = real(*args, **kwargs)
            frame_service.invalidate_predominant_dimensions(engine, pid)
            return histogram

        monkeypatch.setattr(frame_service, "_dimension_histogram", racing)
        _dims(migrated_factory, pid)
        monkeypatch.setattr(frame_service, "_dimension_histogram", real)

        with _statements(engine) as seen:
            _dims(migrated_factory, pid)
        assert len(_grouped_scans(seen)) == 1


# ---------------------------------------------------------------------------
# Gaps in SQL
# ---------------------------------------------------------------------------


def _python_gaps(
    factory: sessionmaker[Session],
    pid: int,
    span_start: datetime,
    span_end: datetime,
    limit: int,
) -> list[frame_service.CaptureGap]:
    """The pre-WP-66-F path: load the capped timestamps, walk them in Python."""
    with session_scope(factory) as session:
        times = list(
            session.execute(
                select(Frame.capture_timestamp)
                .where(
                    Frame.project_id == pid,
                    Frame.capture_timestamp.is_not(None),
                    Frame.lifecycle_state == "active",
                )
                .order_by(Frame.capture_timestamp)
                .limit(limit)
            ).scalars()
        )
    return frame_service.find_capture_gaps(times, span_start, span_end)


class TestSqlGaps:
    def _seed_gappy(self, factory: sessionmaker[Session]) -> int:
        """Five-minute captures; frame-to-frame lapses of 2 h 5 min, 1 day 5 min,
        exactly 10 h and 10 h + 1 us (each lapse adds to the 5-minute step)."""
        pid, _ = _seed(factory, 0, name="gaps")
        times: list[datetime] = []
        t = _BASE
        for block, lapse in enumerate(
            [
                timedelta(hours=2),
                timedelta(days=1),
                timedelta(hours=9, minutes=55),
                timedelta(hours=9, minutes=55, microseconds=1),
                timedelta(minutes=5),
            ]
        ):
            for _ in range(30 + block):
                times.append(t)
                t += timedelta(minutes=5)
            t += lapse
        rows = [
            {
                "project_id": pid,
                "sequence_index": i + 1,
                "capture_timestamp": ts,
                "file_path": f"g{i}.jpg",
                "capture_status": "captured",
                "origin": "captured",
                "lifecycle_state": "soft_deleted" if i % 11 == 0 else "active",
            }
            for i, ts in enumerate(times)
        ]
        rows.append({**rows[0], "sequence_index": 9999, "capture_timestamp": None})
        rows.append({**rows[1], "sequence_index": 9998})  # a duplicate time
        with session_scope(factory) as session:
            session.execute(insert(Frame), rows)
        return pid

    @pytest.mark.parametrize("limit", [5000, 60])
    @pytest.mark.parametrize("span_hours", [250, 24 * 10.5, 400])
    def test_same_gaps_as_the_python_walk(
        self,
        migrated_factory: sessionmaker[Session],
        limit: int,
        span_hours: float,
    ) -> None:
        pid = self._seed_gappy(migrated_factory)
        span_start = _BASE.replace(tzinfo=UTC)
        span_end = span_start + timedelta(hours=span_hours)

        with session_scope(migrated_factory) as session:
            sql = frame_service.list_capture_gaps(
                session, pid, span_start, span_end, limit=limit
            )

        assert sql == _python_gaps(migrated_factory, pid, span_start, span_end, limit)

    def test_threshold_boundary_is_strict(
        self, migrated_factory: sessionmaker[Session]
    ) -> None:
        """A lapse of exactly the threshold is not a gap; one microsecond more is."""
        pid = self._seed_gappy(migrated_factory)
        span_start = _BASE.replace(tzinfo=UTC)
        # 4 % of 250 h = 10 h exactly.
        span_end = span_start + timedelta(hours=250)

        with session_scope(migrated_factory) as session:
            gaps = frame_service.list_capture_gaps(session, pid, span_start, span_end)

        durations = sorted(g.duration for g in gaps)
        assert timedelta(hours=10) not in durations
        assert timedelta(hours=10, microseconds=1) in durations

    def test_bounds_match_the_capped_population(
        self, migrated_factory: sessionmaker[Session]
    ) -> None:
        pid = self._seed_gappy(migrated_factory)
        with session_scope(migrated_factory) as session:
            capped = frame_service.active_frame_time_bounds(session, pid, limit=60)
            full = frame_service.active_frame_time_bounds(session, pid)
        with session_scope(migrated_factory) as session:
            times = list(
                session.execute(
                    select(Frame.capture_timestamp)
                    .where(
                        Frame.project_id == pid,
                        Frame.capture_timestamp.is_not(None),
                        Frame.lifecycle_state == "active",
                    )
                    .order_by(Frame.capture_timestamp)
                ).scalars()
            )
        assert capped is not None and full is not None
        assert (capped.first, capped.last, capped.count) == (
            times[0],
            times[59],
            60,
        )
        assert (full.first, full.last, full.count) == (times[0], times[-1], len(times))

    def test_empty_project_has_no_bounds(
        self, migrated_factory: sessionmaker[Session]
    ) -> None:
        pid, _ = _seed(migrated_factory, 0, name="empty")
        with session_scope(migrated_factory) as session:
            assert frame_service.active_frame_time_bounds(session, pid) is None
            assert (
                frame_service.list_capture_gaps(
                    session, pid, _BASE, _BASE + timedelta(hours=1)
                )
                == []
            )
