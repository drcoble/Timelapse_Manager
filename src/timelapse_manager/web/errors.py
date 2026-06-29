"""Web error handling: send unauthenticated browsers to the login page.

The cookie-auth guard raises ``401`` when a request carries no live session. For
a machine caller (the local API, a liveness probe, or an explicit JSON client)
that bare ``401`` is the right answer. For a *browser* navigating to a UI route,
returning a bare error is hostile -- the human should be taken to the login page
and, after signing in, returned to where they were headed.

:func:`auth_redirect_exception_handler` implements exactly that: it converts a
``401`` into a ``303`` redirect to ``/login`` with the originally-requested path
preserved as a same-origin ``next`` parameter, while leaving every other status
code -- and the machine paths -- untouched.
"""

from __future__ import annotations

from urllib.parse import quote, urlsplit

from fastapi import FastAPI
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

# Path prefixes whose 401s stay machine-readable (bare 401/JSON) and are never
# turned into a browser login redirect: the CLI/local API and the liveness probe.
_NO_REDIRECT_PREFIXES = ("/api/", "/healthz")


def _is_browser_navigation(request: Request) -> bool:
    """Return True when this 401 should become a login redirect.

    Only a genuine top-level browser navigation -- one whose ``Accept`` asks for
    ``text/html`` -- or an HTMX request is redirected. A sub-resource fetch (an
    ``<img>`` asking for ``image/*``), an API client, the liveness probe, and any
    programmatic ``Accept: */*``/JSON caller all keep the bare ``401``, for which a
    redirect to an HTML login page would be useless.
    """
    if request.url.path.startswith(_NO_REDIRECT_PREFIXES):
        return False
    if request.headers.get("HX-Request") == "true":
        return True
    return "text/html" in request.headers.get("accept", "")


def _path_from_current_url(current: str | None) -> str | None:
    """Extract the path (and query) from an ``HX-Current-URL`` header value.

    HTMX sends the browser's current address-bar URL on every request. It is a
    real page on this origin, so its path is a valid post-login landing spot.
    Returns ``None`` when the header is absent or carries no usable path.
    """
    if not current:
        return None
    parts = urlsplit(current)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return path


def _return_to_path(request: Request) -> str | None:
    """The path to send the user back to after login, or ``None`` for bare login.

    A background HTMX fetch (the alerts-panel poll, a status/ribbon refresh) has a
    request path that is a server-rendered *fragment* endpoint -- not a page the
    user can land on. Returning to it post-login drops the user on an unstyled
    partial. So for a non-boosted HTMX request the return-to is taken from the
    browser's current page (``HX-Current-URL``), not the polled endpoint. An
    hx-boosted request is a genuine navigation, so its own path is the
    destination. A non-HTMX request keeps its own path, but only for ``GET`` --
    replaying a non-GET as a post-login GET is meaningless.
    """
    is_htmx = request.headers.get("HX-Request") == "true"
    is_boosted = request.headers.get("HX-Boosted") == "true"
    if is_htmx and not is_boosted:
        return _path_from_current_url(request.headers.get("HX-Current-URL"))
    if request.method != "GET":
        return None
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return target


def _login_url(request: Request) -> str:
    """Build the ``/login`` URL, preserving a safe return-to path as ``next``.

    The return-to is the user's actual page (see :func:`_return_to_path`),
    percent-encoded into the query; when there is none, a bare ``/login`` is used.
    The login route re-validates ``next`` as a same-origin navigable path, so this
    only needs to choose the right page, not re-prove its safety.
    """
    target = _return_to_path(request)
    if not target:
        return "/login"
    return f"/login?next={quote(target, safe='')}"


async def auth_redirect_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> Response:
    """Redirect unauthenticated browser requests to the login page.

    A ``401`` from the cookie-auth guard means "no live session". For a browser
    navigation to a UI route this redirects to ``/login`` (preserving the
    originally-requested path as ``next``) instead of returning a bare error.
    HTMX requests get an ``HX-Redirect`` so the client performs a full-page
    navigation rather than swapping the login page into a fragment target. Every
    other status code -- and the machine paths -- fall through to the default
    handler unchanged.
    """
    if exc.status_code == 401 and _is_browser_navigation(request):
        login_url = _login_url(request)
        if request.headers.get("HX-Request") == "true":
            response: Response = Response(status_code=204)
            response.headers["HX-Redirect"] = login_url
            return response
        return RedirectResponse(url=login_url, status_code=303)
    return await http_exception_handler(request, exc)


def install_error_handlers(app: FastAPI) -> None:
    """Register the web error handlers on the application."""
    # Starlette types handlers as accepting a bare ``Exception``; ours narrows to
    # the HTTPException it is registered for, which is what arrives at runtime.
    app.add_exception_handler(
        StarletteHTTPException,
        auth_redirect_exception_handler,  # type: ignore[arg-type]
    )
