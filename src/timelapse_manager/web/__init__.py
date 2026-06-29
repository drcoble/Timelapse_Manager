"""Server-rendered web UI: Jinja2 templates, HTMX partials, and static assets.

The public entry point is :func:`mount_web`, which the application factory calls
to mount the templates, static assets, page/partial router, and the middleware
stack (effective-scheme detection, HTTP-to-HTTPS redirect, the first-run gate,
and cookie-keyed CSRF protection).
"""

from __future__ import annotations

from .app_wiring import mount_web

__all__ = ["mount_web"]
