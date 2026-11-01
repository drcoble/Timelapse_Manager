"""Unit tests for the in-memory attempt limiter (camera admin read, #68).

A fake monotonic clock drives the sliding window deterministically.
"""

from __future__ import annotations

import pytest

from timelapse_manager.security.attempt_limiter import AttemptLimiter


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _limiter(clock: _Clock, *, max_attempts: int = 3) -> AttemptLimiter:
    return AttemptLimiter(max_attempts=max_attempts, window_seconds=60.0, now=clock)


class TestAttemptLimiter:
    def test_refuses_once_any_key_is_full(self) -> None:
        limiter = _limiter(_Clock())
        for _ in range(3):
            assert limiter.try_reserve([("device", "a"), ("user", 1)]) is not None
        # A fresh device, but the user key is full: refused, nothing recorded.
        assert limiter.try_reserve([("device", "b"), ("user", 1)]) is None
        assert limiter.count(("device", "b")) == 0
        assert limiter.count(("user", 1)) == 3

    def test_release_forgets_only_that_attempt(self) -> None:
        limiter = _limiter(_Clock())
        first = limiter.try_reserve(["k"])
        second = limiter.try_reserve(["k"])
        assert first is not None
        assert second is not None
        limiter.release(second)
        assert limiter.count("k") == 1
        limiter.release(second)  # idempotent
        assert limiter.count("k") == 1

    def test_reservations_age_out_of_the_window(self) -> None:
        clock = _Clock()
        limiter = _limiter(clock, max_attempts=1)
        assert limiter.try_reserve(["k"]) is not None
        assert limiter.try_reserve(["k"]) is None
        clock.now += 61
        assert limiter.try_reserve(["k"]) is not None

    def test_idle_keys_are_swept(self) -> None:
        clock = _Clock()
        limiter = _limiter(clock)
        limiter.try_reserve(["idle"])
        clock.now += 61
        limiter.try_reserve(["other"])
        assert "idle" not in limiter._hits  # noqa: SLF001

    def test_duplicate_keys_count_once(self) -> None:
        limiter = _limiter(_Clock())
        limiter.try_reserve(["k", "k"])
        assert limiter.count("k") == 1

    def test_clear_forgets_everything(self) -> None:
        limiter = _limiter(_Clock(), max_attempts=1)
        limiter.try_reserve(["k"])
        limiter.clear()
        assert limiter.try_reserve(["k"]) is not None

    @pytest.mark.parametrize(
        ("max_attempts", "window"), [(0, 60.0), (1, 0.0), (1, -1.0)]
    )
    def test_rejects_bad_configuration(self, max_attempts: int, window: float) -> None:
        with pytest.raises(ValueError):
            AttemptLimiter(max_attempts=max_attempts, window_seconds=window)

    def test_requires_a_key(self) -> None:
        with pytest.raises(ValueError):
            _limiter(_Clock()).try_reserve([])
