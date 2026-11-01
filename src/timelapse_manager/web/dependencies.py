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
templates, which carry few custom filters. Two concern time zones: ``localdt``
converts stored naive-UTC datetimes to the current viewer's preferred timezone,
and ``tz_label`` names that zone for headers and input labels. The same
:func:`viewer_zone` resolver backs both and the routes that read user-entered
times in the viewer's zone (:func:`viewer_local_to_utc`).
"""

from __future__ import annotations

import datetime
import functools
import urllib.parse
import zoneinfo
from pathlib import Path
from typing import Annotated, Any

import jinja2
from fastapi import Depends, HTTPException, Request, status
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session as DbSession

from ..config import Settings
from ..db.models import User
from ..db.session import DbSessionDep
from ..runtime import get_context
from ..security import (
    issue_csrf,
    require_authenticated_session,
    require_operator_or_admin,
    require_role,
    verify_csrf,
)
from ..security.authz import prefixed_cookie_name, session_cookie_name
from ..security.sessions import resolve_request_session
from .assets import static_url
from .body_limit import URLENCODED_FORM_MAX_BYTES, body_limit_for, is_urlencoded
from .interval import format_interval_compact
from .multipart import BodyTooLargeError, read_body_limited

# The templates live alongside this package. One environment is shared across
# the process; FastAPI's wrapper is cheap to construct but we keep a singleton.
_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Viewer time zone: one resolver for display, parsing and the header labels
# ---------------------------------------------------------------------------


def viewer_zone(tz_name: str | None) -> datetime.tzinfo:
    """Return the tzinfo for a viewer's stored IANA zone name, or UTC.

    ``None``, a blank name, an unknown zone, and a malformed name (a path-like
    ``../x`` raises ``ValueError`` inside :mod:`zoneinfo`, an over-long one
    ``OSError``) all fall back to UTC, so a bad stored value can never break a
    render or a parse.
    """
    if not tz_name:
        return datetime.UTC
    try:
        return zoneinfo.ZoneInfo(tz_name)
    except (zoneinfo.ZoneInfoNotFoundError, KeyError, ValueError, OSError):
        return datetime.UTC


# Files under a zone directory that load as zones but are not zone names a user
# should store. ``available_timezones()`` lists every TZif file it finds when no
# ``tzdata`` index is installed, so on Debian it includes ``localtime`` (a link
# to the host's own zone) and would otherwise accept it.
_NOT_ZONE_NAMES = frozenset({"localtime", "posixrules", "posix", "right"})


@functools.cache
def _known_timezones() -> frozenset[str]:
    """The IANA zone names this host knows, read once per process."""
    return frozenset(zoneinfo.available_timezones()) - _NOT_ZONE_NAMES


def is_valid_timezone(tz_name: str) -> bool:
    """Whether ``tz_name`` is a known IANA zone name.

    Membership in :func:`zoneinfo.available_timezones` rather than a trial
    ``ZoneInfo()`` load: a submitted name is a path under the zone database, so
    this also refuses names such as ``localtime`` or ``posixrules`` that load a
    file but are not zones a user should store. Should the listing come back
    empty (a host or bundle without a readable zone index), it falls back to
    whether the name loads, so a valid zone is never refused for that reason.
    """
    if not tz_name or tz_name in _NOT_ZONE_NAMES:
        return False
    known = _known_timezones()
    if known:
        return tz_name in known
    return viewer_zone(tz_name) is not datetime.UTC


def viewer_local_to_utc(
    value: datetime.datetime, tz_name: str | None
) -> datetime.datetime:
    """Read a user-entered datetime in the viewer's zone; return it as aware UTC.

    An aware value (an ISO string with an offset, e.g. a link shared between
    users) already names its instant and is only converted to UTC. A naive
    value (the ``datetime-local`` widget) is wall-clock time in the viewer's
    zone. A wall time repeated at a DST change resolves to its first occurrence
    (``fold=0``); a wall time skipped by the spring-forward gap resolves with
    the offset in force before the gap, so it lands one hour after the
    wall-clock reading (02:30 on the US spring-forward day is 07:30Z).
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=viewer_zone(tz_name))
    return value.astimezone(datetime.UTC)


