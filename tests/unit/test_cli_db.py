"""``db backup`` / ``db restore`` and ``migrate`` failure handling (#67 R6).

Backups go through SQLite's online backup API (WAL-consistent, never a file
copy). ``db restore`` refuses while the service answers on loopback, checks the
source, and keeps the replaced database (with its ``-wal``) beside it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from timelapse_manager.cli import main


def _config(tmp_path: Path) -> tuple[Path, Path]:
    """A config whose SQLite DB is ``tmp_path/state/timelapse.db``."""
    state = tmp_path / "state"
    state.mkdir()
    database = state / "timelapse.db"
    config = tmp_path / "config.yml"
    config.write_text(
        yaml.dump(
            {
                "paths": {"data_dir": str(state)},
                "database": {"url": f"sqlite:///{database}"},
            }
        ),
        encoding="utf-8",
    )
    return config, database


def _run(*args: str) -> int:
    with pytest.raises(SystemExit) as excinfo:
        main(list(args))
    code = excinfo.value.code
    return int(code) if code is not None else 0


def _make_db(path: Path, marker: str, *, revision: str | None = "033_x") -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE marker (v TEXT)")
        conn.execute("INSERT INTO marker VALUES (?)", (marker,))
        if revision is not None:
            conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
            conn.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
        conn.commit()
    finally:
        conn.close()


def _marker(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [row[0] for row in conn.execute("SELECT v FROM marker ORDER BY v")]
    finally:
        conn.close()


@pytest.fixture()
def service_down(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Loopback ``/healthz`` refuses the connection; records the probed URLs."""
    probed: list[str] = []

    def refused(url: str, **_kwargs: Any) -> httpx.Response:
        probed.append(url)
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("timelapse_manager.cli.httpx.get", refused)
    return probed


