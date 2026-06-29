"""Unit tests for the create-time storage pre-flight estimator helpers."""

from __future__ import annotations

from timelapse_manager.storage import estimator as est
from timelapse_manager.storage.estimator import (
    DEFAULT_AVERAGE_FRAME_SIZE_BYTES as DEF,
)


def test_bytes_per_day_uses_default_frame_size() -> None:
    # 60s interval -> 1440 frames/day at the default frame size.
    assert est.estimate_create_time_bytes_per_day(60) == int((86400 / 60) * DEF)


def test_bytes_per_day_nonpositive_interval_is_zero() -> None:
    assert est.estimate_create_time_bytes_per_day(0) == 0
    assert est.estimate_create_time_bytes_per_day(-5) == 0


def test_bytes_per_day_scales_inversely_with_interval() -> None:
    fast = est.estimate_create_time_bytes_per_day(10)
    slow = est.estimate_create_time_bytes_per_day(100)
    assert fast > slow


def test_preflight_level_thresholds() -> None:
    assert est.preflight_level(1_000_000, 100_000_000) == "ok"  # 100 days
    assert est.preflight_level(1_000_000, 30_000_000) == "caution"  # 30 days
    assert est.preflight_level(1_000_000, 10_000_000) == "danger"  # 10 days


def test_preflight_level_no_growth_is_ok() -> None:
    assert est.preflight_level(0, 0) == "ok"
