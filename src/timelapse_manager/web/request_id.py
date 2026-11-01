"""Per-request correlation id: middleware and the shared context plumbing.

Every request is tagged with a correlation id -- taken from an inbound
``X-Request-ID`` header when a proxy supplies one, otherwise freshly generated --
so a client, the response, and every log line emitted while handling the request
can be tied together. :class:`RequestIdMiddleware` sets the id for the duration
of the request and echoes it on the response.

The :data:`request_id_ctx` context variable and :class:`RequestIdFilter` are
defined in :mod:`timelapse_manager.logging` (a leaf module) so logging can attach
the filter without importing the web package, and re-exported here so the web
layer has one obvious import site for the request-id plumbing.
"""

from __future__ import annotations

import re
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..logging import RequestIdFilter, request_id_ctx

_HEADER = "X-Request-ID"

# An inbound correlation id is honoured only when it is a short, opaque token of
# unreserved characters. This bounds what an untrusted client can reflect back
# through the response header and into every log record's ``request_id`` field --
# an arbitrary or oversized value could bloat logs, or reuse another request's id
# to muddy forensic correlation. Anything else is replaced with a fresh id.
_ID_PATTERN = re.compile(r"\A[A-Za-z0-9._-]{1,128}\Z")


def _resolve_request_id(supplied: str | None) -> str:
    """Return a safe correlation id: the supplied one if well-formed, else new."""
    if supplied is not None and _ID_PATTERN.match(supplied):
        return supplied
    return uuid4().hex


__all__ = ["RequestIdFilter", "RequestIdMiddleware", "request_id_ctx"]


class RequestIdMiddleware:
    """Assign, propagate, and echo a correlation id for each request.

    Installed as the outermost middleware so the id is set before any inner layer
    runs -- even a middleware that rejects the request early has its log records
    tagged. The inbound ``X-Request-ID`` is honoured when present (a proxy or
    upstream service may already have assigned one); otherwise a fresh hex id is
    generated. An inbound id is honoured only when it is a short, opaque token
    (see :data:`_ID_PATTERN`); anything else is replaced with a fresh id so an
    untrusted client cannot reflect an oversized or forged value through the
    response header and the logs. The id is exposed on ``request.state.request_id``
    for handlers and stamped on the response's ``X-Request-ID`` header. The context
    variable is always reset in a ``finally`` so no id leaks to the next task on
    the loop.

    A pure ASGI middleware: the inner application runs in this task, so the
    context variable reaches the endpoint (and the threadpool it hops to) without
    any copying, and a streamed response body passes through unbuffered.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        rid = _resolve_request_id(Headers(scope=scope).get(_HEADER))
        Request(scope).state.request_id = rid

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[_HEADER] = rid
            await send(message)

        token = request_id_ctx.set(rid)
        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_ctx.reset(token)
