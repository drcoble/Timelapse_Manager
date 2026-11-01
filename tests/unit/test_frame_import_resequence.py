"""Import: per-file project lock and suffix-only resequence (#66 C2, #67 P2).

Pins the WP-66-F contracts:

* **Invariant.** After any import, ``sequence_index`` order equals
  ``(capture_timestamp, id)`` order across all of the project's rows, with null
  timestamps last -- the order the keyset browser relies on. Every test checks
  it against :func:`_expected_order`, the old full-resequence rule computed in
  Python, so the new suffix implementation is compared with the old behaviour.
* **Suffix only.** Rows earlier than the imported frames keep their indexes, and
  a one-file import into a large project updates a handful of rows.
* **Repair.** An out-of-order prefix (a timestamp edit, a capture with an early
  clock that landed between import files) is still repaired, as the old full
  resequence did.
* **Lock per file.** A capture of the same project is not blocked for the whole
  import, and no SQLite write transaction is open between files.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, event, insert, select, update
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.frames.test_import import (
    _make_exif_jpeg,
    _make_plain_jpeg,
    _make_settings,
    _seed_project,
)
from timelapse_manager.cameras.base import CapturedFrame
from timelapse_manager.capture.frame_writer import FrameWriter
from timelapse_manager.config import Settings
from timelapse_manager.db.models import Event, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.storage import frames as frame_service
from timelapse_manager.storage.paths import frames_root as get_frames_root

_ACTOR = 1
_BASE = datetime(2026, 1, 1, 12, 0)  # naive UTC, as stored


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _engine(factory: sessionmaker[Session]) -> Engine:
    bind = factory.kw["bind"]
    assert isinstance(bind, Engine)
    return bind


def _exif(ts: datetime) -> bytes:
    """A JPEG whose Exif ``DateTimeOriginal`` is ``ts``."""
    return _make_exif_jpeg(ts.strftime("%Y:%m:%d %H:%M:%S"), use_exif_ifd=True)


def _seed_ordered_frames(
    factory: sessionmaker[Session], project_id: int, count: int, *, step_hours: int
) -> None:
    """Insert ``count`` frames at seq ``1..count``, one every ``step_hours``.

    Core inserts (fast); the project's ``frame_count`` is kept in step so the
    writer and the dimension cache see a consistent counter.
    """
    rows = [
        {
            "project_id": project_id,
            "sequence_index": seq,
            "capture_timestamp": _BASE + timedelta(hours=step_hours * (seq - 1)),
            "file_path": f"{seq:08d}.jpg",
            "width": 640,
            "height": 480,
            "file_size_bytes": 1000,
            "capture_status": "captured",
            "origin": "captured",
            "lifecycle_state": "soft_deleted" if seq % 97 == 0 else "active",
        }
        for seq in range(1, count + 1)
    ]
    with session_scope(factory) as session:
        session.execute(insert(Frame), rows)
        session.execute(
            update(Project).where(Project.id == project_id).values(frame_count=count)
        )


def _rows(factory: sessionmaker[Session], project_id: int) -> list[Any]:
    with session_scope(factory) as session:
        return list(
            session.execute(
                select(Frame.id, Frame.sequence_index, Frame.capture_timestamp).where(
                    Frame.project_id == project_id
                )
            ).all()
        )


def _expected_order(rows: list[Any]) -> list[int]:
    """Frame ids in the old full resequence's order: (null last, ts, id)."""
    return [
        r.id
        for r in sorted(
            rows,
            key=lambda r: (
                r.capture_timestamp is None,
                r.capture_timestamp or datetime.min,
                r.id,
            ),
        )
    ]


def _assert_invariant(factory: sessionmaker[Session], project_id: int) -> list[Any]:
    """Sequence order equals the resequence order, and indexes are unique."""
    rows = _rows(factory, project_id)
    by_seq = [r.id for r in sorted(rows, key=lambda r: r.sequence_index)]
    assert by_seq == _expected_order(rows)
    assert len({r.sequence_index for r in rows}) == len(rows)
    return rows


