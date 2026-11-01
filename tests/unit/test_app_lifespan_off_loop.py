"""The lifespan's blocking startup work runs off the event loop (#66 L4).

The SSRF subnet merge (a database read) and the ffmpeg version probe (a
subprocess) hop to a worker thread; a failed merge still logs and lets the
server start on the config baseline. The capture and jobs executors are
registered for the diagnostics while the app runs (#161).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import timelapse_manager.app as app_module
import timelapse_manager.security.ssrf_settings_service as ssrf_service
from timelapse_manager.app import CAPTURE_THREAD_NAME_PREFIX, create_app
from timelapse_manager.config import Settings
from timelapse_manager.config.settings import SsrfSettings
from timelapse_manager.executors import JOBS_THREAD_NAME_PREFIX
from timelapse_manager.monitoring.diagnostics import executors_snapshot
from timelapse_manager.runtime import get_context

_APP_LOGGER = "timelapse_manager.app"


def _on_loop_thread() -> bool:
    return asyncio._get_running_loop() is not None  # noqa: SLF001


@pytest.fixture()
def migrated_settings(web_settings: Settings, alembic_cfg: Config) -> Settings:
    settings = web_settings.model_copy(
        update={"ssrf": SsrfSettings(allowed_private_subnets=["10.9.0.0/24"])}
    )
    alembic_cfg.set_main_option("sqlalchemy.url", settings.database.url)
    alembic_command.upgrade(alembic_cfg, "head")
    return settings


def test_ssrf_merge_runs_in_a_worker_thread(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[bool, str]] = []
    original = ssrf_service.apply_to_runtime

    def recording(session: Session) -> list[str]:
        calls.append((_on_loop_thread(), threading.current_thread().name))
        return original(session)

    monkeypatch.setattr(ssrf_service, "apply_to_runtime", recording)

    with TestClient(create_app(migrated_settings)):
        merged = list(get_context().settings.ssrf.allowed_private_subnets)

    [(on_loop, thread_name)] = calls
    assert on_loop is False, thread_name
    assert "10.9.0.0/24" in merged


def test_ssrf_merge_failure_logs_and_the_server_starts(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def failing(session: Session) -> list[str]:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(ssrf_service, "apply_to_runtime", failing)
    # Keep the app's own logging setup from replacing caplog's handler.
    monkeypatch.setattr(app_module, "configure_logging", lambda settings: None)

    with (
        caplog.at_level(logging.WARNING, logger=_APP_LOGGER),
        TestClient(create_app(migrated_settings)) as client,
    ):
        assert client.get("/healthz").status_code == 200
        baseline = list(get_context().settings.ssrf.allowed_private_subnets)

    assert baseline == ["10.9.0.0/24"]
    [record] = [
        r
        for r in caplog.records
        if r.name == _APP_LOGGER and "Could not merge stored SSRF" in r.getMessage()
    ]
    assert record.levelno == logging.WARNING
    assert record.exc_info is not None


def test_ffmpeg_version_probe_runs_in_a_worker_thread(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool] = []

    def probe(path: Any = None) -> str:
        calls.append(_on_loop_thread())
        return "ffmpeg version test"

    monkeypatch.setattr(app_module, "probe_ffmpeg_version", probe)

    with TestClient(create_app(migrated_settings)):
        assert get_context().ffmpeg_version == "ffmpeg version test"

    assert calls == [False]


def test_capture_and_jobs_pools_are_registered_while_running(
    migrated_settings: Settings,
) -> None:
    def named() -> dict[str, dict[str, Any]]:
        return {pool["name"]: pool for pool in executors_snapshot(None)}

    with TestClient(create_app(migrated_settings)):
        pools = named()
        assert {CAPTURE_THREAD_NAME_PREFIX, JOBS_THREAD_NAME_PREFIX} <= set(pools)
        assert (
            pools[JOBS_THREAD_NAME_PREFIX]["max_workers"]
            == migrated_settings.render.jobs_executor_workers
        )
        assert pools[CAPTURE_THREAD_NAME_PREFIX]["max_workers"] >= 1

    assert set(named()) == {"default"}