# ---------------------------------------------------------------------------
# Jinja2 custom filters: localdt, tz_label
# ---------------------------------------------------------------------------


@jinja2.pass_context
def _localdt_filter(
    ctx: jinja2.runtime.Context,
    value: datetime.datetime | None,
    fmt: str = "%Y-%m-%d %H:%M %Z",
) -> str:
    """Convert a naive-UTC datetime to the viewer's local timezone for display.

    Reads ``viewer_timezone`` from the template context (set by
    :func:`base_context` from the signed-in user's preference). Falls back to
    UTC when the value is ``None``, the timezone name is absent, or the name is
    not a valid IANA zone.

    The default format is ``YYYY-MM-DD HH:MM ZZZ`` where ``ZZZ`` is the
    abbreviated zone name (e.g. ``EST``, ``PDT``, ``UTC``), so the viewer always
    knows which timezone is shown. ``fmt`` overrides it, e.g.
    ``localdt('%Y-%m-%dT%H:%M')`` for a ``datetime-local`` prefill.
    """
    if value is None:
        return ""
    tz = viewer_zone(ctx.get("viewer_timezone"))
    # Stored datetimes are naive UTC; attach UTC before converting.
    aware = value.replace(tzinfo=datetime.UTC)
    local = aware.astimezone(tz)
    return local.strftime(fmt)


# Register once on the shared environment so every template can call
# ``{{ dt_value | localdt }}``.
templates.env.filters["localdt"] = _localdt_filter


def _tz_label_filter(tz_name: str | None, at: datetime.datetime | None = None) -> str:
    """Name the viewer's zone for a column header or an input label.

    A zone's abbreviation depends on the date (``EST`` in January, ``EDT`` in
    July), so a label that stands for many dates names the zone itself
    (``America/New_York``) rather than whichever abbreviation holds today
    (#125). Pass ``at``, the instant the label sits next to, to get the
    abbreviation in force then (``EST``/``EDT``, ``CET``); a zone with only a
    numeric abbreviation (``+0530`` style, which reads poorly as a label) is
    named instead. ``UTC`` is returned when no valid zone is stored. A naive
    ``at`` is read as UTC, like every stored datetime.
    """
    tz = viewer_zone(tz_name)
    if tz is datetime.UTC:
        return "UTC"
    if at is None:
        return str(tz_name)
    if at.tzinfo is None:
        at = at.replace(tzinfo=datetime.UTC)
    abbrev = at.astimezone(tz).strftime("%Z")
    if not abbrev or abbrev[0] in "+-" or abbrev[0].isdigit():
        return str(tz_name)
    return abbrev


templates.env.filters["tz_label"] = _tz_label_filter


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

# ``{{ project.capture_interval_seconds | interval_compact }}`` -> ``15m`` (#316).
templates.env.filters["interval_compact"] = format_interval_compact

# ``{{ static_url('js/x.js') }}`` -> ``/static/js/x.js?v=<content hash>``. It
# reads the manifest ``mount_web`` installs, at render time (see ``.assets``).
templates.env.globals["static_url"] = static_url

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
    if is_urlencoded(content_type):
        # Bounded by the same per-route table the body-size middleware applies
        # (issue #67, S6), so the read stops at the limit rather than buffering
        # whatever arrives; the middleware remains the outer backstop.
        try:
            body = await read_body_limited(request, _form_body_limit(request))
        except BodyTooLargeError:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Request body too large.",
            ) from None
        if body:
            pairs = urllib.parse.parse_qsl(body.decode("utf-8"))
            # Last value wins; these forms never repeat a field meaningfully.
            form = dict(pairs)
    request.state.parsed_form = form
    return form


