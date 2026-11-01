"""Unit tests for the create-time storage pre-flight estimator helpers."""

from __future__ import annotations

from timelapse_manager.storage import estimator as est
from timelapse_manager.storage.estimator import (
    DEFAULT_AVERAGE_FRAME_SIZE_BYTES as DEF,
)


def test_bytes_per_day_uses_default_frame_size() -> None:
    # 1440 captures/day (a 60 s interval) at the default frame size.
    assert est.estimate_create_time_bytes_per_day(1440) == int(1440 * DEF)


def test_bytes_per_day_one_anchor_is_one_frame() -> None:
    # #22: an anchor-only form plans one capture a day.
    assert est.estimate_create_time_bytes_per_day(1.0) == DEF


def test_bytes_per_day_nothing_planned_is_zero() -> None:
    assert est.estimate_create_time_bytes_per_day(None) == 0
    assert est.estimate_create_time_bytes_per_day(0) == 0
    assert est.estimate_create_time_bytes_per_day(-5) == 0


def test_bytes_per_day_scales_with_captures_per_day() -> None:
    assert est.estimate_create_time_bytes_per_day(
        8640
    ) > est.estimate_create_time_bytes_per_day(864)


def test_preflight_level_thresholds() -> None:
    assert est.preflight_level(1_000_000, 100_000_000) == "ok"  # 100 days
    assert est.preflight_level(1_000_000, 30_000_000) == "caution"  # 30 days
    assert est.preflight_level(1_000_000, 10_000_000) == "danger"  # 10 days


def test_preflight_level_no_growth_is_ok() -> None:
    assert est.preflight_level(0, 0) == "ok"
