"""Tests for the command-line interface.

All tests invoke cli.main() in-process (no subprocesses) to keep them fast
and hermetic. The 'system info' success path is excluded from unit tests
because it requires a running server.
"""

from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
import yaml

import timelapse_manager
from timelapse_manager.cli import main

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_cli(*args: str) -> tuple[int, str, str]:
    """Run cli.main() in-process; return (exit_code, stdout, stderr)."""
    old_argv = sys.argv[:]
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.argv = ["timelapse", *args]
    sys.stdout = StringIO()
    sys.stderr = StringIO()
    exit_code = 0
    try:
        main()
    except SystemExit as exc:
        exit_code = int(exc.code) if exc.code is not None else 0
    finally:
        stdout = sys.stdout.getvalue()
        stderr = sys.stderr.getvalue()
        sys.argv = old_argv
        sys.stdout = old_stdout
        sys.stderr = old_stderr
    return exit_code, stdout, stderr


def _write_yaml(path: Path, data: Any) -> Path:
    path.write_text(yaml.dump(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# version subcommand
# ---------------------------------------------------------------------------


class TestCliVersion:
    def test_version_exits_with_0(self) -> None:
        code, _, _ = _run_cli("version")
        assert code == 0

    def test_version_prints_version_string(self) -> None:
        _, stdout, _ = _run_cli("version")
        assert timelapse_manager.__version__ in stdout

    def test_version_prints_non_empty_output(self) -> None:
        _, stdout, _ = _run_cli("version")
        assert stdout.strip() != ""


# ---------------------------------------------------------------------------
# config show subcommand
# ---------------------------------------------------------------------------


class TestCliConfigShow:
    def test_config_show_exits_with_0(self) -> None:
        code, _, _ = _run_cli("config", "show")
        assert code == 0

    def test_config_show_json_exits_with_0(self) -> None:
        code, _, _ = _run_cli("config", "show", "--json")
        assert code == 0

    def test_config_show_json_is_valid_json(self) -> None:
        _, stdout, _ = _run_cli("config", "show", "--json")
        parsed = json.loads(stdout)
        assert isinstance(parsed, dict)

    def test_config_show_json_contains_server_section(self) -> None:
        _, stdout, _ = _run_cli("config", "show", "--json")
        parsed = json.loads(stdout)
        assert "server" in parsed

    def test_config_show_json_contains_database_section(self) -> None:
        _, stdout, _ = _run_cli("config", "show", "--json")
        parsed = json.loads(stdout)
        assert "database" in parsed

    def test_config_show_json_redacts_db_password(self, tmp_path: Path) -> None:
        """A database URL with embedded credentials must be redacted in output."""
        cfg = _write_yaml(
            tmp_path / "config.yaml",
            {"database": {"url": "postgresql://user:mys3cr3t@localhost/tlm"}},
        )
        _, stdout, _ = _run_cli("--config", str(cfg), "config", "show", "--json")
        assert "mys3cr3t" not in stdout, "Database password must be redacted"

    def test_config_show_text_produces_output(self) -> None:
        _, stdout, _ = _run_cli("config", "show")
        assert stdout.strip() != ""

    def test_config_show_with_file_reflects_file_values(self, tmp_path: Path) -> None:
        cfg = _write_yaml(tmp_path / "config.yaml", {"server": {"http_port": 7654}})
        _, stdout, _ = _run_cli("--config", str(cfg), "config", "show", "--json")
        parsed = json.loads(stdout)
        assert parsed["server"]["http_port"] == 7654


# ---------------------------------------------------------------------------
# system info failure path (no running server)
# ---------------------------------------------------------------------------


class TestCliSystemInfoFailure:
    def test_system_info_exits_nonzero_when_no_server(self) -> None:
        """When no server is running, system info must exit with a nonzero code."""
        # Use a port that is almost certainly not in use and has a very short
        # timeout window.  The CLI itself caps at 10 seconds; this test accepts
        # any nonzero exit.
        code, _, _ = _run_cli("system", "info")
        assert code != 0

    def test_system_info_prints_error_to_stderr_when_no_server(self) -> None:
        code, _, stderr = _run_cli("system", "info")
        assert code != 0
        assert stderr.strip() != ""


# ---------------------------------------------------------------------------
# migrate subcommand
# ---------------------------------------------------------------------------


class TestCliCoreOperations:
    """camera/project commands as a thin loopback-API client (httpx mocked)."""

    @staticmethod
    def _mock_api(
        monkeypatch: pytest.MonkeyPatch, status_code: int, body: Any
    ) -> dict[str, Any]:
        """Patch token + httpx.request; return a dict capturing the last call."""
        import httpx

        captured: dict[str, Any] = {}

        def fake_request(method: str, url: str, **kwargs: Any) -> httpx.Response:
            captured["method"] = method
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            captured["headers"] = kwargs.get("headers")
            return httpx.Response(status_code, json=body)

        monkeypatch.setattr(
            "timelapse_manager.cli.ensure_local_token", lambda _s: "tok"
        )
        monkeypatch.setattr("timelapse_manager.cli.httpx.request", fake_request)
        return captured

    def test_camera_add_posts_to_api(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cap = self._mock_api(monkeypatch, 201, {"id": 7, "name": "cam-a"})
        code, out, _ = _run_cli(
            "camera",
            "add",
            "--name",
            "cam-a",
            "--protocol",
            "vapix",
            "--address",
            "10.0.0.5",
            "--username",
            "u",
            "--password",
            "p",
        )
        assert code == 0
        assert cap["method"] == "POST"
        assert cap["url"].endswith("/api/v1/cameras")
        assert cap["json"]["name"] == "cam-a"
        assert cap["json"]["protocol"] == "vapix"
        assert cap["json"]["credentials"] == {"username": "u", "password": "p"}
        assert cap["headers"]["Authorization"] == "Bearer tok"
        assert "camera 7 created" in out

    def test_camera_add_requires_name(self) -> None:
        code, _, _ = _run_cli("camera", "add", "--protocol", "vapix")
        assert code != 0

    def test_project_create_posts_to_api(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cap = self._mock_api(monkeypatch, 201, {"id": 5, "name": "proj"})
        code, out, _ = _run_cli(
            "project",
            "create",
            "--name",
            "proj",
            "--camera-id",
            "2",
            "--interval",
            "30",
        )
        assert code == 0
        assert cap["method"] == "POST"
        assert cap["url"].endswith("/api/v1/projects")
        assert cap["json"] == {
            "name": "proj",
            "camera_id": 2,
            "capture_interval_seconds": 30,
        }
        assert "project 5 created" in out

    def test_project_start_calls_resume(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cap = self._mock_api(monkeypatch, 200, {"id": 3, "lifecycle_state": "active"})
        code, out, _ = _run_cli("project", "start", "3")
        assert code == 0
        assert cap["url"].endswith("/api/v1/projects/3/resume")
        assert "started" in out

    def test_project_start_already_active_is_ok(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mock_api(monkeypatch, 409, {"detail": "not paused"})
        code, out, _ = _run_cli("project", "start", "3")
        assert code == 0
        assert "already active" in out

    def test_project_stop_calls_pause(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cap = self._mock_api(monkeypatch, 200, {"id": 3, "lifecycle_state": "paused"})
        code, out, _ = _run_cli("project", "stop", "3")
        assert code == 0
        assert cap["url"].endswith("/api/v1/projects/3/pause")
        assert "stopped" in out

    def test_project_render_triggers_render(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cap = self._mock_api(monkeypatch, 201, {"id": 11, "status": "pending"})
        code, out, _ = _run_cli("project", "render", "4")
        assert code == 0
        assert cap["method"] == "POST"
        assert cap["url"].endswith("/api/v1/projects/4/renders")
        assert cap["json"] == {}
        assert "render job 11 queued" in out

    def test_project_status_all_lists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cap = self._mock_api(
            monkeypatch,
            200,
            [
                {
                    "id": 1,
                    "name": "a",
                    "operational_status": "running",
                    "lifecycle_state": "active",
                    "frame_count": 9,
                    "disk_used_bytes": 4096,
                    "uptime_seconds": 120,
                }
            ],
        )
        code, out, _ = _run_cli("project", "status")
        assert code == 0
        assert cap["method"] == "GET"
        assert cap["url"].endswith("/api/v1/projects")
        assert "[1] a: running" in out

    def test_project_status_single(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cap = self._mock_api(
            monkeypatch,
            200,
            {
                "id": 2,
                "name": "b",
                "operational_status": "paused",
                "lifecycle_state": "paused",
                "frame_count": 3,
                "disk_used_bytes": 1024,
                "uptime_seconds": None,
            },
        )
        code, out, _ = _run_cli("project", "status", "2")
        assert code == 0
        assert cap["url"].endswith("/api/v1/projects/2")
        assert "[2] b: paused" in out

    def test_project_create_surfaces_api_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mock_api(monkeypatch, 409, {"detail": "a project named 'proj' exists"})
        code, _, err = _run_cli(
            "project",
            "create",
            "--name",
            "proj",
            "--camera-id",
            "2",
            "--interval",
            "30",
        )
        assert code != 0
        assert "409" in err

    def test_project_requires_subcommand(self) -> None:
        code, _, _ = _run_cli("project")
        assert code != 0


class TestCliMigrate:
    def test_migrate_creates_schema_in_temp_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """'migrate' must run Alembic upgrade head against the configured DB."""
        from sqlalchemy import inspect

        from timelapse_manager.db.engine import create_db_engine

        db_path = tmp_path / "migrate-test.db"
        db_url = f"sqlite:///{db_path}"

        # Point at the real alembic.ini (code-root cwd is required for
        # Config("alembic.ini") inside cli._cmd_migrate).
        code_root = Path(__file__).parent.parent.parent
        monkeypatch.chdir(code_root)
        monkeypatch.setenv("TLM_DATABASE__URL", db_url)

        code, _, stderr = _run_cli("migrate")
        assert code == 0, f"migrate exited nonzero; stderr: {stderr}"

        engine = create_db_engine(db_url)
        try:
            tables = inspect(engine).get_table_names()
        finally:
            engine.dispose()

        assert "alembic_version" in tables
        assert "camera" in tables
        assert "project" in tables

    def test_migrate_is_idempotent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Running migrate twice must not raise or corrupt the schema."""
        db_url = f"sqlite:///{tmp_path / 'idempotent.db'}"
        code_root = Path(__file__).parent.parent.parent
        monkeypatch.chdir(code_root)
        monkeypatch.setenv("TLM_DATABASE__URL", db_url)

        code1, _, _ = _run_cli("migrate")
        code2, _, _ = _run_cli("migrate")
        assert code1 == 0
        assert code2 == 0


# ---------------------------------------------------------------------------
# user create subcommand (in-process, against the configured database)
# ---------------------------------------------------------------------------


class TestCliUserCreate:
    """``user create`` opens the configured DB directly (like ``migrate``).

    Each test points ``TLM_DATABASE__URL`` at a temp-file SQLite database and
    runs ``migrate`` first, because the command builds its own engine and so
    cannot share an in-memory database with the test process. Low-cost Argon2
    parameters are set via the environment so the genuine hashing path stays
    fast.
    """

    def _prepare_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
        """Migrate a fresh temp database and return its URL."""
        db_url = f"sqlite:///{tmp_path / 'user-create.db'}"
        code_root = Path(__file__).parent.parent.parent
        monkeypatch.chdir(code_root)
        monkeypatch.setenv("TLM_DATABASE__URL", db_url)
        # Keep Argon2 cheap so hashing in-test is fast.
        monkeypatch.setenv("TLM_AUTH__ARGON2_MEMORY_KIB", "256")
        monkeypatch.setenv("TLM_AUTH__ARGON2_TIME_COST", "1")
        monkeypatch.setenv("TLM_AUTH__ARGON2_PARALLELISM", "1")
        code, _, stderr = _run_cli("migrate")
        assert code == 0, f"migrate exited nonzero; stderr: {stderr}"
        return db_url

    def _load_user(self, db_url: str, username: str) -> dict[str, Any] | None:
        from sqlalchemy import select

        from timelapse_manager.db.engine import create_db_engine
        from timelapse_manager.db.models import User
        from timelapse_manager.db.session import (
            create_session_factory,
            session_scope,
        )

        engine = create_db_engine(db_url)
        try:
            factory = create_session_factory(engine)
            with session_scope(factory) as db:
                row = db.execute(
                    select(User).where(User.username == username)
                ).scalar_one_or_none()
                if row is None:
                    return None
                return {
                    "username": row.username,
                    "enabled": row.enabled,
                    "auth_source": row.auth_source,
                    "role": row.role,
                    "password_hash": row.password_hash,
                }
        finally:
            engine.dispose()

    def test_create_user_persists_enabled_local_row_with_role(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_url = self._prepare_db(tmp_path, monkeypatch)
        password = "OperatorPass123!"  # noqa: S105 - test fixture credential
        code, stdout, stderr = _run_cli(
            "user",
            "create",
            "--username",
            "opuser",
            "--password",
            password,
            "--role",
            "operator",
        )
        assert code == 0, f"user create exited nonzero; stderr: {stderr}"
        assert "opuser" in stdout
        assert "operator" in stdout

        from timelapse_manager.config.settings import AuthSettings
        from timelapse_manager.security.passwords import verify_password

        row = self._load_user(db_url, "opuser")
        assert row is not None
        assert row["enabled"] is True
        assert row["auth_source"] == "local"
        assert row["role"] == "operator"
        assert row["password_hash"] is not None
        auth = AuthSettings(
            argon2_memory_kib=256, argon2_time_cost=1, argon2_parallelism=1
        )
        assert verify_password(password, row["password_hash"], auth) is True

    def test_create_user_defaults_to_viewer_role(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_url = self._prepare_db(tmp_path, monkeypatch)
        code, _, stderr = _run_cli(
            "user", "create", "--username", "viewer1", "--password", "ViewerPass12!"
        )
        assert code == 0, f"stderr: {stderr}"
        row = self._load_user(db_url, "viewer1")
        assert row is not None
        assert row["role"] == "viewer"

    def test_create_user_reads_password_from_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_url = self._prepare_db(tmp_path, monkeypatch)
        password = "EnvPassword123!"  # noqa: S105 - test fixture credential
        monkeypatch.setenv("TLM_USER_PASSWORD", password)
        code, _, stderr = _run_cli(
            "user", "create", "--username", "envuser", "--role", "admin"
        )
        assert code == 0, f"stderr: {stderr}"

        from timelapse_manager.config.settings import AuthSettings
        from timelapse_manager.security.passwords import verify_password

        row = self._load_user(db_url, "envuser")
        assert row is not None
        assert row["role"] == "admin"
        auth = AuthSettings(
            argon2_memory_kib=256, argon2_time_cost=1, argon2_parallelism=1
        )
        assert verify_password(password, row["password_hash"], auth) is True

    def test_missing_password_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._prepare_db(tmp_path, monkeypatch)
        monkeypatch.delenv("TLM_USER_PASSWORD", raising=False)
        code, _, stderr = _run_cli("user", "create", "--username", "nopass")
        assert code != 0
        assert "password" in stderr.lower()

    def test_duplicate_username_fails_without_overwrite(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_url = self._prepare_db(tmp_path, monkeypatch)
        first = "FirstPass1234!"  # noqa: S105 - test fixture credential
        code1, _, _ = _run_cli(
            "user", "create", "--username", "dupe", "--password", first
        )
        assert code1 == 0
        # Second attempt with a different password must be rejected and must NOT
        # change the stored credential.
        code2, _, stderr2 = _run_cli(
            "user",
            "create",
            "--username",
            "dupe",
            "--password",
            "SecondPass99!",
        )
        assert code2 != 0
        assert "already exists" in stderr2.lower()

        from timelapse_manager.config.settings import AuthSettings
        from timelapse_manager.security.passwords import verify_password

        row = self._load_user(db_url, "dupe")
        assert row is not None
        auth = AuthSettings(
            argon2_memory_kib=256, argon2_time_cost=1, argon2_parallelism=1
        )
        # The original password still verifies; the overwrite did not happen.
        assert verify_password(first, row["password_hash"], auth) is True

    def test_invalid_role_rejected_by_argparse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._prepare_db(tmp_path, monkeypatch)
        code, _, stderr = _run_cli(
            "user",
            "create",
            "--username",
            "badrole",
            "--password",
            "SomePass1234!",
            "--role",
            "superuser",
        )
        # argparse exits 2 on invalid choice.
        assert code == 2
        assert "superuser" in stderr or "invalid choice" in stderr.lower()
