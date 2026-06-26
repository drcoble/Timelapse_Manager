"""Web-layer request helpers: templates, context, auth wrappers, and cookies.

This module is the seam between the security primitives and the server-rendered
pages. It owns:

* the shared :class:`Jinja2Templates` environment (one per process),
* a base-context builder that supplies every authenticated render with the
  ``csrf_token``, ``current_user``, and ``flash_messages`` the templates expect,
* thin wrappers over the security authorization dependencies, and
* the single place the session cookie is set and cleared, so the ``Secure``,
  ``HttpOnly``, and ``SameSite`` attributes are applied consistently.

Template values are formatted here (datetimes, derived URLs) rather than in the
templates, which carry no custom filters, with the exception of ``localdt``:
a context-aware filter that converts stored naive-UTC datetimes to the current
viewer's preferred timezone.
"""

from __future__ import annotations

import datetime
import urllib.parse
import zoneinfo
from pathlib import Path
from typing import Annotated, Any

import jinja2
from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session as DbSession

from ..config import Settings
from ..db.models import User
from ..db.session import get_session
from ..runtime import get_context
from ..security import (
    get_session_row,
    issue_csrf,
    require_authenticated_session,
    require_operator_or_admin,
    require_role,
)

# The templates live alongside this package. One environment is shared across
# the process; FastAPI's wrapper is cheap to construct but we keep a singleton.
_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Jinja2 custom filter: localdt
# ---------------------------------------------------------------------------


@jinja2.pass_context
def _localdt_filter(
    ctx: jinja2.runtime.Context,
    value: datetime.datetime | None,
) -> str:
    """Convert a naive-UTC datetime to the viewer's local timezone for display.

    Reads ``viewer_timezone`` from the template context (set by
    :func:`base_context` from the signed-in user's preference). Falls back to
    UTC when the value is ``None``, the timezone name is absent, or the name is
    not a valid IANA zone.

    The format is ``YYYY-MM-DD HH:MM ZZZ`` where ``ZZZ`` is the abbreviated
    zone name (e.g. ``EST``, ``PDT``, ``UTC``), so the viewer always knows
    which timezone is shown.
    """
    if value is None:
        return ""
    tz_name: str | None = ctx.get("viewer_timezone")
    try:
        tz: datetime.tzinfo = zoneinfo.ZoneInfo(tz_name) if tz_name else datetime.UTC
    except (zoneinfo.ZoneInfoNotFoundError, KeyError):
        tz = datetime.UTC
    # Stored datetimes are naive UTC; attach UTC before converting.
    aware = value.replace(tzinfo=datetime.UTC)
    local = aware.astimezone(tz)
    return local.strftime("%Y-%m-%d %H:%M %Z")


# Register once on the shared environment so every template can call
# ``{{ dt_value | localdt }}``.
templates.env.filters["localdt"] = _localdt_filter


