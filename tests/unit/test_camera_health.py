"""Pure state-machine tests for per-camera capture health (#69, CAM-R6)."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from timelapse_manager.cameras.base import (
    AuthCaptureError,
    CameraMissingError,
    OtherCaptureError,
    PTZError,
    UnreachableCaptureError,
)
from timelapse_manager.capture.camera_health import (
    CameraHealthBook,
    HealthTransition,
    is_local_failure,
)


def _fail(book: CameraHealthBook, camera_id: int = 1, **kw: bool):
    return book.record_failure(camera_id, error="down", **kw)


class TestThreshold:
    @pytest.mark.parametrize("threshold", [1, 2, 3, 5])
    def test_offline_once_at_exactly_the_threshold(self, threshold: int) -> None:
        book = CameraHealthBook(threshold)
        kinds = [getattr(_fail(book), "kind", None) for _ in range(threshold + 3)]
        assert kinds == [None] * (threshold - 1) + ["offline"] + [None] * 3
        assert book.is_offline(1)
        assert book.health(1).consecutive_failures == threshold + 3

    def test_threshold_below_one_is_one(self) -> None:
        book = CameraHealthBook(0)
        assert _fail(book) == HealthTransition(
            kind="offline", camera_id=1, consecutive_failures=1, error="down"
        )

    def test_a_success_resets_the_count(self) -> None:
        book = CameraHealthBook(3)
        _fail(book)
        _fail(book)
        assert book.record_success(1) is None  # never alerted: no recovery
        assert _fail(book) is None
        assert _fail(book) is None
        assert _fail(book).kind == "offline"  # type: ignore[union-attr]


class TestRecovery:
    def test_first_success_after_offline_recovers_once(self) -> None:
        book = CameraHealthBook(1)
        _fail(book)
        assert book.record_success(1) == HealthTransition(kind="recovered", camera_id=1)
        assert book.record_success(1) is None
        assert not book.is_offline(1)
        assert book.health(1).last_error is None

    def test_cameras_are_independent(self) -> None:
        book = CameraHealthBook(1)
        _fail(book, camera_id=1)
        assert book.is_offline(1)
        assert not book.is_offline(2)
        assert book.record_success(2) is None
        assert book.is_offline(1)


class TestAuthFastPath:
    def test_auth_failure_raises_at_once(self) -> None:
        book = CameraHealthBook(3)
        transition = _fail(book, auth=True)
        assert transition is not None
        assert (transition.kind, transition.auth_failed) == ("offline", True)

    def test_auth_after_an_alert_raises_nothing_new(self) -> None:
        book = CameraHealthBook(1)
        _fail(book)
        assert _fail(book, auth=True) is None


class TestLocalFailures:
    def test_local_failures_do_not_count_and_report_once_per_streak(self) -> None:
        book = CameraHealthBook(1)
        first = _fail(book, local=True)
        assert first is not None and first.kind == "local"
        assert _fail(book, local=True) is None
        assert not book.is_offline(1)
        assert book.health(1).consecutive_failures == 0
        # A success ends the streak; the next local failure reports again.
        assert book.record_success(1) is None
        assert getattr(_fail(book, local=True), "kind", None) == "local"

    def test_local_failures_between_camera_failures_do_not_reset_them(self) -> None:
        book = CameraHealthBook(2)
        _fail(book)
        _fail(book, local=True)
        assert getattr(_fail(book), "kind", None) == "offline"


class TestSeedThenRearm:
    def test_seed_latches_so_the_first_success_recovers(self) -> None:
        book = CameraHealthBook(3)
        book.seed([4, 7])
        assert book.is_offline(4) and book.is_offline(7)
        assert getattr(book.record_success(4), "kind", None) == "recovered"
        # A seeded camera that is still down raises nothing new.
        assert _fail(book, camera_id=7) is None

    def test_rearm_after_a_lost_offline_event_re_raises(self) -> None:
        book = CameraHealthBook(1)
        transition = _fail(book)
        assert transition is not None
        book.rearm(transition)
        assert not book.is_offline(1)
        assert getattr(_fail(book), "kind", None) == "offline"

    def test_rearm_after_a_lost_recovery_re_recovers(self) -> None:
        book = CameraHealthBook(1)
        _fail(book)
        transition = book.record_success(1)
        assert transition is not None
        book.rearm(transition)
        assert getattr(book.record_success(1), "kind", None) == "recovered"

    def test_rearm_after_a_lost_local_warning_re_reports(self) -> None:
        book = CameraHealthBook(1)
        transition = _fail(book, local=True)
        assert transition is not None
        book.rearm(transition)
        assert getattr(_fail(book, local=True), "kind", None) == "local"

    def test_health_returns_a_copy(self) -> None:
        book = CameraHealthBook(1)
        snapshot = book.health(1)
        snapshot.offline_alerted = True
        assert not book.is_offline(1)


class TestIsLocalFailure:
    @pytest.mark.parametrize(
        ("exc", "local"),
        [
            (UnreachableCaptureError("down"), False),
            (AuthCaptureError("401"), False),
            (RuntimeError("boom"), False),
            (OtherCaptureError("pool timeout", local=True), True),
            (CameraMissingError(3), True),
            (OperationalError("UPDATE", {}, Exception("database is locked")), True),
        ],
    )
    def test_classification(self, exc: BaseException, local: bool) -> None:
        assert is_local_failure(exc) is local

    def test_camera_missing_error_keeps_its_message(self) -> None:
        exc = CameraMissingError(3)
        assert isinstance(exc, RuntimeError)
        assert str(exc) == "camera 3 no longer exists"

    def test_local_survives_the_ptz_re_raise_shape(self) -> None:
        exc = PTZError("busy", local=True)
        again = type(exc)(f"PTZ positioning failed: {exc}", local=exc.local)
        assert is_local_failure(again)


class TestListenerTrack:
    """The event-listener track never touches the offline latch (#69 CAM-R7)."""

    def test_alert_at_exactly_the_threshold_once(self) -> None:
        book = CameraHealthBook(3)
        kinds = [
            getattr(
                book.record_listener_failure(1, 10, error="no events"), "kind", None
            )
            for _ in range(5)
        ]
        assert kinds == [None, None, "events_unavailable", None, None]
        assert book.is_events_unavailable(1)

    def test_listener_failures_never_mark_the_camera_offline(self) -> None:
        book = CameraHealthBook(1)
        book.record_listener_failure(1, 10, error="no events")
        assert not book.is_offline(1)
        health = book.health(1)
        assert health.consecutive_failures == 0
        assert health.project_streaks == {}
        assert health.listener_streaks == {10: 1}

    def test_capture_failures_do_not_raise_the_events_alert(self) -> None:
        book = CameraHealthBook(1)
        assert _fail(book) is not None
        assert not book.is_events_unavailable(1)

    def test_subscribe_resolves_the_alert(self) -> None:
        book = CameraHealthBook(1)
        book.record_listener_failure(1, 10, error="x")
        resolved = book.record_listener_success(1, 10)
        assert resolved == HealthTransition(kind="events_available", camera_id=1)
        assert not book.is_events_unavailable(1)
        assert book.record_listener_success(1, 10) is None

    def test_a_subscribe_clears_the_streak_before_the_alert(self) -> None:
        book = CameraHealthBook(3)
        book.record_listener_failure(1, 10, error="x")
        book.record_listener_failure(1, 10, error="x")
        assert book.record_listener_success(1, 10) is None
        assert book.record_listener_failure(1, 10, error="x") is None

    def test_one_alert_for_projects_sharing_a_camera(self) -> None:
        book = CameraHealthBook(2)
        kinds = []
        for _ in range(2):
            for project in (10, 11):
                t = book.record_listener_failure(1, project, error="x")
                kinds.append(getattr(t, "kind", None))
        assert kinds.count("events_unavailable") == 1

    def test_alert_stands_while_another_listener_still_fails(self) -> None:
        book = CameraHealthBook(1)
        book.record_listener_failure(1, 10, error="x")
        book.record_listener_failure(1, 11, error="x")
        assert book.record_listener_success(1, 10) is None
        assert book.is_events_unavailable(1)
        assert book.record_listener_success(1, 11) is not None

    def test_retiring_the_last_listener_resolves_the_alert(self) -> None:
        book = CameraHealthBook(1)
        book.record_listener_failure(1, 10, error="x")
        book.forget_listener(1, 10)
        assert book.resolve_events(1) == HealthTransition(
            kind="events_available", camera_id=1
        )
        assert book.resolve_events(1) is None

    def test_seed_and_rearm(self) -> None:
        book = CameraHealthBook(1)
        book.seed_events([1])
        assert book.is_events_unavailable(1)
        resolved = book.record_listener_success(1, 10)
        assert resolved is not None
        book.rearm(resolved)
        assert book.is_events_unavailable(1)
        raised_again = CameraHealthBook(1)
        raised = raised_again.record_listener_failure(1, 10, error="x")
        assert raised is not None
        raised_again.rearm(raised)
        assert not raised_again.is_events_unavailable(1)
