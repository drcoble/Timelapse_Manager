"""Request work runs on the request threadpool, long jobs on their own (#66 C3).

Before WP-66-H the ``async def`` routes in ``api/renders.py``, ``api/cameras.py``
and ``web/routers/renders.py`` hopped their database work with
``asyncio.to_thread``: onto the event loop's default executor, which capture,
the scheduler, the dispatcher and every export also share. A long export, or a
burst of capture work, could then starve requests. They now hop with
``run_in_threadpool`` (anyio's limiter, which the connection pool is sized
for), and long jobs run on the ``tlm-jobs`` executor.

The tests swap the *app loop's* default executor for a one-worker pool and then
block that worker (or let an export take it). Every request here must still
answer. On the old code each one waits for the blocked worker and times out.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import anyio.to_thread
import httpx
import pytest
from alembic import command as alembic_command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from tests import loop_guard_support
from tests.api.test_renders_api import (
    _auth,
    _override_admin,
    _seed_done_render,
    _seed_project,
)
from tests.conftest import LOOPBACK_PEER, csrf_of
from timelapse_manager.app import create_app
from timelapse_manager.config.settings import Settings
from timelapse_manager.db.models import RenderJob
from timelapse_manager.db.session import session_scope
from timelapse_manager.encode.encoder import RenderResult
from timelapse_manager.executors import JOBS_THREAD_NAME_PREFIX
from timelapse_manager.render import export as export_mod
from timelapse_manager.runtime import get_context

# How long a request may take while the default executor is unavailable. A
# request that queues behind the blocked worker never answers, so this only has
# to beat the test, not a busy runner.
_REQUEST_BUDGET = 10.0


def _swap_default_executor(client: TestClient, executor: ThreadPoolExecutor) -> None:
    """Make ``executor`` the default executor of the app's event loop."""

    async def swap() -> None:
        loop = asyncio.get_running_loop()
        previous = getattr(loop, "_default_executor", None)
        loop.set_default_executor(executor)
        if previous is not None:
            previous.shutdown(wait=False)

    client.portal.call(swap)  # type: ignore[union-attr]


@pytest.fixture()
def one_worker_default(migrated_client: TestClient) -> Iterator[ThreadPoolExecutor]:
    """The app loop's default executor, shrunk to one worker for the test."""
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="default-1")
    _swap_default_executor(migrated_client, executor)
    yield executor
    # The client's teardown shuts the loop's default executor down.


@pytest.fixture()
def blocked_default(one_worker_default: ThreadPoolExecutor) -> Iterator[None]:
    """Occupy the only default-executor worker until the test ends."""
    release = threading.Event()
    started = threading.Event()

    def hold() -> None:
        started.set()
        release.wait(60)

    one_worker_default.submit(hold)
    assert started.wait(5)
    try:
        yield
    finally:
        release.set()


def _within_budget(call: Callable[[], httpx.Response]) -> httpx.Response:
    """Run a blocking client call, failing (not hanging) past the budget."""
    with ThreadPoolExecutor(max_workers=1) as runner:
        future = runner.submit(call)
        try:
            return future.result(timeout=_REQUEST_BUDGET)
        except TimeoutError:
            pytest.fail(
                "request did not answer while the default executor was busy: it "
                "still hops through asyncio.to_thread"
            )


