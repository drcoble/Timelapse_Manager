"""Unit tests for the brute-force login throttle."""

from __future__ import annotations

from timelapse_manager.config.settings import AuthSettings
from timelapse_manager.security.throttle import BruteForceThrottle


def _settings(max_failures: int = 5, window_seconds: int = 300) -> AuthSettings:
    return AuthSettings(
        throttle_max_failures=max_failures,
        throttle_window_seconds=window_seconds,
    )


def _fake_clock(start: float = 0.0) -> list[float]:
    """Return a mutable 1-element list that BruteForceThrottle can call."""
    return [start]


def _monotonic_from(state: list[float]) -> float:
    return state[0]


class TestBruteForceThrottleTrigger:
    def test_not_throttled_before_any_failures(self) -> None:
        throttle = BruteForceThrottle(_settings())
        assert throttle.is_throttled(ip="1.2.3.4", username="alice") is False

    def test_throttled_after_max_failures_from_same_ip(self) -> None:
        now = [0.0]
        throttle = BruteForceThrottle(
            _settings(max_failures=3), monotonic=lambda: now[0]
        )
        ip = "10.0.0.1"
        for _ in range(3):
            throttle.record_failure(ip=ip, username="alice")
        assert throttle.is_throttled(ip=ip, username="alice") is True

    def test_not_throttled_one_below_limit(self) -> None:
        now = [0.0]
        throttle = BruteForceThrottle(
            _settings(max_failures=5), monotonic=lambda: now[0]
        )
        ip = "10.0.0.2"
        for _ in range(4):
            throttle.record_failure(ip=ip, username="alice")
        assert throttle.is_throttled(ip=ip, username="alice") is False

    def test_throttled_after_max_failures_for_same_username(self) -> None:
        now = [0.0]
        throttle = BruteForceThrottle(
            _settings(max_failures=3), monotonic=lambda: now[0]
        )
        username = "bob"
        for i in range(3):
            throttle.record_failure(ip=f"10.0.0.{i}", username=username)
        # A fresh IP should still be blocked because the username counter fired.
        assert throttle.is_throttled(ip="10.0.0.99", username=username) is True

    def test_failures_outside_window_do_not_count(self) -> None:
        now = [0.0]
        throttle = BruteForceThrottle(
            _settings(max_failures=3, window_seconds=60), monotonic=lambda: now[0]
        )
        ip = "10.0.0.3"
        # Record 3 failures at t=0.
        for _ in range(3):
            throttle.record_failure(ip=ip, username="charlie")
        # Advance clock past the window.
        now[0] = 61.0
        # Should no longer be throttled.
        assert throttle.is_throttled(ip=ip, username="charlie") is False


class TestBruteForceThrottleSuccessReset:
    def test_success_clears_ip_counter(self) -> None:
        now = [0.0]
        throttle = BruteForceThrottle(
            _settings(max_failures=3), monotonic=lambda: now[0]
        )
        ip = "10.0.0.4"
        for _ in range(3):
            throttle.record_failure(ip=ip, username="dave")
        assert throttle.is_throttled(ip=ip, username="dave") is True
        throttle.record_success(ip=ip, username="dave")
        assert throttle.is_throttled(ip=ip, username="dave") is False

    def test_success_clears_username_counter(self) -> None:
        now = [0.0]
        throttle = BruteForceThrottle(
            _settings(max_failures=3), monotonic=lambda: now[0]
        )
        username = "eve"
        for i in range(3):
            throttle.record_failure(ip=f"10.0.0.{i}", username=username)
        throttle.record_success(ip="10.0.0.0", username=username)
        assert throttle.is_throttled(ip="10.0.0.99", username=username) is False


class TestBruteForceThrottleNonEnumerating:
    def test_unknown_and_known_username_produce_same_throttle_path(self) -> None:
        # Failures are recorded against the submitted username string whether or
        # not the account exists — so both paths count identically.
        now = [0.0]
        max_fail = 3
        throttle = BruteForceThrottle(
            _settings(max_failures=max_fail), monotonic=lambda: now[0]
        )
        ip = "10.0.0.5"
        for _ in range(max_fail):
            throttle.record_failure(ip=ip, username="no-such-user-at-all")
        # Both real and nonexistent usernames are throttled from this IP.
        assert throttle.is_throttled(ip=ip, username="no-such-user-at-all") is True
        # The IP counter is also saturated, so any username from this IP is blocked.
        assert throttle.is_throttled(ip=ip, username="real-admin-account") is True

    def test_no_hard_lockout_for_valid_username_only(self) -> None:
        # Failures against a specific username do NOT create a permanent lockout;
        # after clearing the failure budget the username can attempt again.
        now = [0.0]
        throttle = BruteForceThrottle(
            _settings(max_failures=3, window_seconds=60), monotonic=lambda: now[0]
        )
        username = "frank"
        ip = "10.0.0.6"
        for _ in range(3):
            throttle.record_failure(ip=ip, username=username)
        # Advance past window.
        now[0] = 61.0
        assert throttle.is_throttled(ip=ip, username=username) is False

    def test_valid_login_after_previous_failure_resets_counters(self) -> None:
        now = [0.0]
        throttle = BruteForceThrottle(
            _settings(max_failures=5), monotonic=lambda: now[0]
        )
        ip = "10.0.0.7"
        username = "grace"
        throttle.record_failure(ip=ip, username=username)
        throttle.record_success(ip=ip, username=username)
        assert throttle.is_throttled(ip=ip, username=username) is False
