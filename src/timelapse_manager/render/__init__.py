"""Render orchestration: queue, scheduler, and post-render actions.

This package drives the encoder (:mod:`timelapse_manager.encode`) from the rest
of the application. The :class:`RenderQueue` is a bounded background worker that
turns pending :class:`~timelapse_manager.db.models.RenderJob` rows into video
files; the :class:`RenderScheduler` periodically enqueues recurring renders and
archive snapshots; :mod:`.post_actions` runs the built-in follow-up actions
(export, webhook, prune) after a successful render.

Both the queue and the scheduler mirror the capture supervisor: one long-lived
asyncio task each, an injectable clock, and a bulletproof ``stop()`` that the
application lifespan calls on shutdown.
"""

from __future__ import annotations

from .queue import Clock, RenderQueue
from .scheduler import RenderScheduler
from .settings import (
    combination_warning,
    is_supported_combination,
    output_settings_from_schedule,
    render_settings_view,
)
from .spec import SpecBuildError, build_render_spec, project_render_root

__all__ = [
    "Clock",
    "RenderQueue",
    "RenderScheduler",
    "SpecBuildError",
    "build_render_spec",
    "combination_warning",
    "is_supported_combination",
    "output_settings_from_schedule",
    "project_render_root",
    "render_settings_view",
]
