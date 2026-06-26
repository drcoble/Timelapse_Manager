"""Frame store and on-disk path management for captured images and renders.

The public surface is the relocatable path layer (:mod:`.paths`) and the passive
disk-space gate (:mod:`.monitor`). The capture writer and supervisor depend on
these names rather than on filesystem details directly.
"""

from __future__ import annotations

from .monitor import DiskSpaceMonitor
from .paths import (
    ProjectRef,
    frame_dir,
    frames_root,
    resolve_absolute,
    to_stored,
    uses_default_layout,
)

__all__ = [
    "DiskSpaceMonitor",
    "ProjectRef",
    "frame_dir",
    "frames_root",
    "resolve_absolute",
    "to_stored",
    "uses_default_layout",
]
