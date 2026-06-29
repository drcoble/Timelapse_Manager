"""ASGI middleware stack for the web UI.

These middlewares run inside the application (so they are exercised by a test
client, not only behind the real server) and layer up the request-time security
posture:

* :class:`EffectiveSchemeMiddleware` decides whether the request is effectively
  HTTPS -- from a real TLS connection or a trusted reverse proxy's
  ``X-Forwarded-Proto`` header -- and records it on ``request.state`` for
  everything downstream (redirect decision, the ``Secure`` cookie flag).
* :class:`HttpsRedirectMiddleware` bounces effective-HTTP requests to HTTPS with
  a ``308`` when redirection is enabled, so a browser that lands on the plaintext
  port is moved to the encrypted one without changing the method.
* :class:`FirstRunGateMiddleware` funnels every request to the first-run setup
  page until an administrator account exists, so a brand-new install cannot be
  driven before it is secured.
* :class:`CsrfMiddleware` enforces a synchronizer-token check on unsafe methods
  for cookie-authenticated requests, while leaving the CLI bearer-token path
  untouched.

Order matters and is documented at the wiring site (:mod:`.app_wiring`): the
effective scheme must be known before the redirect and cookie logic read it, and
the first-run gate must run before route handlers but after the redirect so the
setup page is itself served over HTTPS.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from ..config import Settings
from ..db.session import session_scope
from ..runtime import get_context
from ..security import (
    first_run_needed,
    get_session_row,
    has_session_cookie,
    verify_csrf,
)

logger = logging.getLogger(__name__)

_DispatchNext = Callable[[Request], Awaitable[Response]]

# Methods that never mutate state and therefore never require a CSRF token.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Header and form field a presented CSRF token may arrive in.
_CSRF_HEADER = "X-CSRF-Token"
_CSRF_FORM_FIELD = "csrf_token"

# Path prefixes the first-run gate and the redirect leave untouched so the setup
# flow, static assets, the liveness probe, and the CLI API are always reachable.
_FIRST_RUN_ALLOW_PREFIXES = ("/first-run", "/static", "/healthz", "/api/")

# The HTTP->HTTPS redirect applies to the human-facing web UI only. The machine
# endpoints stay reachable over plaintext: the CLI talks to the local API over
# loopback HTTP with a bearer token (a 308 to HTTPS would make the client drop
# the Authorization header on the cross-scheme hop), and liveness probes must not
# be bounced. TLS for the web UI is still enforced for every other path.
_REDIRECT_EXEMPT_PREFIXES = ("/api/", "/healthz")


def _settings() -> Settings:
    """Return the running process settings."""
    return get_context().settings


class EffectiveSchemeMiddleware(BaseHTTPMiddleware):
    """Record whether the request is effectively served over HTTPS.

    The effective scheme is HTTPS when the ASGI connection itself is TLS, or when
    a reverse proxy in front of the application asserts it through the
    ``X-Forwarded-Proto`` header. The result is stashed on
    ``request.state.effective_scheme`` (``"https"`` or ``"http"``) so the redirect
    decision and the ``Secure`` cookie flag share one source of truth.

    The forwarded header is only honoured because a deployment that terminates TLS
    at a proxy is expected to set it; a direct client cannot use it to forge a
    ``Secure`` cookie beyond what it could already do over a real TLS socket.
    """

    async def dispatch(self, request: Request, call_next: _DispatchNext) -> Response:
        forwarded = request.headers.get("x-forwarded-proto")
        if forwarded:
            # A proxy may pass a comma-separated list; the first entry is the
            # scheme the original client used.
            scheme = forwarded.split(",")[0].strip().lower()
        else:
            scheme = request.url.scheme
        request.state.effective_scheme = "https" if scheme == "https" else "http"
        return await call_next(request)


class HttpsRedirectMiddleware(BaseHTTPMiddleware):
    """Redirect effective-HTTP requests to HTTPS with a ``308``.

    Active only when ``server.redirect_http_to_https`` is set. A ``308`` (rather
    than ``301``/``302``) preserves the method and body, so a POST that lands on
    the plaintext port is replayed against HTTPS. The redirect target keeps the
    request host but swaps to the configured HTTPS port. When redirection is
    disabled the request passes through unchanged (the service runner closes the
    public HTTP port in that mode).
    """

    async def dispatch(self, request: Request, call_next: _DispatchNext) -> Response:
        settings = _settings()
        effective_scheme = getattr(request.state, "effective_scheme", "http")
        path = request.url.path
        exempt = path.startswith(_REDIRECT_EXEMPT_PREFIXES)
        if (
            not exempt
            and settings.server.redirect_http_to_https
            and effective_scheme != "https"
        ):
            return RedirectResponse(
                url=self._https_url(request, settings.server.https_port),
                status_code=308,
            )
        return await call_next(request)

    @staticmethod
    def _https_url(request: Request, https_port: int) -> str:
        """Build the HTTPS URL for the same request on the HTTPS port."""
        url = request.url.replace(scheme="https")
        host = url.hostname or "localhost"
        # Standard HTTPS port is implicit; any other is made explicit.
        if https_port == 443:
            url = url.replace(netloc=host)
        else:
            url = url.replace(netloc=f"{host}:{https_port}")
        return str(url)


class FirstRunGateMiddleware(BaseHTTPMiddleware):
    """Force all traffic to the first-run setup page until an admin exists.

    Before any real administrator account is created the application is
    unsecured, so every non-allowlisted request is redirected to ``/first-run``.
    Once setup is complete the gate is transparent. The allowlist keeps the setup
    page itself, static assets, the liveness probe, and the CLI API reachable so
    the gate cannot deadlock the bootstrap.
    """

    async def dispatch(self, request: Request, call_next: _DispatchNext) -> Response:
        path = request.url.path
        if not path.startswith(_FIRST_RUN_ALLOW_PREFIXES) and self._first_run_needed():
            return RedirectResponse(url="/first-run", status_code=303)
        return await call_next(request)

    @staticmethod
    def _first_run_needed() -> bool:
        """Return whether first-run setup is still required.

        Any failure to consult the database (e.g. before migrations run) is
        treated as *not* requiring first-run so the gate never wedges startup;
        the setup routes themselves re-check authoritatively.
        """
        try:
            factory = get_context().session_factory
            with session_scope(factory) as db:
                return first_run_needed(db)
        except Exception:
            return False


class CsrfMiddleware(BaseHTTPMiddleware):
    """Enforce a CSRF synchronizer token on unsafe, cookie-authenticated requests.

    The check keys on the *presence of the session cookie*, not on any
    ``Authorization`` header: a browser auto-sends the session cookie on a
    cross-site request, so a cookie-bearing unsafe request must echo a matching
    CSRF token. A request with no session cookie is the CLI bearer-token path and
    is exempt -- there is no ambient credential for an attacker to ride.

    Critically, CSRF is required only when the cookie resolves to a *live*
    session (one that has a CSRF secret). A present-but-dead cookie (expired or
    bogus) is treated as exempt, so a user whose session has lapsed can still
    ``POST`` to ``/login`` -- the login page cannot mint a token for a session
    that no longer exists. Safe methods never require a token.
    """

    async def dispatch(self, request: Request, call_next: _DispatchNext) -> Response:
        if request.method in _SAFE_METHODS or not has_session_cookie(request):
            return await call_next(request)

        expected = self._expected_secret(request)
        if expected is None:
            # Cookie present but not a live session: nothing to protect, and
            # requiring a token here would lock out re-login. Let it through; the
            # route's own auth dependency rejects an unauthenticated mutation.
            return await call_next(request)

        presented = await self._presented_token(request)
        if not verify_csrf(expected, presented):
            return PlainTextResponse("CSRF validation failed.", status_code=403)
        return await call_next(request)

    @staticmethod
    def _expected_secret(request: Request) -> str | None:
        """Return the live session's CSRF secret, or ``None`` if not live."""
        settings = _settings()
        raw_token = request.cookies.get(settings.session.cookie_name)
        if not raw_token:
            return None
        try:
            factory = get_context().session_factory
            with session_scope(factory) as db:
                row = get_session_row(db, raw_token, settings=settings.session)
                if row is None:
                    return None
                return row.csrf_secret
        except Exception:
            # A database hiccup must not become a CSRF bypass: fail closed by
            # treating it as "no expected secret" only when there is genuinely no
            # session to protect would be unsafe, so here we deny instead.
            return _CSRF_DENY

    @staticmethod
    async def _presented_token(request: Request) -> str | None:
        """Read the presented CSRF token from the header or a form field.

        Prefers the ``X-CSRF-Token`` header (the HTMX path). Otherwise reads the
        ``csrf_token`` field from the urlencoded body via the shared parser, whose
        result is cached on ``request.state`` so the route handler reading the
        same form does not consume the body a second time.
        """
        header_token = request.headers.get(_CSRF_HEADER)
        if header_token:
            return header_token
        from .dependencies import parsed_form

        form = await parsed_form(request)
        return form.get(_CSRF_FORM_FIELD)


# Sentinel returned when the CSRF secret lookup fails unexpectedly: it can never
# equal a real (high-entropy) presented token, so verification fails closed.
_CSRF_DENY = "\x00csrf-lookup-failed\x00"
