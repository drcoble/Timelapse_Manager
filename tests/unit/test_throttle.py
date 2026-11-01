"""Unit tests for the brute-force login throttle.

The throttle is backed by the ``login_attempt`` table, so these tests run it
against a real migrated SQLite database (``migrated_factory``) and inject a fake
clock to drive the sliding window deterministically.
"""

from __future__ import annotations

import datetime

from sqlalchemy.orm import sessionmaker

from timelapse_manager.config.settings import AuthSettings
from timelapse_manager.security.throttle import BruteForceThrottle

_BASE = datetime.datetime(2026, 1, 1, 12, 0, 0)


def _settings(max_failures: int = 5, window_seconds: int = 300) -> AuthSettings:
    return AuthSettings(
        throttle_max_failures=max_failures,
        throttle_window_seconds=window_seconds,
    )


class _Clock:
    """A mutable callable returning a naive-UTC datetime the throttle can read."""

    def __init__(self, start: datetime.datetime = _BASE) -> None:
        self.now = start

    def __call__(self) -> datetime.datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += datetime.timedelta(seconds=seconds)


def _throttle(
    factory: sessionmaker,  # type: ignore[type-arg]
    *,
    max_failures: int = 5,
    window_seconds: int = 300,
    clock: _Clock | None = None,
) -> BruteForceThrottle:
    return BruteForceThrottle(
        _settings(max_failures=max_failures, window_seconds=window_seconds),
        session_factory=factory,
        now=clock or _Clock(),
    )