def _form_body_limit(request: Request) -> int:
    """Return the byte limit for ``request``'s urlencoded body.

    Resolved by :func:`.body_limit.body_limit_for`, like the middleware. Without
    a running context (a bare unit test) the generic urlencoded cap applies.
    """
    try:
        settings = get_context().settings
    except Exception:
        return URLENCODED_FORM_MAX_BYTES
    return body_limit_for(
        request.method,
        request.scope["path"],
        request.headers.get("content-type", ""),
        settings,
    )


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

    The token is masked afresh for every call (#70 F-S4), so it differs on
    every response while always verifying against the session's secret.

    This lookup is the second session-touch path (every full page and partial
    rendered through :func:`base_context` runs it), so it applies the same
    passive-poll rule as the auth dependency (#70 F-S1).
    """
    secret = _live_csrf_secret(request, db)
    if not secret:
        return ""
    return issue_csrf(secret)


def csrf_presented_is_current(request: Request, db: DbSession) -> bool:
    """Return False only when the request carries a stale ``X-CSRF-Token``.

    Stale means: the header is present, the cookie resolves to a live session
    with a CSRF secret, and the header does not verify against that secret --
    typically because another tab signed in again and the browser now sends the
    new session's cookie with this page's old token (#70 F-R3). The check
    unmasks and verifies (:func:`verify_csrf`); string equality would call every
    masked token stale. With no header, or no live secret to rotate to, there is
    nothing to report, so the answer is True.

    Uses the same passive-poll touch rule as :func:`csrf_token_for`.
    """
    presented = request.headers.get("X-CSRF-Token")
    if not presented:
        return True
    secret = _live_csrf_secret(request, db)
    if not secret:
        return True
    return verify_csrf(secret, presented)


def _live_csrf_secret(request: Request, db: DbSession) -> str | None:
    """Return the CSRF secret of the request's live cookie session, or ``None``.

    Reads the request's single session resolution (#67 R3), which already applied
    the passive-poll touch rule and the directory re-check.
    """
    resolved = resolve_request_session(
        request, db, settings=get_context().settings.session
    )
    if resolved is None or not resolved.csrf_secret:
        return None
    return resolved.csrf_secret


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
    * ``reporting_enabled`` -- whether the "Report a problem" link is shown.
    """
    context: dict[str, Any] = {
        "request": request,
        "csrf_token": csrf_token_for(request, db),
        "current_user": user,
        "flash_messages": flash_messages or [],
        "viewer_timezone": getattr(user, "viewer_timezone", None),
        "user_theme": getattr(user, "theme_preference", "system"),
        "can_operate": getattr(user, "role", None) in (ADMIN_ROLE, OPERATOR_ROLE),
        # Whether the "Report a problem" link is shown (#8); any role may file.
        "reporting_enabled": get_context().settings.reporting.enabled,
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

    Whenever the request is effectively HTTPS (and ``session.host_prefix`` is on)
    the name gets the ``__Host-`` prefix (#67 S18): ``Secure``, ``Path=/`` and no
    ``Domain`` are what the prefix demands, and the same
    :func:`..security.authz.uses_host_prefix` test that decides the name decides
    ``Secure``, so a prefixed cookie is never sent without it (a browser would
    drop it and nobody could sign in). The old unprefixed cookie is deleted in the
    same response so it cannot shadow or outlive the new one.
    """
    session_settings = settings.session
    max_age = session_settings.persistent_timeout_seconds if persistent else None
    name = session_cookie_name(request, session_settings)
    response.set_cookie(
        key=name,
        value=raw_token,
        max_age=max_age,
        httponly=True,
        secure=is_secure_request(request),
        samesite=session_settings.samesite,
        path="/",
    )
    if name != session_settings.cookie_name:
        response.delete_cookie(key=session_settings.cookie_name, path="/")


def clear_session_cookie(response: Any, *, settings: Settings) -> None:
    """Remove the session cookie from the client, under both of its names.

    The prefixed deletion carries ``Secure``: a browser ignores a ``__Host-``
    cookie write that lacks it, deletion included.
    """
    cookie_name = settings.session.cookie_name
    prefixed = prefixed_cookie_name(cookie_name)
    response.delete_cookie(key=cookie_name, path="/")
    if prefixed != cookie_name:
        response.delete_cookie(key=prefixed, path="/", secure=True)


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
DbDep = DbSessionDep
FormDep = Annotated[dict[str, str], Depends(parsed_form)]
