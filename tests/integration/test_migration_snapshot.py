"""Pre-migration snapshots (#67 R6).

``apply_migrations`` writes a verified, WAL-consistent copy of an existing
database that is behind head before it upgrades it, keeps the newest three, and
aborts startup (``MigrationAbortedError``) instead of migrating when the copy
cannot be made.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from timelapse_manager.config.settings import DatabaseSettings, PathsSettings, Settings
from timelapse_manager.db import backup as backup_mod
from timelapse_manager.db.backup import (
    SNAPSHOT_KEEP,
    BackupError,
    backup_file_to_file,
    list_snapshots,
    prune_snapshots,
    read_revision,
    take_snapshot,
)
from timelapse_manager.db.migrate import MigrationAbortedError, apply_migrations


def _settings(db_path: Path) -> Settings:
    return Settings(
        database=DatabaseSettings(url=f"sqlite:///{db_path}"),
        paths=PathsSettings(data_dir=db_path.parent),
    )


def _previous_head(cfg: Config) -> str:
    script = ScriptDirectory.from_config(cfg)
    head = script.get_revision(script.get_current_head())
    assert head is not None and isinstance(head.down_revision, str)
    return head.down_revision


def _head(cfg: Config) -> str:
    head = ScriptDirectory.from_config(cfg).get_current_head()
    assert head is not None
    return head


def _row_counts(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {
            t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables
        }
    finally:
        conn.close()


def _integrity_ok(db_path: Path) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    finally:
        conn.close()


@pytest.fixture()
def _restore_logging() -> Iterator[None]:
    """Alembic's ``fileConfig`` resets the root logger; put it back."""
    import logging

    root = logging.getLogger()
    level, handlers = root.level, root.handlers[:]
    yield
    root.setLevel(level)
    root.handlers[:] = handlers


@pytest.fixture()
def behind_head(
    alembic_cfg: Config, tmp_db_path: Path, _restore_logging: None
) -> tuple[Path, str]:
    """A database at the revision before head, with a camera row in it."""
    previous = _previous_head(alembic_cfg)
    command.upgrade(alembic_cfg, previous)
    conn = sqlite3.connect(tmp_db_path)
    try:
        conn.execute("INSERT INTO camera (name) VALUES ('snap-cam')")
        conn.commit()
    finally:
        conn.close()
    return tmp_db_path, previous


class TestSnapshotOnUpgrade:
    def test_upgrade_from_previous_head_writes_one_verified_snapshot(
        self, behind_head: tuple[Path, str], alembic_cfg: Config
    ) -> None:
        db_path, previous = behind_head
        before = _row_counts(db_path)
        assert before["camera"] == 1

        apply_migrations(_settings(db_path))

        snaps = list_snapshots(db_path)
        assert len(snaps) == 1
        snap = snaps[0]
        assert snap.revision == previous
        assert snap.path.name.startswith(f"{db_path.name}.pre-{previous}-")
        assert _integrity_ok(snap.path)
        assert read_revision(snap.path) == previous
        assert _row_counts(snap.path) == before
        assert read_revision(db_path) == _head(alembic_cfg)
        # Self-contained and private: no sidecars, owner-only.
        assert not Path(f"{snap.path}-wal").exists()
        assert (snap.path.stat().st_mode & 0o777) == 0o600

    def test_database_at_head_is_not_copied(
        self, behind_head: tuple[Path, str]
    ) -> None:
        db_path, _ = behind_head
        apply_migrations(_settings(db_path))
        apply_migrations(_settings(db_path))
        assert len(list_snapshots(db_path)) == 1

    def test_new_database_is_not_copied(
        self, tmp_db_path: Path, _restore_logging: None
    ) -> None:
        apply_migrations(_settings(tmp_db_path))
        assert tmp_db_path.is_file()
        assert list_snapshots(tmp_db_path) == []

    def test_unmigrated_database_file_is_not_copied(
        self, tmp_db_path: Path, _restore_logging: None
    ) -> None:
        sqlite3.connect(tmp_db_path).close()  # empty file
        conn = sqlite3.connect(tmp_db_path)
        conn.execute("CREATE TABLE stray (id INTEGER)")
        conn.close()
        apply_migrations(_settings(tmp_db_path))
        assert list_snapshots(tmp_db_path) == []

    def test_wal_pages_are_in_the_snapshot(self, behind_head: tuple[Path, str]) -> None:
        """A row only in the ``-wal`` file (not checkpointed) is copied."""
        db_path, previous = behind_head
        holder = sqlite3.connect(db_path)
        try:
            holder.execute("PRAGMA journal_mode=WAL")
            holder.execute("PRAGMA wal_autocheckpoint=0")
            holder.execute("CREATE TABLE wal_probe (v TEXT)")
            holder.execute("INSERT INTO wal_probe VALUES ('in-wal')")
            holder.commit()
            assert Path(f"{db_path}-wal").stat().st_size > 0
            snapshot = take_snapshot(db_path, previous)
        finally:
            holder.close()
        conn = sqlite3.connect(snapshot)
        try:
            assert conn.execute("SELECT v FROM wal_probe").fetchall() == [("in-wal",)]
        finally:
            conn.close()


