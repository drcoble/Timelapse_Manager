"""Pure unit tests for _backoff_delay.

Uses a seeded random.Random for full determinism. Tests: exponential growth,
cap, jitter bounds, zero-jitter case, and first-attempt value.
"""

from __future__ import annotations

import random

import pytest

from timelapse_manager.capture.supervisor import _backoff_delay

# A stable seed for determinism
_SEED = 42


def _rng() -> random.Random:
    return random.Random(_SEED)


# ---------------------------------------------------------------------------
# First attempt (attempt=1) — no exponent growth yet
# ---------------------------------------------------------------------------


class TestFirstAttempt:
    def test_first_attempt_base_value_no_jitter(self) -> None:
        # With jitter_fraction=0 the result is exactly base * 2**0 = base
        d = _backoff_delay(1, base=1.0, maximum=300.0, jitter_fraction=0.0, rng=_rng())
        assert d == pytest.approx(1.0, abs=1e-10)

    def test_first_attempt_within_jitter_band(self) -> None:
        rng = _rng()
        d = _backoff_delay(1, base=1.0, maximum=300.0, jitter_fraction=0.1, rng=rng)
        assert 0.9 <= d <= 1.1

    def test_first_attempt_with_large_base(self) -> None:
        d = _backoff_delay(1, base=5.0, maximum=300.0, jitter_fraction=0.0, rng=_rng())
        assert d == pytest.approx(5.0, abs=1e-10)


# ---------------------------------------------------------------------------
# Exponential growth
# ---------------------------------------------------------------------------


class TestExponentialGrowth:
    def test_attempt_2_doubles_base(self) -> None:
        # attempt=2: raw = base * 2**1 = 2
        d = _backoff_delay(2, base=1.0, maximum=300.0, jitter_fraction=0.0, rng=_rng())
        assert d == pytest.approx(2.0, abs=1e-10)

    def test_attempt_3_quadruples_base(self) -> None:
        d = _backoff_delay(3, base=1.0, maximum=300.0, jitter_fraction=0.0, rng=_rng())
        assert d == pytest.approx(4.0, abs=1e-10)

    def test_attempt_4_is_8x_base(self) -> None:
        d = _backoff_delay(4, base=1.0, maximum=300.0, jitter_fraction=0.0, rng=_rng())
        assert d == pytest.approx(8.0, abs=1e-10)

    def test_growth_is_exactly_doubling_each_step(self) -> None:
        base = 2.0
        prev = _backoff_delay(
            1, base=base, maximum=1e9, jitter_fraction=0.0, rng=_rng()
        )
        for attempt in range(2, 8):
            cur = _backoff_delay(
                attempt, base=base, maximum=1e9, jitter_fraction=0.0, rng=_rng()
            )
            assert cur == pytest.approx(prev * 2.0, rel=1e-10)
            prev = cur


# ---------------------------------------------------------------------------
# Cap / maximum
# ---------------------------------------------------------------------------


class TestCap:
    def test_delay_capped_at_maximum(self) -> None:
        # attempt=10: base*2**9 = 512 > maximum=300 => capped
        d = _backoff_delay(10, base=1.0, maximum=300.0, jitter_fraction=0.0, rng=_rng())
        assert d == pytest.approx(300.0, abs=1e-10)

    def test_delay_never_exceeds_maximum_with_jitter(self) -> None:
        for attempt in range(1, 20):
            d = _backoff_delay(
                attempt,
                base=1.0,
                maximum=300.0,
                jitter_fraction=0.5,
                rng=random.Random(attempt),
            )
            assert d <= 300.0 * 1.5  # maximum * (1 + jitter_fraction)

    def test_delay_never_negative(self) -> None:
        for attempt in range(1, 15):
            d = _backoff_delay(
                attempt,
                base=1.0,
                maximum=300.0,
                jitter_fraction=0.9,
                rng=random.Random(attempt),
            )
            assert d >= 0.0

    def test_high_attempt_saturates_at_cap(self) -> None:
        d = _backoff_delay(50, base=1.0, maximum=300.0, jitter_fraction=0.0, rng=_rng())
        assert d == pytest.approx(300.0, abs=1e-10)


# ---------------------------------------------------------------------------
# Jitter
# ---------------------------------------------------------------------------


class TestJitter:
    def test_seeded_rng_produces_deterministic_result(self) -> None:
        d1 = _backoff_delay(3, base=1.0, maximum=300.0, jitter_fraction=0.1, rng=_rng())
        d2 = _backoff_delay(3, base=1.0, maximum=300.0, jitter_fraction=0.1, rng=_rng())
        assert d1 == d2

    def test_different_seeds_produce_different_results(self) -> None:
        rng_a = random.Random(1)
        rng_b = random.Random(999)
        d1 = _backoff_delay(3, base=10.0, maximum=300.0, jitter_fraction=0.2, rng=rng_a)
        d2 = _backoff_delay(3, base=10.0, maximum=300.0, jitter_fraction=0.2, rng=rng_b)
        # Almost certainly different with a 20% jitter band
        # (could theoretically be equal, but vanishingly unlikely with these seeds)
        assert d1 != d2

    def test_jitter_stays_within_symmetric_band(self) -> None:
        # For a known pre-cap raw value, test every jitter outcome over many seeds
        base = 10.0
        maximum = 1e6
        jitter = 0.2
        expected_raw = base  # attempt=1 => 2**0 = 1
        for seed in range(200):
            d = _backoff_delay(
                1,
                base=base,
                maximum=maximum,
                jitter_fraction=jitter,
                rng=random.Random(seed),
            )
            assert expected_raw * (1 - jitter) <= d <= expected_raw * (1 + jitter), (
                f"seed={seed} produced d={d} outside [{expected_raw * (1 - jitter)}, "
                f"{expected_raw * (1 + jitter)}]"
            )

    def test_zero_jitter_fraction_is_deterministic_regardless_of_rng(self) -> None:
        for seed in range(10):
            d = _backoff_delay(
                2, base=1.0, maximum=300.0, jitter_fraction=0.0, rng=random.Random(seed)
            )
            assert d == pytest.approx(2.0, abs=1e-10)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_attempt_zero_treated_like_attempt_one(self) -> None:
        # exponent = max(0, attempt-1) so attempt=0 => exponent=0 => base*1
        d = _backoff_delay(0, base=5.0, maximum=300.0, jitter_fraction=0.0, rng=_rng())
        assert d == pytest.approx(5.0, abs=1e-10)

    def test_maximum_less_than_base_caps_immediately(self) -> None:
        d = _backoff_delay(1, base=100.0, maximum=10.0, jitter_fraction=0.0, rng=_rng())
        assert d == pytest.approx(10.0, abs=1e-10)

    def test_base_zero_yields_zero(self) -> None:
        d = _backoff_delay(5, base=0.0, maximum=300.0, jitter_fraction=0.5, rng=_rng())
        assert d == 0.0