def _reltime_filter(value: datetime.datetime | None) -> str:
    """Render a naive-UTC datetime as a short relative time, e.g. ``5m ago``.

    Coarse buckets (just now / Nm / Nh / Nd ago) for at-a-glance recency in
    compact lists; absolute timestamps still use ``localdt``. A future value
    (clock skew) reads as ``just now``.
    """
    if value is None:
        return ""
    now = datetime.datetime.now(datetime.UTC)
    secs = int((now - value.replace(tzinfo=datetime.UTC)).total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


templates.env.filters["reltime"] = _reltime_filter

# Roles recognised by the UI. Operator sits between admin and viewer: it may
# mutate the operational surface (cameras, projects, renders, frames) but not
# user accounts or system settings, which stay admin-only.
ADMIN_ROLE = "admin"
OPERATOR_ROLE = "operator"
VIEWER_ROLE = "viewer"


async def parsed_form(request: Request) -> dict[str, str]:
    """Return the urlencoded form body as a dict, reading it at most once.

    The web layer parses ``application/x-www-form-urlencoded`` bodies by hand
    rather than via FastAPI's ``Form()`` (which requires an optional dependency
    that is not installed). The parsed result is cached on ``request.state`` --
    shared between the CSRF middleware's request and the route handler's request
    -- so the body stream is consumed exactly once per request regardless of how
    many layers need the form. Non-form requests yield an empty mapping.

    Used as an async FastAPI dependency so synchronous handlers stay synchronous:
    FastAPI awaits this, then runs the handler in its threadpool.
    """
    cached: dict[str, str] | None = getattr(request.state, "parsed_form", None)
    if cached is not None:
        return cached
    content_type = request.headers.get("content-type", "")
    form: dict[str, str] = {}
    if content_type.startswith("application/x-www-form-urlencoded"):
        body = await request.body()
        if body:
            pairs = urllib.parse.parse_qsl(body.decode("utf-8"))
            # Last value wins; these forms never repeat a field meaningfully.
            form = dict(pairs)
    request.state.parsed_form = form
    return form


def effective_scheme(request: Request) -> str:
    """Return the request's effective scheme (``"https"``/``"http"``).

    Set by the scheme middleware; defaults to ``"http"`` if a request somehow
    reaches here without having passed through it.
    """
    return getattr(request.state, "effective_scheme", "http")


def is_secure_request(request: Request) -> bool:
    """Return whether the cookie issued for this request should be ``Secure``."""
    return effective_scheme(request) == "https"


def csrf_token_for(request: Request, db: DbSession) -> str:
    """Return the CSRF token for the current session, or an empty string.

    Reads the session row for the request's cookie and issues its synchronizer
    token. Pre-authentication pages (login, first-run) have no session yet and
    receive an empty token; their forms still submit because those routes are
    CSRF-exempt until a live session exists.
    """
    settings = get_context().settings
    raw_token = request.cookies.get(settings.session.cookie_name)
    if not raw_token:
        return ""
    row = get_session_row(db, raw_token, settings=settings.session)
    if row is None or not row.csrf_secret:
        return ""
    return issue_csrf(row.csrf_secret)


def base_context(
    request: Request,
    db: DbSession,
    user: User,
    *,
    flash_messages: list[dict[str, str]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build the common template context for an authenticated render.

    Supplies ``request`` (Starlette requires it), the per-session ``csrf_token``,
    the ``current_user``, ``flash_messages``, and display-preference keys:

    * ``viewer_timezone`` -- the user's stored IANA timezone name, or ``None``
      for UTC.  Read by the ``localdt`` Jinja2 filter when converting timestamps.
    * ``user_theme`` -- the user's stored theme preference (``"light"``,
      ``"dark"``, or ``"system"``).  Inlined into ``base.html`` so the no-flash
      inline script can set ``data-theme`` before the stylesheet loads.
    * ``can_operate`` -- whether the viewer may use the operational mutation
      surface (cameras, projects, renders, frames). True for the admin and
      operator roles, false for a viewer. Templates gate mutation affordances
      on this flag so the buttons they show match exactly what the routes
      allow. Account and system administration (users, settings, notification
      settings) stay admin-only and are gated on the role directly, not this
      flag. Threaded through every standalone partial render because those all
      build their context here.
    """
    context: dict[str, Any] = {
        "request": request,
        "csrf_token": csrf_token_for(request, db),
        "current_user": user,
        "flash_messages": flash_messages or [],
        "viewer_timezone": getattr(user, "viewer_timezone", None),
        "user_theme": getattr(user, "theme_preference", "system"),
        "can_operate": getattr(user, "role", None) in (ADMIN_ROLE, OPERATOR_ROLE),
    }
    context.update(extra)
    return context


def set_session_cookie(
    response: Any,
    request: Request,
    raw_token: str,
    *,
    settings: Settings,
    persistent: bool,
) -> None:
    """Set the session cookie on ``response`` with the correct attributes.

    The cookie is ``HttpOnly`` (no script access), carries the configured
    ``SameSite`` policy, and is marked ``Secure`` whenever the request is
    effectively HTTPS. A persistent ("remember me") session is given an explicit
    ``max_age`` so it survives a browser restart; a non-persistent one is a
    session cookie (no ``max_age``). The raw token is the cookie value and is
    never logged.
    """
    session_settings = settings.session
    max_age = session_settings.persistent_timeout_seconds if persistent else None
    response.set_cookie(
        key=session_settings.cookie_name,
        value=raw_token,
        max_age=max_age,
        httponly=True,
        secure=is_secure_request(request),
        samesite=session_settings.samesite,
        path="/",
    )


def clear_session_cookie(response: Any, *, settings: Settings) -> None:
    """Remove the session cookie from the client."""
    response.delete_cookie(key=settings.session.cookie_name, path="/")


# Thin re-exports of the security authorization dependencies, named for the web
# layer's call sites. Both are deny-by-default and 401 an anonymous request.
# ``require_admin`` admits only the admin role (a viewer or operator is 403);
# ``require_operator`` admits operator *and* admin (a viewer is 403), gating the
# operational mutation surface.
require_authenticated = require_authenticated_session
require_admin = require_role(ADMIN_ROLE)
require_operator = require_operator_or_admin()

CurrentUser = Annotated[User, Depends(require_authenticated)]
AdminUser = Annotated[User, Depends(require_admin)]
OperatorUser = Annotated[User, Depends(require_operator)]
DbDep = Annotated[DbSession, Depends(get_session)]
FormDep = Annotated[dict[str, str], Depends(parsed_form)]
