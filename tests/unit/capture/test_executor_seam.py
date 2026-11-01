"""The capture executor seam (``_run_blocking``).

Every blocking hop in ``capture/`` goes through one seam, so the executor that
capture work runs on is chosen in one place (master plan G5, WP-63-A). These
tests pin:

* no ``asyncio.to_thread`` call anywhere under ``capture/`` except the seam's
  own fallback in ``capture/executor.py``;
* with an injected executor, supervisor, one-shot and listener hops run on it;
* context variables reach the worker, as with ``asyncio.to_thread``.
"""

from __future__ import annotations

import ast
import asyncio
import contextvars
import threading
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import timelapse_manager.capture as capture_pkg
from tests.conftest import FakeAdapter
from tests.unit.test_campaign_bounds import (
    _make_settings,
    _permissive_disk_monitor,
    _seed_camera_and_project,
)
from timelapse_manager.capture.event_listener import EventListenerFactory
from timelapse_manager.capture.executor import run_blocking
from timelapse_manager.capture.one_shot import capture_one_now
from timelapse_manager.capture.supervisor import CaptureSupervisor, CaptureTarget

_CAPTURE_DIR = Path(capture_pkg.__file__).parent
# The one place allowed to call asyncio.to_thread: the seam's no-executor path.
_ALLOWED = {("executor.py", "run_blocking")}


def _to_thread_calls(path: Path) -> list[tuple[str, int]]:
    """Return ``(enclosing function, line)`` for each ``to_thread`` call."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, int]] = []

    def visit(node: ast.AST, enclosing: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = enclosing
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = child.name
            if isinstance(child, ast.Call):
                func = child.func
                attr = func.attr if isinstance(func, ast.Attribute) else None
                ident = func.id if isinstance(func, ast.Name) else None
                if "to_thread" in (attr, ident):
                    found.append((name, child.lineno))
            visit(child, name)

    visit(tree, "<module>")
    return found


def test_no_to_thread_outside_the_seam() -> None:
    """Every blocking hop in capture/ goes through ``_run_blocking``."""
    offenders = [
        f"{path.relative_to(_CAPTURE_DIR)}:{line} in {func}"
        for path in sorted(_CAPTURE_DIR.rglob("*.py"))
        for func, line in _to_thread_calls(path)
        if (path.name, func) not in _ALLOWED
    ]
    assert offenders == [], (
        "asyncio.to_thread in capture/ bypasses the executor seam; use "
        "CaptureSupervisor._run_blocking (or the listener's): " + ", ".join(offenders)
    )


def test_the_guard_finds_a_call(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import asyncio\nasync def f():\n    await asyncio.to_thread(print)\n"
    )
    assert _to_thread_calls(sample) == [("f", 3)]


_marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker", default="")


async def test_run_blocking_uses_the_executor_and_copies_context() -> None:
    _marker.set("from-the-loop")
    with ThreadPoolExecutor(thread_name_prefix="tlm-capture-test") as pool:
        name, marker = await run_blocking(
            pool, lambda: (threading.current_thread().name, _marker.get())
        )
    assert name.startswith("tlm-capture-test")
    assert marker == "from-the-loop"


async def test_run_blocking_without_executor_runs_off_the_loop() -> None:
    _marker.set("default-path")
    loop_thread = threading.get_ident()
    ident, marker = await run_blocking(
        None, lambda: (threading.get_ident(), _marker.get())
    )
    assert ident != loop_thread
    assert marker == "default-path"


async def test_run_blocking_passes_arguments_and_raises() -> None:
    def add(a: int, *, b: int) -> int:
        return a + b

    assert await run_blocking(None, add, 1, b=2) == 3

    def boom() -> None:
        raise ValueError("from the worker")

    try:
        await run_blocking(None, boom)
    except ValueError as exc:
        assert str(exc) == "from the worker"
    else:  # pragma: no cover - the call must raise
        raise AssertionError("expected ValueError")


async def test_supervisor_and_one_shot_hops_run_on_the_injected_executor(
    migrated_factory: Any, tmp_path: Path
) -> None:
    """A one-shot capture's blocking work (disk gate, loader, write, event) all
    runs on the supervisor's executor."""
    threads: list[str] = []
    ctx = _seed_camera_and_project(migrated_factory, tmp_path, name="exec")
    with ThreadPoolExecutor(thread_name_prefix="tlm-capture-test") as pool:
        supervisor = CaptureSupervisor(
            _make_settings(tmp_path),
            migrated_factory,
            disk_monitor=_permissive_disk_monitor(),
            executor=pool,
        )
        assert supervisor.executor is pool
        real = supervisor._run_blocking

        async def spy(fn: Any, /, *args: Any, **kwargs: Any) -> Any:
            def record_then_call() -> Any:
                threads.append(threading.current_thread().name)
                return fn(*args, **kwargs)

            return await real(record_then_call)

        target = CaptureTarget(
            project_id=ctx["project_id"],
            project_name="exec",
            camera_id=ctx["camera_id"],
            interval_seconds=60,
            storage_path=str(ctx["storage_path"]),
        )
        with (
            patch.object(supervisor, "_run_blocking", side_effect=spy),
            patch(
                "timelapse_manager.capture.one_shot.build_adapter",
                return_value=FakeAdapter(),
            ),
        ):
            written = await capture_one_now(supervisor, target, reason="anchor:clock")
        await supervisor.stop()

    assert written is not None
    # disk gate, merged loader, frame write, audit event.
    assert len(threads) == 4
    assert all(name.startswith("tlm-capture-test") for name in threads)


async def _parked() -> AsyncIterator[Any]:
    await asyncio.Event().wait()
    yield  # pragma: no cover


async def test_listener_subscription_load_runs_on_the_injected_executor() -> None:
    seen: list[str] = []

    def camera_loader(_camera_id: int) -> Any:
        seen.append(threading.current_thread().name)
        return SimpleNamespace(
            protocol="onvif",
            address="10.0.0.5",
            credentials=None,
            credentials_inherit_default=False,
            snapshot_uri=None,
            stream_uri=None,
            default_resolution=None,
        )

    adapter = MagicMock()
    adapter.open_event_source = MagicMock(side_effect=_parked)
    adapter.close = AsyncMock()
    target = CaptureTarget(
        project_id=1, project_name="p", camera_id=7, interval_seconds=None
    )
    with ThreadPoolExecutor(thread_name_prefix="tlm-capture-test") as pool:
        factory = EventListenerFactory(MagicMock(), camera_loader, executor=pool)
        with patch(
            "timelapse_manager.capture.event_listener.build_adapter",
            return_value=adapter,
        ):
            source = factory(target)
            consumer = asyncio.create_task(anext(source))
            for _ in range(200):
                if adapter.open_event_source.called:
                    break
                await asyncio.sleep(0.01)
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            await source.aclose()

    assert len(seen) == 1
    assert seen[0].startswith("tlm-capture-test")
