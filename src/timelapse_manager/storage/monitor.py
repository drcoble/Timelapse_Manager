"""Passive disk-space gate for the capture loop.

:class:`DiskSpaceMonitor` answers one question -- *is there enough free space to
keep capturing on this volume?* -- and is built to be called straight from the
existing per-project capture loop without adding any background task:

* **Passive + throttled.** It does not poll. Each call to
  :meth:`is_capture_allowed` re-probes free space only if at least
  ``check_interval_seconds`` have elapsed since the last probe for that volume;
  otherwise it answers from the cached reading. This keeps ``shutil.disk_usage``
  off the hot path while the loop spins on short intervals.
* **Hysteresis.** Capture is *paused* when free space drops below the low
  watermark and only *resumed* once it recovers above a higher resume watermark,
  so a volume hovering around the threshold cannot flap the gate on and off.
* **Keep-all.** The monitor never deletes anything. Low space pauses capture; it
  is the operator's job to reclaim space. There is no eviction path here by
  design.
* **Injectable probes + clock.** Free and total bytes are read through callables
  that default to :func:`shutil.disk_usage`, and the time source is injectable,
  so the gate's behaviour can be exercised without a real disk or real clock.

The watermarks are evaluated as *pause when free is below the low byte floor OR
below the low percentage floor* (whichever triggers first -- the conservative
reading) and *resume only when free is above both resume floors*. Resume floors
are required to sit at or above the low floors so the hysteresis band is real.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def _default_free_bytes(path: Path) -> int:
    """Return free bytes on the volume containing ``path``."""
    return shutil.disk_usage(path).free


def _default_total_bytes(path: Path) -> int:
    """Return total bytes on the volume containing ``path``."""
    return shutil.disk_usage(path).total


def _nearest_existing(path: Path) -> Path:
    """Return ``path`` or its closest existing ancestor.

    The gate is evaluated before the first capture creates a project's frame
    directory, so a probe straight at that path would raise. Walking up to the
    nearest existing parent yields the same volume's free space without requiring
    the leaf to exist yet; the filesystem root is the guaranteed terminator.
    """
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            return current
        current = parent
    return current


@dataclass
class _VolumeState:
    """Per-volume cached probe result and latched pause decision.

    ``paused`` is the hysteresis latch: it flips to ``True`` only on crossing the
    low watermark and back to ``False`` only on crossing the (higher) resume
    watermark, so a reading between the two preserves the prior decision.
    """

    last_checked_at: datetime
    free_bytes: int
    total_bytes: int
    paused: bool


class DiskSpaceMonitor:
    """A throttled, hysteresis-latched free-space gate keyed by volume."""

    def __init__(
        self,
        low_watermark_bytes: int,
        low_watermark_percent: float,
        resume_watermark_bytes: int,
        resume_watermark_percent: float,
        check_interval_seconds: float,
        get_free_bytes: Callable[[Path], int] = _default_free_bytes,
        get_total_bytes: Callable[[Path], int] = _default_total_bytes,
    ) -> None:
        """Create a monitor.

        :param low_watermark_bytes: pause when free bytes fall below this.
        :param low_watermark_percent: pause when free percentage falls below this.
        :param resume_watermark_bytes: resume only once free bytes exceed this.
        :param resume_watermark_percent: resume only once free percentage exceeds
            this.
        :param check_interval_seconds: minimum seconds between probes per volume;
            calls in between answer from the cached reading.
        :param get_free_bytes: probe for free bytes; injectable for testing.
        :param get_total_bytes: probe for total bytes; injectable for testing.
        :raises ValueError: if a resume watermark sits below its low watermark
            (which would defeat the hysteresis band).
        """
        if resume_watermark_bytes < low_watermark_bytes:
            raise ValueError("resume_watermark_bytes must be >= low_watermark_bytes")
        if resume_watermark_percent < low_watermark_percent:
            raise ValueError(
                "resume_watermark_percent must be >= low_watermark_percent"
            )
        self._low_bytes = low_watermark_bytes
        self._low_percent = low_watermark_percent
        self._resume_bytes = resume_watermark_bytes
        self._resume_percent = resume_watermark_percent
        self._check_interval = check_interval_seconds
        self._get_free = get_free_bytes
        self._get_total = get_total_bytes
        self._volumes: dict[Path, _VolumeState] = {}

    def is_capture_allowed(self, path: Path, *, now: datetime | None = None) -> bool:
        """Return whether capture may proceed on the volume holding ``path``.

        Synchronous and cheap: re-probes only when the throttle interval has
        elapsed for this volume, then applies hysteresis. Safe to call every
        loop cycle. ``now`` is injectable so the throttle can be driven in tests.
        """
        moment = now if now is not None else datetime.now(UTC)
        key = self._volume_key(path)
        state = self._volumes.get(key)
        if state is not None and not self._is_stale(state, moment):
            return not state.paused

        probe_path = _nearest_existing(path)
        free = self._get_free(probe_path)
        total = self._get_total(probe_path)
        prior_paused = state.paused if state is not None else False
        paused = self._decide(free=free, total=total, prior_paused=prior_paused)
        self._volumes[key] = _VolumeState(
            last_checked_at=moment,
            free_bytes=free,
            total_bytes=total,
            paused=paused,
        )
        return not paused

    def pause_state(self, path: Path) -> bool:
        """Return the latched pause decision for a volume, without probing.

        Reflects the most recent :meth:`is_capture_allowed` evaluation for the
        volume; ``False`` when the volume has never been evaluated. Lets the
        caller edge-trigger events off the latch rather than the raw reading.
        """
        state = self._volumes.get(self._volume_key(path))
        return state.paused if state is not None else False

    @staticmethod
    def _volume_key(path: Path) -> Path:
        """Return a stable key identifying the volume a path probes against."""
        return _nearest_existing(path)

    def _is_stale(self, state: _VolumeState, now: datetime) -> bool:
        """Return whether ``state`` is older than the throttle interval."""
        elapsed = (now - state.last_checked_at).total_seconds()
        return elapsed >= self._check_interval

    def _decide(self, *, free: int, total: int, prior_paused: bool) -> bool:
        """Apply the watermark hysteresis to a fresh reading.

        Pause is the conservative (OR) condition; resume requires *both* byte and
        percentage to clear their higher floors. Between the bands the prior
        latch is preserved.
        """
        free_percent = (free / total * 100.0) if total > 0 else 0.0
        if prior_paused:
            recovered = (
                free > self._resume_bytes and free_percent > self._resume_percent
            )
            return not recovered
        below = free < self._low_bytes or free_percent < self._low_percent
        return below
