"""Wire the web UI onto the FastAPI application.

:func:`mount_web` is the single entry point the application factory calls after
the API is mounted. It mounts the static assets, includes the page/partial
router, and installs the middleware stack in the one correct order.

Middleware order (Starlette runs the *last* added middleware first, so it is the
outermost layer):

1. ``EffectiveSchemeMiddleware`` -- outermost, so every layer below sees
   ``request.state.effective_scheme``.
2. ``HttpsRedirectMiddleware`` -- bounces plaintext to HTTPS before any work.
3. ``FirstRunGateMiddleware`` -- funnels to setup until an admin exists, but only
   after the request is on HTTPS.
4. ``CsrfMiddleware`` -- innermost, closest to the routes, so it can read the form
   body and the resolved session.

They are therefore *added* in the reverse of that list.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .dependencies import templates
from .errors import install_error_handlers
from .middleware import (
    CsrfMiddleware,
    EffectiveSchemeMiddleware,
    FirstRunGateMiddleware,
    HttpsRedirectMiddleware,
)
from .routers import router as web_router

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def mount_web(app: FastAPI) -> None:
    """Mount the web UI: static assets, page router, and middleware stack.

    Called by the application factory after the API is mounted. Installing the
    middleware here (rather than in the service runner) keeps the scheme/redirect
    and CSRF behaviour inside ``create_app`` so it is exercised by a test client,
    not only behind the real TLS socket.
    """
    # Ensure the templates environment is initialised (also lets callers reach it).
    assert templates is not None

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    app.include_router(web_router)

    # Convert the cookie-auth guard's 401 into a login redirect for browsers,
    # while machine paths keep the bare 401 (see :mod:`.errors`).
    install_error_handlers(app)

    # Added inner-to-outer: the last add is the outermost layer.
    app.add_middleware(CsrfMiddleware)
    app.add_middleware(FirstRunGateMiddleware)
    app.add_middleware(HttpsRedirectMiddleware)
    app.add_middleware(EffectiveSchemeMiddleware)
