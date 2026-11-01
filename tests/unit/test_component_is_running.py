"""Tests for the is_running() liveness predicate on the background components.

Each long-lived component reports running only when it was started, not stopped,
and its loop task exists and has not finished. The predicate is exercised as a
pure function by poking the component's flags and a stand-in task, so the tests
need no event loop or database.
"""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import MagicMock

import pytest

from timelapse_manager.capture.supervisor import CaptureSupervisor
from timelapse_manager.config.settings import Settings
from timelapse_manager.monitoring.dispatcher import NotificationDispatcher
from timelapse_manager.render.queue import RenderQueue
from timelapse_manager.render.scheduler import RenderScheduler


class _FakeTask:
    """A stand-in for an asyncio task exposing only ``done()``."""

    def __init__(self, *, done: bool) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


# (build the component, the name of its loop-task attribute)
def _build_supervisor(settings: Settings) -> tuple[object, str]:
    return CaptureSupervisor(settings, MagicMock()), "_reconcile_task"


def _build_queue(settings: Settings) -> tuple[object, str]:
    return RenderQueue(settings, MagicMock()), "_loop_task"


def _build_scheduler(settings: Settings) -> tuple[object, str]:
    queue = RenderQueue(settings, MagicMock())
    return RenderScheduler(settings, MagicMock(), queue), "_task"


def _build_dispatcher(settings: Settings) -> tuple[object, str]:
    dispatcher = NotificationDispatcher(
        MagicMock(), channels=[], settings=settings.monitoring
    )
    return dispatcher, "_loop_task"


_BUILDERS: list[Callable[[Settings], tuple[object, str]]] = [
    _build_supervisor,
    _build_queue,
    _build_scheduler,
    _build_dispatcher,
]


@pytest.fixture(
    params=_BUILDERS, ids=["supervisor", "queue", "scheduler", "dispatcher"]
)
def component(request: pytest.FixtureRequest, settings: Settings) -> tuple[object, str]:
    return request.param(settings)


class TestIsRunning:
    """is_running() reflects the started/stopped/task-alive state."""

    def test_false_before_start(self, component: tuple[object, str]) -> None:
        obj, _ = component
        assert obj.is_running() is False  # type: ignore[attr-defined]

    def test_true_when_started_with_live_task(
        self, component: tuple[object, str]
    ) -> None:
        obj, task_attr = component
        obj._started = True  # type: ignore[attr-defined]
        setattr(obj, task_attr, _FakeTask(done=False))
        assert obj.is_running() is True  # type: ignore[attr-defined]

    def test_false_after_stop(self, component: tuple[object, str]) -> None:
        obj, task_attr = component
        obj._started = True  # type: ignore[attr-defined]
        setattr(obj, task_attr, _FakeTask(done=False))
        obj._stopped = True  # type: ignore[attr-defined]
        assert obj.is_running() is False  # type: ignore[attr-defined]

    def test_false_when_task_done(self, component: tuple[object, str]) -> None:
        """A finished (crashed) loop task reads as not running."""
        obj, task_attr = component
        obj._started = True  # type: ignore[attr-defined]
        setattr(obj, task_attr, _FakeTask(done=True))
        assert obj.is_running() is False  # type: ignore[attr-defined]

    def test_false_when_started_but_no_task(
        self, component: tuple[object, str]
    ) -> None:
        obj, task_attr = component
        obj._started = True  # type: ignore[attr-defined]
        setattr(obj, task_attr, None)
        assert obj.is_running() is False  # type: ignore[attr-defined]
