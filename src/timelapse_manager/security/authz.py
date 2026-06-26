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

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session as DbSession

from ..db.models import User
from ..db.session import get_session
from ..runtime import get_context


def has_session_cookie(request: Request) -> bool:
    """Return True if the request carries the session cookie.

    This is the discriminator the CSRF rule keys on: a browser sends the session
    cookie automatically on a cross-site request, so its *presence* (not its
    validity) is what marks a request as cookie-authenticated and therefore
    requiring a CSRF token. A CLI bearer-token call carries no such cookie and is
    exempt. Keying on cookie presence -- rather than on an ``Authorization``
    header -- prevents an attacker from suppressing the CSRF check by attaching a
    bogus bearer header.
    """
    cookie_name = get_context().settings.session.cookie_name
    return request.cookies.get(cookie_name) is not None


def require_authenticated_session(
    request: Request,
    db: Annotated[DbSession, Depends(get_session)],
) -> User:
    """Resolve the session cookie to a live :class:`User`, or raise ``401``.

    Reads the raw token from the configured session cookie and validates it
    through the session layer (which enforces revocation, idle/absolute timeouts,
    and the user being enabled). On success the user and a ``True``
    cookie-auth marker are stashed on ``request.state`` for downstream use.
    """
    settings = get_context().settings.session
    raw_token = request.cookies.get(settings.cookie_name)
    user: User | None = None
    if raw_token:
        # Imported here so the module has no import-time dependency on the
        # session functions' transitive imports beyond what it already needs.
        from .sessions import lookup_session

        user = lookup_session(db, raw_token, settings=settings)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )
    request.state.user = user
    request.state.authenticated_via_cookie = True
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
