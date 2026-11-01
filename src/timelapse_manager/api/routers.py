"""Versioned local API router.

Assembles the ``/api/v1`` surface. The router is **bearer-only**: every route
under it first runs the local bearer-token dependency, so a valid bearer token is
required and a session cookie alone is ``401`` (the web UI does not call this
API). The role dependencies on the individual routes also read a session cookie;
that branch applies only to a request that presents a valid bearer *and* a cookie,
in which case the cookie's user and role decide (``403`` for a role that may not
act) and the CSRF layer demands a matching token, because a browser could be
making the call. ``ApiBearerGateMiddleware`` makes the bearer and cookie decision
before the request body is read. The system, cameras, frames, renders, projects,
alerts and reports sub-routers are functional.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..security.token import require_local_token
from . import alerts, cameras, frames, projects, renders, reports, system

# Exposed for callers that mount the projects resource directly (and kept stable
# so the API shape does not shift).
projects_router = projects.router


def build_api_router() -> APIRouter:
    """Return the ``/api/v1`` router with all sub-routers mounted and gated.

    The bearer-token dependency is attached to the parent router so it applies
    uniformly to every child route: no route here accepts a cookie session on its
    own (#261).
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
    api.include_router(reports.router)
    return api
