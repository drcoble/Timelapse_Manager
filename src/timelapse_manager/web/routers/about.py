"""The About page route and its bundled-license rendering."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import (
    HTMLResponse,
    Response,
)

from ...runtime import get_context
from ...version import get_app_version, get_build_info
from .. import dependencies as deps
from ..dependencies import (
    CurrentUser,
    DbDep,
    templates,
)

logger = logging.getLogger(__name__)

router = APIRouter()


_LICENSE_NAME = "Apache-2.0"

# The repo-root LICENSE file, relative to this module
# (web/routers/about.py -> web/routers -> web -> timelapse_manager -> src -> root).
_LICENSE_PATH = Path(__file__).resolve().parents[4] / "LICENSE"

# Shown in place of the license text when the LICENSE file cannot be read (for
# example from a frozen bundle that did not include it).
_LICENSE_UNAVAILABLE = (
    "The full Apache License, Version 2.0 text ships in the LICENSE file "
    "alongside this application and is available at "
    "https://www.apache.org/licenses/LICENSE-2.0."
)


def _read_license_text() -> str:
    """Return the full license text, or a clear pointer if it cannot be read.

    Reading is defensive: a missing or unreadable LICENSE file (a frozen bundle
    that omitted it, a permission error) degrades to a short pointer rather than
    failing the page.
    """
    try:
        return _LICENSE_PATH.read_text(encoding="utf-8")
    except OSError:
        return _LICENSE_UNAVAILABLE


@router.get("/about", response_class=HTMLResponse)
def about_page(request: Request, db: DbDep, user: CurrentUser) -> Response:
    """Render the About page: app version, build info, ffmpeg version, license.

    Open to any authenticated role -- the version and license are not sensitive.
    The application version comes from the single version source; the build
    number (commit SHA + UTC date) from the generated build-info module, falling
    back to ``"unknown"`` in a dev checkout. The ffmpeg version is the value
    cached at startup (the same source the system endpoint reports), so this page
    never shells out to ffmpeg on a request.
    """
    return templates.TemplateResponse(
        request,
        "about.html",
        deps.base_context(
            request,
            db,
            user,
            app_version=get_app_version(),
            build_info=get_build_info(),
            ffmpeg_version=get_context().ffmpeg_version,
            license_name=_LICENSE_NAME,
            license_text=_read_license_text(),
        ),
    )
