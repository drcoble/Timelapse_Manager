"""Security-response-header middleware for the web UI.

:class:`SecurityHeadersMiddleware` stamps a fixed set of hardening headers onto
every response so a browser enforces clickjacking, MIME-sniffing, referrer, and
content-source protections regardless of which route produced the response.
:func:`apply_security_headers` applies the same set to a response built outside
the middleware stack (a 500 handler), so both paths share one definition.

Header choices:

* ``X-Content-Type-Options: nosniff`` -- never let the browser second-guess a
  declared content type.
* ``X-Frame-Options: DENY`` plus a CSP ``frame-ancestors 'none'`` -- belt and
  braces against framing/clickjacking (the legacy header for old browsers, the
  CSP directive for current ones).
* ``Referrer-Policy: no-referrer`` -- never leak the current URL to any
  navigation or sub-resource origin.
* ``Permissions-Policy`` -- disable powerful features the UI never uses.
* ``Strict-Transport-Security`` -- only on effective-HTTPS responses, so the
  plaintext HTTP fallback (used by the local CLI/loopback path) is never pinned
  to HTTPS by a stray header.

Content-Security-Policy: ``default-src 'self'`` with these deliberate
relaxations, kept as tight as the existing server-rendered UI allows:

* ``img-src 'self' data:`` -- inline SVG icon sprites are referenced as
  ``data:`` URIs.
* ``style-src 'self' 'unsafe-inline'`` -- the templates carry hundreds of inline
  ``style="..."`` attributes (and one inline ``<style>`` block); these cannot be
  enumerated as hashes, so inline styles are permitted.
* ``script-src 'self'`` -- no ``'unsafe-inline'`` and no ``'unsafe-eval'``
  (#70 F-S3, #67-S17). Every script is an external file under ``static/js``;
  the templates carry no inline ``<script>`` and no ``on*=`` handler, and
  behaviour is reached through ``data-action`` (``actions.js``). Per-request
  values the old bootstrap scripts inlined (theme preference, viewer time
  zone) travel as ``data-*`` attributes on ``<html>``. htmx is configured with
  ``allowEval: false`` and ``allowScriptTags: false``, so a swapped fragment
  can never run script either. ``object-src 'none'`` and ``base-uri 'self'``
  close the remaining injection vectors, and ``form-action 'self'`` keeps form
  posts on the origin.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# A single source-of-truth Content-Security-Policy for every response. See the
# module docstring for why each non-``'self'`` source is present.
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; "
    "connect-src 'self'"
)

# Lock down powerful browser features the UI never invokes. ``interest-cohort``
# additionally opts out of FLoC topic sampling.
_PERMISSIONS_POLICY = "geolocation=(), camera=(), microphone=(), interest-cohort=()"

# One-year HSTS covering sub-domains; only emitted on effective-HTTPS responses.
_STRICT_TRANSPORT_SECURITY = "max-age=31536000; includeSubDomains"


def apply_security_headers(request: Request, response: Response) -> None:
    """Set the security headers on ``response`` (HSTS only over HTTPS).

    The single public entry point for code that builds a response outside the
    middleware's reach -- e.g. a 500 handler running in Starlette's
    ``ServerErrorMiddleware``, which sits outside every user middleware. The
    middleware itself stamps the same headers through the same helper.
    """
    _set_security_headers(response.headers, _effective_scheme(request))


def _effective_scheme(request: Request) -> str:
    """Return the scheme the HSTS decision keys on.

    :class:`~.middleware.EffectiveSchemeMiddleware` records it on
    ``request.state``; a request that somehow bypassed that layer falls back to
    the connection's own scheme.
    """
    return getattr(request.state, "effective_scheme", request.url.scheme)


def _set_security_headers(headers: MutableHeaders, effective_scheme: str) -> None:
    """Write the fixed header set into ``headers``, replacing any existing values."""
    headers["X-Content-Type-Options"] = "nosniff"
    headers["X-Frame-Options"] = "DENY"
    headers["Referrer-Policy"] = "no-referrer"
    headers["Permissions-Policy"] = _PERMISSIONS_POLICY
    headers["Content-Security-Policy"] = _CONTENT_SECURITY_POLICY
    if effective_scheme == "https":
        headers["Strict-Transport-Security"] = _STRICT_TRANSPORT_SECURITY


class SecurityHeadersMiddleware:
    """Stamp the fixed security headers onto every response.

    Runs inside :class:`EffectiveSchemeMiddleware` so ``request.state`` already
    carries the effective scheme by the time the HSTS decision is made. It also
    sits outside the redirect and first-run gates, so even their redirect
    responses leave with the full header set.

    A pure ASGI middleware: the headers are written into the
    ``http.response.start`` message as it passes, and every body message is
    forwarded untouched, so a streamed response is never buffered.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                # Read the scheme now, not at entry: this matches the old
                # post-``call_next`` read, after every inner layer has run.
                scheme = _effective_scheme(Request(scope))
                _set_security_headers(MutableHeaders(scope=message), scheme)
            await send(message)

        await self.app(scope, receive, send_with_headers)