class TestRequestsSkipTheDefaultExecutor:
    """Converted ``async def`` routes answer with the default executor blocked."""

    def test_api_cancel_of_a_finished_render(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
        blocked_default: None,
    ) -> None:
        project_id = _seed_project(migrated_factory, settings_no_autostart)
        job_id, _ = _seed_done_render(
            migrated_factory, settings_no_autostart, project_id
        )
        _override_admin(migrated_client.app)

        resp = _within_budget(
            lambda: migrated_client.post(
                f"/api/v1/renders/{job_id}/cancel", headers=_auth(cam_auth_token)
            )
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "done"

    def test_api_delete_of_a_finished_render(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
        blocked_default: None,
    ) -> None:
        project_id = _seed_project(migrated_factory, settings_no_autostart)
        job_id, output = _seed_done_render(
            migrated_factory, settings_no_autostart, project_id
        )
        _override_admin(migrated_client.app)

        resp = _within_budget(
            lambda: migrated_client.delete(
                f"/api/v1/renders/{job_id}", headers=_auth(cam_auth_token)
            )
        )
        assert resp.status_code == 204
        assert not output.exists()

    def test_api_camera_validate_unknown_camera(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        blocked_default: None,
    ) -> None:
        _override_admin(migrated_client.app)
        resp = _within_budget(
            lambda: migrated_client.post(
                "/api/v1/cameras/999/validate", headers=_auth(cam_auth_token)
            )
        )
        assert resp.status_code == 404

    def test_range_stream_chunk_reads(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
        blocked_default: None,
    ) -> None:
        """G10: the open, seek and chunk reads of a range stream."""
        content = bytes(range(256)) * 1024  # 256 KiB: several 64 KiB chunks
        project_id = _seed_project(migrated_factory, settings_no_autostart)
        job_id, _ = _seed_done_render(
            migrated_factory, settings_no_autostart, project_id, content=content
        )

        resp = _within_budget(
            lambda: migrated_client.get(
                f"/api/v1/renders/{job_id}/stream",
                headers={**_auth(cam_auth_token), "Range": "bytes=1000-200999"},
            )
        )
        assert resp.status_code == 206
        assert resp.headers["Content-Range"] == f"bytes 1000-200999/{len(content)}"
        assert resp.content == content[1000:201000]


def test_web_cancel_of_a_finished_render(
    admin_client: TestClient,
    blocked_web_default: None,
) -> None:
    """The web card's cancel route (``web/routers/renders.py``)."""
    context = get_context()
    project_id = _seed_project(context.session_factory, context.settings)
    job_id, _ = _seed_done_render(context.session_factory, context.settings, project_id)
    token = csrf_of(admin_client, "/")

    resp = _within_budget(
        lambda: admin_client.post(
            f"/renders/{job_id}/cancel",
            data={"csrf_token": token},
            headers={"HX-Request": "true"},
        )
    )
    assert resp.status_code == 200


@pytest.fixture()
def blocked_web_default(admin_client: TestClient) -> Iterator[None]:
    """``blocked_default`` for the web client fixtures."""
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="default-1")
    _swap_default_executor(admin_client, executor)
    release = threading.Event()
    started = threading.Event()

    def hold() -> None:
        started.set()
        release.wait(60)

    executor.submit(hold)
    assert started.wait(5)
    try:
        yield
    finally:
        release.set()


# ---------------------------------------------------------------------------
# A long export cannot exhaust the request threadpool
# ---------------------------------------------------------------------------


def _migrate(alembic_cfg: Config, settings: Settings) -> None:
    alembic_cfg.set_main_option("sqlalchemy.url", settings.database.url)
    alembic_command.upgrade(alembic_cfg, "head")


@pytest.fixture()
def export_client(
    settings_no_autostart: Settings, alembic_cfg: Config
) -> Iterator[TestClient]:
    """An app whose worker takes three exports but has two jobs workers."""
    render = settings_no_autostart.render.model_copy(
        update={
            "max_concurrent": 3,
            "max_concurrent_exports": 3,
            "jobs_executor_workers": 2,
        }
    )
    settings = settings_no_autostart.model_copy(update={"render": render})
    _migrate(alembic_cfg, settings)
    with TestClient(create_app(settings), client=LOOPBACK_PEER) as client:
        yield client


def _enqueue_exports(count: int) -> list[int]:
    context = get_context()
    project_id = _seed_project(context.session_factory, context.settings)
    ids: list[int] = []
    with session_scope(context.session_factory) as session:
        for _ in range(count):
            job = RenderJob(
                project_id=project_id,
                kind="export",
                status="pending",
                output_settings={"frame_ids": []},
            )
            session.add(job)
            session.flush()
            ids.append(job.id)
    return ids


