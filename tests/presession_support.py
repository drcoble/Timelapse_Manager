"""Test-client support for the pre-session login token (#67 S21).

``POST /login`` and ``POST /first-run`` need the token their own page renders,
checked against the ``tlm_presession`` cookie. Most tests only want to sign in, so
:class:`WebTestClient` fetches the page first and supplies the token when the
test's form data has no ``csrf_token`` key. A test that checks the refusal passes
``csrf_token`` itself (an empty or wrong one), which is left alone.

:func:`session_cookie` reads the session cookie under either of its names, so
tests do not care whether the client is effectively HTTPS (``__Host-`` prefix) or
HTTP.
"""

from __future__ import annotations

import re
import secrets
from typing import Any
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from timelapse_manager.runtime import get_context
from timelapse_manager.security.authz import prefixed_cookie_name
from timelapse_manager.security.csrf import mask_secret

_PRESESSION_PATHS = frozenset({"/login", "/first-run"})
_PRESESSION_NAME = "tlm_presession"


def presession_cookie(client: TestClient) -> str | None:
    """Return the client's pre-session secret under whichever name it holds it."""
    return client.cookies.get(prefixed_cookie_name(_PRESESSION_NAME)) or (
        client.cookies.get(_PRESESSION_NAME)
    )


def session_cookie(client: TestClient) -> str | None:
    """Return the client's session token under the prefixed or the plain name."""
    name = get_context().settings.session.cookie_name
    return client.cookies.get(prefixed_cookie_name(name)) or client.cookies.get(name)


def session_set_cookie(response: Any) -> str:
    """Return the ``Set-Cookie`` header that sets the session cookie, or ``""``.

    A login response also carries the pre-session cookie's deletion, so tests
    about the session cookie's attributes look at its header alone. A deletion
    of the session cookie (``Max-Age=0``) is not what is meant here.
    """
    for header in response.headers.get_list("set-cookie"):
        name = header.split("=", 1)[0]
        if name.endswith("tlm_session") and "Max-Age=0" not in header:
            return str(header)
    return ""


_TOKEN_VALUE = re.compile(
    r'(name="csrf_token"\s+content="|name="csrf_token" value=")[^"]*'
)
_META_VALUE = re.compile(r'(<meta name="csrf-token" content=")[^"]*')


def without_token(html: str) -> str:
    """Return ``html`` with the (random, masked) form token blanked.

    The token differs on every render by design, so two renders of the same
    page are compared with it removed.
    """
    return _META_VALUE.sub(r"\1", _TOKEN_VALUE.sub(r"\1", html))


def ensure_presession(client: TestClient) -> str:
    """Return the client's pre-session secret, planting the cookie if need be.

    Post :func:`presession_token` (the masked form), never this bare secret: the
    app refuses the bare secret (#188).

    Plants the pre-session cookie itself, under both of its names (which one the
    app reads depends on the request's effective scheme), instead of fetching the
    page: it keeps a test that checks the app's state before the first page load
    (for example "no setup-code file yet") unchanged.
    """
    existing = presession_cookie(client)
    if existing is not None:
        return existing
    secret = secrets.token_urlsafe(32)
    client.cookies.set(_PRESESSION_NAME, secret)
    client.cookies.set(prefixed_cookie_name(_PRESESSION_NAME), secret)
    return secret


def presession_token(client: TestClient) -> str:
    """Return a masked pre-session token the client's next login POST will pass."""
    return mask_secret(ensure_presession(client))


class WebTestClient(TestClient):
    """A :class:`TestClient` that carries the pre-session token on login POSTs."""

    def request(self, method: str, url: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        path = urlsplit(str(url)).path
        data = kwargs.get("data")
        if (
            method.upper() == "POST"
            and path in _PRESESSION_PATHS
            and isinstance(data, dict)
            and "csrf_token" not in data
        ):
            kwargs["data"] = {**data, "csrf_token": presession_token(self)}
        return super().request(method, url, **kwargs)
