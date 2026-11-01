"""Chunked suffix renumber (#145) and order repair after edits (#144).

* **Chunked renumber.** A suffix larger than ``RENUMBER_CHUNK_ROWS`` is renumbered
  in chunks, each its own transaction, with the SQLite write lock free between
  them. The tests drive it with a tiny chunk size and look at the database at every
  chunk boundary: indexes stay unique, no row goes missing, and the order is always
  "prefix, rows already in final position, the rest in their original order".
* **Repair after edits.** A single upload, a timestamp edit and a bulk offset put
  ``sequence_index`` back in time order once they commit.

Refs #144, #145.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.frames.test_import import _make_plain_jpeg
from tests.unit.test_frame_import_resequence import (
    _BASE,
    _assert_invariant,
    _engine,
    _expected_order,
    _import,
    _rows,
    _seed_ordered_frames,
)
from timelapse_manager.cameras.base import CapturedFrame
from timelapse_manager.capture.frame_writer import FrameWriter
from timelapse_manager.db.models import Frame
from timelapse_manager.db.session import session_scope
from timelapse_manager.storage import frames as frame_service
from timelapse_manager.storage.paths import frames_root as get_frames_root

_ACTOR = 1


@pytest.fixture()
def project(migrated_factory: sessionmaker[Session], tmp_path: Path) -> dict[str, Any]:
    from tests.integration.frames.test_import import _make_settings, _seed_project

    ctx = _seed_project(migrated_factory, tmp_path, "fu3-renumber")
    ctx["settings"] = _make_settings(tmp_path)
    return ctx


def _scramble_tail(
    factory: sessionmaker[Session],
    project_id: int,
    first_seq: int,
    *,
    with_nulls: bool = True,
) -> None:
    """Reverse the timestamps of the frames from ``first_seq`` up, and null two."""
    with session_scope(factory) as session:
        tail = list(
            session.scalars(
                select(Frame)
                .where(
                    Frame.project_id == project_id, Frame.sequence_index >= first_seq
                )
                .order_by(Frame.sequence_index)
            )
        )
        stamps = [frame.capture_timestamp for frame in tail]
        for frame, stamp in zip(tail, reversed(stamps), strict=True):
            frame.capture_timestamp = stamp
        if with_nulls:
            tail[3].capture_timestamp = None
            tail[7].capture_timestamp = None


@contextmanager
def _at_chunk_boundaries(
    monkeypatch: pytest.MonkeyPatch, on_boundary: Callable[[int], None]
) -> Iterator[list[int]]:
    """Call ``on_boundary(n)`` each time the renumber pauses between chunks."""
    calls: list[int] = []

    def pause(_seconds: float) -> None:
        calls.append(len(calls))
        on_boundary(calls[-1])

    monkeypatch.setattr(frame_service.time, "sleep", pause)
    yield calls


class TestChunkedRenumber:
    def test_chunked_result_equals_the_single_transaction_result(
        self, migrated_factory: sessionmaker[Session], project: dict[str, Any]
    ) -> None:
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 40, step_hours=1)
        _scramble_tail(migrated_factory, pid, 21)

        renumbered = frame_service.resequence_project_chunked(
            migrated_factory, pid, chunk_rows=3
        )

        rows = _assert_invariant(migrated_factory, pid)
        assert renumbered > 3
        # Dense 1..K, exactly what the in-transaction renumber produces.
        assert sorted(r.sequence_index for r in rows) == list(range(1, 41))

    def test_every_committed_intermediate_state_is_a_coherent_order(
        self,
        migrated_factory: sessionmaker[Session],
        project: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 40, step_hours=1)
        _scramble_tail(migrated_factory, pid, 21)
        before = _rows(migrated_factory, pid)
        original = [r.id for r in sorted(before, key=lambda r: r.sequence_index)]
        target = _expected_order(before)
        # Rows in front of the first difference are the untouched prefix.
        prefix = 0
        while original[prefix] == target[prefix]:
            prefix += 1
        snapshots: list[list[int]] = []

        def snapshot(_n: int) -> None:
            rows = _rows(migrated_factory, pid)
            assert len({r.sequence_index for r in rows}) == len(rows)
            snapshots.append(
                [r.id for r in sorted(rows, key=lambda r: r.sequence_index)]
            )

        with _at_chunk_boundaries(monkeypatch, snapshot) as pauses:
            frame_service.resequence_project_chunked(
                migrated_factory, pid, chunk_rows=3
            )

        assert len(pauses) >= 8  # the run really was chunked
        suffix_original = original[prefix:]
        for order in snapshots:
            assert sorted(order) == sorted(original)  # nothing missing or doubled
            assert order[:prefix] == original[:prefix]
            tail = order[prefix:]
            # "Assigned j rows, the rest in original order" for some j.
            assigned = next(
                j
                for j in range(len(tail) + 1)
                if tail[:j] == target[prefix : prefix + j]
                and tail[j:] == [x for x in suffix_original if x not in tail[:j]]
            )
            assert assigned <= len(tail)
        assert snapshots[0] == original  # the first states change nothing visible
        _assert_invariant(migrated_factory, pid)

    def test_the_write_lock_is_free_between_chunks(
        self,
        migrated_factory: sessionmaker[Session],
        project: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 30, step_hours=1)
        _scramble_tail(migrated_factory, pid, 11)
        db_path = _engine(migrated_factory).url.database
        assert db_path is not None
        writes: list[bool] = []

        def other_writer(_n: int) -> None:
            conn = sqlite3.connect(db_path, timeout=0)  # no waiting: lock or fail
            try:
                conn.execute("UPDATE project SET name = name WHERE id = ?", (pid,))
                conn.commit()
                writes.append(True)
            except sqlite3.OperationalError:
                writes.append(False)
            finally:
                conn.close()

        with _at_chunk_boundaries(monkeypatch, other_writer):
            frame_service.resequence_project_chunked(
                migrated_factory, pid, chunk_rows=4
            )

        assert writes and all(writes)

    def test_a_capture_landing_mid_run_ends_up_last_and_dense(
        self,
        migrated_factory: sessionmaker[Session],
        project: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 30, step_hours=1)
        _scramble_tail(migrated_factory, pid, 11, with_nulls=False)
        writer = FrameWriter(migrated_factory, get_frames_root(project["settings"]))
        captured: list[int] = []

        def capture(n: int) -> None:
            if n in (1, 5):  # once while parking, once while assigning
                written = writer.write(
                    pid,
                    CapturedFrame(
                        image_bytes=_make_plain_jpeg(),
                        width=640,
                        height=480,
                        format="jpeg",
                        captured_at=datetime(2030, 1, 1, tzinfo=UTC)
                        + timedelta(days=n),
                    ),
                )
                captured.append(written.frame_id)

        with _at_chunk_boundaries(monkeypatch, capture):
            frame_service.resequence_project_chunked(
                migrated_factory, pid, chunk_rows=3
            )

        assert len(captured) == 2
        rows = _assert_invariant(migrated_factory, pid)
        ordered = [r.id for r in sorted(rows, key=lambda r: r.sequence_index)]
        assert ordered[-2:] == captured
        # No gap left by the parked range: the indexes are dense again.
        assert sorted(r.sequence_index for r in rows) == list(range(1, 33))

    def test_a_frame_deleted_mid_run_does_not_abort_it(
        self,
        migrated_factory: sessionmaker[Session],
        project: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 30, step_hours=1)
        _scramble_tail(migrated_factory, pid, 11)
        planned = [r.id for r in _rows(migrated_factory, pid)]
        victim = max(planned)

        def delete_one(n: int) -> None:
            if n == 2:
                with session_scope(migrated_factory) as session:
                    session.execute(delete(Frame).where(Frame.id == victim))

        with _at_chunk_boundaries(monkeypatch, delete_one):
            frame_service.resequence_project_chunked(
                migrated_factory, pid, chunk_rows=3
            )

        rows = _rows(migrated_factory, pid)
        assert victim not in {r.id for r in rows}
        assert len({r.sequence_index for r in rows}) == len(rows)

    def test_a_suffix_that_fits_one_chunk_is_a_single_transaction(
        self,
        migrated_factory: sessionmaker[Session],
        project: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 30, step_hours=1)
        _scramble_tail(migrated_factory, pid, 11)

        with _at_chunk_boundaries(monkeypatch, lambda _n: None) as pauses:
            frame_service.resequence_project_chunked(
                migrated_factory, pid, chunk_rows=1000
            )

        assert pauses == []  # no chunk boundary: one transaction
        _assert_invariant(migrated_factory, pid)

    def test_an_ordered_project_is_left_alone(
        self, migrated_factory: sessionmaker[Session], project: dict[str, Any]
    ) -> None:
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 20, step_hours=1)
        before = {r.id: r.sequence_index for r in _rows(migrated_factory, pid)}

        assert frame_service.resequence_project_chunked(migrated_factory, pid) == 0

        assert {r.id: r.sequence_index for r in _rows(migrated_factory, pid)} == before


class TestImportUsesTheChunkedRenumber:
    def test_an_import_with_a_suffix_over_the_chunk_size_is_chunked(
        self,
        migrated_factory: sessionmaker[Session],
        project: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 30, step_hours=1)
        monkeypatch.setattr(frame_service, "RENUMBER_CHUNK_ROWS", 4)
        calls: list[dict[str, Any]] = []
        real = frame_service.resequence_project_chunked

        def spy(*args: Any, **kwargs: Any) -> int:
            calls.append(kwargs)
            return real(*args, **kwargs)

        monkeypatch.setattr(frame_service, "resequence_project_chunked", spy)
        monkeypatch.setattr(frame_service.time, "sleep", lambda _s: None)

        result = _import(
            migrated_factory,
            project["settings"],
            pid,
            [_BASE - timedelta(days=2), _BASE + timedelta(hours=3, minutes=30)],
        )

        assert result.imported_count == 2
        assert len(calls) == 1
        rows = _assert_invariant(migrated_factory, pid)
        assert sorted(r.sequence_index for r in rows) == list(range(1, 33))

    def test_a_small_import_is_not_chunked(
        self,
        migrated_factory: sessionmaker[Session],
        project: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 30, step_hours=1)
        calls: list[int] = []
        monkeypatch.setattr(
            frame_service,
            "resequence_project_chunked",
            lambda *a, **k: calls.append(1) or 0,
        )

        _import(
            migrated_factory,
            project["settings"],
            pid,
            [_BASE + timedelta(hours=28, minutes=30)],
        )

        assert calls == []
        _assert_invariant(migrated_factory, pid)


class TestTargetedOrderCheck:
    """A few moved frames are checked against their neighbours, not a scan (#263)."""

    def _no_full_scan(self, monkeypatch: pytest.MonkeyPatch) -> list[int]:
        scans: list[int] = []
        real = frame_service.earliest_out_of_order_timestamp

        def counting(session: Session, project_id: int) -> datetime | None:
            scans.append(project_id)
            return real(session, project_id)

        monkeypatch.setattr(frame_service, "earliest_out_of_order_timestamp", counting)
        return scans

    def test_an_in_order_edit_skips_the_full_scan(
        self,
        migrated_factory: sessionmaker[Session],
        project: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 20, step_hours=1)
        middle = sorted(_rows(migrated_factory, pid), key=lambda r: r.sequence_index)[9]
        scans = self._no_full_scan(monkeypatch)

        with session_scope(migrated_factory) as session:
            # Nudged within its neighbours: still in order.
            frame_service.edit_capture_timestamp(
                session,
                middle.id,
                middle.capture_timestamp.replace(tzinfo=UTC) + timedelta(minutes=5),
                _ACTOR,
            )

        assert scans == []
        _assert_invariant(migrated_factory, pid)

    @pytest.mark.parametrize("which", ["newest", "oldest", "middle"])
    def test_an_out_of_order_edit_still_triggers_the_restore(
        self,
        migrated_factory: sessionmaker[Session],
        project: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        which: str,
    ) -> None:
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 20, step_hours=1)
        ordered = sorted(_rows(migrated_factory, pid), key=lambda r: r.sequence_index)
        victim, target = {
            "newest": (ordered[-1], _BASE - timedelta(days=1)),
            "oldest": (ordered[0], _BASE + timedelta(days=1)),
            "middle": (ordered[9], _BASE + timedelta(hours=14, minutes=30)),
        }[which]
        scans = self._no_full_scan(monkeypatch)

        with session_scope(migrated_factory) as session:
            frame_service.edit_capture_timestamp(session, victim.id, target, _ACTOR)

        assert scans, "the neighbour check should hand a disorder to the full scan"
        after = _assert_invariant(migrated_factory, pid)
        assert next(r for r in after if r.id == victim.id).sequence_index != (
            victim.sequence_index
        )

    def test_a_null_timestamp_neighbour_is_handled(
        self, migrated_factory: sessionmaker[Session], project: dict[str, Any]
    ) -> None:
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 10, step_hours=1)
        ordered = sorted(_rows(migrated_factory, pid), key=lambda r: r.sequence_index)
        with session_scope(migrated_factory) as session:
            session.get(Frame, ordered[4].id).capture_timestamp = None  # type: ignore[union-attr]

        # A timestamp after a null-timestamp predecessor sorts before it.
        with session_scope(migrated_factory) as session:
            frame_service.edit_capture_timestamp(
                session, ordered[5].id, _BASE + timedelta(hours=5), _ACTOR
            )

        _assert_invariant(migrated_factory, pid)

    def test_a_large_change_takes_the_full_scan(
        self,
        migrated_factory: sessionmaker[Session],
        project: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 20, step_hours=1)
        monkeypatch.setattr(frame_service, "_TARGETED_CHECK_MAX_FRAMES", 2)
        scans = self._no_full_scan(monkeypatch)
        ids = [
            r.id
            for r in sorted(
                _rows(migrated_factory, pid), key=lambda r: r.sequence_index
            )
        ]

        with session_scope(migrated_factory) as session:
            frame_service.offset_timestamps_many(session, ids[:5], 0, _ACTOR)

        assert scans == [pid]

    def test_an_unknown_frame_falls_back_to_the_full_scan(
        self, migrated_factory: sessionmaker[Session], project: dict[str, Any]
    ) -> None:
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 6, step_hours=1)
        _scramble_tail(migrated_factory, pid, 3, with_nulls=False)

        renumbered = frame_service.restore_project_order(migrated_factory, pid, [10**9])

        assert renumbered > 0
        _assert_invariant(migrated_factory, pid)


