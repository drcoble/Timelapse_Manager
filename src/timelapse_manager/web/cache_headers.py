"""Default ``Cache-Control`` and ``Vary`` for dynamic responses (#70 F-S2, F-R11).

:class:`CacheControlMiddleware` is a pure ASGI middleware that edits the
``http.response.start`` message on its way out; it never touches the body, so a
streamed response (a render, a download) passes through unbuffered.

Rules, applied only to responses outside ``/static/``:

* **``no-store``** on ``text/html`` and JSON (``application/json`` or a
  ``+json`` type) responses that set no ``Cache-Control`` of their own. An
  authenticated page or API payload is then never written to the browser's
  disk cache, so it cannot be redisplayed from history after sign-out. A
  response that chose its own policy keeps it: the frame images and thumbnails
  (``private, max-age=86400``, owner decision D3), render streams and
  downloads. Health and metrics JSON get ``no-store`` too, which is harmless
  for a poller.
* **``Vary: HX-Request``** on every ``text/html`` response, merged with any
  existing ``Vary``. Several routes return a full page or a partial for the
  same URL depending on that header; the ``Vary`` stops a cache (the browser's
  or an intermediary's) serving one in place of the other.

Static assets carry their own policy from :class:`~.assets.VersionedStaticFiles`.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .assets import STATIC_PREFIX

NO_STORE = "no-store"
HX_REQUEST_VARY = "HX-Request"


def _is_static(path: str) -> bool:
    return path.startswith(STATIC_PREFIX) or path == STATIC_PREFIX.rstrip("/")


def _media_type(headers: MutableHeaders) -> str:
    return headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _is_json(media_type: str) -> bool:
    return media_type == "application/json" or (
        media_type.startswith("application/") and media_type.endswith("+json")
    )


def merge_vary(existing: str | None, field: str) -> str:
    """Return ``existing`` with ``field`` added once (case-insensitively).

    ``Vary: *`` already varies on everything and is returned unchanged.
    """
    if not existing or not existing.strip():
        return field
    tokens = [token.strip() for token in existing.split(",") if token.strip()]
    lowered = {token.lower() for token in tokens}
    if "*" in lowered or field.lower() in lowered:
        return existing
    return ", ".join((*tokens, field))


def apply_cache_defaults(headers: MutableHeaders) -> None:
    """Apply the default ``Cache-Control`` / ``Vary`` rules to response headers."""
    media_type = _media_type(headers)
    is_html = media_type == "text/html"
    if (is_html or _is_json(media_type)) and "cache-control" not in headers:
        headers["Cache-Control"] = NO_STORE
    if is_html:
        headers["Vary"] = merge_vary(headers.get("vary"), HX_REQUEST_VARY)


class CacheControlMiddleware:
    """Stamp ``no-store`` and ``Vary: HX-Request`` defaults onto responses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _is_static(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        async def send_with_defaults(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                apply_cache_defaults(MutableHeaders(scope=message))
            await send(message)

        await self.app(scope, receive, send_with_defaults)
