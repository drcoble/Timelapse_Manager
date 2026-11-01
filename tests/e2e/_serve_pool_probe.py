"""E2E app server with connection-pool and response-status probes (#37).

Subprocess entry point for ``test_cancelled_requests.py``; takes the same
arguments as ``_serve.py``, installs the probes below, then runs ``_serve.main``.
Each probe event is one stderr line, ``#37-PROBE {json}``, so the test reads
them from the server log file it already writes:

* ``{"kind": "checkout", "wait": s, "loop": bool, "request": bool}`` for every
  pool checkout. ``wait`` is the time spent in ``QueuePool._do_get`` (waiting
  for a slot, or opening an overflow connection); ``loop`` is whether it ran on
  the event-loop thread; ``request`` is whether a request was being handled
  (the request-id context variable is set).
* ``{"kind": "start", "path": p}`` when the app receives a request, and
  ``{"kind": "status", "status": n, "path": p}`` for every response it starts,
  including one whose client has already gone (uvicorn drops that response,
  so the access log never shows it).

These are the #37 signals themselves, so the tests need no tight wall-clock
budget, which a loaded CI runner cannot honour.

Do NOT import this module in tests -- it is a subprocess entry point only.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from typing import Any

PROBE_PREFIX = "#37-PROBE "

_write_lock = threading.Lock()


def _emit(event: dict[str, Any]) -> None:
    """Write one probe line to stderr (one write call, under a lock)."""
    line = PROBE_PREFIX + json.dumps(event) + "\n"
    with _write_lock:
        sys.stderr.write(line)
        sys.stderr.flush()


def _on_event_loop() -> bool:
    """Return whether the calling thread is running an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _install_pool_probe() -> None:
    """Time every ``QueuePool`` checkout and note which thread made it."""
    from sqlalchemy.pool import QueuePool

    from timelapse_manager.logging import request_id_ctx

    original = QueuePool._do_get

    def timed_do_get(self: QueuePool) -> Any:
        loop = _on_event_loop()
        in_request = request_id_ctx.get() is not None
        start = time.monotonic()
        try:
            return original(self)
        finally:
            _emit(
                {
                    "kind": "checkout",
                    "wait": round(time.monotonic() - start, 4),
                    "loop": loop,
                    "request": in_request,
                }
            )

    QueuePool._do_get = timed_do_get  # type: ignore[method-assign]


def _status_probe(app: Any) -> Any:
    """Wrap an ASGI app to report the status of every response it starts."""

    async def wrapped(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        _emit({"kind": "start", "path": scope["path"]})

        async def probing_send(message: Any) -> None:
            if message["type"] == "http.response.start":
                _emit(
                    {
                        "kind": "status",
                        "status": message["status"],
                        "path": scope["path"],
                    }
                )
            await send(message)

        await app(scope, receive, probing_send)

    return wrapped


def main(argv: list[str]) -> None:
    import _serve  # type: ignore[import-not-found]  # sibling script, on sys.path
    import uvicorn

    _install_pool_probe()
    run = uvicorn.run

    def probing_run(app: Any, **kwargs: Any) -> None:
        run(_status_probe(app), **kwargs)

    uvicorn.run = probing_run  # type: ignore[assignment]
    _serve.main(argv)


if __name__ == "__main__":
    main(sys.argv[1:])
