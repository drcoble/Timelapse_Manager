"""Local HTTP API routers shared by the web UI and the CLI."""

from __future__ import annotations

from fastapi import FastAPI

from .routers import build_api_router
from .system import metrics_router


def mount_api(app: FastAPI) -> None:
    """Mount the versioned local API onto ``app``.

    The metrics endpoint shares the ``/api/v1`` path prefix but is included as a
    separate router, so it does not inherit the versioned API's blanket
    bearer-token dependency. It owns its access control instead -- an enable flag
    and an admin gate -- which lets a disabled deployment answer 404 rather than
    the API's 401 while keeping the path under the prefix that startup gates leave
    reachable.
    """
    app.include_router(build_api_router())
    app.include_router(metrics_router, prefix="/api/v1", tags=["metrics"])


__all__ = ["mount_api"]
