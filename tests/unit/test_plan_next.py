"""Pure unit tests for _plan_next — the supervisor's pure decision function.

All tests call _plan_next directly with explicit parameters. No async, no DB,
no real clock. Every branch in the function is exercised.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from timelapse_manager.capture.supervisor import (
    Decision,
    _advance_due,
    _plan_next,
    _retry_instant,
)

_UTC = UTC


def _now() -> datetime:
    return datetime(2026, 6, 1, 12, 0, 0, tzinfo=_UTC)


def _utc(**kw: int) -> datetime:
    base = _now()
    return base + timedelta(**kw)


MONO = 1000.0  # an arbitrary monotonic reading; only differences matter


def _plan(
    *,
    is_open: bool = True,
    next_change: datetime | None = None,
    interval: float | None = 60.0,
    max_idle_sleep: float = 300.0,
    next_due: float | None = None,
    next_retry: float | None = None,
    next_wake: datetime | None = None,
) -> Decision:
    """Call ``_plan_next`` at wall ``_now()`` and monotonic ``MONO``.

    ``next_due`` and ``next_retry`` are offsets in seconds from ``MONO``.
    """
    return _plan_next(
        _now(),
        is_open=is_open,
        next_change=next_change,
        interval=interval,
        max_idle_sleep=max_idle_sleep,
        now_mono=MONO,
        next_due_mono=None if next_due is None else MONO + next_due,
        next_retry_mono=None if next_retry is None else MONO + next_retry,
        next_wake=next_wake,
    )


# ---------------------------------------------------------------------------
# Gate closed: always waits, never captures
# ---------------------------------------------------------------------------


class TestGateClosed:
    def test_closed_far_away_waits_capped_at_max_idle(self) -> None:
        d = _plan(is_open=False, next_change=_utc(hours=8))
        assert d.action == "wait"
        assert d.sleep_seconds == 300.0  # capped at max_idle

    def test_closed_near_waits_until_next_change(self) -> None:
        d = _plan(is_open=False, next_change=_utc(seconds=90))
        assert d.action == "wait"
        assert d.sleep_seconds == pytest.approx(90.0, abs=0.001)

    def test_closed_with_no_next_change_waits_max_idle(self) -> None:
        d = _plan(is_open=False)
        assert d.action == "wait"
        assert d.sleep_seconds == 300.0

    def test_closed_with_pending_backoff_still_waits_not_captures(self) -> None:
        # Even if a retry is due now, a closed gate must not trigger capture.
        d = _plan(is_open=False, next_change=_utc(hours=1), next_retry=-5.0)
        assert d.action == "wait"

    def test_closed_reports_a_pending_retry_as_wall_time(self) -> None:
        d = _plan(is_open=False, next_retry=60.0)
        assert d.next_retry_at == _utc(seconds=60)


# ---------------------------------------------------------------------------
# Gate open: capture decisions on the monotonic grid
# ---------------------------------------------------------------------------


class TestGateOpenCapture:
    def test_open_with_no_grid_yet_captures_immediately(self) -> None:
        d = _plan()
        assert d.action == "capture"
        assert d.sleep_seconds == 0.0
        assert d.next_retry_at is None

    def test_open_slot_reached_captures(self) -> None:
        assert _plan(next_due=-5.0).action == "capture"

    def test_open_slot_exactly_now_captures(self) -> None:
        assert _plan(next_due=0.0).action == "capture"

    def test_open_slot_ahead_waits_remaining(self) -> None:
        d = _plan(next_due=30.0)
        assert d.action == "wait"
        assert d.sleep_seconds == pytest.approx(30.0, abs=0.001)

    def test_open_slot_ahead_caps_at_max_idle(self) -> None:
        d = _plan(interval=1000.0, next_due=900.0)
        assert d.action == "wait"
        assert d.sleep_seconds == 300.0

    def test_open_waits_until_next_change_when_shorter(self) -> None:
        d = _plan(next_due=30.0, next_change=_utc(seconds=20))
        assert d.action == "wait"
        assert d.sleep_seconds == pytest.approx(20.0, abs=0.001)

    def test_wall_clock_is_not_consulted_for_the_grid(self) -> None:
        # The same monotonic inputs decide the same way at any wall time.
        for wall in (_utc(hours=-1), _utc(), _utc(hours=1)):
            d = _plan_next(
                wall,
                is_open=True,
                next_change=None,
                interval=60.0,
                max_idle_sleep=300.0,
                now_mono=MONO,
                next_due_mono=MONO + 30.0,
                next_retry_mono=None,
            )
            assert (d.action, d.sleep_seconds) == ("wait", 30.0)


# ---------------------------------------------------------------------------
# Gate open: backoff retry logic
# ---------------------------------------------------------------------------


class TestGateOpenBackoff:
    def test_retry_ahead_waits_until_the_retry(self) -> None:
        d = _plan(next_due=60.0, next_retry=45.0)
        assert d.action == "wait"
        assert d.sleep_seconds == pytest.approx(45.0, abs=0.001)
        assert d.next_retry_at == _utc(seconds=45)

    def test_retry_due_captures_before_the_next_slot(self) -> None:
        d = _plan(next_due=40.0, next_retry=-1.0)
        assert d.action == "capture"
        assert d.next_retry_at is None

    def test_retry_sleep_capped_by_max_idle(self) -> None:
        d = _plan(interval=1000.0, next_due=900.0, next_retry=600.0)
        assert d.action == "wait"
        assert d.sleep_seconds == 300.0  # capped, not 600


# ---------------------------------------------------------------------------
# Cadence: the grid does not drift, skips missed slots, and recovers within
# one interval (#69 CAM-R5)
# ---------------------------------------------------------------------------


class TestAdvanceDue:
    def test_next_slot_is_one_interval_after_the_slot_not_after_the_attempt(
        self,
    ) -> None:
        # Slot 1000, the capture finished 1.5 s later: the next slot is 1060.
        assert _advance_due(1000.0, 60.0, 1001.5) == 1060.0

    def test_no_cumulative_drift_over_100_cycles(self) -> None:
        due: float | None = 0.0
        for cycle in range(1, 101):
            # Wake exactly at the slot, spend 1.5 s capturing.
            now = cycle * 60.0 - 60.0 + 1.5
            due = _advance_due(due, 60.0, now)
            assert due == cycle * 60.0
        assert due == 6000.0

    def test_missed_slots_are_skipped_not_burst(self) -> None:
        # A 190 s outage across slots 60, 120, 180: one capture, next slot 240.
        assert _advance_due(60.0, 60.0, 250.0) == 300.0

    def test_exactly_on_a_slot_moves_to_the_following_one(self) -> None:
        assert _advance_due(0.0, 60.0, 60.0) == 120.0

    def test_no_grid_yet_starts_one_interval_out(self) -> None:
        assert _advance_due(None, 60.0, 500.0) == 560.0

    def test_a_retry_before_its_slot_leaves_the_slot_alone(self) -> None:
        assert _advance_due(1060.0, 60.0, 1030.0) == 1060.0


class TestRetryInstant:
    def test_short_backoff_is_used(self) -> None:
        assert _retry_instant(100.0, 5.0, 160.0) == 105.0

    def test_backoff_is_capped_at_the_next_slot(self) -> None:
        assert _retry_instant(100.0, 300.0, 160.0) == 160.0

    def test_no_grid_uses_the_backoff(self) -> None:
        assert _retry_instant(100.0, 300.0, None) == 400.0


class TestOutageRecovery:
    def test_first_success_after_recovery_is_within_one_interval(self) -> None:
        """Backoff grew to 300 s on a 60 s grid; the camera returns at t=130."""
        interval = 60.0
        due: float | None = 0.0
        now = 0.0
        recovered_at = 130.0
        captured_at: float | None = None
        retry: float | None = None
        for _ in range(50):
            d = _plan_next(
                _now(),
                is_open=True,
                next_change=None,
                interval=interval,
                max_idle_sleep=300.0,
                now_mono=now,
                next_due_mono=due,
                next_retry_mono=retry,
            )
            now += d.sleep_seconds
            if d.action == "wait":
                continue
            due = _advance_due(due, interval, now)
            if now >= recovered_at:
                captured_at = now
                break
            retry = _retry_instant(now, 300.0, due)  # saturated backoff
        assert captured_at is not None
        assert captured_at - recovered_at <= interval


# ---------------------------------------------------------------------------
# Decision is a NamedTuple — verify fields
# ---------------------------------------------------------------------------


class TestDecisionNamedTuple:
    def test_decision_has_expected_fields(self) -> None:
        d = Decision(action="capture", sleep_seconds=0.0, next_retry_at=None)
        assert d.action == "capture"
        assert d.sleep_seconds == 0.0
        assert d.next_retry_at is None

    def test_decision_wait_carries_retry_at(self) -> None:
        retry = _now() + timedelta(seconds=30)
        d = Decision(action="wait", sleep_seconds=30.0, next_retry_at=retry)
        assert d.next_retry_at == retry


# ---------------------------------------------------------------------------
# Anchor-only (interval=None): the engine basis for "solar / scheduled times
# only" capture mode -- it must NEVER return a "capture" action from here, even
# with the gate wide open. Captures come solely from the anchor evaluation.
# ---------------------------------------------------------------------------


class TestAnchorOnlyNoInterval:
    def test_open_with_no_interval_never_captures(self) -> None:
        d = _plan(interval=None)  # gate open, but no recurring interval
        assert d.action == "wait"  # the load-bearing guarantee
        assert d.sleep_seconds == 300.0

    def test_open_no_interval_waits_until_next_anchor_wake(self) -> None:
        d = _plan(interval=None, next_wake=_utc(seconds=120))
        assert d.action == "wait"
        assert d.sleep_seconds == pytest.approx(120.0, abs=0.001)
