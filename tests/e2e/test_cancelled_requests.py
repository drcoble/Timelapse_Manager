"""Regression for #37: a burst of in-flight requests must not stall the next one.

Before the fix, about 24 parallel dashboard sub-requests (the thumbnails, ribbon,
status and alert partials a dashboard load fires) froze the server for the 30 s
QueuePool timeout, whether or not the client then cancelled them:

* every authenticated request wrote ``session.last_active`` in its own
  transaction, so each held SQLite's single write lock until its endpoint
  returned, and the rest waited on the lock while holding pooled connections;
* the default pool (5 + 10) ran dry, and the first-run gate middleware then
  checked out a connection *on the event loop thread*, freezing the loop for
  ``pool_timeout`` -- including the loop work that would have committed the
  lock holder's transaction.

These tests drive a real uvicorn server (the E2E ``_serve.py`` subprocess,
seeded like ``live_server_with_data``) over raw HTTP/1.1 sockets, as a browser
over HTTP/2 does: open the dashboard's sub-requests at once, reset the
connections mid-flight (or let them run), and time the next page.
``last_active`` is backdated first so every request in the burst is due a
session touch -- otherwise the touch throttle alone would make the burst
read-only and the test would pass on a server that still deadlocks under write
contention.

What is asserted is the bug's own signal, not a tight wall clock (a loaded CI
runner took 3-5 s to serve the next page, with nothing wrong): the server runs
under ``_serve_pool_probe.py``, which logs every pool checkout and response
status, and each round asserts

* no checkout waited more than ``_MAX_POOL_WAIT`` for a pool slot,
* no checkout during a request happened on the event-loop thread,
* no response was a 5xx,

plus a loose wall-clock cap on the next page, under the 10 s ``pool_timeout``.

No browser is needed, so these run in the default suite (no ``ui`` marker).
"""

from __future__ import annotations

import json
import re
import socket
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from tests.e2e.conftest import (
    _free_port,
    _seed_admin,
    _seed_demo_data,
    loop_guard_args,
)
from timelapse_manager.security.csrf import mask_secret

# The dashboard's sub-requests for the seeded project (id 1, frames 1..65): the
# shape of one dashboard load. Repeated to the burst size below.
_DASHBOARD_PATHS = (
    *(f"/projects/1/frames/{frame_id}/image" for frame_id in range(1, 9)),
    "/partials/projects/1/ribbon?h=20",
    "/partials/status",
    "/alerts/summary",
    "/partials/projects",
)
_BURST = 24
# Every socket read is bounded, but past the pre-#37 30 s stall, so a regressed
# server finishes its round and fails on the probe assertions (which say why)
# rather than on a bare socket timeout.
_SOCKET_TIMEOUT = 40.0
# Loose on purpose: an unloaded /renders takes ~20 ms, but a busy 2-core CI
# runner still finishing the burst took up to ~5 s. A pool wait would show as
# >= the 10 s pool_timeout (or a 5xx), and the pre-#37 stall was 30 s.
_NEXT_REQUEST_BUDGET = 8.0
# The longest any checkout may wait for a pool slot (or to open an overflow
# connection). Healthy waits are microseconds to milliseconds.
_MAX_POOL_WAIT = 0.5
# How long to wait for the burst's responses to be recorded before asserting.
_DRAIN_TIMEOUT = 40.0
# Probe log line prefix; must match ``_serve_pool_probe.PROBE_PREFIX``.
_PROBE_PREFIX = "#37-PROBE "


def _connect(base_url: str) -> socket.socket:
    """Open a raw TCP connection to the live server."""
    parsed = urllib.parse.urlsplit(base_url)
    assert parsed.hostname is not None and parsed.port is not None
    sock = socket.create_connection((parsed.hostname, parsed.port))
    sock.settimeout(_SOCKET_TIMEOUT)
    return sock


def _read_response(sock: socket.socket) -> bytes:
    """Read one ``Connection: close`` response to EOF."""
    chunks = []
    while chunk := sock.recv(65536):
        chunks.append(chunk)
    return b"".join(chunks)


def _status(response: bytes) -> int:
    """Return the status code of a raw HTTP/1.1 response."""
    match = re.match(rb"HTTP/1\.1 (\d{3}) ", response)
    assert match is not None, f"not an HTTP response: {response[:80]!r}"
    return int(match.group(1))