class TestBackup:
    def test_writes_a_verified_private_copy(self, tmp_path: Path) -> None:
        config, database = _config(tmp_path)
        _make_db(database, "live")
        target = tmp_path / "out.db"

        assert _run("--config", str(config), "db", "backup", str(target)) == 0

        assert _marker(target) == ["live"]
        assert (target.stat().st_mode & 0o777) == 0o600
        assert not Path(f"{target}-wal").exists()

    def test_includes_uncheckpointed_wal_pages(self, tmp_path: Path) -> None:
        config, database = _config(tmp_path)
        _make_db(database, "live")
        holder = sqlite3.connect(database)
        try:
            holder.execute("PRAGMA wal_autocheckpoint=0")
            holder.execute("INSERT INTO marker VALUES ('only-in-wal')")
            holder.commit()
            target = tmp_path / "out.db"
            assert _run("--config", str(config), "db", "backup", str(target)) == 0
        finally:
            holder.close()
        assert _marker(target) == ["live", "only-in-wal"]

    def test_refuses_to_overwrite(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config, database = _config(tmp_path)
        _make_db(database, "live")
        target = tmp_path / "out.db"
        target.write_bytes(b"keep")
        assert _run("--config", str(config), "db", "backup", str(target)) == 1
        assert target.read_bytes() == b"keep"
        assert "already exists" in capsys.readouterr().err

    def test_missing_database_is_an_error_and_creates_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config, database = _config(tmp_path)
        target = tmp_path / "out.db"
        assert _run("--config", str(config), "db", "backup", str(target)) == 1
        assert not database.exists() and not target.exists()
        assert "does not exist" in capsys.readouterr().err


class TestRestore:
    @pytest.mark.parametrize(
        "answer",
        [
            httpx.Response(200, json={"db_status": "ok"}),
            httpx.Response(307, headers={"location": "https://x/"}),
            httpx.Response(503),
            httpx.ReadTimeout("slow"),
        ],
        ids=["ok", "redirect", "error-status", "timeout"],
    )
    def test_refuses_while_the_service_answers(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        answer: httpx.Response | Exception,
    ) -> None:
        config, database = _config(tmp_path)
        _make_db(database, "live")
        source = tmp_path / "backup.db"
        _make_db(source, "old")
        seen: list[str] = []

        def probe(url: str, **_kwargs: Any) -> httpx.Response:
            seen.append(url)
            if isinstance(answer, Exception):
                raise answer
            return answer

        monkeypatch.setattr("timelapse_manager.cli.httpx.get", probe)

        assert _run("--config", str(config), "db", "restore", str(source)) == 1
        assert seen == ["http://127.0.0.1:8080/healthz"]
        assert _marker(database) == ["live"]
        assert list(database.parent.glob("*.replaced-*")) == []
        assert "stop it first" in capsys.readouterr().err

    def test_replaces_the_database_and_keeps_the_old_one(
        self, tmp_path: Path, service_down: list[str]
    ) -> None:
        config, database = _config(tmp_path)
        _make_db(database, "live")
        # Leave committed data only in the live -wal, plus a stale -shm and
        # -journal: the set-aside copy must keep the WAL, and the restored file
        # must not inherit any of them.
        # Copy the files while a connection holds them (a crash image): the
        # main file lacks the last row, which lives only in the -wal.
        holder = sqlite3.connect(database)
        holder.execute("PRAGMA wal_autocheckpoint=0")
        holder.execute("INSERT INTO marker VALUES ('live-wal')")
        holder.commit()
        main_bytes = database.read_bytes()
        wal = Path(f"{database}-wal").read_bytes()
        shm = Path(f"{database}-shm").read_bytes()
        holder.close()
        database.write_bytes(main_bytes)
        Path(f"{database}-wal").write_bytes(wal)
        Path(f"{database}-shm").write_bytes(shm)
        Path(f"{database}-journal").write_bytes(b"stale journal")
        mode_before = database.stat().st_mode & 0o777
        source = tmp_path / "backup.db"
        _make_db(source, "old")

        assert _run("--config", str(config), "db", "restore", str(source)) == 0

        assert service_down == ["http://127.0.0.1:8080/healthz"]
        assert _marker(database) == ["old"]
        assert (database.stat().st_mode & 0o777) == mode_before
        (replaced,) = database.parent.glob("timelapse.db.replaced-*[0-9]")
        # Checked before opening it: SQLite deletes a journal that is not hot.
        assert Path(f"{replaced}-journal").read_bytes() == b"stale journal"
        assert _marker(replaced) == ["live", "live-wal"]
        assert not Path(f"{database}-shm").exists()
        assert not Path(f"{database}-journal").exists()
        assert _marker(source) == ["old"]  # the source is left as it was

    def test_restores_into_an_empty_state_dir(
        self, tmp_path: Path, service_down: list[str]
    ) -> None:
        config, database = _config(tmp_path)
        source = tmp_path / "backup.db"
        _make_db(source, "old")
        assert _run("--config", str(config), "db", "restore", str(source)) == 0
        assert _marker(database) == ["old"]

    def test_refuses_a_corrupt_source(
        self,
        tmp_path: Path,
        service_down: list[str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        config, database = _config(tmp_path)
        _make_db(database, "live")
        source = tmp_path / "backup.db"
        source.write_bytes(b"not a database" * 500)
        assert _run("--config", str(config), "db", "restore", str(source)) == 1
        assert _marker(database) == ["live"]
        assert "restore failed" in capsys.readouterr().err

    def test_refuses_a_database_without_a_revision(
        self,
        tmp_path: Path,
        service_down: list[str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        config, database = _config(tmp_path)
        _make_db(database, "live")
        source = tmp_path / "other.db"
        _make_db(source, "foreign", revision=None)
        assert _run("--config", str(config), "db", "restore", str(source)) == 1
        assert _marker(database) == ["live"]
        assert "not a Timelapse Manager database" in capsys.readouterr().err

    def test_round_trip_with_backup(
        self, tmp_path: Path, service_down: list[str]
    ) -> None:
        config, database = _config(tmp_path)
        _make_db(database, "v1")
        saved = tmp_path / "saved.db"
        assert _run("--config", str(config), "db", "backup", str(saved)) == 0
        conn = sqlite3.connect(database)
        conn.execute("DELETE FROM marker")
        conn.commit()
        conn.close()
        assert _run("--config", str(config), "db", "restore", str(saved)) == 0
        assert _marker(database) == ["v1"]


def test_migrate_reports_an_aborted_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from timelapse_manager.db import migrate

    config, _ = _config(tmp_path)

    def abort(_settings: Any) -> None:
        raise migrate.MigrationAbortedError("database migration aborted: no space")

    monkeypatch.setattr(migrate, "apply_migrations", abort)
    assert _run("--config", str(config), "migrate") == 1
    assert "migration aborted: no space" in capsys.readouterr().err
