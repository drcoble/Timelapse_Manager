"""The application runs capture on its own executor (#66 C3, WP-66-I).

``app.py`` builds a ``tlm-capture`` thread pool in the lifespan and hands it to
the capture supervisor and the event-listener factory, so every blocking
capture hop leaves the loop's default executor. These tests go through
``create_app`` (the wiring is what this WP adds; ``test_executor_seam.py``
covers a supervisor built by hand):

* with the default executor saturated, a capture still writes its frame within
  budget, and every hop ran on a ``tlm-capture`` thread;
* the executor has the configured (or default) size, the connection pool is
  grown by it, and the lifespan shuts it down after the supervisor stops;
* the event-listener factory shares the supervisor's executor and its
  camera-session registry (#153).
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from fastapi.testclient import TestClient

from tests import loop_guard_support
from tests.api.test_request_threadpool import _swap_default_executor
from tests.conftest import LOOPBACK_PEER, FakeAdapter
from tests.unit.test_campaign_bounds import _seed_camera_and_project
from timelapse_manager.app import CAPTURE_THREAD_NAME_PREFIX, create_app
from timelapse_manager.capture.event_listener import EventListenerFactory
from timelapse_manager.capture.one_shot import capture_one_now
from timelapse_manager.capture.supervisor import CaptureTarget
from timelapse_manager.config.settings import CaptureSettings, Settings
from timelapse_manager.db.engine import (
    POOL_SIZE,
    background_executor_workers,
    capture_executor_workers,
    pool_capacity,
)
from timelapse_manager.runtime import get_context

# Well inside the anchor fire timeout (120 s at the default capture timeout).
_CAPTURE_BUDGET = 10.0


def _on_capture_thread(name: str) -> bool:
    return name.startswith(f"{CAPTURE_THREAD_NAME_PREFIX}_")


def _capture_threads() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if _on_capture_thread(t.name)}


def _with_workers(settings: Settings, workers: int | None) -> Settings:
    capture = settings.capture.model_copy(update={"executor_workers": workers})
    return settings.model_copy(update={"capture": capture})


@pytest.fixture()
def app_settings(settings_no_autostart: Settings, alembic_cfg: Config) -> Settings:
    alembic_cfg.set_main_option("sqlalchemy.url", settings_no_autostart.database.url)
    alembic_command.upgrade(alembic_cfg, "head")
    return settings_no_autostart


@pytest.fixture()
def saturated_client(app_settings: Settings) -> Iterator[TestClient]:
    """An app whose loop default executor has one worker, held for the test."""
    release = threading.Event()
    with TestClient(create_app(app_settings), client=LOOPBACK_PEER) as client:
        default = ThreadPoolExecutor(max_workers=1, thread_name_prefix="default-1")
        _swap_default_executor(client, default)
        started = threading.Event()

        def hold() -> None:
            started.set()
            release.wait(60)

        default.submit(hold)
        assert started.wait(5)
        try:
            yield client
        finally:
            release.set()


def test_capture_completes_while_the_default_executor_is_saturated(
    saturated_client: TestClient, tmp_path: Path
) -> None:
    """Every hop of a capture runs on ``tlm-capture``, none on the blocked pool.

    On the pre-WP wiring (no executor passed) the first hop queues behind the
    held default worker and the capture times out.
    """
    context = get_context()
    supervisor = context.capture_supervisor
    assert supervisor is not None
    seeded = _seed_camera_and_project(context.session_factory, tmp_path, name="sat")
    target = CaptureTarget(
        project_id=seeded["project_id"],
        project_name="sat",
        camera_id=seeded["camera_id"],
        interval_seconds=60,
        storage_path=str(seeded["storage_path"]),
    )
    threads: list[str] = []
    real = supervisor._run_blocking

    async def spy(fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        def record_then_call() -> Any:
            threads.append(threading.current_thread().name)
            return fn(*args, **kwargs)

        return await real(record_then_call)

    async def capture() -> Any:
        return await asyncio.wait_for(
            capture_one_now(supervisor, target, reason="anchor:clock"),
            _CAPTURE_BUDGET,
        )

    started = time.monotonic()
    with (
        patch.object(supervisor, "_run_blocking", side_effect=spy),
        patch(
            "timelapse_manager.capture.one_shot.build_adapter",
            return_value=FakeAdapter(),
        ),
    ):
        written = saturated_client.portal.call(capture)  # type: ignore[union-attr]
    elapsed = time.monotonic() - started

    assert written is not None
    assert elapsed < _CAPTURE_BUDGET
    # Disk gate, camera load, frame write, audit event -- all off the default.
    assert len(threads) >= 4, threads
    assert all(_on_capture_thread(name) for name in threads), threads


def test_lifespan_owns_the_capture_executor(app_settings: Settings) -> None:
    settings = _with_workers(app_settings, 3)
    before = _capture_threads()
    with TestClient(create_app(settings), client=LOOPBACK_PEER):
        context = get_context()
        supervisor = context.capture_supervisor
        assert supervisor is not None
        executor = supervisor.executor
        assert isinstance(executor, ThreadPoolExecutor)
        assert executor._max_workers == 3
        # The listener factory shares the executor and the session cache.
        factory = supervisor._event_source_factory
        assert isinstance(factory, EventListenerFactory)
        assert factory._executor is executor
        assert factory._camera_sessions is supervisor.camera_sessions
        # The pool has a connection for every capture worker.
        pool = context.db_engine.pool
        expected = pool_capacity(settings.render.jobs_executor_workers, 3)
        assert pool._max_overflow == expected - POOL_SIZE  # type: ignore[attr-defined]
        # Threads start lazily: run work so there is a thread to clean up.
        names = {
            f.result(5)
            for f in [
                executor.submit(lambda: threading.current_thread().name)
                for _ in range(3)
            ]
        }
        assert all(_on_capture_thread(n) for n in names)
        assert _capture_threads() - before
    with pytest.raises(RuntimeError, match="shutdown"):
        executor.submit(lambda: None)
    deadline = time.monotonic() + 5
    while _capture_threads() - before and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not _capture_threads() - before


def test_unset_workers_keep_the_default_executor_size(app_settings: Settings) -> None:
    assert app_settings.capture.executor_workers is None
    with TestClient(create_app(app_settings), client=LOOPBACK_PEER):
        supervisor = get_context().capture_supervisor
        assert supervisor is not None
        executor = supervisor.executor
        assert isinstance(executor, ThreadPoolExecutor)
        assert executor._max_workers == background_executor_workers()


class TestCaptureExecutorWorkersSetting:
    def test_default_is_unset(self) -> None:
        assert CaptureSettings().executor_workers is None

    @pytest.mark.parametrize("bad", [0, -1])
    def test_below_one_is_rejected(self, bad: int) -> None:
        with pytest.raises(ValueError, match="executor_workers"):
            CaptureSettings(executor_workers=bad)

    def test_resolution(self) -> None:
        assert capture_executor_workers(None) == background_executor_workers()
        assert capture_executor_workers(5) == 5
        with pytest.raises(ValueError, match="at least 1"):
            capture_executor_workers(0)

    @pytest.mark.parametrize(
        ("cpus", "workers"), [(2, 6), (4, 8), (8, 12), (64, 32), (None, 5)]
    )
    def test_default_size_by_host(
        self, monkeypatch: pytest.MonkeyPatch, cpus: int | None, workers: int
    ) -> None:
        """Test VM (2 vCPU) 6, Pi (4) 8, prod (8) 12, capped at 32."""
        monkeypatch.setattr("os.cpu_count", lambda: cpus)
        assert capture_executor_workers(None) == workers


def test_loop_guard_allow_lists_stay_empty() -> None:
    """Moving capture to its own executor adds no loop-guard exception."""
    assert loop_guard_support.KNOWN_LOOP_DB == {}
    assert loop_guard_support.KNOWN_BACKGROUND_LOOP_DB == {}