def _request(base_url: str, raw: bytes) -> bytes:
    """Send one raw request and return the full response."""
    with _connect(base_url) as sock:
        sock.sendall(raw)
        return _read_response(sock)


def _get(path: str, cookie: bytes) -> bytes:
    """Build a cookie-authenticated ``GET`` that closes the connection after."""
    return (
        f"GET {path} HTTP/1.1\r\nHost: test\r\n".encode()
        + b"Cookie: "
        + cookie
        + b"\r\nConnection: close\r\n\r\n"
    )


def _login(base_url: str) -> bytes:
    """Log in as the seeded admin; return the ``name=value`` session cookie."""
    # The sign-in form's pre-session token: the cookie holds the secret, the
    # body a masked token for it (the bare secret is refused).
    presession = "p" * 43
    body = urllib.parse.urlencode(
        {
            "username": "admin",
            "password": "AdminP@ssw0rd1234",
            "csrf_token": mask_secret(presession),
        }
    ).encode()
    response = _request(
        base_url,
        b"POST /login HTTP/1.1\r\nHost: test\r\n"
        + f"Cookie: tlm_presession={presession}\r\n".encode()
        + b"Content-Type: application/x-www-form-urlencoded\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"Connection: close\r\n\r\n"
        + body,
    )
    assert _status(response) == 303, response[:200]
    match = re.search(rb"set-cookie: ([^;]+)", response, re.IGNORECASE)
    assert match is not None, "login set no session cookie"
    return match.group(1)


def _backdate_sessions(db_path: Path) -> None:
    """Make every session's ``last_active`` stale, so each request touches it."""
    conn = sqlite3.connect(str(db_path), timeout=_SOCKET_TIMEOUT)
    try:
        conn.execute("UPDATE session SET last_active = datetime('now', '-10 minutes')")
        conn.commit()
    finally:
        conn.close()


def _open_burst(base_url: str, cookie: bytes) -> list[socket.socket]:
    """Send the dashboard's sub-requests on ``_BURST`` parallel connections."""
    paths = (_DASHBOARD_PATHS * 3)[:_BURST]
    socks = []
    for path in paths:
        sock = _connect(base_url)
        sock.sendall(_get(path, cookie))
        socks.append(sock)
    return socks


