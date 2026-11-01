"""Unit tests for the login throttle's reserve-then-release path (#67 S7, S23).

Runs :class:`BruteForceThrottle` against a real migrated SQLite database with a
fake clock. Covers the atomic reservation under concurrency, the source and
username keys, the per-username delay schedule, the global ceiling, the
directory-account exemption, and the password-change key's separate namespace.
"""

from __future__ import annotations

import datetime
import threading

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from timelapse_manager.config.settings import AuthSettings
from timelapse_manager.db.models import LoginAttempt, User
from timelapse_manager.db.session import session_scope
from timelapse_manager.security.throttle import (
    LOCAL_BACKOFF_MAX_SECONDS,
    BruteForceThrottle,
    backoff_seconds,
    password_change_key,
    throttle_ip_key,
)

_BASE = datetime.datetime(2026, 1, 1, 12, 0, 0)


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
    global_max: int = 100,
    backoff_after: int = 3,
    clock: _Clock | None = None,
) -> BruteForceThrottle:
    return BruteForceThrottle(
        AuthSettings(
            throttle_max_failures=max_failures,
            throttle_window_seconds=300,
            throttle_global_max_failures=global_max,
            local_backoff_after_failures=backoff_after,
        ),
        session_factory=factory,
        now=clock or _Clock(),
    )


def _add_user(
    factory: sessionmaker,  # type: ignore[type-arg]
    username: str,
    *,
    source: str,
) -> int:
    with session_scope(factory) as db:
        user = User(username=username, auth_source=source, role="viewer")
        db.add(user)
        db.flush()
        return int(user.id)


def _rows(factory: sessionmaker) -> list[tuple[str, str, bool]]:  # type: ignore[type-arg]
    with session_scope(factory) as db:
        return [
            (row.ip, row.username, row.succeeded)
            for row in db.execute(select(LoginAttempt)).scalars()
        ]