@contextmanager
def _update_rowcounts(engine: Engine) -> Iterator[list[int]]:
    """Collect the rowcount of every UPDATE the engine runs."""
    counts: list[int] = []

    def listener(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("UPDATE FRAME"):
            counts.append(cursor.rowcount)

    event.listen(engine, "after_cursor_execute", listener)
    try:
        yield counts
    finally:
        event.remove(engine, "after_cursor_execute", listener)


def _import(
    factory: sessionmaker[Session],
    settings: Settings,
    project_id: int,
    timestamps: list[datetime],
) -> frame_service.ImportResult:
    files = [(f"f{i}.jpg", _exif(ts)) for i, ts in enumerate(timestamps)]
    return frame_service.import_frames(
        factory,
        settings,
        project_id,
        files,
        fallback_timestamp=datetime(2026, 6, 1, tzinfo=UTC),
        actor_user_id=_ACTOR,
    )


@pytest.fixture()
def project(migrated_factory: sessionmaker[Session], tmp_path: Path) -> dict[str, Any]:
    ctx = _seed_project(migrated_factory, tmp_path, "wp66f")
    ctx["settings"] = _make_settings(tmp_path)
    return ctx


# ---------------------------------------------------------------------------
# Suffix resequence: invariant, prefix untouched, equivalence with the old rule
# ---------------------------------------------------------------------------


class TestSuffixResequence:
    def test_historic_import_into_the_middle_keeps_the_prefix(
        self, migrated_factory: sessionmaker[Session], project: dict[str, Any]
    ) -> None:
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 1000, step_hours=1)
        before = {r.id: r.sequence_index for r in _rows(migrated_factory, pid)}
        # Interleave five photos around frame 600 (hour 599), out of order.
        stamps = [_BASE + timedelta(hours=h, minutes=30) for h in (601, 599, 750, 599)]

        result = _import(migrated_factory, project["settings"], pid, stamps)

        assert result.imported_count == 4
        rows = _assert_invariant(migrated_factory, pid)
        # Old full resequence (dense 1..K in the same order) gives the same map.
        expected = {fid: i for i, fid in enumerate(_expected_order(rows), start=1)}
        assert {r.id: r.sequence_index for r in rows} == expected
        # Every row before the earliest imported time kept its index.
        earliest = min(stamps)
        for r in rows:
            if r.id in before and r.capture_timestamp < earliest:
                assert r.sequence_index == before[r.id]

    def test_importing_the_oldest_frame_equals_the_full_resequence(
        self, migrated_factory: sessionmaker[Session], project: dict[str, Any]
    ) -> None:
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 200, step_hours=1)

        _import(migrated_factory, project["settings"], pid, [_BASE - timedelta(days=3)])

        rows = _assert_invariant(migrated_factory, pid)
        assert sorted(r.sequence_index for r in rows) == list(range(1, 202))
        oldest = min(rows, key=lambda r: r.capture_timestamp)
        assert oldest.sequence_index == 1

    def test_null_timestamps_stay_last(
        self, migrated_factory: sessionmaker[Session], project: dict[str, Any]
    ) -> None:
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 50, step_hours=1)
        with session_scope(migrated_factory) as session:
            session.execute(
                update(Frame)
                .where(Frame.project_id == pid, Frame.sequence_index.in_([10, 20]))
                .values(capture_timestamp=None)
            )

        _import(
            migrated_factory, project["settings"], pid, [_BASE + timedelta(hours=5)]
        )

        rows = _assert_invariant(migrated_factory, pid)
        top = sorted(rows, key=lambda r: r.sequence_index)[-2:]
        assert [r.capture_timestamp for r in top] == [None, None]

    def test_full_resequence_still_returns_every_row(
        self, migrated_factory: sessionmaker[Session], project: dict[str, Any]
    ) -> None:
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 30, step_hours=1)
        with session_scope(migrated_factory) as session:
            session.execute(
                update(Frame)
                .where(Frame.project_id == pid, Frame.sequence_index == 3)
                .values(capture_timestamp=_BASE + timedelta(days=30))
            )
        with session_scope(migrated_factory) as session:
            assert frame_service.resequence_project(session, pid) == 30
        _assert_invariant(migrated_factory, pid)

    def test_sparse_prefix_never_collides(
        self, migrated_factory: sessionmaker[Session], project: dict[str, Any]
    ) -> None:
        """A sparse, high prefix index (a single upload) keeps UNIQUE intact."""
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 20, step_hours=1)
        # Frame at seq 5 jumps to 900 (as a permanent delete + upload would
        # leave it), keeping its early time: the prefix max is now far above
        # the suffix's original indexes.
        with session_scope(migrated_factory) as session:
            session.execute(
                update(Frame)
                .where(Frame.project_id == pid, Frame.sequence_index == 5)
                .values(sequence_index=900)
            )
            frame_service.resequence_project_from(
                session, pid, _BASE + timedelta(hours=2)
            )
        rows = _rows(migrated_factory, pid)
        assert len({r.sequence_index for r in rows}) == len(rows)


