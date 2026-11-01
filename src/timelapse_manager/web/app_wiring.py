"""Wire the web UI onto the FastAPI application.

:func:`mount_web` is the single entry point the application factory calls after
the API is mounted. It builds the content-hashed asset manifest (see
:mod:`.assets`), mounts the static assets, includes the page/partial
router, and installs the middleware stack in the one correct order.

Middleware order (Starlette runs the *last* added middleware first, so it is the
outermost layer):

0. ``RequestIdMiddleware`` -- the outermost layer, so every request is tagged
   with a correlation id before any inner layer runs; even an inner middleware
   that rejects the request early has its log lines carry the id.
0a. ``AllowedHostsMiddleware`` -- only when ``server.allowed_hosts`` is not
   ``*``: a request for a host the service does not serve is refused before any
   other work, including the redirect that would echo that host back.
1. ``BodyLimitMiddleware`` -- so the per-route body cap rejects an oversized
   body (or an over-limit ``Content-Length``) before any inner layer, including
   the HTTPS redirect, reads it.
2. ``EffectiveSchemeMiddleware`` -- so every layer below sees
   ``request.state.effective_scheme``.
3. ``SecurityHeadersMiddleware`` -- inside the scheme layer (it reads the
   effective scheme for the HSTS decision) but outside the redirect/gate layers,
   so even their redirect responses carry the security headers.
3a. ``CacheControlMiddleware`` -- just inside the security headers: defaults
   ``no-store`` on HTML/JSON and ``Vary: HX-Request`` on HTML, for every
   response the inner layers and routes produce (see :mod:`.cache_headers`).
4. ``HttpsRedirectMiddleware`` -- bounces plaintext to HTTPS before any work.
5. ``FirstRunGateMiddleware`` -- funnels to setup until an admin exists, but only
   after the request is on HTTPS.
5a. ``ApiBearerGateMiddleware`` -- just outside the CSRF layer: refuses a
   bearer-less unsafe ``/api/v1`` call before CSRF or the route can read its body.
6. ``CsrfMiddleware`` -- innermost, closest to the routes, so it can read the form
   body and the resolved session.

They are therefore *added* in the reverse of that list.
"""

from __future__ import annotations

from fastapi import FastAPI

from ..config import Settings
from .assets import (
    PACKAGE_STATIC_DIR,
    AssetManifest,
    VersionedStaticFiles,
    install_manifest,
)
from .body_limit import BodyLimitMiddleware
from .cache_headers import CacheControlMiddleware
from .dependencies import templates
from .errors import install_error_handlers
from .middleware import (
    AllowedHostsMiddleware,
    ApiBearerGateMiddleware,
    CsrfMiddleware,
    EffectiveSchemeMiddleware,
    FirstRunGateMiddleware,
    HttpsRedirectMiddleware,
)
from .request_id import RequestIdMiddleware
from .routers import router as web_router
from .security_headers import SecurityHeadersMiddleware

_STATIC_DIR = PACKAGE_STATIC_DIR


def mount_web(app: FastAPI) -> None:
    """Mount the web UI: static assets, page router, and middleware stack.

    Called by the application factory after the API is mounted. Installing the
    middleware here (rather than in the service runner) keeps the scheme/redirect
    and CSRF behaviour inside ``create_app`` so it is exercised by a test client,
    not only behind the real TLS socket.
    """
    # Ensure the templates environment is initialised (also lets callers reach it).
    assert templates is not None

    # Hash every static file (in memory; the package directory is read-only in
    # production) and publish the manifest to the templates' ``static_url``.
    manifest = AssetManifest.build(_STATIC_DIR)
    install_manifest(manifest)
    app.mount(
        "/static",
        VersionedStaticFiles(directory=str(_STATIC_DIR), manifest=manifest),
        name="static",
    )
    app.include_router(web_router)

    # Convert the cookie-auth guard's 401 into a login redirect for browsers,
    # while machine paths keep the bare 401 (see :mod:`.errors`).
    install_error_handlers(app)

    # Added inner-to-outer: the last add is the outermost layer.
    app.add_middleware(CsrfMiddleware)
    app.add_middleware(ApiBearerGateMiddleware)
    app.add_middleware(FirstRunGateMiddleware)
    app.add_middleware(HttpsRedirectMiddleware)
    app.add_middleware(CacheControlMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(EffectiveSchemeMiddleware)
    app.add_middleware(BodyLimitMiddleware)
    allowed_hosts = _allowed_hosts(app)
    if "*" not in allowed_hosts:
        app.add_middleware(AllowedHostsMiddleware, allowed_hosts=allowed_hosts)
    # Outermost layer (last add): tag every request with a correlation id before
    # any inner middleware runs, so even an early rejection carries the id.
    app.add_middleware(RequestIdMiddleware)


def _allowed_hosts(app: FastAPI) -> list[str]:
    """Return ``server.allowed_hosts`` from the settings the app was built with.

    Read once, when the stack is built: the list is fixed for the life of the
    process, like every other server setting. An app built without settings on
    its state (alternative wiring) gets the permissive ``*`` default.
    """
    settings: Settings | None = getattr(app.state, "settings", None)
    if settings is None:
        return ["*"]
    return list(settings.server.allowed_hosts)
