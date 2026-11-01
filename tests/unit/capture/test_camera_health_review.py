"""Camera-health fixes from the WP-69-C review (#130, #131, #132, #133, #135).

#130 and #133 are pinned in ``tests/unit/test_supervisor.py`` next to the tests
they changed (the four-anchor blip, the local-failure event level). This module
covers PTZ classification (#131), deleted cameras (#132) and write ordering
(#135).
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.unit.test_supervisor import (
    _health_events,
    _health_reasons,
    _health_supervisor,
    _interval_attempt,
    _ScriptedHealthAdapter,
    _seed_shared_camera,
)
from timelapse_manager.cameras.base import (
    AuthCaptureError,
    CameraMissingError,
    OtherCaptureError,
    PTZError,
    PTZUnsupportedError,
    TimeoutCaptureError,
    UnreachableCaptureError,
)
from timelapse_manager.capture.camera_health import (
    CameraHealthBook,
    classify_failure,
)
from timelapse_manager.capture.supervisor import CaptureState
from timelapse_manager.db.models import Event
from timelapse_manager.db.session import session_scope


def _ptz(cause: BaseException | None, *, rewrapped: bool = True) -> PTZError:
    """Build a PTZ failure the way the adapter and ``_apply_ptz`` chain it."""
    try:
        try:
            if cause is None:
                raise PTZError("camera rejected PTZ move: Error: preset not found")
            try:
                raise cause
            except type(cause) as inner:
                raise PTZError(f"PTZ move failed: {inner}") from inner
        except PTZError as adapter_error:
            if not rewrapped:
                raise
            raise PTZError(
                f"PTZ positioning to preset 'gate' failed: {adapter_error}"
            ) from adapter_error
    except PTZError as exc:
        return exc


def _pool_exhausted() -> OtherCaptureError:
    """The error ``http_get_image`` raises when the local pool is exhausted."""
    import httpx

    from timelapse_manager.cameras.http_jpeg import pool_exhausted_error

    try:
        try:
            raise httpx.PoolTimeout("no free connection")
        except httpx.PoolTimeout as pool:
            raise pool_exhausted_error("https://10.0.16.5/ptz") from pool
    except OtherCaptureError as exc:
        return exc


class TestClassifyFailure:
    @pytest.mark.parametrize(
        ("exc", "kind"),
        [
            (UnreachableCaptureError("down"), "camera"),
            (TimeoutCaptureError("slow"), "camera"),
            (OtherCaptureError("HTTP 500"), "camera"),
            (AuthCaptureError("401"), "auth"),
            (OtherCaptureError("pool", local=True), "local"),
            (CameraMissingError(7), "local"),
            (_ptz(UnreachableCaptureError("down")), "camera"),
            (_ptz(TimeoutCaptureError("slow")), "camera"),
            (_ptz(AuthCaptureError("401")), "auth"),
            (_ptz(AuthCaptureError("401"), rewrapped=False), "auth"),
            (_ptz(None), "project"),
            (_ptz(OtherCaptureError("HTTP 400 from ptz.cgi")), "project"),
            (_ptz(OtherCaptureError("pool", local=True)), "local"),
            (_ptz(_pool_exhausted()), "local"),
            (_ptz(_pool_exhausted(), rewrapped=False), "local"),
            (PTZUnsupportedError("no PTZ"), "project"),
        ],
    )
    def test_kind(self, exc: BaseException, kind: str) -> None:
        assert classify_failure(exc) == kind


class TestPerProjectStreaks:
    def test_one_failure_per_project_is_one_round(self) -> None:
        book = CameraHealthBook(3)
        for project_id in (1, 2, 3, 4):
            assert book.record_failure(9, error="x", project_id=project_id) is None
        assert not book.is_offline(9)
        assert book.health(9).consecutive_failures == 4

    def test_the_longest_streak_trips_the_alert(self) -> None:
        book = CameraHealthBook(3)
        book.record_failure(9, error="x", project_id=1)
        book.record_failure(9, error="x", project_id=2)
        book.record_failure(9, error="x", project_id=1)
        transition = book.record_failure(9, error="x", project_id=1)
        assert transition is not None
        assert (transition.kind, transition.consecutive_failures) == ("offline", 3)

    def test_a_success_on_any_project_resets_every_streak(self) -> None:
        book = CameraHealthBook(3)
        for _ in range(2):
            book.record_failure(9, error="x", project_id=1)
        book.record_success(9)
        assert book.record_failure(9, error="x", project_id=1) is None
        assert book.health(9).project_streaks == {1: 1}


class _PtzAdapter(_ScriptedHealthAdapter):
    """A fake whose PTZ move fails with ``error`` (capture would succeed)."""

    def __init__(self, error: BaseException) -> None:
        super().__init__(["ok"])
        self._error = error
        self.moves = 0

    async def move_to(self, **_kwargs: object) -> None:
        self.moves += 1
        raise self._error


async def _ptz_attempt(supervisor, target, state, error: BaseException) -> None:  # noqa: ANN001
    import random

    with patch(
        "timelapse_manager.capture.supervisor.build_adapter",
        return_value=_PtzAdapter(error),
    ):
        await supervisor._attempt_capture(target, state, random.Random(1))


class TestPtzFailuresAndCameraHealth:
    """#131: a PTZ failure counts by what it wraps."""

    async def test_a_rejected_move_is_a_project_error(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["gate"])
        target = replace(target, ptz_preset="gate")
        supervisor = _health_supervisor(migrated_factory, tmp_path, threshold=1)
        state = CaptureState(project_id=target.project_id, camera_id=target.camera_id)
        for _ in range(3):
            await _ptz_attempt(supervisor, target, state, PTZError("rejected"))
        await supervisor.stop()

        assert _health_events(migrated_factory, scope="camera") == []
        assert not supervisor._camera_health.is_offline(target.camera_id)
        # The project still shows its own failure and backs off.
        assert state.last_error is not None and "PTZ positioning" in state.last_error
        assert state.attempt_count == 3

    async def test_a_rejected_login_on_the_move_raises_the_auth_alert_at_once(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["gate"])
        target = replace(target, ptz_preset="gate")
        supervisor = _health_supervisor(migrated_factory, tmp_path, threshold=3)
        state = CaptureState(project_id=target.project_id, camera_id=target.camera_id)
        await _ptz_attempt(
            supervisor,
            target,
            state,
            _ptz(AuthCaptureError("401 Unauthorized"), rewrapped=False),
        )
        await supervisor.stop()

        (event,) = _health_events(migrated_factory, scope="camera")
        assert (event[2]["reason"], event[2]["auth_failed"]) == ("camera_offline", True)

    async def test_a_pool_timeout_on_the_vapix_move_stays_local(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        """WP-69-E's pool exhaustion inside a real VAPIX move: no alert."""
        from unittest.mock import AsyncMock

        from timelapse_manager.cameras.vapix import VapixAdapter

        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["gate"])
        target = replace(target, ptz_preset="gate")
        supervisor = _health_supervisor(migrated_factory, tmp_path, threshold=1)
        state = CaptureState(project_id=target.project_id, camera_id=target.camera_id)
        adapter = VapixAdapter(AsyncMock(), address="10.0.16.5")
        import random

        with (
            patch(
                "timelapse_manager.cameras.vapix.http_get_image",
                new=AsyncMock(side_effect=_pool_exhausted()),
            ),
            patch(
                "timelapse_manager.capture.supervisor.build_adapter",
                return_value=adapter,
            ),
        ):
            await supervisor._attempt_capture(target, state, random.Random(1))
        await supervisor.stop()

        assert _health_reasons(migrated_factory, scope="camera") == []
        system = _health_events(migrated_factory, scope="system")
        assert [m["reason"] for _, _, m in system] == ["capture_local_error"]

    async def test_an_unreachable_camera_on_the_move_counts_against_it(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["gate"])
        target = replace(target, ptz_preset="gate")
        supervisor = _health_supervisor(migrated_factory, tmp_path, threshold=1)
        state = CaptureState(project_id=target.project_id, camera_id=target.camera_id)
        await _ptz_attempt(
            supervisor,
            target,
            state,
            _ptz(UnreachableCaptureError("connect refused"), rewrapped=False),
        )
        await supervisor.stop()

        (event,) = _health_events(migrated_factory, scope="camera")
        assert (event[2]["reason"], event[2]["auth_failed"]) == (
            "camera_offline",
            False,
        )


