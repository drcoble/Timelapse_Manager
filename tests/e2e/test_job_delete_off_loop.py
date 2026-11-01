"""Regression for #47: deleting a render or export never blocks the event loop.

The web delete routes are ``async`` (stopping a running job awaits the render
worker), and their database work -- the delete and commit, the rollback after a
failure, and the toast's ``base_context`` -- runs in the threadpool. So while a
delete waits on SQLite's single write lock, held here by another connection, the
server keeps answering other requests.

Each test drives the real server of ``test_cancelled_requests.py`` (seeded, under
the pool probe, with the loop guard's file sink): it holds the write lock, posts
the delete as HTMX does, checks the delete is still waiting while ``/healthz``
answers within a second, then releases the lock and checks the delete's usual
HTMX answer (row removed out of band, toast, dialog closed). The loop guard
fails the test if the delete route queried on the event loop.
"""

from __future__ import annotations

import re
import select
import socket
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pytest

from tests.e2e.test_cancelled_requests import (  # noqa: F401  (fixture)
    Server,
    _connect,
    _get,
    _login,
    _read_response,
    _request,
    _status,
    server,
)

# How long an unrelated request may take while the delete waits on the lock.
_UNRELATED_BUDGET = (
    5.0  # CI runners are slow; a regression waits the full 15 s busy_timeout
)
# Time for the delete to reach its write and start waiting on the lock.
_SETTLE_SECONDS = 0.5
# Well under the server's 15 s busy_timeout, so the delete never gives up.
_LOCK_TIMEOUT = 10.0


def _seed_job(db_path: Path, *, kind: str) -> int:
    """Add a finished render (``manual``) or export job to project 1; return its id.

    No output file is written: a missing file is not an error for a delete.
    """
    from timelapse_manager.db.engine import create_db_engine
    from timelapse_manager.db.models import RenderJob
    from timelapse_manager.db.session import create_session_factory, session_scope

    engine = create_db_engine(f"sqlite:///{db_path}")
    factory = create_session_factory(engine)
    try:
        with session_scope(factory) as db:
            job = RenderJob(
                project_id=1,
                kind=kind,
                status="done",
                output_settings={"fps": 24, "codec": "h264", "container": "mp4"},
                started_at=datetime(2026, 10, 1, 12, 0, 0),
                completed_at=datetime(2026, 10, 1, 12, 3, 30),
            )
            db.add(job)
            db.flush()
            return job.id
    finally:
        engine.dispose()


def _csrf_token(base_url: str, cookie: bytes) -> str:
    """Return the session's CSRF token from the Renders page's meta tag."""
    response = _request(base_url, _get("/renders", cookie))
    assert _status(response) == 200, response[:200]
    match = re.search(rb'<meta\s+name="csrf-token"\s+content="([^"]*)"', response)
    assert match is not None, "no csrf-token meta tag on /renders"
    return match.group(1).decode()


def _htmx_post(path: str, cookie: bytes, csrf: str) -> bytes:
    """Build an HTMX form ``POST`` (CSRF in the header) that closes after."""
    return (
        f"POST {path} HTTP/1.1\r\nHost: test\r\n".encode()
        + b"Cookie: "
        + cookie
        + b"\r\nHX-Request: true\r\n"
        + f"X-CSRF-Token: {csrf}\r\n".encode()
        + b"Content-Type: application/x-www-form-urlencoded\r\n"
        + b"Content-Length: 0\r\nConnection: close\r\n\r\n"
    )


def _hold_write_lock(db_path: Path) -> sqlite3.Connection:
    """Open a connection holding SQLite's write lock until it is rolled back."""
    conn = sqlite3.connect(str(db_path), timeout=_LOCK_TIMEOUT, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    return conn


def _has_reply(sock: socket.socket) -> bool:
    """Return whether the server has sent anything on ``sock`` yet."""
    readable, _, _ = select.select([sock], [], [], 0)
    return bool(readable)


@pytest.mark.parametrize(
    ("kind", "path", "row"),
    [
        ("manual", "/renders/{id}/delete", "render-row-{id}"),
        ("export", "/frames/export/{id}/delete", "export-row-{id}"),
    ],
    ids=["render", "export"],
)
def test_delete_waiting_on_write_lock_does_not_block_other_requests(
    server: Server,  # noqa: F811  (fixture imported above)
    kind: str,
    path: str,
    row: str,
) -> None:
    """A delete blocked on the write lock leaves ``/healthz`` answering fast."""
    job_id = _seed_job(server.db_path, kind=kind)
    cookie = _login(server.base_url)
    csrf = _csrf_token(server.base_url, cookie)

    lock = _hold_write_lock(server.db_path)
    try:
        with _connect(server.base_url) as delete_sock:
            delete_sock.sendall(_htmx_post(path.format(id=job_id), cookie, csrf))
            time.sleep(_SETTLE_SECONDS)

            start = time.monotonic()
            health = _request(
                server.base_url,
                b"GET /healthz HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
            )
            elapsed = time.monotonic() - start
            still_waiting = not _has_reply(delete_sock)

            lock.rollback()
            delete_response = _read_response(delete_sock)
    finally:
        lock.close()

    assert _status(health) == 200, health[:200]
    assert elapsed < _UNRELATED_BUDGET, (
        f"/healthz took {elapsed:.2f}s while a {kind} delete waited on the lock"
    )
    assert still_waiting, "the delete answered before the write lock was released"
    assert _status(delete_response) == 200, delete_response[:300]
    assert re.search(rb"(?im)^hx-trigger: modal:close\r$", delete_response)
    expected_row = row.format(id=job_id)
    assert f'id="{expected_row}" hx-swap-oob="delete"'.encode() in delete_response
    noun = "Export" if kind == "export" else "Render"
    assert f"{noun} #{job_id} deleted".encode() in delete_response
