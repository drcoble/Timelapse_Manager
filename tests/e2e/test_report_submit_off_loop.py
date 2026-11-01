"""Regression for #48: filing a problem report never runs database work on the loop.

``POST /report`` is ``async`` (it reads the body and takes the #7 dumps on the
event loop), so its Session work -- the commits, the audit and the form/result
contexts -- has to run in the worker pool. Here another connection holds
SQLite's write lock while a report is filed: the report waits for the lock, and
an unrelated request must still answer at once, because the loop is free.

This reuses the #37 real-server harness (``test_cancelled_requests.py``): the
server runs under the pool probe, so a connection checked out on the event loop
during a request fails the test even when the wall clock looks fine, and the
loop guard (``tests/loop_guard_support.py``) records any statement run there.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
import urllib.parse

from tests.e2e import test_cancelled_requests as harness
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
)

# The #37 harness's seeded server under the pool probe, reused as is.
server = harness.server

# How long the report is left waiting on the held lock before the probe
# request; well under the 15 s busy_timeout, so the filing then succeeds.
_HOLD_BEFORE_PROBE = 0.5
# An unrelated request behind a free loop answers in milliseconds; a loop
# waiting on SQLite's lock would hold it for up to the busy timeout.
_PROBE_BUDGET = (
    5.0  # CI runners are slow; a regression waits the full 15 s busy_timeout
)


def _csrf_token(base_url: str, cookie: bytes) -> str:
    """Read the CSRF token from a page's ``csrf-token`` meta tag."""
    response = _request(base_url, _get("/about", cookie))
    assert _status(response) == 200, response[:200]
    match = re.search(rb'<meta\s+name="csrf-token"\s+content="([^"]*)"', response)
    assert match is not None, "no csrf-token meta tag on /about"
    return match.group(1).decode()


def _report_post(cookie: bytes, csrf: str) -> bytes:
    """A plain (non-HTMX) report submission without a snapshot."""
    body = urllib.parse.urlencode(
        {"title": "Lock held while filing", "description": "#48", "area": "other"}
    ).encode()
    return (
        b"POST /report HTTP/1.1\r\nHost: test\r\n"
        b"Content-Type: application/x-www-form-urlencoded\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + f"X-CSRF-Token: {csrf}\r\n".encode()
        + b"Cookie: "
        + cookie
        + b"\r\nConnection: close\r\n\r\n"
        + body
    )


def test_report_waiting_on_write_lock_does_not_stall_the_loop(server: Server) -> None:
    """A report blocked on the write lock leaves other requests unaffected."""
    cookie = _login(server.base_url)
    csrf = _csrf_token(server.base_url, cookie)
    start = len(_probe_events(server.log_path))

    holder = sqlite3.connect(
        str(server.db_path), timeout=_SOCKET_TIMEOUT, isolation_level=None
    )
    responses: list[bytes] = []
    submit = threading.Thread(
        target=lambda: responses.append(
            _request(server.base_url, _report_post(cookie, csrf))
        )
    )
    try:
        holder.execute("BEGIN IMMEDIATE")
        submit.start()
        time.sleep(_HOLD_BEFORE_PROBE)

        # /healthz needs no session, so it never waits on the lock itself.
        probe_start = time.monotonic()
        probe = _request(
            server.base_url,
            b"GET /healthz HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
        )
        elapsed = time.monotonic() - probe_start
        still_waiting = submit.is_alive()
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        submit.join(timeout=_SOCKET_TIMEOUT)

    assert _status(probe) == 200
    assert elapsed < _PROBE_BUDGET, f"/healthz took {elapsed:.2f}s behind the lock"
    assert still_waiting, "the report did not wait on the held write lock"
    assert responses, "the report submission never answered"
    assert _status(responses[0]) == 200, responses[0][:300]
    assert re.search(rb"TLM-\d{8}-\d{4}", responses[0]), "no report id shown"
    _assert_no_pool_stall(_round_events(server, start), 2)
