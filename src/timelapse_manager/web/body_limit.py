"""Per-route request-body size caps as a pure ASGI middleware.

:class:`BodyLimitMiddleware` is intentionally *not* a
:class:`~starlette.middleware.base.BaseHTTPMiddleware`: that base class buffers
the whole request body before the handler runs, which would defeat the point of
a cap meant to stop an oversized body from ever being held in memory. Instead it
wraps the raw ASGI ``receive`` callable and tallies the bytes of each
``http.request`` event as the body streams in. The instant the running total
exceeds the request's ceiling it stops the upload and answers
``413 Request Entity Too Large`` -- whether or not a ``Content-Length`` header
was sent, so a chunked request with no declared length cannot slip past. A
declared ``Content-Length`` already above the ceiling is refused before the
inner application runs at all.

The ceiling depends on the route (issue #67, S6). The unauthenticated
``/login``, ``/first-run`` and ``/logout`` forms get a few kilobytes, the frame
import keeps the full ``server.max_request_body_bytes``, the raw single-frame
upload gets one image plus headroom, the problem-report form its upload cap, any
other urlencoded form 1 MiB, and everything else
``server.max_default_body_bytes``. :func:`body_limit_for` holds that table;
:func:`.dependencies.parsed_form` uses it too, so the middleware and the form
parser can never disagree about a form's limit.

The count is driven by the handler reading the body, so the cap engages exactly
where the memory risk is: an endpoint that buffers the body (e.g. an upload or
import handler calling ``await request.body()``) is cut off the moment it reads
past the ceiling. An endpoint that never reads the body holds nothing in memory.
"""

from __future__ import annotations

import re

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config import Settings
from ..runtime import get_context
from .multipart import REPORT_FORM_PATH, report_body_limit

# A request whose body total is *equal to* the cap is allowed; only a strictly
# greater total is rejected, so a body sized exactly at the limit still passes.

# The pre-session forms: a username, a password or two, a setup code and a CSRF
# token. Nothing legitimate comes near this; the cap exists because these routes
# accept a body from anyone who can reach the port.
AUTH_FORM_MAX_BYTES = 64 * 1024
AUTH_FORM_PATHS = frozenset({"/login", "/first-run", "/logout"})

# The debug routes (#7) sit outside the bearer-gated router and so outside
# ``ApiBearerGateMiddleware``; their one write takes a tiny JSON object, so they
# get the pre-session cap rather than the 16 MiB default (#102).
DEBUG_API_PREFIX = "/api/v1/system/debug/"

# Every other urlencoded form. The largest real one (a 500-frame bulk selection)
# is a few kilobytes.
URLENCODED_FORM_MAX_BYTES = 1024 * 1024

# Headroom on top of one image for the raw single-frame upload.
UPLOAD_OVERHEAD_BYTES = 64 * 1024

# The problem-report route bounds its own read at ``report_body_limit()`` and
# answers an HTMX submit with an inline error. The middleware backstop sits this
# far above it (more than one received chunk), so the route's own handling is
# what the user sees and the middleware only catches what gets past it.
REPORT_BACKSTOP_MARGIN_BYTES = 1024 * 1024

# The content type :func:`.dependencies.parsed_form` parses. Both sides classify
# with this exact, case-sensitive prefix test so neither can call a body
# urlencoded that the other does not.
URLENCODED_CONTENT_TYPE = "application/x-www-form-urlencoded"

# The multipart frame import, on the web UI and the API.
_FRAME_IMPORT_PATH = re.compile(r"(?:/api/v1)?/projects/[^/]+/frames/import")
# The raw-body single-frame upload (API only).
_FRAME_UPLOAD_PATH = re.compile(r"/api/v1/projects/[^/]+/frames/upload")


def is_urlencoded(content_type: str) -> bool:
    """Return whether ``content_type`` is a body :func:`parsed_form` will read."""
    return content_type.startswith(URLENCODED_CONTENT_TYPE)