def _offline_alert(factory, camera_id: int) -> int:  # noqa: ANN001
    """Insert an active camera-scoped ``camera_offline`` alert; return its id."""
    from datetime import UTC, datetime

    with session_scope(factory) as session:
        event = Event(
            scope="camera",
            scope_id=camera_id,
            level="warning",
            message=f"camera #{camera_id} is offline",
            timestamp=datetime.now(UTC).replace(tzinfo=None),
            event_metadata={"reason": "camera_offline", "camera_id": camera_id},
        )
        session.add(event)
        session.flush()
        return int(event.id)


def _cleared(factory, event_id: int) -> str | None:  # noqa: ANN001
    with session_scope(factory) as session:
        event = session.get(Event, event_id)
        assert event is not None
        return event.alert_clear_reason


class TestDeletedCameraAlerts:
    """#132: a deleted camera's offline alert does not stand forever."""

    def test_deleting_a_camera_clears_its_offline_alert(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        from timelapse_manager.db.models import Camera
        from timelapse_manager.security.principal import ensure_sentinel_admin
        from timelapse_manager.services._base import Actor
        from timelapse_manager.services.cameras import delete_camera

        with session_scope(migrated_factory) as session:
            doomed = Camera(name="doomed", address="10.0.16.9", protocol="vapix")
            kept = Camera(name="kept", address="10.0.16.10", protocol="vapix")
            session.add_all([doomed, kept])
            session.flush()
            doomed_id, kept_id = doomed.id, kept.id
        doomed_alert = _offline_alert(migrated_factory, doomed_id)
        kept_alert = _offline_alert(migrated_factory, kept_id)

        with session_scope(migrated_factory) as session:
            actor = Actor(user_id=ensure_sentinel_admin(session), role="admin")
            delete_camera(session, actor, doomed_id)

        assert _cleared(migrated_factory, doomed_alert) == "auto"
        assert _cleared(migrated_factory, kept_alert) is None

    async def test_start_clears_alerts_of_cameras_that_no_longer_exist(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["alpha"])
        live_alert = _offline_alert(migrated_factory, target.camera_id)
        orphan_alert = _offline_alert(migrated_factory, target.camera_id + 500)
        supervisor = _health_supervisor(migrated_factory, tmp_path)

        await supervisor._ensure_camera_health_synced()
        await supervisor.stop()

        assert _cleared(migrated_factory, orphan_alert) == "auto"
        assert _cleared(migrated_factory, live_alert) is None
        assert supervisor._camera_health.is_offline(target.camera_id)
        assert not supervisor._camera_health.is_offline(target.camera_id + 500)
        system = _health_events(migrated_factory, scope="system")
        assert [(lvl, m["reason"], m["cleared"]) for lvl, _, m in system] == [
            ("info", "camera_offline_alerts_orphaned", 1)
        ]

    async def test_an_offline_for_a_camera_deleted_in_flight_is_not_written(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["alpha"])
        gone = replace(target, camera_id=target.camera_id + 500)
        supervisor = _health_supervisor(migrated_factory, tmp_path, threshold=1)
        await supervisor._report_camera_outcome(gone, UnreachableCaptureError("x"))
        await supervisor.stop()
        assert _health_events(migrated_factory, scope="camera") == []


async def _until(predicate, timeout: float = 5.0) -> None:  # noqa: ANN001
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


class TestHealthEventOrder:
    """#135: one camera's health events commit in the order they were decided."""

    async def test_a_recovery_never_commits_before_its_offline(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["alpha"])
        supervisor = _health_supervisor(migrated_factory, tmp_path, threshold=1)
        book = supervisor._camera_health
        real = supervisor._write_camera_health_event
        offline_may_commit = threading.Event()

        def write(transition) -> None:  # noqa: ANN001
            if transition.kind == "offline":
                # The executor thread writing the offline is slow (a busy
                # database, say): it commits only when the test lets it.
                offline_may_commit.wait(10)
            real(transition)

        with patch.object(supervisor, "_write_camera_health_event", new=write):
            offline = asyncio.create_task(
                supervisor._report_camera_outcome(target, UnreachableCaptureError("x"))
            )
            await _until(lambda: book.is_offline(target.camera_id))
            recovered = asyncio.create_task(
                supervisor._report_camera_outcome(target, None)
            )
            await _until(lambda: not book.is_offline(target.camera_id))
            # Without the per-camera lock the recovery's thread commits here.
            await asyncio.sleep(0.2)
            offline_may_commit.set()
            await asyncio.gather(offline, recovered)
        await supervisor.stop()

        assert _health_reasons(migrated_factory, scope="camera") == [
            "camera_offline",
            "camera_recovered",
        ]

    async def test_a_transition_cancelled_before_its_write_is_rearmed(
        self, migrated_factory, tmp_path: Path
    ) -> None:
        (target,) = _seed_shared_camera(migrated_factory, tmp_path, ["alpha"])
        supervisor = _health_supervisor(migrated_factory, tmp_path, threshold=1)
        book = supervisor._camera_health
        lock = supervisor._camera_health_locks.setdefault(
            target.camera_id, asyncio.Lock()
        )
        await lock.acquire()  # another write on this camera is in progress
        waiting = asyncio.create_task(
            supervisor._report_camera_outcome(target, UnreachableCaptureError("x"))
        )
        await _until(lambda: book.is_offline(target.camera_id))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        lock.release()
        # Never written, so the latch is down and the next failure re-raises.
        assert not book.is_offline(target.camera_id)
        await _interval_attempt(
            supervisor,
            target,
            CaptureState(project_id=target.project_id, camera_id=target.camera_id),
            UnreachableCaptureError("x"),
        )
        await supervisor.stop()
        assert _health_reasons(migrated_factory, scope="camera") == ["camera_offline"]