def test_long_exports_cannot_exhaust_the_request_threadpool(
    export_client: TestClient,
    cam_auth_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three long exports: two run on ``tlm-jobs``, none takes a request thread.

    The loop's default executor is one worker, so on the old code the first
    export took it and every request hop (and the next claim) queued behind it.
    """
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="default-1")
    _swap_default_executor(export_client, executor)
    release = threading.Event()
    lock = threading.Lock()
    running: list[str] = []
    peak = 0

    def long_export(*_args: Any, **_kwargs: Any) -> RenderResult:
        nonlocal peak
        with lock:
            running.append(threading.current_thread().name)
            peak = max(peak, len(running))
        try:
            release.wait(60)
        finally:
            with lock:
                running.remove(threading.current_thread().name)
        return RenderResult(
            success=False,
            output_path=None,
            duration_seconds=None,
            browser_streamable=False,
            codec="zip",
            container="zip",
            error="test export",
        )

    monkeypatch.setattr(export_mod, "build_export_zip", long_export)
    context = get_context()
    queue = context.render_queue
    assert queue is not None
    _enqueue_exports(3)
    project_id = _seed_project(
        context.session_factory, context.settings, name="other-project"
    )
    done_job, _ = _seed_done_render(
        context.session_factory, context.settings, project_id
    )
    _override_admin(export_client.app)

    try:
        export_client.portal.call(queue.start)  # type: ignore[union-attr]
        deadline = time.monotonic() + _REQUEST_BUDGET
        while time.monotonic() < deadline:
            with lock:
                if len(running) == 2:
                    break
            time.sleep(0.02)
        with lock:
            names = list(running)
        assert len(names) == 2, f"expected two exports running, saw {names}"
        assert all(n.startswith(f"{JOBS_THREAD_NAME_PREFIX}_") for n in names)

        async def borrowed() -> float:
            return anyio.to_thread.current_default_thread_limiter().borrowed_tokens

        # The exports hold no request thread...
        assert export_client.portal.call(borrowed) == 0  # type: ignore[union-attr]
        # ...and request work, sync and async routes alike, still answers.
        resp = _within_budget(
            lambda: export_client.get(
                f"/api/v1/renders/{done_job}", headers=_auth(cam_auth_token)
            )
        )
        assert resp.status_code == 200, resp.text
        resp = _within_budget(
            lambda: export_client.post(
                f"/api/v1/renders/{done_job}/cancel", headers=_auth(cam_auth_token)
            )
        )
        assert resp.status_code == 200, resp.text
        # The third export waits for a jobs worker; it never runs a third.
        assert peak == 2
    finally:
        release.set()

    async def settle() -> None:
        await asyncio.wait_for(queue.wait_idle(), _REQUEST_BUDGET)

    export_client.portal.call(settle)  # type: ignore[union-attr]
    assert peak == 2


# ---------------------------------------------------------------------------
# Lifespan ownership of the jobs executor
# ---------------------------------------------------------------------------


def _jobs_threads() -> list[threading.Thread]:
    return [
        t
        for t in threading.enumerate()
        if t.name.startswith(f"{JOBS_THREAD_NAME_PREFIX}_")
    ]


def test_lifespan_creates_and_shuts_down_the_jobs_executor(
    settings_no_autostart: Settings, alembic_cfg: Config
) -> None:
    _migrate(alembic_cfg, settings_no_autostart)
    before = set(_jobs_threads())
    with TestClient(create_app(settings_no_autostart)):
        context = get_context()
        executor = context.jobs_executor
        assert executor is not None
        assert (
            executor._max_workers == settings_no_autostart.render.jobs_executor_workers
        )
        # Threads start lazily: run work so there is a thread to clean up.
        names = {
            f.result(5)
            for f in [
                executor.submit(lambda: threading.current_thread().name)
                for _ in range(2)
            ]
        }
        assert all(n.startswith(f"{JOBS_THREAD_NAME_PREFIX}_") for n in names)
        assert set(_jobs_threads()) - before
    with pytest.raises(RuntimeError, match="shutdown"):
        executor.submit(lambda: None)
    deadline = time.monotonic() + 5
    while set(_jobs_threads()) - before and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not set(_jobs_threads()) - before


def test_loop_guard_allow_lists_stay_empty() -> None:
    """WP-66-H moves work between executors; it adds no loop-guard exception."""
    assert loop_guard_support.KNOWN_LOOP_DB == {}
    assert loop_guard_support.KNOWN_BACKGROUND_LOOP_DB == {}
