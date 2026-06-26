"""Versioned local API router.

Assembles the ``/api/v1`` surface. Every route under this router is gated by the
local bearer-token dependency. The system, cameras, frames, renders, and
projects sub-routers are functional.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..security.token import require_local_token
from . import alerts, cameras, frames, projects, renders, system

# Exposed for callers that mount the projects resource directly (and kept stable
# so the API shape does not shift).
projects_router = projects.router


def build_api_router() -> APIRouter:
    """Return the ``/api/v1`` router with all sub-routers mounted and gated.

    The bearer-token dependency is attached to the parent router so it applies
    uniformly to every child route.
    """
    api = APIRouter(prefix="/api/v1", dependencies=[Depends(require_local_token)])
    api.include_router(system.router, tags=["system"])
    api.include_router(cameras.router)
    api.include_router(frames.router)
    api.include_router(frames.admin_router)
    api.include_router(renders.router)
    api.include_router(renders.renders_router)
    api.include_router(projects_router)
    api.include_router(alerts.router)
    return api