class TestPruning:
    @staticmethod
    def _fake(db_path: Path, revision: str, when: datetime) -> Path:
        path = db_path.with_name(
            f"{db_path.name}.pre-{revision}-{when.strftime('%Y%m%dT%H%M%S')}"
        )
        path.write_bytes(b"x")
        return path

    def test_keeps_the_newest_three(self, tmp_db_path: Path) -> None:
        start = datetime(2026, 10, 1, tzinfo=UTC)
        paths = [
            self._fake(tmp_db_path, f"0{n}_rev", start + timedelta(days=n))
            for n in range(5)
        ]
        removed = prune_snapshots(tmp_db_path, "99_other")
        assert set(removed) == set(paths[:2])
        assert [s.path for s in list_snapshots(tmp_db_path)] == paths[2:]
        assert SNAPSHOT_KEEP == 3

    def test_retries_at_one_revision_keep_the_first_copy(
        self, tmp_db_path: Path
    ) -> None:
        """A crash-looping migration must not prune the pre-first-attempt copy."""
        start = datetime(2026, 10, 1, tzinfo=UTC)
        first = self._fake(tmp_db_path, "032_x", start)
        retries = [
            self._fake(tmp_db_path, "032_x", start + timedelta(seconds=5 * n))
            for n in range(1, 5)
        ]
        prune_snapshots(tmp_db_path, "032_x")
        kept = [s.path for s in list_snapshots(tmp_db_path)]
        assert kept == [first, *retries[-3:]]

    def test_pruning_after_snapshot_keeps_three(
        self, behind_head: tuple[Path, str]
    ) -> None:
        db_path, previous = behind_head
        start = datetime(2026, 1, 1, tzinfo=UTC)
        for n in range(4):
            self._fake(db_path, f"00{n}_old", start + timedelta(days=n))
        made = take_snapshot(db_path, previous)
        names = [s.path for s in list_snapshots(db_path)]
        assert len(names) == 3
        assert names[-1] == made

    def test_ignores_unrelated_and_symlinked_files(self, tmp_db_path: Path) -> None:
        other = tmp_db_path.with_name("other.db.pre-01_x-20260101T000000")
        other.write_bytes(b"x")
        target = tmp_db_path.with_name("victim")
        target.write_bytes(b"keep")
        link = tmp_db_path.with_name(f"{tmp_db_path.name}.pre-01_x-20200101T000000")
        link.symlink_to(target)
        start = datetime(2026, 10, 1, tzinfo=UTC)
        for n in range(4):
            self._fake(tmp_db_path, "02_y", start + timedelta(days=n))
        prune_snapshots(tmp_db_path, "02_y")
        assert other.exists() and link.is_symlink() and target.read_bytes() == b"keep"


class TestAbort:
    def test_low_free_space_aborts_without_migrating(
        self, behind_head: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path, previous = behind_head
        real = shutil.disk_usage

        def tiny(path: Any) -> Any:
            usage = real(path)
            return type(usage)(usage.total, usage.used, 1024)

        monkeypatch.setattr(backup_mod.shutil, "disk_usage", tiny)
        with pytest.raises(MigrationAbortedError, match="not enough free space"):
            apply_migrations(_settings(db_path))
        assert read_revision(db_path) == previous
        assert list_snapshots(db_path) == []

    def test_failed_copy_aborts_and_leaves_no_partial_file(
        self, behind_head: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path, previous = behind_head

        def broken(_dst: sqlite3.Connection, destination: Path) -> None:
            raise BackupError(f"copy at {destination} failed its integrity check")

        monkeypatch.setattr(backup_mod, "_finish_copy", broken)
        with pytest.raises(MigrationAbortedError, match="integrity check"):
            apply_migrations(_settings(db_path))
        assert read_revision(db_path) == previous
        assert (
            sorted(p.name for p in db_path.parent.iterdir() if "pre-" in p.name) == []
        )

    def test_failing_migration_leaves_original_and_valid_snapshot(
        self, behind_head: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path, previous = behind_head
        before = _row_counts(db_path)

        def failing_upgrade(_cfg: Config, _target: str) -> None:
            raise RuntimeError("migration exploded")

        monkeypatch.setattr(command, "upgrade", failing_upgrade)
        with pytest.raises(RuntimeError, match="migration exploded"):
            apply_migrations(_settings(db_path))

        assert read_revision(db_path) == previous
        assert _row_counts(db_path) == before
        (snap,) = list_snapshots(db_path)
        assert _integrity_ok(snap.path)
        assert _row_counts(snap.path) == before

    def test_unreadable_database_aborts(
        self, tmp_db_path: Path, _restore_logging: None
    ) -> None:
        tmp_db_path.write_bytes(b"this is not a sqlite database at all" * 200)
        with pytest.raises(MigrationAbortedError, match="could not read the revision"):
            apply_migrations(_settings(tmp_db_path))


class TestBackupUnderLoad:
    def test_backup_during_concurrent_writes_passes_integrity_check(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "live.db"
        conn = sqlite3.connect(source)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, payload BLOB)")
        conn.commit()
        conn.close()

        stop = threading.Event()
        written = 0

        def writer() -> None:
            nonlocal written
            w = sqlite3.connect(source, timeout=15)
            try:
                while not stop.is_set():
                    w.execute("INSERT INTO t (payload) VALUES (?)", (os.urandom(512),))
                    w.commit()
                    written += 1
            finally:
                w.close()

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            copies = []
            for n in range(5):
                target = tmp_path / f"copy-{n}.db"
                backup_file_to_file(source, target)
                copies.append(target)
        finally:
            stop.set()
            thread.join()

        assert written > 0
        for copy in copies:
            assert _integrity_ok(copy)
        assert _row_counts(copies[-1])["t"] <= written

    def test_backup_refuses_to_overwrite(self, tmp_path: Path) -> None:
        source = tmp_path / "s.db"
        sqlite3.connect(source).close()
        existing = tmp_path / "exists.db"
        existing.write_bytes(b"keep")
        with pytest.raises(BackupError, match="already exists"):
            backup_file_to_file(source, existing)
        assert existing.read_bytes() == b"keep"
