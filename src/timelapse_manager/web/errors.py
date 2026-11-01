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

import json
import logging
import math
from html import escape
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)

from ..services.errors import AppError
from .cache_headers import apply_cache_defaults
from .dependencies import templates
from .safe_redirect import safe_local_path
from .security_headers import apply_security_headers

logger = logging.getLogger(__name__)

# Path prefixes whose errors stay machine-readable (bare status/JSON) and are
# never turned into a browser page: the CLI/local API and the liveness probe.
_NO_REDIRECT_PREFIXES = ("/api/", "/healthz")


def _wants_machine_response(request: Request) -> bool:
    """Return True when this request should get a machine (JSON/bare) response.

    A machine caller -- the local API and CLI, the liveness probe, an explicit
    JSON client, or any programmatic ``Accept: */*`` caller -- gets a bare
    status/JSON body. A genuine top-level browser navigation (its ``Accept`` asks
    for ``text/html``) or an HTMX request instead gets a browser-appropriate
    response (a login redirect for a 401, a rendered page/fragment for an
    :class:`~timelapse_manager.services.errors.AppError`). This is the single
    discriminator both error handlers share.
    """
    if request.url.path.startswith(_NO_REDIRECT_PREFIXES):
        return True
    if request.headers.get("HX-Request") == "true":
        return False
    return "text/html" not in request.headers.get("accept", "")


def _is_browser_navigation(request: Request) -> bool:
    """Return True when this 401 should become a login redirect.

    The inverse of :func:`_wants_machine_response`: a browser navigation or HTMX
    request (never a machine caller) is what a login redirect is meaningful for.
    """
    return not _wants_machine_response(request)


def _path_from_current_url(request: Request, current: str | None) -> str | None:
    """Extract the path (and query) from an ``HX-Current-URL`` header value.

    HTMX sends the browser's current address-bar URL on every request, so on an
    honest request it is a real page on this origin and its path is a valid
    post-login landing spot. But any client can send the header, so it is not
    trusted: the URL must be http(s) with either no host or exactly the
    request's ``Host``, and the path must pass
    :func:`.safe_redirect.safe_local_path`. Returns ``None`` when the header is
    absent or fails any of that, which gives a bare ``/login``.
    """
    if not current:
        return None
    try:
        parts = urlsplit(current)
    except ValueError:
        return None
    if parts.scheme not in ("", "http", "https"):
        return None
    if parts.netloc and parts.netloc.lower() != request.headers.get("host", "").lower():
        return None
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return safe_local_path(path)


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
        return _path_from_current_url(request, request.headers.get("HX-Current-URL"))
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


async def app_error_handler(request: Request, exc: AppError) -> Response:
    """Render a service-layer :class:`AppError` for the caller's surface.

    A machine caller (see :func:`_wants_machine_response`) gets a JSON body
    carrying the human ``detail`` and the stable machine ``code`` at the error's
    status. A browser gets an HTML response instead: a ``403`` renders the
    access-denied page; an HTMX request gets an inline error fragment it can swap
    into the page; anything else renders a minimal generic error page. Every
    branch preserves the error's status code.

    The request's database session is already closed by the time an exception
    handler runs, so this handler never touches the session -- it renders from the
    exception alone.
    """
    if _wants_machine_response(request):
        return JSONResponse(
            {"detail": exc.detail, "code": exc.code},
            status_code=exc.status,
        )
    if exc.status == 403:
        return templates.TemplateResponse(
            request,
            "errors/403.html",
            {"request": request, "detail": exc.detail},
            status_code=exc.status,
        )
    if request.headers.get("HX-Request") == "true":
        return HTMLResponse(
            f'<div class="inline-error" role="alert">{escape(exc.detail)}</div>',
            status_code=exc.status,
        )
    return templates.TemplateResponse(
        request,
        "errors/generic.html",
        {"request": request, "detail": exc.detail},
        status_code=exc.status,
    )


def _server_error_message(request_id: str | None) -> str:
    """The human text for an unhandled error, quoting the request id when known."""
    if request_id:
        return f"Server error (request id {request_id})"
    return "Server error"