def body_limit_for(
    method: str, path: str, content_type: str, settings: Settings
) -> int:
    """Return the most bytes a request body to ``method path`` may carry.

    ``path`` is the decoded ASGI ``scope["path"]`` -- the same value the router
    matches -- and the patterns use ``fullmatch``, so a path the router would
    not route to a body-reading endpoint never earns a larger limit. The result
    never exceeds ``server.max_request_body_bytes``, the hard ceiling.
    """
    server = settings.server
    limit: int
    if path in AUTH_FORM_PATHS or path.startswith(DEBUG_API_PREFIX):
        limit = AUTH_FORM_MAX_BYTES
    elif (
        method == "POST"
        and _FRAME_IMPORT_PATH.fullmatch(path)
        and not is_urlencoded(content_type)
    ):
        # Not for a urlencoded body: that is a form (parsed on the loop by
        # ``parsed_form``), never an upload, and gets the form cap below (#217).
        limit = server.max_request_body_bytes
    elif (
        method == "POST"
        and _FRAME_UPLOAD_PATH.fullmatch(path)
        and not is_urlencoded(content_type)
    ):
        limit = settings.capture.max_image_bytes + UPLOAD_OVERHEAD_BYTES
    elif method == "POST" and path == REPORT_FORM_PATH:
        limit = report_body_limit(settings) + REPORT_BACKSTOP_MARGIN_BYTES
    elif is_urlencoded(content_type):
        limit = URLENCODED_FORM_MAX_BYTES
    else:
        limit = server.max_default_body_bytes
    return min(limit, server.max_request_body_bytes)


def _declared_length(headers: Headers) -> int | None:
    """Return the request's ``Content-Length`` as an int, or ``None``.

    A missing or unparseable value yields ``None``. That only skips the early
    reject: the streaming count below still enforces the limit, so a malformed
    header can never mean "no limit".
    """
    raw = headers.get("content-length")
    if raw is None:
        return None
    raw = raw.strip()
    if not (raw.isascii() and raw.isdigit()):
        return None
    return int(raw)


class BodyLimitMiddleware:
    """Reject any request whose body exceeds its route's ceiling.

    The ceiling is resolved per request by :func:`body_limit_for` from the
    running process configuration. When the running context is unavailable
    (e.g. before the lifespan has installed it) the request passes through
    unguarded rather than erroring -- the route's own machinery will fail on its
    own terms.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = self._settings()
        if settings is None:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        max_bytes = body_limit_for(
            scope["method"],
            scope["path"],
            headers.get("content-type", ""),
            settings,
        )
        declared = _declared_length(headers)
        if declared is not None and declared > max_bytes:
            # Refuse before any inner layer (redirect, CSRF, the route) runs or
            # a single body byte is read.
            await self._send_too_large(send)
            return

        received = 0
        overflowed = False
        response_started = False

        async def wrapped_receive() -> Message:
            nonlocal received, overflowed
            message = await receive()
            if message["type"] != "http.request":
                return message
            received += len(message.get("body", b""))
            if received > max_bytes:
                # Signal the disconnect upstream so the handler's body read
                # unwinds promptly; the authoritative 413 is sent below.
                overflowed = True
                return {"type": "http.disconnect"}
            return message

        async def wrapped_send(message: Message) -> None:
            nonlocal response_started
            if overflowed:
                # Our 413 is authoritative once the cap trips: swallow whatever
                # the unwinding handler tries to emit (typically a 500 from the
                # ClientDisconnect) so it never reaches the wire.
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, wrapped_receive, wrapped_send)
        except Exception:
            # The disconnect we injected unwinds the handler as a server error;
            # suppress it only when we triggered it, never a genuine failure.
            if not overflowed:
                raise

        if overflowed and not response_started:
            await self._send_too_large(send)

    @staticmethod
    def _settings() -> Settings | None:
        """Return the running settings, or ``None`` if unavailable."""
        try:
            return get_context().settings
        except Exception:
            return None

    @staticmethod
    async def _send_too_large(send: Send) -> None:
        """Emit a minimal ``413`` response and close the connection."""
        body = b"Request body exceeds the configured size limit.\n"
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
