"""Post-commit notifications are safe from any thread (#66 L5).

``CaptureSupervisor.notify_reconcile`` and ``RenderQueue.notify`` are called by
``services._base.run_effects``, which sync (threadpool) request handlers run.
``asyncio.Event`` is not thread-safe: setting it from a worker thread schedules
the waiter's wake-up with a non-thread-safe ``call_soon`` that does not wake a
loop parked in its selector, and can corrupt the loop's ready queue. Both
components therefore marshal the notification onto the loop they were started
on.

The regression tests are deterministic, not timing-based:

* the loop runs in debug mode, where a cross-thread ``call_soon`` raises
  ``RuntimeError`` in the calling thread (captured and asserted absent);
* the periodic interval is an hour, so the second pass can only come from the
  notification; the ``wait_for`` bound is a hang guard, not the assertion.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session, sessionmaker

from timelapse_manager.capture.supervisor import CaptureSupervisor
from timelapse_manager.config.settings import Settings
from timelapse_manager.render.queue import RenderQueue

#: Long enough that a periodic tick can never stand in for the notification.
_NEVER_SECONDS = 3600.0
#: Hang guard only: a correct wake-up arrives within a few loop iterations.
_HANG_GUARD_SECONDS = 5.0


def _quiet_settings(settings: Settings) -> Settings:
    """Return ``settings`` with both periodic re-polls pushed out of reach."""
    return settings.model_copy(
        update={
            "capture": settings.capture.model_copy(
                update={"reconcile_interval_seconds": _NEVER_SECONDS}
            ),
            "render": settings.render.model_copy(
                update={"scheduler_check_interval_seconds": _NEVER_SECONDS}
            ),
        }
    )


def _call_in_thread(fn: Callable[[], None]) -> list[BaseException]:
    """Run ``fn`` on a fresh thread, join it, and return what it raised.

    ``threading.Thread`` only prints an exception from its target, so the
    target is wrapped to collect it for the assertion.
    """
    errors: list[BaseException] = []

    def target() -> None:
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            errors.append(exc)

    thread = threading.Thread(target=target, name="notify-from-worker")
    thread.start()
    thread.join(timeout=_HANG_GUARD_SECONDS)
    assert not thread.is_alive(), "the notifying thread hung"
    return errors


@contextmanager
def _loop_debug() -> Iterator[None]:
    """Turn on asyncio debug mode (cross-thread call_soon raises) for the block."""
    loop = asyncio.get_running_loop()
    previous = loop.get_debug()
    loop.set_debug(True)
    try:
        yield
    finally:
        loop.set_debug(previous)


async def _stop_bounded(component: CaptureSupervisor | RenderQueue) -> None:
    """Stop ``component``, failing (not hanging) if a lost wake-up wedged it.

    Without the fix a cross-thread ``Event.set`` can mark the waiter's future
    done without scheduling its task, so the task never resumes and ``stop``
    would wait on it forever.
    """
    await asyncio.wait_for(component.stop(), timeout=_HANG_GUARD_SECONDS)


class _PassCounter:
    """Stands in for a reconcile/drain pass: counts calls and signals each one."""

    def __init__(self, on_pass: Callable[[], None] | None = None) -> None:
        self.calls = 0
        self._on_pass = on_pass
        self._changed = asyncio.Event()

    async def __call__(self) -> None:
        if self._on_pass is not None:
            self._on_pass()
        self.calls += 1
        self._changed.set()

    async def wait_for_calls(self, n: int) -> None:
        """Return once at least ``n`` passes have run (bounded by the guard)."""

        async def wait() -> None:
            while self.calls < n:
                self._changed.clear()
                await self._changed.wait()

        await asyncio.wait_for(wait(), timeout=_HANG_GUARD_SECONDS)


# ---------------------------------------------------------------------------
# CaptureSupervisor.notify_reconcile
# ---------------------------------------------------------------------------


def _supervisor(
    settings: Settings,
    factory: sessionmaker[Session],
) -> CaptureSupervisor:
    return CaptureSupervisor(_quiet_settings(settings), factory)


class TestNotifyReconcileFromThread:
    async def test_worker_thread_notify_wakes_reconcile_loop(
        self,
        migrated_factory: sessionmaker[Session],
        settings_no_autostart: Settings,
    ) -> None:
        supervisor = _supervisor(settings_no_autostart, migrated_factory)
        passes = _PassCounter()
        with patch.object(supervisor, "_reconcile_once", passes), _loop_debug():
            await supervisor.start()
            try:
                # The loop clears its wake-up at the top of each pass, so wait
                # for the first pass to finish before notifying.
                await passes.wait_for_calls(1)

                errors = _call_in_thread(supervisor.notify_reconcile)

                assert errors == []
                await passes.wait_for_calls(2)
            finally:
                await _stop_bounded(supervisor)

    async def test_on_loop_notify_still_wakes_reconcile_loop(
        self,
        migrated_factory: sessionmaker[Session],
        settings_no_autostart: Settings,
    ) -> None:
        # The fast path: a caller already on the loop sets the event directly.
        supervisor = _supervisor(settings_no_autostart, migrated_factory)
        passes = _PassCounter()
        with patch.object(supervisor, "_reconcile_once", passes), _loop_debug():
            await supervisor.start()
            try:
                await passes.wait_for_calls(1)
                supervisor.notify_reconcile()
                await passes.wait_for_calls(2)
            finally:
                await _stop_bounded(supervisor)

    async def test_worker_thread_notify_before_start_is_a_safe_noop(
        self,
        migrated_factory: sessionmaker[Session],
        settings_no_autostart: Settings,
    ) -> None:
        supervisor = _supervisor(settings_no_autostart, migrated_factory)
        with _loop_debug():
            assert _call_in_thread(supervisor.notify_reconcile) == []
        await supervisor.stop()

    async def test_worker_thread_notify_after_stop_does_not_raise(
        self,
        migrated_factory: sessionmaker[Session],
        settings_no_autostart: Settings,
    ) -> None:
        supervisor = _supervisor(settings_no_autostart, migrated_factory)
        with patch.object(supervisor, "_reconcile_once", _PassCounter()):
            await supervisor.start()
            await supervisor.stop()
        with _loop_debug():
            assert _call_in_thread(supervisor.notify_reconcile) == []

    def test_notify_after_loop_closed_does_not_raise(
        self,
        migrated_factory: sessionmaker[Session],
        settings_no_autostart: Settings,
    ) -> None:
        # A threadpool request can still be finishing when shutdown closes the
        # loop; its post-commit notify must not raise "Event loop is closed".
        supervisor = _supervisor(settings_no_autostart, migrated_factory)

        async def run() -> None:
            with patch.object(supervisor, "_reconcile_once", _PassCounter()):
                await supervisor.start()
                await supervisor.stop()

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(run())
        finally:
            loop.close()

        assert _call_in_thread(supervisor.notify_reconcile) == []
        supervisor.notify_reconcile()  # and from this (loop-less) thread


# ---------------------------------------------------------------------------
# RenderQueue.notify
# ---------------------------------------------------------------------------


def _queue(settings: Settings, factory: sessionmaker[Session]) -> RenderQueue:
    return RenderQueue(_quiet_settings(settings), factory, encoder=MagicMock())


class TestRenderQueueNotifyFromThread:
    async def test_worker_thread_notify_wakes_drain_loop_and_clears_idle(
        self,
        migrated_factory: sessionmaker[Session],
        settings_no_autostart: Settings,
    ) -> None:
        queue = _queue(settings_no_autostart, migrated_factory)
        # Record whether the queue was still marked idle as each pass starts:
        # the drain loop re-sets ``_idle`` at the end of an empty pass, so the
        # clear is only observable at the start of the pass the notify caused.
        idle_at_pass_start: list[bool] = []
        passes = _PassCounter(lambda: idle_at_pass_start.append(queue._idle.is_set()))
        with (
            patch.object(queue, "_sweep_orphans", _PassCounter()),
            patch.object(queue, "_launch_ready_jobs", passes),
            _loop_debug(),
        ):
            await queue.start()
            try:
                await passes.wait_for_calls(1)
                await asyncio.wait_for(queue.wait_idle(), _HANG_GUARD_SECONDS)

                errors = _call_in_thread(queue.notify)

                assert errors == []
                await passes.wait_for_calls(2)
                assert idle_at_pass_start[1] is False
            finally:
                await _stop_bounded(queue)

    async def test_worker_thread_notify_before_start_is_a_safe_noop(
        self,
        migrated_factory: sessionmaker[Session],
        settings_no_autostart: Settings,
    ) -> None:
        queue = _queue(settings_no_autostart, migrated_factory)
        with _loop_debug():
            assert _call_in_thread(queue.notify) == []
        await queue.stop()

    async def test_worker_thread_notify_after_stop_does_not_raise(
        self,
        migrated_factory: sessionmaker[Session],
        settings_no_autostart: Settings,
    ) -> None:
        queue = _queue(settings_no_autostart, migrated_factory)
        with patch.object(queue, "_launch_ready_jobs", _PassCounter()):
            await queue.start()
            await queue.stop()
        with _loop_debug():
            assert _call_in_thread(queue.notify) == []

    def test_notify_after_loop_closed_does_not_raise(
        self,
        migrated_factory: sessionmaker[Session],
        settings_no_autostart: Settings,
    ) -> None:
        queue = _queue(settings_no_autostart, migrated_factory)

        async def run() -> None:
            with patch.object(queue, "_launch_ready_jobs", _PassCounter()):
                await queue.start()
                await queue.stop()

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(run())
        finally:
            loop.close()

        assert _call_in_thread(queue.notify) == []
        queue.notify()
