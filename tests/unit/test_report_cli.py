"""Tests for ``timelapse-manager report`` (#8): an offline bundle from the state dir.

The CLI must build the same redacted bundle without a running service, refuse
to run as a non-root user unless the test override is set, and write nothing
into the state directory.
"""

from __future__ import annotations

import json
import os
import sys
from io import StringIO
from pathlib import Path

import pytest
import yaml
from alembic import command as alembic_command
from alembic.config import Config

from tests.reports_support import (
    CONFIG_CAMERA_PASSWORD,
    DB_SECRETS,
    GITEA_TOKEN,
    assert_no_secrets,
    file_secret,
    seed_database_secrets,
)
from timelapse_manager.cli import main
from timelapse_manager.db.engine import create_db_engine
from timelapse_manager.db.session import create_session_factory, session_scope
from timelapse_manager.security.crypto import set_key_provider
from timelapse_manager.security.keystore import KeyFileProvider


@pytest.fixture(autouse=True)
def _machine_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fixed host name, so anonymizing never depends on the runner's (#21)."""
    import socket

    monkeypatch.setattr(socket, "gethostname", lambda: "tlm-test-host-42")
    monkeypatch.setattr(socket, "getfqdn", lambda *_: "tlm-test-host-42.example.org")


def _run(*args: str) -> tuple[int, str, str]:
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = StringIO(), StringIO()
    code = 0
    try:
        main(list(args))
    except SystemExit as exc:
        code = int(exc.code or 0)
    finally:
        out, err = sys.stdout.getvalue(), sys.stderr.getvalue()
        sys.stdout, sys.stderr = old_out, old_err
    return code, out, err


@pytest.fixture()
def state(tmp_path: Path, alembic_cfg: Config) -> tuple[Path, Path, int]:
    """A migrated, seeded state directory and its config file."""
    data_dir = tmp_path / "state"
    data_dir.mkdir()
    db_url = f"sqlite:///{data_dir / 'tlm.db'}"
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)
    alembic_command.upgrade(alembic_cfg, "head")
    # Secrets are encrypted with the state directory's own key file, as the
    # service would; the CLI must find and use that same file.
    set_key_provider(KeyFileProvider(data_dir / ".secret-key"))
    (data_dir / ".local-token").write_text("local-token-S3cret-77aa\n")
    token_file = tmp_path / "gitea-token"
    token_file.write_text(GITEA_TOKEN)
    engine = create_db_engine(db_url)
    try:
        with session_scope(create_session_factory(engine)) as session:
            project_id = seed_database_secrets(session)
    finally:
        engine.dispose()
    set_key_provider(None)
    log_file = data_dir / "app.log"
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.dump(
            {
                "database": {"url": db_url},
                "paths": {"data_dir": str(data_dir)},
                "logging": {"file_sink": str(log_file)},
                "secrets": {"use_os_keystore": False},
                # #8's plain bundle; the #21 tests turn anonymizing on.
                "reporting": {
                    "gitea": {"token_file": str(token_file)},
                    "github": {"enabled": False},
                },
                "cameras": [{"name": "c", "password": CONFIG_CAMERA_PASSWORD}],
            }
        )
    )
    return config, data_dir, project_id


