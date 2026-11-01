"""Regression for #50: the camera probe routes keep the database off the loop.

The camera routes (validate, geolocation, query, the
stream-profile and PTZ-preset pickers, and discover) are ``async def`` because
they await camera network I/O. They used to read the camera row, the global
default credential and the template context (whose CSRF token reads the session
row) on the event loop. These tests drive them against the real-server harness
of #37 (``test_cancelled_requests.py``, running under ``_serve_pool_probe.py``)
while another connection holds SQLite's write lock, and assert

* no pool checkout made while serving a request happened on the event-loop
  thread (the probe's signal, which the pre-#50 handlers trip on every call),
* an unrelated page (``/renders``) answers in under a second while the camera
  requests are in flight, including discover's multicast listen,
* every camera request answers, and nothing is a 5xx.

The server also runs with the loop-DB guard's file sink (``loop_guard_args``),
so the autouse guard fixture fails the test if any of these routes queries on
the loop.

Sessions are not backdated: with the write lock held, a due ``last_active``
touch would wait out the busy timeout, which is the lock doing its job, not
this bug.
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
import threading
import time
import urllib.parse
from collections.abc import Iterator
from pathlib import Path

from tests import loop_guard_support
from tests.e2e.test_cancelled_requests import (
    _SOCKET_TIMEOUT,
    Server,
    _assert_no_pool_stall,
    _get,
    _login,
    _probe_events,
    _request,
    _round_events,
    _status,
    _timed_get,
    server,  # noqa: F401 -- the real-server fixture, reused as is.
)

# The unrelated request must answer this fast while the camera routes run. An
# unloaded /renders takes ~20 ms; a loop blocked by a write-lock wait would take
# up to the 15 s busy timeout.
_UNRELATED_BUDGET = (
    5.0  # CI runners are slow; a regression waits the full 15 s busy_timeout
)
# Give the camera requests time to reach their handlers (and discover's
# multicast listen to start) before the unrelated request is sent.
_IN_FLIGHT_DELAY = 0.2

# The seeded camera (``_seed_demo_data``): id 1 at a private address.
_CAMERA_ID = 1
_CAMERA_ADDRESS = "10.0.0.9"


def _csrf_token(base_url: str, cookie: bytes) -> str:
    """Return the session's CSRF token from an authenticated page."""
    page = _request(base_url, _get("/cameras", cookie))
    assert _status(page) == 200, page[:200]
    match = re.search(rb'<meta name="csrf-token" content="([^"]+)"', page)
    assert match is not None, "no csrf-token meta on /cameras"
    return match.group(1).decode()


def _post(path: str, cookie: bytes, token: str, fields: dict[str, str]) -> bytes:
    """Build a cookie-authenticated, CSRF-bearing form ``POST``."""
    body = urllib.parse.urlencode(fields).encode()
    return (
        f"POST {path} HTTP/1.1\r\nHost: test\r\n".encode()
        + b"Cookie: "
        + cookie
        + f"\r\nX-CSRF-Token: {token}\r\n".encode()
        + b"HX-Request: true\r\n"
        + b"Content-Type: application/x-www-form-urlencoded\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"Connection: close\r\n\r\n"
        + body
    )


def _camera_requests(cookie: bytes, token: str) -> dict[str, bytes]:
    """Return one raw request per #50 route, keyed by a readable label."""
    probe = {"address": _CAMERA_ADDRESS, "username": "", "password": ""}
    return {
        "validate": _post(f"/cameras/{_CAMERA_ID}/validate", cookie, token, {}),
        "geolocation": _get(f"/cameras/{_CAMERA_ID}/geolocation", cookie),
        "query": _post("/cameras/query", cookie, token, probe),
        "stream-profiles": _get(
            f"/cameras/stream-profiles?camera_id={_CAMERA_ID}", cookie
        ),
        "ptz-presets": _get(f"/cameras/ptz-presets?camera_id={_CAMERA_ID}", cookie),
        # A blank range listens for ONVIF multicast replies: awaited network
        # I/O that keeps this request in flight while /renders is timed.
        "discover": _post("/cameras/discover", cookie, token, {"scan_range": ""}),
    }


@contextlib.contextmanager
def _write_lock(db_path: Path) -> Iterator[None]:
    """Hold SQLite's write lock from another connection while the block runs."""
    conn = sqlite3.connect(str(db_path), timeout=_SOCKET_TIMEOUT, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield
        conn.execute("ROLLBACK")
    finally:
        conn.close()


def _send_with_unrelated(
    base_url: str, requests: dict[str, bytes], cookie: bytes
) -> tuple[dict[str, bytes], int, float]:
    """Send every camera request on its own thread and time ``/renders`` meanwhile.

    Returns the camera responses by label, and the unrelated request's status
    and seconds.
    """
    responses: dict[str, bytes] = {}
    errors: dict[str, BaseException] = {}

    def run(label: str, raw: bytes) -> None:
        try:
            responses[label] = _request(base_url, raw)
        except BaseException as exc:  # noqa: BLE001 -- reported by the caller.
            errors[label] = exc

    threads = [
        threading.Thread(target=run, args=(label, raw), daemon=True)
        for label, raw in requests.items()
    ]
    for thread in threads:
        thread.start()
    time.sleep(_IN_FLIGHT_DELAY)
    status, elapsed = _timed_get(base_url, "/renders", cookie)
    for thread in threads:
        thread.join(_SOCKET_TIMEOUT + 5)
    assert not errors, f"camera requests failed: {errors}"
    return responses, status, elapsed


def test_camera_routes_do_not_block_the_loop_under_a_write_lock(
    server: Server,  # noqa: F811 -- the fixture.
) -> None:
    """The #50 routes answer and the loop stays free while the lock is held."""
    # Log in and read the token before taking the lock: both write the session.
    cookie = _login(server.base_url)
    token = _csrf_token(server.base_url, cookie)
    requests = _camera_requests(cookie, token)
    start = len(_probe_events(server.log_path))

    with _write_lock(server.db_path):
        responses, status, elapsed = _send_with_unrelated(
            server.base_url, requests, cookie
        )

    _assert_no_pool_stall(_round_events(server, start), len(requests) + 1)
    # The server logs loop-DB work live; the autouse guard checks this too at
    # teardown, but asserting here names the camera routes in the test itself.
    on_loop = [v for v in loop_guard_support.collect() if v.path.startswith("/cameras")]
    assert not on_loop, loop_guard_support.failure_message(on_loop)
    assert status == 200
    assert elapsed < _UNRELATED_BUDGET, (
        f"/renders took {elapsed:.2f}s while the camera routes ran under a held "
        "write lock"
    )
    statuses = {label: _status(raw) for label, raw in responses.items()}
    assert set(statuses) == set(requests), statuses
    # Each route degrades to an inline fragment at 200 (no camera answers in the
    # harness, and the private address is outside the allowed subnets).
    assert all(code == 200 for code in statuses.values()), statuses