# ---------------------------------------------------------------------------
# Repair of a disorder the import did not write
# ---------------------------------------------------------------------------


class TestDisorderRepair:
    def test_earliest_out_of_order_timestamp(
        self, migrated_factory: sessionmaker[Session], project: dict[str, Any]
    ) -> None:
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 40, step_hours=1)
        with session_scope(migrated_factory) as session:
            assert frame_service.earliest_out_of_order_timestamp(session, pid) is None
            # Frame 30 is edited back to before frame 10.
            session.execute(
                update(Frame)
                .where(Frame.project_id == pid, Frame.sequence_index == 30)
                .values(capture_timestamp=_BASE + timedelta(hours=8, minutes=30))
            )
        with session_scope(migrated_factory) as session:
            assert frame_service.earliest_out_of_order_timestamp(
                session, pid
            ) == _BASE + timedelta(hours=8, minutes=30)

    def test_equal_timestamps_out_of_id_order_are_flagged(
        self, migrated_factory: sessionmaker[Session], project: dict[str, Any]
    ) -> None:
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 10, step_hours=1)
        tie = _BASE + timedelta(hours=4)
        with session_scope(migrated_factory) as session:
            # seq 6 gets seq 5's time: equal times, ids in order -> fine.
            session.execute(
                update(Frame)
                .where(Frame.project_id == pid, Frame.sequence_index == 6)
                .values(capture_timestamp=tie)
            )
        with session_scope(migrated_factory) as session:
            assert frame_service.earliest_out_of_order_timestamp(session, pid) is None
            # Swap the two tied rows' indexes: now the higher id comes first.
            session.execute(
                update(Frame)
                .where(Frame.project_id == pid, Frame.sequence_index == 5)
                .values(sequence_index=1000)
            )
            session.execute(
                update(Frame)
                .where(Frame.project_id == pid, Frame.sequence_index == 6)
                .values(sequence_index=5)
            )
            session.execute(
                update(Frame)
                .where(Frame.project_id == pid, Frame.sequence_index == 1000)
                .values(sequence_index=6)
            )
        with session_scope(migrated_factory) as session:
            assert frame_service.earliest_out_of_order_timestamp(session, pid) == tie

    def test_import_repairs_an_earlier_timestamp_edit(
        self, migrated_factory: sessionmaker[Session], project: dict[str, Any]
    ) -> None:
        """The old full resequence healed edits on every import; so does this."""
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 300, step_hours=1)
        with session_scope(migrated_factory) as session:
            edited = session.execute(
                select(Frame.id).where(
                    Frame.project_id == pid, Frame.sequence_index == 250
                )
            ).scalar_one()
            frame_service.edit_capture_timestamp(
                session, edited, datetime(2025, 12, 1, tzinfo=UTC), _ACTOR
            )

        # A recent one-file import (newer than everything).
        _import(
            migrated_factory, project["settings"], pid, [_BASE + timedelta(days=60)]
        )

        _assert_invariant(migrated_factory, pid)

    def test_capture_with_an_early_clock_between_files_is_ordered(
        self,
        migrated_factory: sessionmaker[Session],
        project: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A capture that lands mid-import, older than the imported files."""
        pid = project["project_id"]
        settings = project["settings"]
        _seed_ordered_frames(migrated_factory, pid, 100, step_hours=1)
        writer = FrameWriter(migrated_factory, get_frames_root(settings))
        real = frame_service._import_one_file
        calls = {"n": 0}

        def import_then_capture(*args: Any, **kwargs: Any) -> Any:
            record = real(*args, **kwargs)
            calls["n"] += 1
            if calls["n"] == 1:
                skewed = _BASE + timedelta(hours=50, minutes=10)
                writer.write(
                    pid,
                    CapturedFrame(
                        image_bytes=_make_plain_jpeg(),
                        width=640,
                        height=480,
                        format="jpeg",
                        captured_at=skewed.replace(tzinfo=UTC),
                    ),
                )
            return record

        monkeypatch.setattr(frame_service, "_import_one_file", import_then_capture)
        # Imported files are all newer than the skewed capture.
        stamps = [_BASE + timedelta(hours=h, minutes=5) for h in (70, 80)]
        _import(migrated_factory, settings, pid, stamps)

        _assert_invariant(migrated_factory, pid)


# ---------------------------------------------------------------------------
# Rows written: suffix only
# ---------------------------------------------------------------------------


class TestRowsWritten:
    def test_one_recent_file_into_ten_thousand_frames_updates_a_handful(
        self, migrated_factory: sessionmaker[Session], project: dict[str, Any]
    ) -> None:
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 10_000, step_hours=1)

        with _update_rowcounts(_engine(migrated_factory)) as counts:
            _import(
                migrated_factory,
                project["settings"],
                pid,
                [_BASE + timedelta(days=1000)],
            )

        # Park one row and assign one row (the old code rewrote 2 x 10,001).
        assert sum(counts) <= 4, counts
        _assert_invariant(migrated_factory, pid)

    def test_already_ordered_tail_import_writes_only_its_suffix(
        self, migrated_factory: sessionmaker[Session], project: dict[str, Any]
    ) -> None:
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 2000, step_hours=1)
        # Two days before the newest frame: a 48-row suffix plus 3 imports.
        stamps = [_BASE + timedelta(hours=1999 - 48 + h, minutes=1) for h in (0, 1, 2)]

        with _update_rowcounts(_engine(migrated_factory)) as counts:
            _import(migrated_factory, project["settings"], pid, stamps)

        assert sum(counts) <= 2 * (49 + 3), counts
        _assert_invariant(migrated_factory, pid)


# ---------------------------------------------------------------------------
# Locking: the project lock and the SQLite write lock are per file
# ---------------------------------------------------------------------------


class _PausedImport:
    """Run an import in a thread that pauses after its first file.

    The pause is between two ``_import_one_file`` calls, where no session or
    transaction of the import is open. ``paused`` is set while it waits;
    ``resume()`` lets it finish.
    """

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        run: Callable[[], frame_service.ImportResult],
    ) -> None:
        self.paused = threading.Event()
        self._release = threading.Event()
        self.result: frame_service.ImportResult | None = None
        self.error: BaseException | None = None
        real = frame_service._import_one_file
        calls = {"n": 0}

        def pausing(*args: Any, **kwargs: Any) -> Any:
            record = real(*args, **kwargs)
            calls["n"] += 1
            if calls["n"] == 1:
                self.paused.set()
                assert self._release.wait(timeout=30)
            return record

        monkeypatch.setattr(frame_service, "_import_one_file", pausing)

        def target() -> None:
            try:
                self.result = run()
            except BaseException as exc:  # pragma: no cover - surfaced below
                self.error = exc

        self.thread = threading.Thread(target=target, daemon=True)

    def __enter__(self) -> _PausedImport:
        self.thread.start()
        assert self.paused.wait(timeout=30), "import never reached its pause"
        return self

    def resume(self) -> None:
        self._release.set()

    def __exit__(self, *exc: object) -> None:
        self.resume()
        self.thread.join(timeout=60)
        assert not self.thread.is_alive()
        if self.error is not None:
            raise self.error


class TestPerFileLocking:
    def test_capture_completes_while_the_import_is_between_files(
        self,
        migrated_factory: sessionmaker[Session],
        project: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pid = project["project_id"]
        settings = project["settings"]
        _seed_ordered_frames(migrated_factory, pid, 100, step_hours=1)
        stamps = [_BASE + timedelta(hours=h, minutes=7) for h in range(10, 60)]
        writer = FrameWriter(migrated_factory, get_frames_root(settings))
        captured: dict[str, Any] = {}

        def capture() -> None:
            captured["frame"] = writer.write(
                pid,
                CapturedFrame(
                    image_bytes=_make_plain_jpeg(),
                    width=640,
                    height=480,
                    format="jpeg",
                    captured_at=datetime(2026, 9, 1, tzinfo=UTC),
                ),
            )

        with _PausedImport(
            monkeypatch, lambda: _import(migrated_factory, settings, pid, stamps)
        ) as paused:
            capture_thread = threading.Thread(target=capture, daemon=True)
            capture_thread.start()
            # Before WP-66-F the import held the project lock for the whole
            # batch, so this write waited until the import finished.
            capture_thread.join(timeout=10)
            assert not capture_thread.is_alive(), "capture blocked by the import"
            assert paused.result is None  # the import is still paused
            paused.resume()

        assert paused.result is not None and paused.result.imported_count == 50
        assert "frame" in captured
        rows = _assert_invariant(migrated_factory, pid)
        newest = max(rows, key=lambda r: r.sequence_index)
        assert newest.id == captured["frame"].frame_id

    def test_no_sqlite_write_transaction_is_open_between_files(
        self,
        migrated_factory: sessionmaker[Session],
        project: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        tmp_db_path: Path,
    ) -> None:
        """Another writer gets the database lock at once while the import pauses."""
        pid = project["project_id"]
        settings = project["settings"]
        stamps = [_BASE + timedelta(hours=h) for h in range(5)]

        with _PausedImport(
            monkeypatch, lambda: _import(migrated_factory, settings, pid, stamps)
        ):
            other = sqlite3.connect(tmp_db_path, timeout=0.2)
            try:
                other.execute("BEGIN IMMEDIATE")
                other.execute("ROLLBACK")
            finally:
                other.close()

    def test_audit_events_are_written_once_per_imported_file(
        self, migrated_factory: sessionmaker[Session], project: dict[str, Any]
    ) -> None:
        pid = project["project_id"]
        result = _import(
            migrated_factory,
            project["settings"],
            pid,
            [_BASE + timedelta(hours=h) for h in range(3)],
        )
        with session_scope(migrated_factory) as session:
            events = session.execute(
                select(Event).where(Event.scope == "project", Event.scope_id == pid)
            ).scalars()
            imported = sorted(
                e.event_metadata["frame_id"]
                for e in events
                if e.event_metadata.get("action") == "import"
            )
        assert imported == sorted(r.frame_id for r in result.imported)


# ---------------------------------------------------------------------------
# Atomicity against writers that do not take the project lock
# ---------------------------------------------------------------------------


class TestResequenceTransaction:
    def test_a_timestamp_edit_cannot_slip_in_between_the_reads_and_the_park(
        self,
        migrated_factory: sessionmaker[Session],
        project: dict[str, Any],
        tmp_db_path: Path,
    ) -> None:
        """The resequence reads run inside the write transaction.

        A timestamp edit or offset does not take the project lock. If the
        suffix were read outside the write transaction, an edit committed
        between that read and the park could move a suffix row below
        ``from_ts``: the park would skip it, and the assign would then collide
        on UNIQUE (or strand a row parked above the project). Here the edit is
        attempted from another connection at that exact point; it must find
        the database locked.
        """
        pid = project["project_id"]
        _seed_ordered_frames(migrated_factory, pid, 50, step_hours=1)
        engine = _engine(migrated_factory)
        outcome: dict[str, str] = {}

        def edit_from_another_connection(
            conn: Any,
            cursor: Any,
            statement: str,
            parameters: Any,
            context: Any,
            executemany: bool,
        ) -> None:
            if outcome or not statement.lstrip().startswith("SELECT frame.id"):
                return
            other = sqlite3.connect(tmp_db_path, timeout=0)
            try:
                other.execute(
                    "UPDATE frame SET capture_timestamp = ? "
                    "WHERE project_id = ? AND sequence_index = 40",
                    ("2026-01-01 00:30:00.000000", pid),
                )
                other.commit()
                outcome["edit"] = "committed"
            except sqlite3.OperationalError as exc:
                outcome["edit"] = str(exc)
            finally:
                other.close()

        event.listen(engine, "before_cursor_execute", edit_from_another_connection)
        try:
            _import(
                migrated_factory,
                project["settings"],
                pid,
                [_BASE + timedelta(hours=20, minutes=30)],
            )
        finally:
            event.remove(engine, "before_cursor_execute", edit_from_another_connection)

        assert outcome.get("edit") == "database is locked"
        _assert_invariant(migrated_factory, pid)
