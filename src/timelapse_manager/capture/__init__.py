"""Asyncio capture engine that schedules and records camera stills.

The public surface is the :class:`CaptureSupervisor` (the per-project background
scheduler that owns the shared HTTP client) and the :class:`FrameWriter` (the
single atomic file-then-row persistence path shared by the scheduler and the
manual-capture endpoint).
"""

from __future__ import annotations

from .frame_writer import FrameWriter, WrittenFrame
from .supervisor import CaptureState, CaptureSupervisor, CaptureTarget

__all__ = [
    "CaptureSupervisor",
    "CaptureState",
    "CaptureTarget",
    "FrameWriter",
    "WrittenFrame",
]
