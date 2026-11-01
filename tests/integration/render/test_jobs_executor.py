"""Long render-side jobs run on the jobs executor (#66 C3 and N3, WP-66-H).

The render queue and the post-render actions take the executor at construction
(``jobs_executor=``) or per call (``executor=``). These tests inject a real
``tlm-jobs`` executor and record the thread each long job runs on: render
preparation, the export zip, the free-space and output-file probes, the codec
backfill scan, and the post-render copy and prunes. The short claim and the
terminal writes must stay off it, so a terminal write never queues behind an
export.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import sessionmaker

from tests.integration.render.test_queue import (
    FakeEncoder,
    _enqueue_job,
    _job_status,
    _seed_project,
)
from timelapse_manager.config.settings import Settings
from timelapse_manager.encode.encoder import RenderResult
from timelapse_manager.executors import JOBS_THREAD_NAME_PREFIX, create_jobs_executor
from timelapse_manager.render import export as export_mod
from timelapse_manager.render import post_actions
from timelapse_manager.render.queue import RenderQueue


def _on_jobs_thread() -> bool:
    return threading.current_thread().name.startswith(f"{JOBS_THREAD_NAME_PREFIX}_")


class _ThreadLog:
    """Records, per label, whether each call ran on a ``tlm-jobs`` thread."""

    def __init__(self) -> None:
        self.calls: dict[str, list[bool]] = {}
        self._lock = threading.Lock()

    def note(self, label: str) -> None:
        with self._lock:
            self.calls.setdefault(label, []).append(_on_jobs_thread())

    def wrap(self, label: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            self.note(label)
            return fn(*args, **kwargs)

        return wrapper


@pytest.fixture()
def jobs_executor() -> Iterator[Any]:
    executor = create_jobs_executor(2)
    yield executor
    executor.shutdown(wait=True)


async def _drain(queue: RenderQueue) -> None:
    await queue.start()
    try:
        await asyncio.wait_for(queue.wait_idle(), 30.0)
    finally:
        await queue.stop()


async def test_render_prepare_and_disk_probes_run_on_the_jobs_executor(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
    jobs_executor: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _ThreadLog()
    monkeypatch.setattr(
        RenderQueue, "_prepare", log.wrap("prepare", RenderQueue._prepare)
    )
    monkeypatch.setattr(
        RenderQueue,
        "_claim_next_pending",
        log.wrap("claim", RenderQueue._claim_next_pending),
    )
    monkeypatch.setattr(
        RenderQueue,
        "_record_result_sync",
        log.wrap("record", RenderQueue._record_result_sync),
    )
    monkeypatch.setattr(
        export_mod,
        "free_space_below_low_watermark",
        log.wrap("free_space", export_mod.free_space_below_low_watermark),
    )
    original_is_file = Path.is_file
    rendered: list[Path] = []

    def is_file(path: Path, *args: Any, **kwargs: Any) -> bool:
        if path in rendered:
            log.note("output_is_file")
        return original_is_file(path, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", is_file)

    class RecordingEncoder(FakeEncoder):
        async def render(self, spec: Any) -> RenderResult:
            rendered.append(spec.output_path)
            return await super().render(spec)

    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_job(migrated_factory, project_id)
    queue = RenderQueue(
        settings_no_autostart,
        migrated_factory,
        encoder=RecordingEncoder(),
        jobs_executor=jobs_executor,
    )
    await _drain(queue)

    assert _job_status(migrated_factory, job_id) == "done"
    assert log.calls["prepare"] == [True]
    assert log.calls["free_space"] == [True]
    # N3: the probe's existence check of the finished output is off the loop.
    assert log.calls["output_is_file"] == [True]
    # The short claim and the terminal write stay on the default executor.
    assert set(log.calls["claim"]) == {False}
    assert log.calls["record"] == [False]


async def test_export_zip_runs_on_the_jobs_executor(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
    jobs_executor: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.integration.render.test_export_queue import (
        _enqueue_export,
    )
    from tests.integration.render.test_export_queue import (
        _seed_project as _seed_export_project,
    )

    log = _ThreadLog()
    monkeypatch.setattr(
        export_mod,
        "build_export_zip",
        log.wrap("export_zip", export_mod.build_export_zip),
    )
    project_id, frame_ids = _seed_export_project(
        migrated_factory, settings_no_autostart
    )
    job_id = _enqueue_export(migrated_factory, project_id, frame_ids)
    queue = RenderQueue(
        settings_no_autostart, migrated_factory, jobs_executor=jobs_executor
    )
    await _drain(queue)

    assert _job_status(migrated_factory, job_id) == "done"
    assert log.calls["export_zip"] == [True]


async def test_backfill_scan_runs_on_the_jobs_executor(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
    jobs_executor: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _ThreadLog()
    monkeypatch.setattr(
        RenderQueue,
        "_backfill_candidates",
        log.wrap("candidates", RenderQueue._backfill_candidates),
    )
    queue = RenderQueue(
        settings_no_autostart,
        migrated_factory,
        encoder=FakeEncoder(),
        jobs_executor=jobs_executor,
    )
    assert await queue.backfill_codecs() == 0
    assert log.calls["candidates"] == [True]


async def test_post_render_copy_and_prunes_run_on_the_jobs_executor(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
    jobs_executor: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    log = _ThreadLog()
    monkeypatch.setattr(post_actions, "running_under_docker", lambda: False)
    monkeypatch.setattr(post_actions, "_export", log.wrap("copy", lambda *a: None))
    monkeypatch.setattr(post_actions, "_prune", log.wrap("prune", lambda *a, **k: None))
    monkeypatch.setattr(
        post_actions, "_auto_prune", log.wrap("auto_prune", lambda *a, **k: None)
    )
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_job(migrated_factory, project_id, kind="scheduled")
    output = tmp_path / "render.mp4"
    output.write_bytes(b"\x00")

    await post_actions.run_post_actions(
        settings_no_autostart,
        migrated_factory,
        job_id=job_id,
        output_path=output,
        action_specs=[
            {"type": "export", "destination": str(tmp_path / "out")},
            {"type": "prune", "keep": 3},
        ],
        kind="scheduled",
        executor=jobs_executor,
    )

    assert log.calls == {"copy": [True], "prune": [True], "auto_prune": [True]}


async def test_without_an_executor_long_jobs_use_the_default_executor(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``jobs_executor=None`` (tests, alternative wiring) keeps ``to_thread``."""
    log = _ThreadLog()
    monkeypatch.setattr(
        RenderQueue, "_prepare", log.wrap("prepare", RenderQueue._prepare)
    )
    project_id = _seed_project(migrated_factory, settings_no_autostart)
    job_id = _enqueue_job(migrated_factory, project_id)
    queue = RenderQueue(settings_no_autostart, migrated_factory, encoder=FakeEncoder())
    await _drain(queue)

    assert _job_status(migrated_factory, job_id) == "done"
    assert log.calls["prepare"] == [False]