def _render_server_error(request: Request, request_id: str | None) -> Response:
    """Build the caller-appropriate ``500`` body (headers are added afterwards).

    Mirrors :func:`app_error_handler`'s dispatch: JSON for a machine caller, an
    inline-error fragment plus a ``tlm:server-error`` trigger for HTMX (htmx does
    not swap a 5xx, so ``htmx-errors.js`` toasts the fragment's text), and the
    styled standalone error page for a browser navigation.
    """
    message = _server_error_message(request_id)
    if _wants_machine_response(request):
        return JSONResponse(
            {"detail": "Internal Server Error", "request_id": request_id},
            status_code=500,
        )
    if request.headers.get("HX-Request") == "true":
        response = HTMLResponse(
            f'<div class="inline-error" role="alert">{escape(message)}</div>',
            status_code=500,
        )
        response.headers["HX-Trigger"] = json.dumps(
            {"tlm:server-error": {"requestId": request_id}}
        )
        return response
    try:
        return templates.TemplateResponse(
            request,
            "errors/generic.html",
            {"request": request, "detail": message},
            status_code=500,
        )
    except Exception:  # noqa: BLE001 -- a broken template must not hide the 500
        logger.exception("could not render the styled 500 page")
        return HTMLResponse(
            f"<!DOCTYPE html><title>Server error</title><p>{escape(message)}</p>",
            status_code=500,
        )


async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
    """Answer an unhandled exception with a styled, header-complete ``500``.

    Starlette runs an ``Exception`` handler inside ``ServerErrorMiddleware``,
    which wraps the whole user middleware stack. The response therefore bypasses
    the security-header, cache-control and request-id layers, so this handler
    applies them itself: the shared :func:`apply_security_headers` (CSP and the
    rest), the ``no-store`` cache defaults, and the ``X-Request-ID`` the request
    was tagged with. Starlette re-raises ``exc`` after sending this response, so
    the server still logs the traceback; nothing about ``exc`` reaches the body.
    """
    request_id = getattr(request.state, "request_id", None)
    response = _render_server_error(request, request_id)
    apply_security_headers(request, response)
    apply_cache_defaults(response.headers)
    if request_id:
        response.headers["X-Request-ID"] = request_id
    return response


def _finite_json(value: Any) -> Any:
    """Return ``value`` with every non-finite float replaced by its JSON spelling.

    ``json.loads`` accepts ``NaN``, ``Infinity`` and ``-Infinity``, but
    Starlette's :class:`JSONResponse` encodes with ``allow_nan=False``. A float
    like that inside an echoed validation input would turn the 422 into a 500
    (#97), so it is sent back as the string ``"NaN"``/``"Infinity"`` instead.
    """
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, dict):
        return {k: _finite_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_finite_json(v) for v in value]
    return value


async def request_validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Answer a request-validation failure with FastAPI's 422 body, JSON-safe.

    The body is the same ``{"detail": [...]}`` FastAPI's default handler sends;
    only non-finite floats in it are rewritten (see :func:`_finite_json`).
    """
    detail = _finite_json(jsonable_encoder(exc.errors()))
    return JSONResponse(status_code=422, content={"detail": detail})


def install_error_handlers(app: FastAPI) -> None:
    """Register the web error handlers on the application.

    The handlers dispatch by exception *type*: the auth redirect handles the
    cookie-guard's ``HTTPException``, :func:`app_error_handler` handles a
    service-layer :class:`AppError`, and :func:`unhandled_exception_handler`
    answers anything else with a styled ``500``. They do not overlap.
    """
    # Starlette types handlers as accepting a bare ``Exception``; ours narrows to
    # the HTTPException it is registered for, which is what arrives at runtime.
    app.add_exception_handler(
        StarletteHTTPException,
        auth_redirect_exception_handler,  # type: ignore[arg-type]
    )
    app.add_exception_handler(
        AppError,
        app_error_handler,  # type: ignore[arg-type]
    )
    app.add_exception_handler(
        RequestValidationError,
        request_validation_exception_handler,  # type: ignore[arg-type]
    )
    # A bare ``Exception`` handler is Starlette's last resort: it runs in
    # ``ServerErrorMiddleware`` only for errors nothing above handled.
    app.add_exception_handler(Exception, unhandled_exception_handler)
