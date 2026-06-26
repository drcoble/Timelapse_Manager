"""Server-rendered web UI routes and HTMX partials.

Every page is rendered server-side from the database and the running service
objects (the capture supervisor, the render queue) -- the browser never calls
the local JSON API. Reads are open to any authenticated role; mutations are
deny-by-default and require an administrator. The login and first-run routes are
the only unauthenticated surface.

This package splits the page/partial routes into one module per domain (auth,
dashboard, cameras, frames, renders, projects, ...). Each module owns its own
``APIRouter``; this aggregator includes them in a fixed top-to-bottom order so
the resulting route table is deterministic. Order is load-bearing where path
patterns overlap -- in particular the ``/projects/{project_id}/{action}``
catch-all (in :mod:`.projects`) is included after the specific ``/projects/*``
routes (dashboard, frames, renders, milestones) so it never shadows them. The
``milestones`` module is included before ``projects`` so its ``POST
/projects/{id}/milestones`` is not captured by that single-segment catch-all.

View-model mapping lives in :mod:`._viewmodels`: the templates carry no custom
filters, so that module formats datetimes, derives display URLs, and translates
internal status vocabularies into the words the templates branch on. The project
operational status in particular is a *presentation* value derived from the live
capture state -- it is never persisted, because the stored enum uses a different
vocabulary. Cross-cutting non-view helpers (audit writes, the running settings,
form-field parsing) live in :mod:`._shared`.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import (
    about,
    account,
    alerts,
    auth,
    cameras,
    dashboard,
    events,
    frames,
    ldap,
    milestones,
    notifications,
    projects,
    renders,
    settings,
    users,
)

# Back-compat re-exports: external callers (and tests) import these view-model
# helpers from ``timelapse_manager.web.routers`` directly.
from ._viewmodels import _project_operational_status, _project_view

# The aggregate router the application factory mounts. Sub-routers are included
# in the original top-to-bottom domain order so the final route table -- and the
# relative order of any overlapping path patterns -- matches the historical
# single-module layout.
router = APIRouter()
for _module in (
    auth,
    dashboard,
    about,
    cameras,
    frames,
    renders,
    milestones,
    projects,
    settings,
    account,
    users,
    events,
    alerts,
    notifications,
    ldap,
):
    router.include_router(_module.router)

__all__ = [
    "router",
    "_project_operational_status",
    "_project_view",
]
