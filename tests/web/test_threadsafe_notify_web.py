"""A sync route's post-commit notify reaches a running supervisor (#66 L5).

``POST /settings/camera-credentials`` is a ``def`` route, so FastAPI runs it in
the threadpool and its ``run_effects`` call fires ``notify_reconcile`` from a
worker thread. ``asyncio.Event`` is not thread-safe, so the notify must hand
the ``set`` to the app's event loop rather than run it on the worker thread.

The test records the thread every ``set`` of the reconcile wake-up runs on and
asserts it is the loop's. That is deterministic and, unlike asyncio debug mode
(used by the unit tests in ``tests/unit/test_threadsafe_notify.py``), a
regression cannot wedge the client's loop and hang the session at teardown.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.capture.supervisor import CaptureSupervisor
from timelapse_manager.runtime import get_context

#: Hang guard only: a correct wake-up arrives within a few loop iterations.
_HANG_GUARD_SECONDS = 5.0


class _ThreadRecordingEvent(asyncio.Event):
    """An ``asyncio.Event`` that records the thread each ``set`` runs on."""

    def __init__(self) -> None:
        super().__init__()
        self.set_threads: list[int] = []

    def set(self) -> None:
        self.set_threads.append(threading.get_ident())
        super().set()


async def _stop_bounded(supervisor: CaptureSupervisor) -> None:
    """Stop ``supervisor``, failing rather than hanging if it wedged."""
    await asyncio.wait_for(supervisor.stop(), timeout=_HANG_GUARD_SECONDS)


def test_sync_route_notify_is_set_on_the_loop_and_wakes_reconcile(
    admin_client: TestClient,
) -> None:
    portal = admin_client.portal
    assert portal is not None, "the client must be inside its lifespan"
    loop_thread = portal.call(threading.get_ident)
    ctx = get_context()
    settings = ctx.settings.model_copy(
        update={
            "capture": ctx.settings.capture.model_copy(
                # Only the notification can cause a second pass.
                update={"reconcile_interval_seconds": 3600.0}
            )
        }
    )
    supervisor = CaptureSupervisor(settings, ctx.session_factory)
    wakeup = _ThreadRecordingEvent()
    supervisor._reconcile_wakeup = wakeup
    passes: list[None] = []
    first_pass = threading.Event()
    second_pass = threading.Event()

    async def counting_pass() -> None:
        passes.append(None)
        (first_pass if len(passes) == 1 else second_pass).set()

    csrf = csrf_of(admin_client, "/settings")
    previous_supervisor = ctx.capture_supervisor
    with patch.object(supervisor, "_reconcile_once", counting_pass):
        portal.call(supervisor.start)
        try:
            assert first_pass.wait(_HANG_GUARD_SECONDS)
            ctx.capture_supervisor = supervisor

            resp = admin_client.post(
                "/settings/camera-credentials",
                data={"csrf_token": csrf, "camera_defaults_enabled": "on"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )

            assert resp.status_code == 303
            assert second_pass.wait(_HANG_GUARD_SECONDS), "reconcile was not woken"
            assert wakeup.set_threads, "the route did not notify the supervisor"
            assert set(wakeup.set_threads) == {loop_thread}
        finally:
            ctx.capture_supervisor = previous_supervisor
            portal.call(_stop_bounded, supervisor)