class TestBruteForceThrottleTrigger:
    def test_not_throttled_before_any_failures(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        throttle = _throttle(migrated_factory)
        assert throttle.is_throttled(ip="1.2.3.4", username="alice") is False

    def test_throttled_after_max_failures_from_same_ip(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        throttle = _throttle(migrated_factory, max_failures=3)
        ip = "10.0.0.1"
        for _ in range(3):
            throttle.record_failure(ip=ip, username="alice")
        assert throttle.is_throttled(ip=ip, username="alice") is True

    def test_not_throttled_one_below_limit(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        throttle = _throttle(migrated_factory, max_failures=5)
        ip = "10.0.0.2"
        for _ in range(4):
            throttle.record_failure(ip=ip, username="alice")
        assert throttle.is_throttled(ip=ip, username="alice") is False

    def test_username_failures_from_many_ips_do_not_lock_out_a_clean_ip(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        # Failing one username from many distinct IPs must NOT lock that username
        # out for an innocent client on a clean IP: a per-username lockout would
        # let anyone deny a victim access and would confirm the account exists.
        throttle = _throttle(migrated_factory, max_failures=3)
        username = "bob"
        for i in range(6):
            throttle.record_failure(ip=f"10.0.0.{i}", username=username)
        assert throttle.is_throttled(ip="10.0.0.99", username=username) is False

    def test_failures_outside_window_do_not_count(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        clock = _Clock()
        throttle = _throttle(
            migrated_factory, max_failures=3, window_seconds=60, clock=clock
        )
        ip = "10.0.0.3"
        for _ in range(3):
            throttle.record_failure(ip=ip, username="charlie")
        # Advance the clock past the window; the old failures should age out.
        clock.advance(61)
        assert throttle.is_throttled(ip=ip, username="charlie") is False


class TestBruteForceThrottleSuccessReset:
    def test_success_clears_ip_counter(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        clock = _Clock()
        throttle = _throttle(migrated_factory, max_failures=3, clock=clock)
        ip = "10.0.0.4"
        for _ in range(3):
            throttle.record_failure(ip=ip, username="dave")
        assert throttle.is_throttled(ip=ip, username="dave") is True
        clock.advance(1)
        throttle.record_success(ip=ip, username="dave")
        assert throttle.is_throttled(ip=ip, username="dave") is False

    def test_success_for_one_account_does_not_reset_spray_on_others(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        # An attacker on one IP sprays several usernames to the ceiling, then logs
        # in successfully to an account they legitimately hold. That success must
        # forgive only THAT account -- it must not wipe the IP's budget and let the
        # spray continue against the other usernames.
        clock = _Clock()
        throttle = _throttle(migrated_factory, max_failures=3, clock=clock)
        ip = "10.0.0.7"
        for i in range(3):
            throttle.record_failure(ip=ip, username=f"victim{i}")
        assert throttle.is_throttled(ip=ip, username="victim0") is True
        clock.advance(1)
        throttle.record_success(ip=ip, username="owned-account")
        # The failures against victim0..2 are untouched: the IP stays throttled.
        assert throttle.is_throttled(ip=ip, username="victim0") is True

    def test_success_for_the_same_account_forgives_its_failures(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        # A genuine login to the same account clears that account's own earlier
        # typos so a legitimate user is not penalised for them.
        clock = _Clock()
        throttle = _throttle(migrated_factory, max_failures=3, clock=clock)
        ip = "10.0.0.8"
        for _ in range(3):
            throttle.record_failure(ip=ip, username="frank")
        assert throttle.is_throttled(ip=ip, username="frank") is True
        clock.advance(1)
        throttle.record_success(ip=ip, username="frank")
        assert throttle.is_throttled(ip=ip, username="frank") is False

    def test_new_failures_after_success_re_accumulate(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        clock = _Clock()
        throttle = _throttle(migrated_factory, max_failures=3, clock=clock)
        ip = "10.0.0.8"
        for _ in range(3):
            throttle.record_failure(ip=ip, username="heidi")
        clock.advance(1)
        throttle.record_success(ip=ip, username="heidi")
        assert throttle.is_throttled(ip=ip, username="heidi") is False
        # Failures recorded after the success count afresh.
        for _ in range(3):
            clock.advance(1)
            throttle.record_failure(ip=ip, username="heidi")
        assert throttle.is_throttled(ip=ip, username="heidi") is True


class TestBruteForceThrottlePersistence:
    def test_count_survives_throttle_object_rebuild(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        """A restart (new throttle object, same DB) still sees the failures.

        This is the whole point of persisting to the table: the count cannot be
        reset by discarding in-process state.
        """
        ip = "10.0.0.9"
        first = _throttle(migrated_factory, max_failures=3)
        for _ in range(3):
            first.record_failure(ip=ip, username="ivan")

        # Simulate a process restart: a brand-new throttle reading the same DB.
        second = _throttle(migrated_factory, max_failures=3)
        assert second.is_throttled(ip=ip, username="ivan") is True

    def test_success_persists_across_rebuild(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        clock = _Clock()
        ip = "10.0.0.10"
        first = _throttle(migrated_factory, max_failures=3, clock=clock)
        for _ in range(3):
            first.record_failure(ip=ip, username="judy")
        clock.advance(1)
        first.record_success(ip=ip, username="judy")

        second = _throttle(migrated_factory, max_failures=3, clock=clock)
        assert second.is_throttled(ip=ip, username="judy") is False


class TestBruteForceThrottleNonEnumerating:
    def test_unknown_and_known_username_produce_same_throttle_path(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        # Failures are recorded against the submitted username string whether or
        # not the account exists -- so both paths count identically.
        max_fail = 3
        throttle = _throttle(migrated_factory, max_failures=max_fail)
        ip = "10.0.0.5"
        for _ in range(max_fail):
            throttle.record_failure(ip=ip, username="no-such-user-at-all")
        # Both real and nonexistent usernames are throttled from this IP.
        assert throttle.is_throttled(ip=ip, username="no-such-user-at-all") is True
        # The IP counter is also saturated, so any username from this IP is blocked.
        assert throttle.is_throttled(ip=ip, username="real-admin-account") is True

    def test_no_hard_lockout_for_valid_username_only(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        # Failures against a specific username do NOT create a permanent lockout;
        # after the failure budget ages out the username can attempt again.
        clock = _Clock()
        throttle = _throttle(
            migrated_factory, max_failures=3, window_seconds=60, clock=clock
        )
        username = "frank"
        ip = "10.0.0.6"
        for _ in range(3):
            throttle.record_failure(ip=ip, username=username)
        clock.advance(61)
        assert throttle.is_throttled(ip=ip, username=username) is False

    def test_valid_login_after_previous_failure_resets_counters(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        clock = _Clock()
        throttle = _throttle(migrated_factory, max_failures=5, clock=clock)
        ip = "10.0.0.7"
        username = "grace"
        throttle.record_failure(ip=ip, username=username)
        clock.advance(1)
        throttle.record_success(ip=ip, username=username)
        assert throttle.is_throttled(ip=ip, username=username) is False