def _reset(sock: socket.socket) -> None:
    """Close with an RST (``SO_LINGER`` 0), as a browser cancelling does."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()


def _timed_get(base_url: str, path: str, cookie: bytes) -> tuple[int, float]:
    """Return ``(status, seconds)`` for one ``GET``."""
    start = time.monotonic()
    response = _request(base_url, _get(path, cookie))
    return _status(response), time.monotonic() - start


def _wait_ready(base_url: str, proc: subprocess.Popen[bytes]) -> None:
    """Wait until the server answers ``/login`` (the e2e harness's readiness)."""
    deadline = time.monotonic() + 45.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early with {proc.returncode}")
        try:
            response = _request(
                base_url,
                b"GET /login HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )
            if _status(response) == 200:
                return
        except OSError:
            pass
        time.sleep(0.2)
    raise RuntimeError("server did not become ready within 45 s")


class Server(NamedTuple):
    """A running probe server: where it listens, its database and its log."""

    base_url: str
    db_path: Path
    log_path: Path


def _probe_events(log_path: Path) -> list[dict[str, Any]]:
    """Return every probe event the server has logged so far, in order."""
    events = []
    for line in log_path.read_text(errors="replace").splitlines():
        if line.startswith(_PROBE_PREFIX):
            events.append(json.loads(line[len(_PROBE_PREFIX) :]))
    return events


def _round_events(server: Server, start: int) -> list[dict[str, Any]]:
    """Wait until every request of the round has a response; return its events.

    A reset request keeps running in the server after its client has gone, so
    its checkouts and status land after the next page may already be served. A
    request reset before the server read it never starts at all, so the round is
    done once every request the app *received* has started a response (stable
    across two polls), not once a fixed count has.
    """
    deadline = time.monotonic() + _DRAIN_TIMEOUT
    settled = False
    while True:
        events = _probe_events(server.log_path)[start:]
        starts = sum(1 for event in events if event["kind"] == "start")
        statuses = sum(1 for event in events if event["kind"] == "status")
        done = starts > 0 and statuses >= starts
        if (done and settled) or time.monotonic() > deadline:
            return events
        settled = done
        time.sleep(0.1)


def _assert_no_pool_stall(events: list[dict[str, Any]], min_responses: int) -> None:
    """Assert the #37 signals are absent from one round's probe events."""
    starts = [event for event in events if event["kind"] == "start"]
    statuses = [event for event in events if event["kind"] == "status"]
    checkouts = [event for event in events if event["kind"] == "checkout"]
    assert len(statuses) >= max(len(starts), min_responses), (
        f"{len(statuses)} responses for {len(starts)} requests received "
        f"(at least {min_responses} expected) within {_DRAIN_TIMEOUT:.0f}s"
    )
    assert checkouts, "the probe recorded no pool checkouts"
    max_wait = max(event["wait"] for event in checkouts)
    assert max_wait < _MAX_POOL_WAIT, (
        f"a checkout waited {max_wait:.2f}s for a pool slot"
    )
    on_loop = [e for e in checkouts if e["request"] and e["loop"]]
    assert not on_loop, f"{len(on_loop)} request checkouts ran on the event loop"
    errors = [(e["path"], e["status"]) for e in statuses if e["status"] >= 500]
    assert not errors, f"5xx responses: {errors}"


@pytest.fixture()
def server(tmp_path: Path) -> Iterator[Server]:
    """Boot the seeded app under the pool probe; yield where it runs.

    Unlike ``live_server_with_data`` the server's output goes to a file, not an
    unread pipe, and teardown kills a server that ignores ``SIGTERM``: a server
    wedged by this very bug (its loop frozen, its stderr full of tracebacks)
    must fail the test, not hang the run.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = tmp_path / "e2e_test.db"
    db_url = f"sqlite:///{db_path}"
    _seed_admin(db_url, data_dir)
    _seed_demo_data(db_url, data_dir)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    serve_script = Path(__file__).parent / "_serve_pool_probe.py"
    log_path = tmp_path / "server.log"
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                str(serve_script),
                "--db-url",
                db_url,
                "--data-dir",
                str(data_dir),
                "--port",
                str(port),
                *loop_guard_args(data_dir),
            ],
            stdout=log,
            stderr=log,
        )
    try:
        _wait_ready(base_url, proc)
        yield Server(base_url, db_path, log_path)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# Two rounds per test: the stall was timing dependent, so each test gets two
# chances to hit the window, and the second round also proves the first left
# nothing (connections, the write lock) behind.
_ROUNDS = 2


@pytest.mark.parametrize("abort_after_ms", [30, 100])
def test_cancelled_burst_does_not_stall_next_request(
    server: Server, abort_after_ms: int
) -> None:
    """Resetting a burst mid-flight leaves the pool and the loop free."""
    cookie = _login(server.base_url)
    for _ in range(_ROUNDS):
        _backdate_sessions(server.db_path)
        start = len(_probe_events(server.log_path))
        socks = _open_burst(server.base_url, cookie)
        time.sleep(abort_after_ms / 1000)
        for sock in socks:
            _reset(sock)

        status, elapsed = _timed_get(server.base_url, "/renders", cookie)

        # Requests reset before the server read them never start; /renders must.
        _assert_no_pool_stall(_round_events(server, start), 1)
        assert status == 200
        assert elapsed < _NEXT_REQUEST_BUDGET, (
            f"/renders took {elapsed:.2f}s after {_BURST} requests were reset "
            f"{abort_after_ms} ms into flight"
        )


def test_burst_in_flight_does_not_stall_or_fail(server: Server) -> None:
    """A request arriving behind a live burst is not stalled, and nothing 5xx's.

    No client cancels here: the stall needed only the burst and one more request
    arriving once it was in flight. The burst's own requests used to end ``500``
    (``database is locked``) after ~30 s.
    """
    cookie = _login(server.base_url)
    for _ in range(_ROUNDS):
        _backdate_sessions(server.db_path)
        start = len(_probe_events(server.log_path))
        socks = _open_burst(server.base_url, cookie)
        try:
            time.sleep(0.05)
            status, elapsed = _timed_get(server.base_url, "/renders", cookie)
            burst_statuses = [_status(_read_response(sock)) for sock in socks]
        finally:
            for sock in socks:
                sock.close()

        _assert_no_pool_stall(_round_events(server, start), _BURST + 1)
        assert status == 200
        assert elapsed < _NEXT_REQUEST_BUDGET, (
            f"/renders took {elapsed:.2f}s behind {_BURST} in-flight requests"
        )
        # Frame images 404 (the seed writes no files); nothing may be a 5xx.
        assert all(code < 500 for code in burst_statuses), burst_statuses
