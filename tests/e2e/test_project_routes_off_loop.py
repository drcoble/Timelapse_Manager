"""A project create/edit waiting on the write lock must not stall the server (#46).

``POST /projects`` and ``POST /projects/{id}/settings`` write the database. While
another connection holds SQLite's write lock, such a write waits up to the busy
timeout (15 s). Before #46 that wait ran on the event loop, so *every* request
waited with it -- the #37 failure mode. Now the write runs in the threadpool and
an unrelated request still answers at once.

Each test holds the write lock from a separate ``sqlite3`` connection, sends the
project write on its own thread, checks the write is still waiting, times an
unrelated ``GET /healthz`` and only then releases the lock so the write can
finish. It reuses the real-server harness (and its loop guard log) from
``test_cancelled_requests.py``.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
import urllib.parse
from pathlib import Path

from tests.e2e import test_cancelled_requests as _harness
from tests.e2e.test_cancelled_requests import (
    Server,
    _get,
    _login,
    _request,
    _status,
    _timed_get,
)

# The #37 probe server: the seeded app as a real uvicorn subprocess.
server = _harness.server

# The unrelated request must answer well inside the busy timeout; an idle
# /healthz takes milliseconds, a loop blocked behind the write takes ~15 s.
_UNRELATED_BUDGET = (
    5.0  # CI runners are slow; a regression waits the full 15 s busy_timeout
)
# How long the project write is given to reach the locked write before the
# unrelated request is timed.
_SETTLE = 0.75
# How long the write may take once the lock is released.
_WRITE_TIMEOUT = 30.0


def _csrf(base_url: str, cookie: bytes) -> str:
    """Return the session's CSRF token from a page's meta tag."""
    response = _request(base_url, _get("/renders", cookie))
    match = re.search(rb'<meta\s+name="csrf-token"\s+content="([^"]*)"', response)
    assert match is not None, "no csrf-token meta tag on /renders"
    return match.group(1).decode()


def _post_form(path: str, cookie: bytes, fields: dict[str, str]) -> bytes:
    """Build a cookie-authenticated urlencoded ``POST``."""
    body = urllib.parse.urlencode(fields).encode()
    return (
        f"POST {path} HTTP/1.1\r\nHost: test\r\n".encode()
        + b"Cookie: "
        + cookie
        + b"\r\nContent-Type: application/x-www-form-urlencoded\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"Connection: close\r\n\r\n"
        + body
    )


def _hold_write_lock(db_path: Path) -> sqlite3.Connection:
    """Open a connection holding SQLite's write lock (``BEGIN IMMEDIATE``)."""
    conn = sqlite3.connect(str(db_path), timeout=_WRITE_TIMEOUT, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    return conn


def _assert_write_does_not_stall_loop(server: Server, raw: bytes) -> bytes:
    """Run *raw* behind a held write lock; assert /healthz stays fast.

    Returns the write's response once the lock has been released.
    """
    responses: list[bytes] = []
    lock = _hold_write_lock(server.db_path)
    try:
        writer = threading.Thread(
            target=lambda: responses.append(_request(server.base_url, raw)),
            daemon=True,
        )
        writer.start()
        time.sleep(_SETTLE)
        # The write must still be waiting on the lock, or the timing below proves
        # nothing.
        assert writer.is_alive(), "the project write finished despite the lock"

        start = time.monotonic()
        health = _request(
            server.base_url,
            b"GET /healthz HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
        )
        elapsed = time.monotonic() - start
        still_waiting = writer.is_alive()
    finally:
        lock.rollback()
        lock.close()

    writer.join(_WRITE_TIMEOUT)
    assert elapsed < _UNRELATED_BUDGET, (
        f"/healthz took {elapsed:.2f}s while a project write waited on the lock"
    )
    assert still_waiting, "the project write finished before the lock was released"
    assert not writer.is_alive(), "the project write never finished"
    assert _status(health) in (200, 503)
    return responses[0]


def test_edit_waiting_on_write_lock_does_not_stall_loop(server: Server) -> None:
    cookie = _login(server.base_url)
    csrf = _csrf(server.base_url, cookie)
    raw = _post_form(
        "/projects/1/settings",
        cookie,
        {
            "name": "E2E Project renamed",
            "camera_id": "1",
            "capture_interval_value": "120",
            "capture_interval_unit": "seconds",
            "csrf_token": csrf,
        },
    )

    response = _assert_write_does_not_stall_loop(server, raw)

    assert _status(response) == 303, response[:300]
    assert b"location: /projects/1\r\n" in response.lower()
    status, _ = _timed_get(server.base_url, "/projects/1", cookie)
    assert status == 200


def test_create_waiting_on_write_lock_does_not_stall_loop(server: Server) -> None:
    cookie = _login(server.base_url)
    csrf = _csrf(server.base_url, cookie)
    raw = _post_form(
        "/projects",
        cookie,
        {
            "name": "E2E Created Behind Lock",
            "camera_id": "1",
            "capture_interval_value": "60",
            "capture_interval_unit": "seconds",
            "csrf_token": csrf,
        },
    )

    response = _assert_write_does_not_stall_loop(server, raw)

    assert _status(response) == 303, response[:300]
    assert re.search(rb"(?i)location: /projects/\d+\r\n", response), response[:300]