class TestOrderRepairAfterTimestampChanges:
    """A change that moves a frame in time puts the browse order right (#144)."""

    def test_an_upload_older_than_the_newest_frame_lands_in_order(
        self, migrated_factory: sessionmaker[Session], project: dict[str, Any]
    ) -> None:
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 20, step_hours=1)

        frame = frame_service.upload_frame(
            migrated_factory,
            project["settings"],
            pid,
            _make_plain_jpeg(),
            "jpeg",
            (_BASE + timedelta(hours=4, minutes=30)).replace(tzinfo=UTC),
            _ACTOR,
        )

        rows = _assert_invariant(migrated_factory, pid)
        assert frame.sequence_index == 6  # after the frame at hour 4 (seq 5)
        assert next(r for r in rows if r.id == frame.id).sequence_index == 6
        assert sorted(r.sequence_index for r in rows) == list(range(1, 22))

    def test_a_timestamp_edit_reorders_after_commit(
        self, migrated_factory: sessionmaker[Session], project: dict[str, Any]
    ) -> None:
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 20, step_hours=1)
        rows = _rows(migrated_factory, pid)
        newest = max(rows, key=lambda r: r.sequence_index)

        with session_scope(migrated_factory) as session:
            frame_service.edit_capture_timestamp(
                session, newest.id, _BASE - timedelta(days=1), _ACTOR
            )

        after = _assert_invariant(migrated_factory, pid)
        assert next(r for r in after if r.id == newest.id).sequence_index == 1

    def test_a_bulk_offset_reorders_after_commit(
        self, migrated_factory: sessionmaker[Session], project: dict[str, Any]
    ) -> None:
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 20, step_hours=1)
        ids = [
            r.id
            for r in sorted(
                _rows(migrated_factory, pid), key=lambda r: r.sequence_index
            )
        ]

        with session_scope(migrated_factory) as session:
            result = frame_service.offset_timestamps_many(
                session, ids[:5], 3600 * 10, _ACTOR
            )
        assert len(result.shifted) == 5

        _assert_invariant(migrated_factory, pid)

    def test_a_rolled_back_edit_does_not_trigger_a_repair(
        self,
        migrated_factory: sessionmaker[Session],
        project: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 5, step_hours=1)
        calls: list[int] = []
        monkeypatch.setattr(
            frame_service, "restore_project_order", lambda *a: calls.append(1) or 0
        )
        victim = _rows(migrated_factory, pid)[0].id

        with pytest.raises(RuntimeError), session_scope(migrated_factory) as session:
            frame_service.edit_capture_timestamp(session, victim, _BASE, _ACTOR)
            raise RuntimeError("boom")

        assert calls == []

    def test_a_failing_repair_is_logged_not_raised(
        self,
        migrated_factory: sessionmaker[Session],
        project: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 5, step_hours=1)

        def boom(*_a: Any) -> int:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(frame_service, "restore_project_order", boom)
        victim = _rows(migrated_factory, pid)[-1].id

        with (
            caplog.at_level(logging.ERROR, logger=frame_service.logger.name),
            session_scope(migrated_factory) as session,
        ):
            frame_service.edit_capture_timestamp(
                session, victim, _BASE - timedelta(days=1), _ACTOR
            )

        assert "could not restore the frame order" in caplog.text
        with session_scope(migrated_factory) as session:
            stored = session.get(Frame, victim)
            assert stored is not None
            assert stored.capture_timestamp == _BASE - timedelta(days=1)
