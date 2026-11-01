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
* :class:`AllowedHostsMiddleware` answers ``400`` to a request whose ``Host``
  is not in ``server.allowed_hosts``; it is installed only when that setting
  is not ``*``.

Order matters and is documented at the wiring site (:mod:`.app_wiring`): the
effective scheme must be known before the redirect and cookie logic read it, and
the first-run gate must run before route handlers but after the redirect so the
setup page is itself served over HTTPS.

The first-run gate and the CSRF check read the database, and the engine is
synchronous, so their queries run in the worker threadpool, never on the event
loop (issue #37): a blocking checkout or lock wait on the loop thread freezes
every request in the process, including the one whose commit would end the wait.

Every class here is a pure ASGI middleware rather than a
:class:`~starlette.middleware.base.BaseHTTPMiddleware`: that base class runs the
inner app in a separate task and pipes every response message through an
in-memory stream, which wraps each streamed body (render playback, exports) in
one extra hop per layer and complicates client-disconnect handling. Here each
layer either answers early or awaits the inner app directly in the same task.
"""

from __future__ import annotations

import ipaddress
import logging
import threading
import weakref
from collections import deque

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import URL, Headers
from starlette.requests import Request
from starlette.responses import (
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config import Settings
from ..config.settings import HOST_NAME_PATTERN
from ..db.session import session_scope
from ..runtime import get_context
from ..security import (
    first_run_needed,
    has_session_cookie,
    verify_csrf,
)
from ..security.authz import session_cookie_value
from ..security.csrf import fetch_site_rejected
from ..security.proxy_networks import (
    IpAddress,
    IpNetwork,
    in_networks,
    parse_address,
    parse_networks,
    parse_networks_cached,
)
from ..security.sessions import (
    invalidate_request_session,
    live_principal_from_cache,
    resolve_request_session,
)
from ..security.token import (
    _extract_bearer,
    enforce_token_peer,
    invalid_token_error,
    require_local_token,
)
from .multipart import (
    FRAME_IMPORT_PATH,
    FRAME_UPLOAD_PATH,
    MAX_FIELD_BYTES,
    REPORT_FORM_PATH,
    BodyTooLargeError,
    FormEnd,
    MultipartError,
    MultipartParser,
    PartData,
    PartEnd,
    PartStart,
    multipart_boundary,
    parse_multipart,
    read_body_limited,
    report_body_limit,
)

logger = logging.getLogger(__name__)

# Methods that never mutate state and therefore never require a CSRF token.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Methods the first-run gate lets through when it cannot tell whether setup is
# complete: the reads a health check or a page load makes.
_GATE_PASS_METHODS = frozenset({"GET", "HEAD"})

# Header and form field a presented CSRF token may arrive in.
_CSRF_HEADER = "X-CSRF-Token"
_CSRF_FORM_FIELD = "csrf_token"

# ``HX-Trigger`` value on an HTMX request's CSRF 403: ``htmx-errors.js`` turns
# the ``tlm:csrf-failed`` event into a "session changed" toast with a Reload.
CSRF_FAILED_TRIGGER = '{"tlm:csrf-failed": {}}'

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


# The trusted-proxy parsing lives in ``security.proxy_networks`` so the bearer
# token peer policy shares this exact implementation (#174). The names stay
# here because the rest of this module (and its tests) use them.
_IpAddress = IpAddress
_IpNetwork = IpNetwork
_parse_networks = parse_networks
_parse_networks_cached = parse_networks_cached
_parse_address = parse_address
_in_networks = in_networks


def _forwarded_scheme(request: Request) -> str | None:
    """Return the scheme from ``X-Forwarded-Proto``, or ``None`` if absent.

    A proxy may pass a comma-separated list; the left-most entry is the scheme
    the original client used.
    """
    forwarded = request.headers.get("x-forwarded-proto")
    if not forwarded:
        return None
    scheme = forwarded.split(",")[0].strip().lower()
    return "https" if scheme == "https" else "http"


def _leftmost_untrusted_hop(request: Request, trusted: list[_IpNetwork]) -> str | None:
    """Return the canonical client IP from ``X-Forwarded-For``, or ``None``.

    The proxy appends the address it accepted the connection from to the *right*
    of ``X-Forwarded-For``, so the chain reads (left) original client .. trusted
    proxies (right). The trustworthy client is therefore found by walking the
    chain right-to-left and returning the first hop that is **not** itself a
    trusted proxy -- everything to the right of it is link we control, everything
    to the left is attacker-supplied and must not be believed. Malformed entries
    are skipped. Returns ``None`` when every hop is trusted or unparseable.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        return None
    for hop in reversed(forwarded.split(",")):
        address = _parse_address(hop.strip())
        if address is None:
            continue
        if _in_networks(address, trusted):
            continue
        return str(address)
    return None


def resolve_effective_client(request: Request) -> tuple[str, str]:
    """Return the effective ``(client_ip, scheme)`` for ``request``.

    Reverse-proxy forwarded headers are honoured **only** when the immediate peer
    is a configured trusted proxy; otherwise the real peer address and the
    connection's own scheme are authoritative and ``X-Forwarded-For`` /
    ``X-Forwarded-Proto`` are ignored entirely, because a direct client could set
    them to anything.

    When the peer is trusted, the client IP is taken from the right-to-left first
    ``X-Forwarded-For`` hop that is not itself a trusted proxy, and the scheme
    from ``X-Forwarded-Proto``. Either falls back to the peer-derived value when
    the corresponding header is absent or unusable.
    """
    settings = _settings()
    peer_host = request.client.host if request.client is not None else None
    peer_ip = peer_host or "unknown"
    peer_scheme = "https" if request.url.scheme == "https" else "http"

    trusted = _parse_networks(settings.server.trusted_proxies)
    peer_address = _parse_address(peer_host) if peer_host else None
    if not trusted or peer_address is None or not _in_networks(peer_address, trusted):
        # Directly exposed, or the peer is not a trusted proxy: forwarded headers
        # are unauthenticated and must be ignored.
        return peer_ip, peer_scheme

    client_ip = _leftmost_untrusted_hop(request, trusted) or peer_ip
    scheme = _forwarded_scheme(request) or peer_scheme
    return client_ip, scheme


class EffectiveSchemeMiddleware:
    """Record the effective client IP and HTTPS scheme on ``request.state``.

    The effective scheme is HTTPS when the ASGI connection itself is TLS, or when
    a trusted reverse proxy in front of the application asserts it through the
    ``X-Forwarded-Proto`` header. Both the scheme and the resolved client IP are
    stashed on ``request.state`` (``effective_scheme`` is ``"https"``/``"http"``;
    ``effective_client_ip`` is the address to attribute the request to) so the
    redirect decision, the ``Secure`` cookie flag, and the login throttle share
    one source of truth.

    Forwarded headers are honoured only when the immediate peer is a configured
    trusted proxy. A direct client cannot use them to forge a ``Secure`` cookie
    or spoof its source address: when the peer is untrusted (or no proxies are
    configured) the headers are ignored and the real peer connection wins.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        # Set once the untrusted-forwarder warning has been logged (#67 R17).
        self._warned_untrusted_forwarder = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # ``request.state`` is backed by ``scope["state"]``, so every inner
        # layer and the endpoint see these values.
        request = Request(scope)
        client_ip, scheme = resolve_effective_client(request)
        request.state.effective_scheme = scheme
        request.state.effective_client_ip = client_ip
        if not self._warned_untrusted_forwarder:
            self._warn_untrusted_forwarder(request)

        await self.app(scope, receive, send)

    def _warn_untrusted_forwarder(self, request: Request) -> None:
        """Log once when a peer outside ``trusted_proxies`` sends ``X-Forwarded-For``.

        That is what a reverse proxy missing from ``server.trusted_proxies``
        looks like: every client it forwards is then attributed to the proxy,
        so they share one login-throttle budget and one client fails it for
        all. The header may also be a direct client's forgery, which is
        ignored either way. Only the peer address is logged, never the header.
        """
        if "x-forwarded-for" not in request.headers or request.client is None:
            return
        peer = _parse_address(request.client.host)
        if peer is None:
            return
        trusted = _parse_networks(_settings().server.trusted_proxies)
        if _in_networks(peer, trusted):
            return
        self._warned_untrusted_forwarder = True
        logger.warning(
            "a request from %s carries X-Forwarded-For, but %s is not in "
            "server.trusted_proxies: its clients are attributed to %s itself. If "
            "it is your reverse proxy, set server.trusted_proxies",
            peer,
            peer,
            peer,
        )


class HttpsRedirectMiddleware:
    """Redirect effective-HTTP requests to HTTPS with a ``308``.

    Active only when ``server.redirect_http_to_https`` is set. A ``308`` (rather
    than ``301``/``302``) preserves the method and body, so a POST that lands on
    the plaintext port is replayed against HTTPS. The redirect target keeps the
    request host but swaps to the configured HTTPS port. When redirection is
    disabled the request passes through unchanged (the service runner closes the
    public HTTP port in that mode).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        settings = _settings()
        effective_scheme = getattr(request.state, "effective_scheme", "http")
        path = request.url.path
        exempt = path.startswith(_REDIRECT_EXEMPT_PREFIXES)
        if (
            not exempt
            and settings.server.redirect_http_to_https
            and effective_scheme != "https"
        ):
            response = RedirectResponse(
                url=self._https_url(request, settings.server.https_port),
                status_code=308,
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

    @staticmethod
    def _https_url(request: Request, https_port: int) -> str:
        """Build the HTTPS URL for the same request on the HTTPS port.

        The host is :func:`request_host`'s validated parse of the ``Host``
        header (``localhost`` when it is invalid), never ``urlsplit``'s view of
        the raw header, so ``Host: a@b`` cannot redirect to ``b`` and a
        malformed header cannot raise. When ``server.allowed_hosts`` is a list,
        :class:`AllowedHostsMiddleware` has already refused any host outside
        it; under the ``*`` default the target only ever redirects the client
        that sent it. Path and query are carried over byte for byte when the
        path is origin-form (starts with ``/``); otherwise the target is ``/``.
        """
        scope = request.scope
        host = request_host(scope) or "localhost"
        if ":" in host:
            # An IPv6 literal must be bracketed again before a port is added.
            host = f"[{host}]"
        # Standard HTTPS port is implicit; any other is made explicit.
        netloc = host if https_port == 443 else f"{host}:{https_port}"
        raw_path: bytes = scope.get("raw_path") or scope["path"].encode("utf-8")
        if not raw_path.startswith(b"/"):
            # An origin-form target always starts with "/". Anything else (an
            # asterisk, absolute or authority form, or a bare ``@evil``) would
            # extend the authority: ``https://host:8443@evil`` points at evil.
            raw_path = b"/"
        target = f"https://{netloc}{raw_path.decode('latin-1')}"
        query: bytes = scope.get("query_string", b"")
        if query:
            target += "?" + query.decode("latin-1")
        return target


# Session factories (one per database) known to hold a real administrator. Once an
# admin exists the gate never asks again: a request-time query per page load for a
# fact that only ever flips once is pure cost, and under load it queued behind the
# write lock (#66 C8). Weak, so a factory that is dropped (a test, an embedded app
# restarting) takes its entry with it; the latch is per process, so a restart asks
# once more.
_SETUP_COMPLETE: weakref.WeakSet[object] = weakref.WeakSet()
_SETUP_COMPLETE_LOCK = threading.Lock()

# What :meth:`FirstRunGateMiddleware._first_run_state` answers.
_STATE_NEEDED = "needed"
_STATE_COMPLETE = "complete"
_STATE_UNKNOWN = "unknown"


class FirstRunGateMiddleware:
    """Force all traffic to the first-run setup page until an admin exists.

    Before any real administrator account is created the application is
    unsecured, so every non-allowlisted request is redirected to ``/first-run``.
    Once setup is complete the gate is transparent, and it stops asking: the
    first time an administrator is seen, the answer is latched for the life of the
    process. The allowlist keeps the setup page itself, static assets, the
    liveness probe, and the CLI API reachable so the gate cannot deadlock the
    bootstrap.

    When the database cannot be consulted (e.g. before migrations ran) the
    gate never wedges startup for a *read*: ``GET``/``HEAD`` pass through. A
    request that could change state is refused with ``503``, because letting it
    through would act on an installation whose setup state is unknown. The
    first-run routes are allowlisted, so setup itself is never refused.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path = request.url.path
        if path.startswith(_FIRST_RUN_ALLOW_PREFIXES):
            await self.app(scope, receive, send)
            return
        state = await run_in_threadpool(self._first_run_state)
        if state == _STATE_NEEDED:
            response = RedirectResponse(url="/first-run", status_code=303)
            await response(scope, receive, send)
            return
        if state == _STATE_UNKNOWN and request.method not in _GATE_PASS_METHODS:
            unavailable = PlainTextResponse("setup state unavailable", status_code=503)
            await unavailable(scope, receive, send)
            return
        await self.app(scope, receive, send)

    @staticmethod
    def _first_run_state() -> str:
        """Return ``needed``, ``complete`` or ``unknown`` (the database failed).

        ``complete`` is latched per session factory, so a database error after
        setup finished can never turn into a refusal. The failure case is logged:
        failing open silently once hid a 30 s pool stall (#37).

        Synchronous on purpose: :meth:`__call__` runs it in the threadpool.
        """
        try:
            factory = get_context().session_factory
            if factory in _SETUP_COMPLETE:
                return _STATE_COMPLETE
            with session_scope(factory) as db:
                needed = first_run_needed(db)
        except Exception:
            logger.warning(
                "first-run check failed; reads pass, state changes are refused",
                exc_info=True,
            )
            return _STATE_UNKNOWN
        if needed:
            return _STATE_NEEDED
        with _SETUP_COMPLETE_LOCK:
            _SETUP_COMPLETE.add(factory)
        return _STATE_COMPLETE


# The bearer-gated API surface (``api.routers.build_api_router``). The metrics
# and debug routers share the prefix but are mounted without the blanket bearer
# dependency, so they keep their own access control (and their own 404 while
# disabled); the gate below leaves them alone.
_API_PREFIX = "/api/v1/"
_API_UNGATED_PREFIXES = ("/api/v1/metrics", "/api/v1/system/debug")


def api_requires_bearer(method: str, path: str) -> bool:
    """Return whether ``method path`` is a body-bearing call to the bearer API.

    ``path`` is the decoded ASGI path the router matches, so a path the router
    would not send to the bearer-gated routers is never judged here.
    """
    return (
        method not in _SAFE_METHODS
        and path.startswith(_API_PREFIX)
        and not path.startswith(_API_UNGATED_PREFIXES)
    )


def _credential_refusal(request: Request) -> HTTPException | None:
    """Return the refusal the route's own auth would give, or ``None`` to admit.

    Synchronous, for a threadpool hop: the token file is read and the session is
    looked up in the database. It makes the *same* decision the route makes
    afterwards, from the same helpers:

    * the router-wide :func:`~..security.token.require_local_token` (peer policy,
      token file, constant-time bearer compare), run as-is;
    * when the request also carries a session cookie, the principal dependencies
      prefer it, so the session is resolved through the per-request cache
      (:func:`resolve_request_session`, the one the CSRF layer and the route share)
      and re-checked live (:func:`live_principal_from_cache`). A session that is
      not live is the same ``401`` the route gives. The role is not judged here:
      it depends on the route.
    """
    try:
        require_local_token(request)
        if not has_session_cookie(request):
            return None
        settings = _settings()
        with session_scope(get_context().session_factory) as db:
            resolved = resolve_request_session(request, db, settings=settings.session)
            user = live_principal_from_cache(db, resolved) if resolved else None
        if user is None:
            return HTTPException(status_code=401, detail="Authentication required.")
    except HTTPException as exc:
        return exc
    except Exception:
        logger.exception("API credential pre-check failed")
        return HTTPException(status_code=503, detail="Authentication unavailable.")
    return None


class ApiBearerGateMiddleware:
    """Judge an unsafe ``/api/v1`` request's credentials before its body is read.

    FastAPI reads and parses a JSON body *before* a route's dependencies run, so
    without this layer a client without valid credentials could make the service
    buffer up to ``server.max_default_body_bytes`` (16 MiB) before getting its
    ``401`` (#102, #260).

    The ``/api/v1`` router is bearer-only (#261): every route first runs
    :func:`~..security.token.require_local_token`, so a request needs a valid
    bearer token whatever else it carries, and a session cookie alone is ``401``.
    (A request that carries a valid bearer *and* a session cookie is judged by
    the cookie's role and CSRF as well; see the router.) The gate therefore
    decides, unread:

    * no bearer and no cookie: answered at once, exactly as the dependency would
      (``403`` for a peer outside the allowed clients, otherwise ``401``), without
      touching the token file or the database (#102);
    * anything else: the very decision the route will make (``_credential_refusal``,
      run in the threadpool so the file read and database lookup stay off the
      loop). A wrong bearer, a refused peer or a dead session gets its ``403`` /
      ``401`` / ``503`` here and its body is never read (#260). A request that
      passes still has its body bounded by
      :class:`~.body_limit.BodyLimitMiddleware` and is judged again by the route,
      which stays authoritative.

    The refusal is the response the dependency would give, so the gate is no new
    token oracle, and a refused peer is logged by the same rate-limited warning.
    There is no per-client failure counter on the bearer path (only the login
    forms have a throttle), so there is nothing for an early reject to skip.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not api_requires_bearer(
            scope["method"], scope["path"]
        ):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if _extract_bearer(request) or has_session_cookie(request):
            refusal = await run_in_threadpool(_credential_refusal, request)
            if refusal is None:
                await self.app(scope, receive, send)
                return
        else:
            try:
                enforce_token_peer(
                    request, _settings().server.api_token_allowed_clients
                )
                refusal = invalid_token_error()
            except HTTPException as exc:
                refusal = exc
        response = JSONResponse(
            {"detail": refusal.detail},
            status_code=refusal.status_code,
            headers=refusal.headers,
        )
        await response(scope, receive, send)


class CsrfMiddleware:
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

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Any body this layer reads (to find a form token) goes through the
        # recorder, so the endpoint can be handed the same bytes again.
        recorder = _BodyRecorder(receive)
        request = Request(scope, recorder.receive)
        if request.method in _SAFE_METHODS or not has_session_cookie(request):
            await self.app(scope, receive, send)
            return

        expected = await run_in_threadpool(self._expected_secret, request)
        if expected is None:
            # Cookie present but not a live session: nothing to protect, and
            # requiring a token here would lock out re-login. Let it through; the
            # route's own auth dependency rejects an unauthenticated mutation.
            await self.app(scope, receive, send)
            return
        if expected is _CSRF_DENY or fetch_site_rejected(request.headers):
            # A failed lookup denies outright: the sentinel is a public constant,
            # so a token "matching" it is forgeable (and, masked, header-safe).
            # A request the browser labels cross-site or same-site is refused
            # whatever token it carries (#70 F-S4).
            await self._reject(request, scope, receive, send)
            return

        try:
            presented = await self._presented_token(request, recorder)
        except (BodyTooLargeError, HTTPException) as exc:
            # ``parsed_form`` reports an over-limit form as an HTTP 413; raised
            # here, outside the routes, it would otherwise surface as a 500.
            if isinstance(exc, HTTPException) and exc.status_code != 413:
                raise
            too_large = PlainTextResponse("Request body too large.", status_code=413)
            await too_large(scope, receive, send)
            return
        if not verify_csrf(expected, presented):
            await self._reject(request, scope, receive, send)
            return
        await self.app(scope, recorder.replaying_receive(), send)

    @staticmethod
    async def _reject(
        request: Request, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Send the CSRF 403: one fixed body, whatever the reason.

        ``net.js`` recognises the failure by this exact body, so every rejection
        (bad token, cross-site fetch, failed lookup) uses it.
        """
        rejected = PlainTextResponse("CSRF validation failed.", status_code=403)
        if request.headers.get("HX-Request") == "true":
            # htmx does not swap a 4xx, so without a signal the click just
            # does nothing. The event lets the page explain and offer a
            # reload (#70 F-R1; the client half of #67 R14).
            rejected.headers["HX-Trigger"] = CSRF_FAILED_TRIGGER
        await rejected(scope, receive, send)

    @staticmethod
    def _expected_secret(request: Request) -> str | None:
        """Return the live session's CSRF secret, or ``None`` if not live.

        Synchronous on purpose: :meth:`__call__` runs it in the threadpool.
        """
        settings = _settings()
        if not session_cookie_value(request, settings.session):
            return None
        try:
            factory = get_context().session_factory
            with session_scope(factory) as db:
                # Cached on the request: the endpoint's auth dependency and the
                # page's CSRF token reuse this resolution instead of repeating it.
                resolved = resolve_request_session(
                    request, db, settings=settings.session
                )
            if resolved is None:
                return None
            # No secret to compare against means nothing can match: deny.
            return resolved.csrf_secret or _CSRF_DENY
        except Exception:
            # A database hiccup must not become a CSRF bypass: fail closed by
            # treating it as "no expected secret" only when there is genuinely no
            # session to protect would be unsafe, so here we deny instead.
            logger.warning(
                "CSRF session lookup failed; denying the request", exc_info=True
            )
            # The resolution may have been cached before the failure (a commit at
            # scope exit): never leave an answer a later layer could trust.
            invalidate_request_session(request)
            return _CSRF_DENY

    @staticmethod
    async def _presented_token(request: Request, recorder: _BodyRecorder) -> str | None:
        """Read the presented CSRF token from the header or a form field.

        Prefers the ``X-CSRF-Token`` header (the HTMX path). Otherwise reads the
        ``csrf_token`` field from the body: a multipart part for an upload form,
        or the urlencoded field via the shared parser, whose result is cached on
        ``request.state`` so the route handler reading the same form does not
        consume the body a second time.

        The frame import's body is never buffered here: only its leading part is
        read, through the same streaming parser the route then uses
        (:func:`_streamed_first_part_token`).
        """
        header_token = request.headers.get(_CSRF_HEADER)
        if header_token:
            return header_token
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("multipart/form-data") and (
            FRAME_IMPORT_PATH.fullmatch(request.scope["path"])
        ):
            return await _streamed_first_part_token(recorder, content_type)
        if FRAME_IMPORT_PATH.fullmatch(request.scope["path"]) or (
            FRAME_UPLOAD_PATH.fullmatch(request.scope["path"])
        ):
            # An upload route reads no form: a urlencoded (or any other) body
            # with no header token is refused unread, not parsed on the loop
            # ahead of the role check (#217).
            return None
        if content_type.startswith("multipart/form-data"):
            # A no-JS upload form (frame import, problem report) has no header;
            # its token is a ``csrf_token`` part. Reading the body here is safe:
            # the outer body-size middleware bounds it, and this middleware
            # replays the recorded body to the route (``_BodyRecorder``).
            body = await _multipart_body(request)
            multipart = parse_multipart(body, request.headers["content-type"])
            return multipart.fields.get(_CSRF_FORM_FIELD)
        from .dependencies import parsed_form

        form = await parsed_form(request)
        return form.get(_CSRF_FORM_FIELD)


async def _streamed_first_part_token(
    recorder: _BodyRecorder, content_type: str
) -> str | None:
    """Read the ``csrf_token`` that must lead a streamed multipart upload.

    The frame import can be hundreds of megabytes, so the no-JS form's token is
    required to be its **first** part (the template puts the hidden field before
    the file input) and is read before anything else is looked at: only that
    part's bytes are pulled from the client, through the same
    :class:`MultipartParser` the route uses for the whole body. They stay in the
    recorder and are replayed to the route, which therefore sees the complete
    body, and no upload byte is spooled before the token is checked. ``None`` --
    refused by the caller -- when the first part is anything else (a file, or a
    field with another name), the body is malformed, or it ends first.
    """
    boundary = multipart_boundary(content_type)
    if boundary is None:
        return None
    parser = MultipartParser(boundary)
    value = bytearray()
    started = False
    while True:
        message = await recorder.receive()
        if message["type"] != "http.request":
            return None  # the client went away
        try:
            events = parser.feed(message.get("body", b""))
        except MultipartError:
            return None
        for event in events:
            if isinstance(event, PartStart):
                if (
                    started
                    or event.name != _CSRF_FORM_FIELD
                    or event.filename is not None
                ):
                    return None
                started = True
            elif isinstance(event, PartData):
                value += event.data
            elif isinstance(event, PartEnd):
                return bytes(value[:MAX_FIELD_BYTES]).decode("utf-8", errors="replace")
            elif isinstance(event, FormEnd):
                return None
        if not message.get("more_body", False):
            return None


async def _multipart_body(request: Request) -> bytes:
    """Read a multipart body for the CSRF check, bounded for the report form.

    The problem-report form (``POST /report``) is open to every role, so its
    upload cap is enforced here on the bytes actually received -- this layer
    reads the body before the route can. Other upload routes keep the
    replayable ``request.body()``, bounded by the body-size middleware's
    per-route limit (``body_limit_for``).
    """
    if request.url.path == REPORT_FORM_PATH:
        return await read_body_limited(request, report_body_limit())
    return await request.body()


# Sentinel returned when the CSRF secret lookup fails unexpectedly. The caller
# rejects it by identity before any token comparison: the value is public, so a
# token built to "match" it must never be compared at all (fail closed).
_CSRF_DENY = "\x00csrf-lookup-failed\x00"


class _BodyRecorder:
    """Record the request-body messages a middleware consumes, for replay.

    An ASGI request body can be received only once. When :class:`CsrfMiddleware`
    reads the body to find a form token, the endpoint behind it still needs the
    same bytes -- whether it goes through the ``request.state`` caches
    (``parsed_form``, ``read_body_limited``) or reads ``request.body()``,
    ``request.stream()`` or ``request.form()`` itself. Every message the
    middleware pulls is recorded; :meth:`replaying_receive` hands them to the
    endpoint in order and then falls through to the original ``receive``, so a
    later ``http.disconnect`` (which a streamed response listens for) still
    arrives from the server, never synthesised here.
    """

    def __init__(self, receive: Receive) -> None:
        self._receive = receive
        self._messages: deque[Message] = deque()

    async def receive(self) -> Message:
        """Pull the next message from the server and keep a copy of it."""
        message = await self._receive()
        self._messages.append(message)
        return message

    def replaying_receive(self) -> Receive:
        """Return a ``receive`` that replays the recorded messages, then delegates.

        When nothing was recorded this is the original ``receive`` itself.
        """
        if not self._messages:
            return self._receive
        pending = self._messages
        original = self._receive

        async def replay() -> Message:
            if pending:
                return pending.popleft()
            return await original()

        return replay


# Host names that are always answered, whatever ``server.allowed_hosts`` says:
# the CLI, ``tlm-upgrade.sh`` and local probes reach the service on loopback.
_ALWAYS_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Distinct rejected hosts logged so far; capped so a scanner cycling through
# Host values cannot grow it (or the log) without bound.
_LOGGED_HOSTS_CAP = 64


def request_host(scope: Scope) -> str | None:
    """Return the request's host name (no port), or ``None`` if it is not valid.

    The single parser for the ``Host`` header: :class:`AllowedHostsMiddleware`
    checks its result and the HTTPS redirect builds its target from it. The
    raw header must be exactly a DNS name or an IP literal (IPv6 in brackets),
    optionally followed by ``:port``; anything else -- ``@``, ``/``, ``\\``,
    whitespace, an unbracketed IPv6 address, a bad port -- is ``None``. The
    result is lower-cased, has one trailing dot removed, and gives IP literals
    in canonical form.

    As a cross-check it must equal the normalised ``request.url.hostname``
    (Starlette's ``urlsplit`` of the same header, which every other piece of
    code that reads the URL sees); if the two parsers ever disagree the host is
    refused rather than trusted.
    """
    raw = Headers(scope=scope).get("host")
    host = _parse_host_header(raw) if raw else None
    if host is None:
        return None
    try:
        url_host = URL(scope=scope).hostname
    except ValueError:
        return None
    if url_host is None or _normalise_host(url_host) != host:
        return None
    return host


def _parse_host_header(raw: str) -> str | None:
    """Strictly parse a ``Host`` header value; see :func:`request_host`."""
    value = raw.lower()
    if value.startswith("["):
        literal, sep, rest = value[1:].partition("]")
        if not sep or (rest and not _is_port_suffix(rest)):
            return None
        try:
            return str(ipaddress.IPv6Address(literal))
        except ValueError:
            return None
    name, sep, port = value.partition(":")
    if sep and not _is_port_suffix(":" + port):
        return None
    name = name[:-1] if name.endswith(".") else name
    if not HOST_NAME_PATTERN.fullmatch(name):
        return None
    return _normalise_host(name)


def _normalise_host(host: str) -> str:
    """Lower-case ``host``, drop one trailing dot, canonicalise an IP literal."""
    host = host.lower()
    host = host[:-1] if host.endswith(".") else host
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return host


def _is_port_suffix(value: str) -> bool:
    """Return whether ``value`` is ``:`` followed by 1-5 ASCII digits."""
    digits = value[1:]
    return (
        value.startswith(":")
        and 0 < len(digits) <= 5
        and digits.isascii()
        and digits.isdigit()
    )


def host_allowed(host: str, allowed: frozenset[str]) -> bool:
    """Return whether ``host`` matches an entry of ``allowed``.

    Entries are exact names or IP literals, or ``*.example.com``, which matches
    any subdomain of ``example.com`` but not ``example.com`` itself (list that
    separately) and not a look-alike such as ``evil-example.com``: the suffix
    compared includes the leading dot. Both sides are already normalised.
    """
    if host in allowed:
        return True
    return any(entry.startswith("*.") and host.endswith(entry[1:]) for entry in allowed)


class AllowedHostsMiddleware:
    """Answer ``400`` to any request whose ``Host`` is not allowed (S20).

    Like Starlette's ``TrustedHostMiddleware`` but it parses the header
    properly: case-insensitive, port and trailing dot ignored, and IPv6
    literals in brackets (Starlette's ``split(":")`` can never match
    ``[::1]:8080``). Loopback names are always allowed. ``mount_web`` installs
    it only when ``server.allowed_hosts`` is not ``*``.
    """

    def __init__(self, app: ASGIApp, allowed_hosts: list[str]) -> None:
        self.app = app
        # ``ServerSettings`` has already validated and normalised the entries.
        self.allowed = frozenset(set(allowed_hosts) | _ALWAYS_ALLOWED_HOSTS)
        self._logged: set[str] = set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        host = request_host(scope)
        if host is not None and host_allowed(host, self.allowed):
            await self.app(scope, receive, send)
            return
        self._log_rejection(host)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        response = PlainTextResponse("Invalid host header", status_code=400)
        await response(scope, receive, send)

    def _log_rejection(self, host: str | None) -> None:
        """Log a rejected host once, so a wrong ``allowed_hosts`` is visible."""
        key = host or ""
        if key in self._logged or len(self._logged) >= _LOGGED_HOSTS_CAP:
            return
        self._logged.add(key)
        logger.warning(
            "request refused: Host %r is not in server.allowed_hosts", key[:255]
        )
