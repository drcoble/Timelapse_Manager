"""Unit tests for storage.monitor.DiskSpaceMonitor.

All probes are injected — no real disk access. The clock (``now``) is driven
explicitly so throttle behaviour is deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest  # noqa: F401 (used in pytest.raises)

from timelapse_manager.storage.monitor import DiskSpaceMonitor

_UTC = UTC


def _now(offset_seconds: float = 0.0) -> datetime:
    """Return a fixed aware-UTC instant, shifted by offset_seconds."""
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=_UTC)
    return base + timedelta(seconds=offset_seconds)


def _make_monitor(
    *,
    low_bytes: int = 1_000_000_000,
    low_pct: float = 5.0,
    resume_bytes: int = 2_000_000_000,
    resume_pct: float = 10.0,
    interval: float = 60.0,
    free_bytes: int = 10_000_000_000,
    total_bytes: int = 100_000_000_000,
) -> DiskSpaceMonitor:
    return DiskSpaceMonitor(
        low_watermark_bytes=low_bytes,
        low_watermark_percent=low_pct,
        resume_watermark_bytes=resume_bytes,
        resume_watermark_percent=resume_pct,
        check_interval_seconds=interval,
        get_free_bytes=lambda _p: free_bytes,
        get_total_bytes=lambda _p: total_bytes,
    )


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_raises_when_resume_bytes_below_low_bytes(self) -> None:
        with pytest.raises(ValueError, match="resume_watermark_bytes"):
            DiskSpaceMonitor(
                low_watermark_bytes=2_000_000_000,
                low_watermark_percent=5.0,
                resume_watermark_bytes=1_000_000_000,  # lower than low
                resume_watermark_percent=10.0,
                check_interval_seconds=60.0,
                get_free_bytes=lambda _p: 0,
                get_total_bytes=lambda _p: 0,
            )

    def test_raises_when_resume_percent_below_low_percent(self) -> None:
        with pytest.raises(ValueError, match="resume_watermark_percent"):
            DiskSpaceMonitor(
                low_watermark_bytes=1_000_000_000,
                low_watermark_percent=10.0,
                resume_watermark_bytes=2_000_000_000,
                resume_watermark_percent=5.0,  # lower than low
                check_interval_seconds=60.0,
                get_free_bytes=lambda _p: 0,
                get_total_bytes=lambda _p: 0,
            )

    def test_equal_watermarks_are_valid(self) -> None:
        # Equal low == resume is the minimum valid hysteresis band
        monitor = DiskSpaceMonitor(
            low_watermark_bytes=1_000_000_000,
            low_watermark_percent=5.0,
            resume_watermark_bytes=1_000_000_000,
            resume_watermark_percent=5.0,
            check_interval_seconds=60.0,
            get_free_bytes=lambda _p: 10_000_000_000,
            get_total_bytes=lambda _p: 100_000_000_000,
        )
        assert monitor is not None


# ---------------------------------------------------------------------------
# Ample space: always allowed
# ---------------------------------------------------------------------------


class TestAmpleSpace:
    def test_above_both_resume_watermarks_is_allowed(self, tmp_path: Path) -> None:
        # free = 50 GB, total = 100 GB => 50%; well above both resume floors
        monitor = _make_monitor(
            free_bytes=50_000_000_000,
            total_bytes=100_000_000_000,
            low_bytes=1_000_000_000,
            low_pct=5.0,
            resume_bytes=2_000_000_000,
            resume_pct=10.0,
        )
        assert monitor.is_capture_allowed(tmp_path, now=_now()) is True

    def test_pause_state_is_false_when_ample(self, tmp_path: Path) -> None:
        monitor = _make_monitor(free_bytes=50_000_000_000, total_bytes=100_000_000_000)
        monitor.is_capture_allowed(tmp_path, now=_now())
        assert monitor.pause_state(tmp_path) is False


# ---------------------------------------------------------------------------
# Low space: pausing
# ---------------------------------------------------------------------------


class TestLowSpace:
    def test_below_low_bytes_pauses(self, tmp_path: Path) -> None:
        # 500 MB free, threshold 1 GB — below byte floor
        monitor = _make_monitor(
            free_bytes=500_000_000,
            total_bytes=100_000_000_000,
            low_bytes=1_000_000_000,
            low_pct=0.001,  # pct floor very low so only bytes triggers
        )
        assert monitor.is_capture_allowed(tmp_path, now=_now()) is False

    def test_below_low_percent_pauses_despite_ample_bytes(self, tmp_path: Path) -> None:
        # free=10 GB on a 1 TB disk => 1% free; low_pct=5 => triggers pct floor
        monitor = _make_monitor(
            free_bytes=10_000_000_000,  # 10 GB — above byte low watermark
            total_bytes=1_000_000_000_000,  # 1 TB total
            low_bytes=1_000_000_000,  # 1 GB byte floor (satisfied)
            low_pct=5.0,  # 5% floor — 1% does NOT satisfy
            resume_bytes=2_000_000_000,
            resume_pct=10.0,
        )
        assert monitor.is_capture_allowed(tmp_path, now=_now()) is False

    def test_pause_state_reflects_low_disk(self, tmp_path: Path) -> None:
        monitor = _make_monitor(
            free_bytes=500_000_000,
            total_bytes=100_000_000_000,
            low_bytes=1_000_000_000,
            low_pct=0.001,
        )
        monitor.is_capture_allowed(tmp_path, now=_now())
        assert monitor.pause_state(tmp_path) is True


# ---------------------------------------------------------------------------
# Hysteresis: paused state preserved inside the band
# ---------------------------------------------------------------------------


class TestHysteresis:
    def test_paused_monitor_stays_paused_inside_hysteresis_band(
        self, tmp_path: Path
    ) -> None:
        """Once paused, space above low but below resume keeps the latch paused."""
        # Step 1: Trigger pause (500 MB free, low at 1 GB)
        free_seq = [500_000_000, 1_500_000_000, 1_500_000_000]
        call_idx = 0

        def _free(_p: Path) -> int:
            nonlocal call_idx
            val = free_seq[call_idx] if call_idx < len(free_seq) else free_seq[-1]
            call_idx += 1
            return val

        monitor = DiskSpaceMonitor(
            low_watermark_bytes=1_000_000_000,
            low_watermark_percent=0.001,
            resume_watermark_bytes=2_000_000_000,
            resume_watermark_percent=0.001,
            check_interval_seconds=1.0,
            get_free_bytes=_free,
            get_total_bytes=lambda _p: 100_000_000_000,
        )
        # First probe: 500 MB free → pause
        assert monitor.is_capture_allowed(tmp_path, now=_now(0)) is False
        # Second probe (after interval): 1.5 GB — between low (1 GB) and resume (2 GB)
        assert monitor.is_capture_allowed(tmp_path, now=_now(2)) is False
        # Third probe (after interval): 1.5 GB free again — still in band, still paused
        assert monitor.is_capture_allowed(tmp_path, now=_now(4)) is False

    def test_paused_monitor_resumes_only_above_both_resume_watermarks(
        self, tmp_path: Path
    ) -> None:
        """Resume requires free > resume_bytes AND free_pct > resume_pct."""
        free_seq = [
            500_000_000,  # first: trigger pause (byte below 1 GB)
            3_000_000_000,  # second: above resume_bytes (2 GB) but check pct
        ]
        call_idx = 0

        def _free(_p: Path) -> int:
            nonlocal call_idx
            val = free_seq[min(call_idx, len(free_seq) - 1)]
            call_idx += 1
            return val

        # total = 1 TB; resume_pct = 10% → resume requires 100 GB free
        # 3 GB / 1 TB = 0.3% — below resume_pct despite above resume_bytes
        monitor = DiskSpaceMonitor(
            low_watermark_bytes=1_000_000_000,
            low_watermark_percent=0.001,
            resume_watermark_bytes=2_000_000_000,
            resume_watermark_percent=10.0,
            check_interval_seconds=1.0,
            get_free_bytes=_free,
            get_total_bytes=lambda _p: 1_000_000_000_000,  # 1 TB
        )
        # First: paused
        assert monitor.is_capture_allowed(tmp_path, now=_now(0)) is False
        # Second: bytes OK but pct still too low → still paused
        assert monitor.is_capture_allowed(tmp_path, now=_now(2)) is False

    def test_paused_monitor_resumes_when_both_floors_cleared(
        self, tmp_path: Path
    ) -> None:
        """Resume happens when free > resume_bytes AND free_pct > resume_pct."""
        free_seq = [
            500_000_000,  # trigger pause
            15_000_000_000,  # 15 GB on 100 GB disk = 15% > 10% resume_pct
        ]
        call_idx = 0

        def _free(_p: Path) -> int:
            nonlocal call_idx
            val = free_seq[min(call_idx, len(free_seq) - 1)]
            call_idx += 1
            return val

        monitor = DiskSpaceMonitor(
            low_watermark_bytes=1_000_000_000,
            low_watermark_percent=0.001,
            resume_watermark_bytes=2_000_000_000,
            resume_watermark_percent=10.0,
            check_interval_seconds=1.0,
            get_free_bytes=_free,
            get_total_bytes=lambda _p: 100_000_000_000,
        )
        assert monitor.is_capture_allowed(tmp_path, now=_now(0)) is False
        # After recovery above both floors
        assert monitor.is_capture_allowed(tmp_path, now=_now(2)) is True

    def test_above_low_but_never_paused_stays_allowed(self, tmp_path: Path) -> None:
        """Not paused initially, and space stays above low → always allowed."""
        # 1.5 GB free on 10 GB disk = 15% — above 5% low_pct; 1.5 GB > 1 GB low_bytes
        monitor = _make_monitor(
            free_bytes=1_500_000_000,
            total_bytes=10_000_000_000,
            low_bytes=1_000_000_000,
            low_pct=5.0,
            resume_bytes=2_000_000_000,
            resume_pct=10.0,
        )
        assert monitor.is_capture_allowed(tmp_path, now=_now()) is True


# ---------------------------------------------------------------------------
# Throttle: probe count limited to once per interval
# ---------------------------------------------------------------------------


class TestThrottle:
    def test_second_call_within_interval_does_not_reprobe(self, tmp_path: Path) -> None:
        probe_count = 0

        def _counting_free(_p: Path) -> int:
            nonlocal probe_count
            probe_count += 1
            return 10_000_000_000

        monitor = DiskSpaceMonitor(
            low_watermark_bytes=1_000_000_000,
            low_watermark_percent=5.0,
            resume_watermark_bytes=2_000_000_000,
            resume_watermark_percent=10.0,
            check_interval_seconds=60.0,
            get_free_bytes=_counting_free,
            get_total_bytes=lambda _p: 100_000_000_000,
        )
        t0 = _now(0)
        monitor.is_capture_allowed(tmp_path, now=t0)
        # Within 60s: no re-probe
        monitor.is_capture_allowed(tmp_path, now=_now(10))
        monitor.is_capture_allowed(tmp_path, now=_now(30))
        monitor.is_capture_allowed(tmp_path, now=_now(59))

        assert probe_count == 1

    def test_call_after_interval_elapses_reprobes(self, tmp_path: Path) -> None:
        probe_count = 0

        def _counting_free(_p: Path) -> int:
            nonlocal probe_count
            probe_count += 1
            return 10_000_000_000

        monitor = DiskSpaceMonitor(
            low_watermark_bytes=1_000_000_000,
            low_watermark_percent=5.0,
            resume_watermark_bytes=2_000_000_000,
            resume_watermark_percent=10.0,
            check_interval_seconds=60.0,
            get_free_bytes=_counting_free,
            get_total_bytes=lambda _p: 100_000_000_000,
        )
        monitor.is_capture_allowed(tmp_path, now=_now(0))
        # Exactly at interval: re-probe triggers
        monitor.is_capture_allowed(tmp_path, now=_now(60))

        assert probe_count == 2

    def test_two_different_paths_probe_independently(self, tmp_path: Path) -> None:
        """Each volume key tracks its own probe count and time."""
        probe_count: dict[str, int] = {}

        def _counting_free(p: Path) -> int:
            key = str(p)
            probe_count[key] = probe_count.get(key, 0) + 1
            return 10_000_000_000

        path_a = tmp_path / "a"
        path_a.mkdir()
        path_b = tmp_path / "b"
        path_b.mkdir()

        monitor = DiskSpaceMonitor(
            low_watermark_bytes=1_000_000_000,
            low_watermark_percent=5.0,
            resume_watermark_bytes=2_000_000_000,
            resume_watermark_percent=10.0,
            check_interval_seconds=60.0,
            get_free_bytes=_counting_free,
            get_total_bytes=lambda _p: 100_000_000_000,
        )
        t0 = _now(0)
        monitor.is_capture_allowed(path_a, now=t0)
        monitor.is_capture_allowed(path_a, now=_now(30))  # within interval
        monitor.is_capture_allowed(path_b, now=t0)  # different path, first probe

        # path_a probed once (second call was within interval)
        assert probe_count.get(str(path_a), 0) == 1
        # path_b probed once independently
        assert probe_count.get(str(path_b), 0) == 1

    def test_pause_state_before_any_probe_is_false(self, tmp_path: Path) -> None:
        """pause_state returns False when no probe has been run yet."""
        monitor = _make_monitor()
        assert monitor.pause_state(tmp_path) is False


# ---------------------------------------------------------------------------
# Zero-total edge case
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_zero_total_bytes_treated_as_zero_percent(self, tmp_path: Path) -> None:
        """A volume reporting zero total bytes is evaluated without a divide-by-zero."""
        monitor = DiskSpaceMonitor(
            low_watermark_bytes=0,
            low_watermark_percent=1.0,
            resume_watermark_bytes=0,
            resume_watermark_percent=2.0,
            check_interval_seconds=60.0,
            get_free_bytes=lambda _p: 0,
            get_total_bytes=lambda _p: 0,
        )
        # Should not raise; 0/0 is treated as 0% → below 1% low → paused
        result = monitor.is_capture_allowed(tmp_path, now=_now())
        assert isinstance(result, bool)
