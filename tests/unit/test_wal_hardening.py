"""WAL size hardening and the housekeeping hooks for issue #66 (C4, C9).

* every connection carries ``journal_size_limit`` (64 MiB), and durability is
  untouched (``synchronous`` stays at SQLite's WAL default, FULL; D-66-1);
* the passive checkpoint helper returns SQLite's result row;
* the scheduler's housekeeping pass runs event retention and the checkpoint,
  each isolated so one failing step does not stop the others.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from timelapse_manager.config.settings import (
    EventRetentionSettings,
    MonitoringSettings,
    PathsSettings,
    Settings,
)
from timelapse_manager.db.engine import (
    SQLITE_JOURNAL_SIZE_LIMIT_BYTES,
    checkpoint_wal_passive,
    create_db_engine,
)
from timelapse_manager.db.models import Event
from timelapse_manager.db.session import session_scope
from timelapse_manager.render.scheduler import RenderScheduler


def test_journal_size_limit_set_on_new_connection(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'wal.db'}")
    try:
        with engine.connect() as conn:
            assert (
                conn.exec_driver_sql("PRAGMA journal_size_limit").scalar()
                == SQLITE_JOURNAL_SIZE_LIMIT_BYTES
                == 64 * 1024 * 1024
            )
            # Durability unchanged (D-66-1 keeps FULL): 2 == FULL.
            assert conn.exec_driver_sql("PRAGMA synchronous").scalar() == 2
    finally:
        engine.dispose()


def test_passive_checkpoint_returns_the_result_row(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'wal.db'}")
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE t (x INTEGER)")
            conn.exec_driver_sql("INSERT INTO t VALUES (1)")
        busy, wal_frames, checkpointed = checkpoint_wal_passive(engine) or (-1, 0, 0)
        assert busy == 0
        assert wal_frames >= 1
        assert checkpointed == wal_frames
    finally:
        engine.dispose()


def _scheduler(factory: Any, retention: EventRetentionSettings) -> RenderScheduler:
    # A data_dir beside the test database: the default one is the user's real
    # state directory, and housekeeping sweeps its thumbnail cache (#67 R12).
    data_dir = Path(str(factory.kw["bind"].url.database)).parent / "hk-data"
    settings = Settings(
        paths=PathsSettings(data_dir=data_dir),
        monitoring=MonitoringSettings(event_retention=retention),
    )
    return RenderScheduler(settings, factory, queue=None)  # type: ignore[arg-type]


def _add_old_info(factory: Any, count: int) -> None:
    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=90)
    with session_scope(factory) as session:
        for _ in range(count):
            session.add(Event(scope="system", level="info", message="x", timestamp=old))


def _count(factory: Any) -> int:
    with session_scope(factory) as session:
        return len(session.execute(select(Event.id)).all())


class TestHousekeeping:
    async def test_runs_event_retention_and_checkpoint(
        self, migrated_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _add_old_info(migrated_factory, 4)
        checkpoints: list[object] = []
        monkeypatch.setattr(
            "timelapse_manager.render.scheduler.checkpoint_wal_passive",
            checkpoints.append,
        )

        await _scheduler(
            migrated_factory, EventRetentionSettings(info_days=30)
        ).run_housekeeping(force=True)

        assert _count(migrated_factory) == 1  # the newest row is always kept
        assert checkpoints == [migrated_factory.kw["bind"]]

    async def test_off_by_default_keeps_every_event(
        self, migrated_factory: Any
    ) -> None:
        _add_old_info(migrated_factory, 4)

        await _scheduler(migrated_factory, EventRetentionSettings()).run_housekeeping(
            force=True
        )

        assert _count(migrated_factory) == 4

    async def test_a_failing_step_does_not_stop_the_others(
        self, migrated_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        scheduler = _scheduler(migrated_factory, EventRetentionSettings())

        def fail(name: str) -> Any:
            def step() -> None:
                calls.append(name)
                raise RuntimeError(name)

            return step

        monkeypatch.setattr(scheduler, "_prune_reports", fail("reports"))
        monkeypatch.setattr(scheduler, "_prune_events", fail("events"))
        monkeypatch.setattr(scheduler, "_prune_thumbnails", fail("thumbnails"))
        monkeypatch.setattr(scheduler, "_checkpoint_wal", fail("checkpoint"))

        await scheduler.run_housekeeping(force=True)

        assert calls == ["reports", "events", "thumbnails", "checkpoint"]
