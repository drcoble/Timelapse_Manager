"""Authorization dependencies for web (cookie-authenticated) requests.

These FastAPI dependencies sit on top of the session layer and gate web routes
on authentication and role. They read the session cookie named in
:class:`SessionSettings`, resolve it to a live user through :mod:`.sessions`, and
attach the authenticated user (and the cookie-auth signal) to ``request.state``
so downstream code -- including the CSRF check -- can tell a cookie-authenticated
request apart from a CLI bearer-token one.

Authorization is deny-by-default: :func:`require_role` admits only the roles
explicitly listed and rejects everything else with ``403``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, HTTPException, Request, status

from ..db.models import User
from ..db.session import DbSessionDep
from ..logging import recent_requests
from ..runtime import get_context
from .cookie_header import cookie_lookup

if TYPE_CHECKING:
    from ..config.settings import SessionSettings

#: Request header a background poll sets (``poll.js``, ``net.js``) to mark the
#: request as not user-initiated (#70 F-S1).
PASSIVE_HEADER = "X-TLM-Passive"
#: Seconds since the page last saw user input, sent alongside the passive mark.
IDLE_HEADER = "X-TLM-Idle"
# ASCII digits only and bounded: ``str.isdigit`` would also accept other
# Unicode digit characters, and an unbounded value is pointless.
_IDLE_VALUE = re.compile(r"[0-9]{1,9}")


#: Prefix that makes a browser accept a cookie only when it is ``Secure``, scoped
#: to ``Path=/`` and without a ``Domain`` -- so a sibling subdomain cannot set or
#: overwrite it (#67 S18).
HOST_PREFIX = "__Host-"


def effective_https(request: Request) -> bool:
    """Return whether the request is effectively HTTPS.

    The scheme middleware decides: TLS on the connection, or ``X-Forwarded-Proto``
    from a peer in ``server.trusted_proxies``. A request that has not passed
    through it counts as plain HTTP, the safe answer for a ``__Host-`` cookie
    (which must never be issued without ``Secure``).
    """
    return getattr(request.state, "effective_scheme", "http") == "https"


def prefixed_cookie_name(name: str) -> str:
    """Return ``name`` with the ``__Host-`` prefix (added once)."""
    return name if name.startswith(HOST_PREFIX) else HOST_PREFIX + name


def uses_host_prefix(request: Request, settings: SessionSettings) -> bool:
    """Return whether cookies for this request carry the ``__Host-`` prefix.

    Only when the request is effectively HTTPS: the prefix requires ``Secure``,
    and a browser silently drops a prefixed cookie set without it, which would
    lock everyone out. The decision is the one that sets ``Secure`` on the
    cookie, so the two cannot disagree.
    """
    return settings.host_prefix and effective_https(request)


def session_cookie_name(request: Request, settings: SessionSettings) -> str:
    """Return the name the session cookie is issued under for this request."""
    if uses_host_prefix(request, settings):
        return prefixed_cookie_name(settings.cookie_name)
    return settings.cookie_name


def session_cookie_value(request: Request, settings: SessionSettings) -> str | None:
    """Return the raw session cookie value this request carries, or ``None``.

    Exactly one name is read: the one :func:`session_cookie_name` issues for this
    request. Over HTTPS that is ``__Host-<cookie_name>``, and the unprefixed name
    is not looked at at all (a sibling subdomain can plant it, which is what the
    prefix prevents; the transition that honoured it ended after 0.2.0, #238).
    Over plain HTTP, or with ``host_prefix`` off, it is the unprefixed name. The
    lookup is the strict raw-header one (:func:`cookie_lookup`), so a name that
    matches only after Unicode strip, or a duplicate of the name, yields ``None``.
    ``None`` means no cookie at all: an empty value is still a cookie, so callers
    keep their own ``is None`` or falsy test. The raw value is never logged.
    """
    return cookie_lookup(request, session_cookie_name(request, settings))[0]


def has_session_cookie(request: Request) -> bool:
    """Return True if the request carries the session cookie.

    This is the discriminator the CSRF rule keys on: a browser sends the session
    cookie automatically on a cross-site request, so its *presence* (not its
    validity) is what marks a request as cookie-authenticated and therefore
    requiring a CSRF token. A CLI bearer-token call carries no such cookie and is
    exempt. Keying on cookie presence -- rather than on an ``Authorization``
    header -- prevents an attacker from suppressing the CSRF check by attaching a
    bogus bearer header. It reads the cookie through the same helper as the
    session lookup, so the two cannot disagree about which names count.
    """
    return session_cookie_value(request, get_context().settings.session) is not None


def session_touch_allowed(request: Request, settings: SessionSettings) -> bool:
    """Return whether this request may refresh the session's ``last_active``.

    Every request counts as activity unless it is marked passive
    (``X-TLM-Passive: 1``, sent by the background pollers). A passive request
    counts only when ``settings.keepalive_on_poll`` is on, or when its
    ``X-TLM-Idle`` header (whole seconds since the page last saw input) is below
    one touch interval, i.e. the user really was active just now. A missing or
    malformed idle value means "not recently active".

    The headers can only lower the touch rate below what an ordinary request
    gets, so a forged value gains nothing: the sender already holds the cookie
    and could simply send an unmarked request.
    """
    if request.headers.get(PASSIVE_HEADER) != "1":
        return True
    if settings.keepalive_on_poll:
        return True
    raw_idle = request.headers.get(IDLE_HEADER)
    if raw_idle is None or not _IDLE_VALUE.fullmatch(raw_idle):
        return False
    from .sessions import touch_interval

    return int(raw_idle) < touch_interval(settings).total_seconds()


def require_authenticated_session(
    request: Request,
    db: DbSessionDep,
) -> User:
    """Resolve the session cookie to a live :class:`User`, or raise ``401``.

    Reads the raw token from the configured session cookie and validates it
    through the session layer (which enforces revocation, idle/absolute timeouts,
    and the user being enabled), at most once per request. On success the user
    and a ``True`` cookie-auth marker are stashed on ``request.state`` for
    downstream use. A
    passive poll is validated in full but does not refresh the idle clock (see
    :func:`session_touch_allowed`).
    """
    settings = get_context().settings.session
    # Imported here so the module has no import-time dependency on the
    # session functions' transitive imports beyond what it already needs.
    from .sessions import live_principal_from_cache, resolve_request_session

    # Once per request: the CSRF middleware, the CSRF token for the page and any
    # principal gate share this answer (#67 R3). The cached answer may predate a
    # slow body read, so access is re-checked live (primary-key reads only, #207).
    resolved = resolve_request_session(request, db, settings=settings)
    user = live_principal_from_cache(db, resolved) if resolved is not None else None
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )
    request.state.user = user
    request.state.authenticated_via_cookie = True
    # Remember this request id against the user so a problem report can name
    # the reporter's recent requests (#8). Method and path only.
    request_id = getattr(request.state, "request_id", None)
    if isinstance(request_id, str):
        recent_requests.record(user.id, request_id, request.method, request.url.path)
    return user


def require_role(*allowed: str) -> Callable[..., User]:
    """Build a dependency admitting only users whose role is in ``allowed``.

    Deny-by-default: a user whose role is not explicitly listed is rejected with
    ``403`` even if authenticated. Authentication is enforced first (via
    :func:`require_authenticated_session`), so an unauthenticated request still
    fails with ``401``.
    """

    def dependency(
        user: Annotated[User, Depends(require_authenticated_session)],
    ) -> User:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient privileges.",
            )
        return user

    return dependency


def require_operator_or_admin() -> Callable[..., User]:
    """Build a dependency admitting the operator and admin roles.

    Operators hold the day-to-day operational surface (cameras, projects,
    renders, frames); admins keep everything operators have plus account and
    system administration. This wraps :func:`require_role` so the deny-by-default
    semantics -- ``401`` when unauthenticated, ``403`` for any other role -- are
    shared with the admin-only gate.
    """
    return require_role("operator", "admin")
