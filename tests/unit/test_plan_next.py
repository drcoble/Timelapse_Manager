"""Pure unit tests for _plan_next — the supervisor's pure decision function.

All tests call _plan_next directly with explicit parameters. No async, no DB,
no real clock. Every branch in the function is exercised.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from timelapse_manager.capture.supervisor import Decision, _plan_next

_UTC = UTC


def _now() -> datetime:
    return datetime(2026, 6, 1, 12, 0, 0, tzinfo=_UTC)


def _utc(**kw: int) -> datetime:
    base = _now()
    return base + timedelta(**kw)


# ---------------------------------------------------------------------------
# Gate closed: always waits, never captures
# ---------------------------------------------------------------------------


class TestGateClosed:
    def test_closed_far_away_waits_capped_at_max_idle(self) -> None:
        now = _now()
        next_change = now + timedelta(hours=8)
        d = _plan_next(
            now,
            is_open=False,
            next_change=next_change,
            interval=60.0,
            max_idle_sleep=300.0,
            last_capture=None,
            next_retry_at=None,
        )
        assert d.action == "wait"
        assert d.sleep_seconds == 300.0  # capped at max_idle

    def test_closed_near_waits_until_next_change(self) -> None:
        now = _now()
        next_change = now + timedelta(seconds=90)
        d = _plan_next(
            now,
            is_open=False,
            next_change=next_change,
            interval=60.0,
            max_idle_sleep=300.0,
            last_capture=None,
            next_retry_at=None,
        )
        assert d.action == "wait"
        assert d.sleep_seconds == pytest.approx(90.0, abs=0.001)

    def test_closed_with_no_next_change_waits_max_idle(self) -> None:
        now = _now()
        d = _plan_next(
            now,
            is_open=False,
            next_change=None,
            interval=60.0,
            max_idle_sleep=300.0,
            last_capture=None,
            next_retry_at=None,
        )
        assert d.action == "wait"
        assert d.sleep_seconds == 300.0

    def test_closed_with_pending_backoff_still_waits_not_captures(self) -> None:
        # Even if a retry is due now, a closed gate must not trigger capture.
        now = _now()
        past_retry = now - timedelta(seconds=5)
        next_change = now + timedelta(hours=1)
        d = _plan_next(
            now,
            is_open=False,
            next_change=next_change,
            interval=60.0,
            max_idle_sleep=300.0,
            last_capture=None,
            next_retry_at=past_retry,
        )
        assert d.action == "wait"

    def test_closed_returns_existing_next_retry_at(self) -> None:
        now = _now()
        future_retry = now + timedelta(seconds=60)
        d = _plan_next(
            now,
            is_open=False,
            next_change=None,
            interval=60.0,
            max_idle_sleep=300.0,
            last_capture=None,
            next_retry_at=future_retry,
        )
        assert d.next_retry_at == future_retry


# ---------------------------------------------------------------------------
# Gate open: capture decisions
# ---------------------------------------------------------------------------


class TestGateOpenCapture:
    def test_open_no_prior_capture_immediately_captures(self) -> None:
        now = _now()
        d = _plan_next(
            now,
            is_open=True,
            next_change=None,
            interval=60.0,
            max_idle_sleep=300.0,
            last_capture=None,
            next_retry_at=None,
        )
        assert d.action == "capture"
        assert d.sleep_seconds == 0.0
        assert d.next_retry_at is None

    def test_open_interval_elapsed_captures(self) -> None:
        now = _now()
        last_capture = now - timedelta(seconds=65)
        d = _plan_next(
            now,
            is_open=True,
            next_change=None,
            interval=60.0,
            max_idle_sleep=300.0,
            last_capture=last_capture,
            next_retry_at=None,
        )
        assert d.action == "capture"

    def test_open_interval_not_elapsed_waits_remaining(self) -> None:
        now = _now()
        last_capture = now - timedelta(seconds=30)
        d = _plan_next(
            now,
            is_open=True,
            next_change=None,
            interval=60.0,
            max_idle_sleep=300.0,
            last_capture=last_capture,
            next_retry_at=None,
        )
        assert d.action == "wait"
        assert d.sleep_seconds == pytest.approx(30.0, abs=0.001)

    def test_open_interval_not_elapsed_caps_at_max_idle(self) -> None:
        now = _now()
        # interval > max_idle: remaining=900s but cap=300s
        last_capture = now - timedelta(seconds=100)
        d = _plan_next(
            now,
            is_open=True,
            next_change=None,
            interval=1000.0,
            max_idle_sleep=300.0,
            last_capture=last_capture,
            next_retry_at=None,
        )
        assert d.action == "wait"
        assert d.sleep_seconds == 300.0

    def test_open_waits_until_next_change_when_shorter(self) -> None:
        now = _now()
        last_capture = now - timedelta(seconds=30)
        next_change = now + timedelta(seconds=20)
        d = _plan_next(
            now,
            is_open=True,
            next_change=next_change,
            interval=60.0,
            max_idle_sleep=300.0,
            last_capture=last_capture,
            next_retry_at=None,
        )
        assert d.action == "wait"
        assert d.sleep_seconds == pytest.approx(20.0, abs=0.001)


# ---------------------------------------------------------------------------
# Gate open: backoff retry logic
# ---------------------------------------------------------------------------


class TestGateOpenBackoff:
    def test_open_pending_backoff_not_due_waits_until_retry(self) -> None:
        now = _now()
        last_capture = now - timedelta(seconds=65)  # interval already elapsed
        next_retry_at = now + timedelta(seconds=45)
        d = _plan_next(
            now,
            is_open=True,
            next_change=None,
            interval=60.0,
            max_idle_sleep=300.0,
            last_capture=last_capture,
            next_retry_at=next_retry_at,
        )
        # Interval is up, but retry not due: wait for retry
        assert d.action == "wait"
        # sleep = min(45, max_idle=300) = 45
        assert d.sleep_seconds == pytest.approx(45.0, abs=0.001)

    def test_open_pending_backoff_due_now_captures(self) -> None:
        now = _now()
        # Retry due in the past (already past)
        past_retry = now - timedelta(seconds=5)
        d = _plan_next(
            now,
            is_open=True,
            next_change=None,
            interval=60.0,
            max_idle_sleep=300.0,
            last_capture=None,
            next_retry_at=past_retry,
        )
        assert d.action == "capture"
        assert d.next_retry_at is None

    def test_open_pending_backoff_cleared_on_capture_decision(self) -> None:
        now = _now()
        d = _plan_next(
            now,
            is_open=True,
            next_change=None,
            interval=60.0,
            max_idle_sleep=300.0,
            last_capture=None,
            next_retry_at=None,
        )
        assert d.action == "capture"
        assert d.next_retry_at is None

    def test_open_retry_waits_for_max_of_interval_and_retry(self) -> None:
        # Both interval remaining AND retry pending: wait for the larger
        now = _now()
        last_capture = now - timedelta(seconds=10)  # 50s left on 60s interval
        next_retry_at = now + timedelta(seconds=70)  # retry in 70s
        # max(50, 70) = 70 → min(70, next_change=inf, max_idle=300) = 70
        d = _plan_next(
            now,
            is_open=True,
            next_change=None,
            interval=60.0,
            max_idle_sleep=300.0,
            last_capture=last_capture,
            next_retry_at=next_retry_at,
        )
        assert d.action == "wait"
        assert d.sleep_seconds == pytest.approx(70.0, abs=0.001)

    def test_open_retry_sleep_capped_by_max_idle(self) -> None:
        now = _now()
        next_retry_at = now + timedelta(seconds=600)
        d = _plan_next(
            now,
            is_open=True,
            next_change=None,
            interval=60.0,
            max_idle_sleep=300.0,
            last_capture=None,
            next_retry_at=next_retry_at,
        )
        assert d.action == "wait"
        assert d.sleep_seconds == 300.0  # capped, not 600


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
        now = _now()
        retry = now + timedelta(seconds=30)
        d = Decision(action="wait", sleep_seconds=30.0, next_retry_at=retry)
        assert d.next_retry_at == retry


# ---------------------------------------------------------------------------
# Anchor-only (interval=None): the engine basis for "solar / scheduled times
# only" capture mode -- it must NEVER return a "capture" action from here, even
# with the gate wide open. Captures come solely from the anchor evaluation.
# ---------------------------------------------------------------------------


class TestAnchorOnlyNoInterval:
    def test_open_with_no_interval_never_captures(self) -> None:
        now = _now()
        d = _plan_next(
            now,
            is_open=True,  # gate open, but...
            next_change=None,
            interval=None,  # ...no recurring interval -> anchor-only
            max_idle_sleep=300.0,
            last_capture=None,  # even with no prior capture
            next_retry_at=None,
        )
        assert d.action == "wait"  # the load-bearing guarantee
        assert d.sleep_seconds == 300.0

    def test_open_no_interval_waits_until_next_anchor_wake(self) -> None:
        now = _now()
        wake = now + timedelta(seconds=120)
        d = _plan_next(
            now,
            is_open=True,
            next_change=None,
            interval=None,
            max_idle_sleep=300.0,
            last_capture=None,
            next_retry_at=None,
            next_wake=wake,
        )
        assert d.action == "wait"
        assert d.sleep_seconds == pytest.approx(120.0, abs=0.001)