def test_refuses_without_root(
    state: tuple[Path, Path, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        pytest.skip("needs a non-root POSIX user")
    monkeypatch.delenv("TLM_REPORT_ALLOW_NON_ROOT", raising=False)
    config, _, _ = state
    code, _, err = _run("--config", str(config), "report", "--title", "x")
    assert code == 1
    assert "must run as root" in err


def test_builds_redacted_bundle_offline(
    state: tuple[Path, Path, int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TLM_REPORT_ALLOW_NON_ROOT", "1")
    config, data_dir, project_id = state
    secrets = [
        *DB_SECRETS,
        CONFIG_CAMERA_PASSWORD,
        GITEA_TOKEN,
        file_secret(data_dir / ".local-token"),
        file_secret(data_dir / ".secret-key"),
    ]
    assert all(secrets)
    # A log line inside the window, with a plain-text secret in it.
    from datetime import datetime

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S,000")
    (data_dir / "app.log").write_text(
        f"{stamp} WARNING timelapse_manager.x smtp said {DB_SECRETS[1]}\n"
    )
    before = sorted(path.name for path in data_dir.iterdir())
    out = tmp_path / "bundle.zip"

    code, stdout, err = _run(
        "--config",
        str(config),
        "report",
        "--title",
        "UI is down",
        "--project",
        str(project_id),
        "--out",
        str(out),
    )
    assert code == 0, err
    assert str(out) in stdout
    assert out.stat().st_mode & 0o777 == 0o600

    members = assert_no_secrets(out.read_bytes(), [s for s in secrets if s])
    assert set(members) >= {
        "app.json",
        "config.json",
        "supervisor.json",
        "diagnostics.json",
        "project.json",
        "camera.json",
        "events.json",
        "log.txt",
    }
    meta = json.loads(members["report.json"])
    assert meta["source"] == "cli"
    assert meta["report_id"].startswith("TLM-")
    assert json.loads(members["diagnostics.json"])["available"] is False
    assert json.loads(members["supervisor.json"])["available"] is False
    assert json.loads(members["project.json"])["project"]["name"] == "Roof"
    log = members["log.txt"].decode()
    assert "smtp said «redacted»" in log
    # Read-only on the state: no report row, no reports directory, no new files
    # beyond SQLite's own WAL bookkeeping.
    after = sorted(path.name for path in data_dir.iterdir())
    assert "reports" not in after
    assert set(after) - set(before) <= {"tlm.db-wal", "tlm.db-shm"}


def test_refuses_a_missing_database(
    state: tuple[Path, Path, int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrong database path must not create an empty database."""
    monkeypatch.setenv("TLM_REPORT_ALLOW_NON_ROOT", "1")
    config, data_dir, _ = state
    missing = data_dir / "nope.db"
    monkeypatch.setenv("TLM_DATABASE__URL", f"sqlite:///{missing}")
    code, _, err = _run(
        "--config", str(config), "report", "--title", "x", "--out", str(tmp_path / "o")
    )
    assert code == 1
    assert "does not exist" in err
    assert not missing.exists()


def test_out_symlink_is_refused_and_target_untouched(
    state: tuple[Path, Path, int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink planted at --out must not redirect the (root) write."""
    monkeypatch.setenv("TLM_REPORT_ALLOW_NON_ROOT", "1")
    config, _, _ = state
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("do not touch")
    link = tmp_path / "report.zip"
    link.symlink_to(sentinel)
    code, _, err = _run(
        "--config", str(config), "report", "--title", "x", "--out", str(link)
    )
    assert code == 1
    assert "not a regular file" in err
    assert sentinel.read_text() == "do not touch"
    assert link.is_symlink()
    assert not list(tmp_path.glob(".report.zip.*.partial"))


def test_out_replaces_an_existing_regular_file(
    state: tuple[Path, Path, int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TLM_REPORT_ALLOW_NON_ROOT", "1")
    config, _, _ = state
    out = tmp_path / "old.zip"
    out.write_bytes(b"old")
    code, _, err = _run(
        "--config", str(config), "report", "--title", "x", "--out", str(out)
    )
    assert code == 0, err
    assert out.read_bytes().startswith(b"PK")
    assert out.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# --anonymize (#21)
# ---------------------------------------------------------------------------


def _set_github(
    config: Path, *, enabled: bool, camera: str = "Config Gate Cam"
) -> None:
    """Turn the GitHub hand-off on/off and give the config camera a real name."""
    data = yaml.safe_load(config.read_text())
    data["reporting"]["github"] = {"enabled": enabled}
    data["cameras"] = [{"name": camera, "password": CONFIG_CAMERA_PASSWORD}]
    config.write_text(yaml.dump(data))


def _log_line(data_dir: Path, text: str) -> None:
    from datetime import datetime

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S,000")
    (data_dir / "app.log").write_text(f"{stamp} WARNING timelapse_manager.x {text}\n")


def test_anonymize_flag_builds_an_anonymized_bundle(
    state: tuple[Path, Path, int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TLM_REPORT_ALLOW_NON_ROOT", "1")
    monkeypatch.setenv("SUDO_USER", "erin-ops")
    config, data_dir, project_id = state
    _set_github(config, enabled=False)
    _log_line(data_dir, f"erin-ops: Roof cam at 10.0.0.5 wrote {data_dir}/frames/x")
    out = tmp_path / "bundle.zip"

    code, stdout, err = _run(
        "--config",
        str(config),
        "report",
        "--title",
        "Roof cam is down",
        "--project",
        str(project_id),
        "--anonymize",
        "--out",
        str(out),
    )

    assert code == 0, err
    assert "anonymized" in stdout
    members = assert_no_secrets(
        out.read_bytes(),
        [
            *DB_SECRETS,
            "Roof cam",
            "10.0.0.5",
            "auth.example.test",
            "erin-ops",
            str(data_dir),
            "Config Gate Cam",
        ],
    )
    meta = json.loads(members["report.json"])
    assert meta["anonymized"] is True
    assert "camera-" in meta["title"]
    assert "Roof" not in json.loads(members["project.json"])["project"]["name"]


def test_github_enabled_forces_anonymizing(
    state: tuple[Path, Path, int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TLM_REPORT_ALLOW_NON_ROOT", "1")
    # A stock Raspberry Pi account: short, but never refuses (it cannot be
    # renamed from the app); it is still replaced in structured fields.
    monkeypatch.setenv("SUDO_USER", "pi")
    config, data_dir, _ = state
    _set_github(config, enabled=True)
    _log_line(data_dir, "ran as pi")
    out = tmp_path / "bundle.zip"

    code, stdout, err = _run(
        "--config", str(config), "report", "--title", "x", "--out", str(out)
    )

    assert code == 0, err
    assert "anonymized" in stdout
    members = assert_no_secrets(out.read_bytes(), ["Roof cam", "auth.example.test"])
    meta = json.loads(members["report.json"])
    assert meta["anonymized"] is True
    assert meta["reporter"]["username"].startswith("user-")


def test_generic_camera_name_refuses_and_writes_nothing(
    state: tuple[Path, Path, int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TLM_REPORT_ALLOW_NON_ROOT", "1")
    monkeypatch.delenv("SUDO_USER", raising=False)
    config, data_dir, _ = state
    _set_github(config, enabled=True, camera="Pi")
    _log_line(data_dir, "camera Pi stalled")
    out = tmp_path / "bundle.zip"

    code, _, err = _run(
        "--config", str(config), "report", "--title", "x", "--out", str(out)
    )

    assert code == 1
    assert "camera name “Pi”" in err
    assert not out.exists()
    assert not list(tmp_path.glob("*.partial"))