class TestIpKey:
    def test_ipv4_is_canonical(self) -> None:
        assert throttle_ip_key("10.0.40.7") == "10.0.40.7"

    def test_ipv4_mapped_ipv6_folds_to_ipv4(self) -> None:
        assert throttle_ip_key("::ffff:10.0.40.7") == "10.0.40.7"

    def test_ipv6_is_grouped_by_its_64(self) -> None:
        first = throttle_ip_key("2001:db8:1:2::1")
        second = throttle_ip_key("2001:DB8:1:2:ffff:ffff:ffff:ffff")
        assert first == second == "2001:db8:1:2::/64"
        assert throttle_ip_key("2001:db8:1:3::1") != first

    @pytest.mark.parametrize("value", ["testclient", "unknown", ""])
    def test_non_ip_passes_through(self, value: str) -> None:
        assert throttle_ip_key(value) == value

    def test_password_change_key_never_parses_as_an_ip(self) -> None:
        key = password_change_key(7)
        assert key == "account:7"
        assert throttle_ip_key(key) == key

    def test_hosts_in_one_ipv6_64_share_a_budget(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        throttle = _throttle(migrated_factory, max_failures=3)
        for host in range(3):
            ip = throttle_ip_key(f"2001:db8:1:2::{host + 1:x}")
            assert throttle.try_reserve(ip=ip, username=f"u{host}") is not None
        other_host = throttle_ip_key("2001:db8:1:2::abcd")
        assert throttle.try_reserve(ip=other_host, username="u9") is None


class TestAtomicReserve:
    def test_parallel_reservations_grant_exactly_the_limit(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        """Ten simultaneous attempts from one source: exactly five get through."""
        throttle = _throttle(migrated_factory, max_failures=5)
        barrier = threading.Barrier(10)
        grants: list[bool] = []
        lock = threading.Lock()

        def attempt(n: int) -> None:
            barrier.wait()
            granted = throttle.try_reserve(ip="10.0.40.7", username=f"user{n}")
            with lock:
                grants.append(granted is not None)

        threads = [threading.Thread(target=attempt, args=(n,)) for n in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert len(grants) == 10
        assert grants.count(True) == 5
        assert len(_rows(migrated_factory)) == 5

    def test_refusal_records_nothing(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        throttle = _throttle(migrated_factory, max_failures=2)
        for _ in range(2):
            assert throttle.try_reserve(ip="10.0.40.7", username="a") is not None
        assert throttle.try_reserve(ip="10.0.40.7", username="a") is None
        assert len(_rows(migrated_factory)) == 2

    def test_reservation_counts_until_released(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        throttle = _throttle(migrated_factory, max_failures=1)
        reservation = throttle.try_reserve(ip="10.0.40.7", username="a")
        assert reservation is not None
        assert throttle.is_throttled(ip="10.0.40.7", username="a") is True
        throttle.release(reservation)
        assert throttle.is_throttled(ip="10.0.40.7", username="a") is False
        assert _rows(migrated_factory) == []

    def test_plain_release_forgives_nothing_else(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        """A non-counting release must not act as a success for the pair."""
        throttle = _throttle(migrated_factory, max_failures=3)
        throttle.record_failure(ip="10.0.40.7", username="alice")
        throttle.record_failure(ip="10.0.40.7", username="alice")
        reservation = throttle.try_reserve(ip="10.0.40.7", username="alice")
        assert reservation is not None
        throttle.release(reservation)
        assert all(not succeeded for _, _, succeeded in _rows(migrated_factory))
        throttle.record_failure(ip="10.0.40.7", username="alice")
        assert throttle.is_throttled(ip="10.0.40.7", username="alice") is True

    def test_successful_release_forgives_the_pair(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        clock = _Clock()
        throttle = _throttle(migrated_factory, max_failures=3, clock=clock)
        throttle.record_failure(ip="10.0.40.7", username="alice")
        throttle.record_failure(ip="10.0.40.7", username="alice")
        clock.advance(1)
        reservation = throttle.try_reserve(ip="10.0.40.7", username="alice")
        assert reservation is not None
        clock.advance(1)
        throttle.release(reservation, succeeded=True)
        assert throttle.is_throttled(ip="10.0.40.7", username="alice") is False
        assert ("10.0.40.7", "alice", True) in _rows(migrated_factory)


class TestDelaySchedule:
    @pytest.mark.parametrize(
        ("prior", "expected"),
        [(0, 0.0), (2, 0.0), (3, 1.0), (4, 2.0), (5, 4.0), (7, 16.0), (8, 30.0)],
    )
    def test_backoff_seconds(self, prior: int, expected: float) -> None:
        assert backoff_seconds(prior, after=3) == expected

    def test_huge_counts_stay_capped(self) -> None:
        assert backoff_seconds(10_000, after=3) == LOCAL_BACKOFF_MAX_SECONDS

    def test_fourth_attempt_on_one_name_is_delayed_from_any_source(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        """The delay follows the username across sources; the count excludes self."""
        throttle = _throttle(migrated_factory)
        delays = []
        for n in range(6):
            reservation = throttle.try_reserve(ip=f"10.0.40.{n}", username="site-admin")
            assert reservation is not None
            delays.append(reservation.delay_seconds)
        assert delays == [0.0, 0.0, 0.0, 1.0, 2.0, 4.0]

    def test_unknown_name_is_delayed_like_a_local_one(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        throttle = _throttle(migrated_factory)
        for n in range(3):
            throttle.try_reserve(ip=f"10.0.40.{n}", username="nobody")
        reservation = throttle.try_reserve(ip="10.0.40.9", username="nobody")
        assert reservation is not None
        assert reservation.delay_seconds == 1.0

    def test_case_whitespace_and_width_variants_share_one_counter(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        _add_user(migrated_factory, "admin", source="local")
        throttle = _throttle(migrated_factory)
        for n, spelling in enumerate(["Admin", " admin ", "ａｄｍｉｎ"]):
            reservation = throttle.try_reserve(ip=f"10.0.40.{n}", username=spelling)
            assert reservation is not None
            assert reservation.username == "admin"
        reservation = throttle.try_reserve(ip="10.0.40.9", username="ADMIN")
        assert reservation is not None
        assert reservation.delay_seconds == 1.0

    def test_a_success_forgives_only_its_own_sources_failures(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        clock = _Clock()
        throttle = _throttle(migrated_factory, clock=clock)
        for _ in range(4):
            throttle.try_reserve(ip="10.0.12.14", username="site-admin")
            clock.advance(1)
        owner = throttle.try_reserve(ip="10.0.12.14", username="site-admin")
        assert owner is not None
        assert owner.delay_seconds == 2.0
        clock.advance(1)
        throttle.release(owner, succeeded=True)
        clock.advance(1)
        after = throttle.try_reserve(ip="10.0.12.14", username="site-admin")
        assert after is not None
        assert after.delay_seconds == 0.0

    def test_a_success_elsewhere_does_not_drop_a_probers_delay(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        """No existence oracle: the owner's sign-in leaves a prober's delay as is.

        If a success from any source reset the name's delay, a third party
        probing the name would see it fall to zero when the owner signs in.
        """
        clock = _Clock()
        throttle = _throttle(migrated_factory, clock=clock)
        for _ in range(4):
            throttle.try_reserve(ip="10.0.40.50", username="site-admin")
            clock.advance(1)
        owner = throttle.try_reserve(ip="10.0.12.14", username="site-admin")
        assert owner is not None
        assert owner.delay_seconds == 2.0
        clock.advance(1)
        throttle.release(owner, succeeded=True)
        clock.advance(1)
        probe = throttle.try_reserve(ip="10.0.40.50", username="site-admin")
        assert probe is not None
        assert probe.delay_seconds == 2.0

    def test_no_backoff_requested_gives_no_delay(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        throttle = _throttle(migrated_factory)
        for n in range(5):
            throttle.try_reserve(ip=f"10.0.40.{n}", username="site-admin")
        reservation = throttle.try_reserve(
            ip=password_change_key(1), username="site-admin", backoff=False
        )
        assert reservation is not None
        assert reservation.delay_seconds == 0.0


class TestDelayRevealsNothingAboutTheName:
    """The delay depends on the failure history only, never on the account table.

    A local, a directory and an unknown name with the same history must get the
    same delay; otherwise response timing tells them apart (security review of
    WP-67-H, finding 1).
    """

    @pytest.mark.parametrize("failures", [0, 2, 3, 4, 6, 12])
    def test_same_history_same_delay_for_local_directory_and_unknown(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        failures: int,
    ) -> None:
        _add_user(migrated_factory, "site-admin", source="local")
        _add_user(migrated_factory, "alice.smith", source="ldap")
        throttle = _throttle(migrated_factory)
        names = ["site-admin", "alice.smith", "no-such-user"]
        for name in names:
            for n in range(failures):
                throttle.record_failure(ip=f"10.0.41.{n}", username=name)
        delays = {
            name: throttle.try_reserve(ip=f"10.0.42.{i}", username=name)
            for i, name in enumerate(names)
        }
        assert len({r.delay_seconds for r in delays.values() if r}) == 1
        assert all(r is not None for r in delays.values())

    def test_the_owner_directory_login_is_delayed_not_refused(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        _add_user(migrated_factory, "alice.smith", source="ldap")
        throttle = _throttle(migrated_factory)
        for n in range(20):
            throttle.record_failure(ip=f"10.0.41.{n}", username="alice.smith")
        owner = throttle.try_reserve(ip="10.0.12.14", username="Alice.Smith")
        assert owner is not None
        assert owner.delay_seconds == LOCAL_BACKOFF_MAX_SECONDS


class TestGlobalCeiling:
    def test_above_the_ceiling_every_name_is_delayed(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        _add_user(migrated_factory, "alice.smith", source="ldap")
        throttle = _throttle(migrated_factory, global_max=10)
        for n in range(10):
            throttle.record_failure(ip=f"10.0.41.{n}", username=f"spray{n}")
        directory = throttle.try_reserve(ip="10.0.12.14", username="alice.smith")
        fresh = throttle.try_reserve(ip="10.0.12.15", username="never-seen")
        assert directory is not None
        assert fresh is not None
        assert directory.delay_seconds == 1.0
        assert fresh.delay_seconds == 1.0

    def test_one_below_the_ceiling_adds_nothing(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        throttle = _throttle(migrated_factory, global_max=10)
        for n in range(9):
            throttle.record_failure(ip=f"10.0.41.{n}", username=f"spray{n}")
        fresh = throttle.try_reserve(ip="10.0.12.14", username="never-seen")
        assert fresh is not None
        assert fresh.delay_seconds == 0.0


class TestPasswordChangeNamespace:
    def test_password_change_failures_add_no_login_delay(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        """A user mistyping their current password does not slow their login."""
        user_id = _add_user(migrated_factory, "alice", source="local")
        throttle = _throttle(migrated_factory, global_max=3)
        for _ in range(5):
            throttle.try_reserve(
                ip=password_change_key(user_id), username="alice", backoff=False
            )
        login = throttle.try_reserve(ip="10.0.40.7", username="alice")
        assert login is not None
        assert login.delay_seconds == 0.0

    def test_password_change_key_has_its_own_budget(
        self,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        throttle = _throttle(migrated_factory, max_failures=5)
        key = password_change_key(3)
        for _ in range(5):
            assert throttle.try_reserve(ip=key, username="alice", backoff=False)
        assert throttle.try_reserve(ip=key, username="alice", backoff=False) is None
        # Another account's key, and a login source, are unaffected.
        assert throttle.try_reserve(ip=password_change_key(4), username="bob")
        assert throttle.try_reserve(ip="10.0.40.7", username="alice")
