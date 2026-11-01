"""Login-CSRF protection for the pages that have no session yet (#67 S21).

``/login`` and ``/first-run`` are used before a session exists, so the session
CSRF token cannot protect them. Without something else, any site could POST a
victim's browser to ``/login`` with the attacker's credentials and leave the
victim signed in as the attacker (login CSRF).

The synchronizer pattern is reused with a throw-away secret: ``GET`` sets a
random ``tlm_presession`` cookie and renders a token derived from it as the
form's hidden ``csrf_token``; the ``POST`` must present a token that verifies
against the cookie. As for the session token, the rendered value is masked
afresh per response (:func:`..security.csrf.mask_secret`), so it differs on every
render while always verifying against the cookie's secret.

The cookie is ``HttpOnly`` and ``SameSite=Strict``, ``Secure`` when the request is
effectively HTTPS, and then named ``__Host-tlm_presession``: a plain double-submit
cookie can be defeated by planting a cookie from a sibling subdomain, which is the
very attack the prefix exists to stop. So while ``session.host_prefix`` is on,
the unprefixed name is never read over HTTPS (with it off, the unprefixed name
is the only name used, as for the session cookie).
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Any

from fastapi import Request

from ..runtime import get_context
from ..security.authz import effective_https, prefixed_cookie_name, uses_host_prefix
from ..security.cookie_header import cookie_value
from ..security.csrf import mask_secret, verify_csrf

#: Unprefixed cookie name (plain HTTP); HTTPS adds ``__Host-``.
PRESESSION_COOKIE = "tlm_presession"
#: Shown on the sign-in and first-run pages when the check fails.
PRESESSION_RETRY_MESSAGE = "Your sign-in page expired. Please try again."

_SECRET_BYTES = 32
# ``token_urlsafe(32)``: 43 characters of the URL-safe base64 alphabet. A cookie
# that is anything else is not ours (or is a plant) and is replaced.
_WELL_FORMED = re.compile(r"[A-Za-z0-9_-]{43}")


@dataclass(frozen=True)
class PreSession:
    """The pre-session secret for a request.

    :param secret: the random value held in the cookie.
    :param is_new: whether it was just minted, so the response must set the cookie.
    """

    secret: str
    is_new: bool

    def token(self) -> str:
        """Return a freshly masked form token for this secret."""
        return mask_secret(self.secret)


def _cookie_name(request: Request) -> str:
    """Return the name the cookie is issued and read under for this request."""
    if uses_host_prefix(request, get_context().settings.session):
        return prefixed_cookie_name(PRESESSION_COOKIE)
    return PRESESSION_COOKIE


def _existing_secret(request: Request) -> str | None:
    """Return the request's well-formed pre-session secret, or ``None``."""
    value = cookie_value(request, _cookie_name(request))
    if value is None or _WELL_FORMED.fullmatch(value) is None:
        return None
    return value


def presession_for(request: Request) -> PreSession:
    """Return the request's pre-session, minting one when there is none.

    A well-formed cookie is reused, so a second tab opened on the sign-in page
    does not invalidate the first.
    """
    existing = _existing_secret(request)
    if existing is not None:
        return PreSession(existing, is_new=False)
    return PreSession(secrets.token_urlsafe(_SECRET_BYTES), is_new=True)


def attach_presession(response: Any, request: Request, presession: PreSession) -> None:
    """Set the pre-session cookie on ``response`` if it was minted for this request."""
    if not presession.is_new:
        return
    response.set_cookie(
        key=_cookie_name(request),
        value=presession.secret,
        httponly=True,
        secure=effective_https(request),
        samesite="strict",
        path="/",
    )


def clear_presession(response: Any, request: Request) -> None:
    """Delete the pre-session cookie once it has done its job.

    Only the name this request would have been issued under exists in the
    browser: a ``__Host-`` cookie is never stored over plain HTTP, and the plain
    one is never read over HTTPS.
    """
    prefixed = uses_host_prefix(request, get_context().settings.session)
    response.delete_cookie(key=_cookie_name(request), path="/", secure=prefixed)


def presession_valid(request: Request, presented: str | None) -> bool:
    """Return whether ``presented`` is a token for the request's pre-session cookie.

    A missing or malformed cookie, and a missing or empty field, never match; the
    comparison runs through :func:`verify_csrf` (constant time, never raises on
    odd input).
    """
    secret = _existing_secret(request)
    if secret is None or not presented:
        return False
    return verify_csrf(secret, presented)
