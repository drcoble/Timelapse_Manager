"""#49: a frame import waiting on the SQLite write lock must not stall the server.

``POST /projects/{id}/frames/import`` used to touch the request's ``Session`` on
the event loop (the project lookup, the transaction release before the threaded
importer and the result render's CSRF read). This drives the real uvicorn
server from ``test_cancelled_requests.py`` (seeded, under the pool probe and the
loop guard) with a large-ish batch -- the importer's full ``MAX_IMPORT_BATCH``
of padded JPEGs, about 10 MB of multipart body -- while another connection
holds SQLite's write lock, and asserts:

* the import really is waiting on the lock while it is held (the request has
  not answered), so the checks below run during the contention;
* ``GET /healthz`` keeps answering, each well under a second, meanwhile;
* the import completes once the lock is released, with every file imported
  and every frame row written;
* the probe saw no request checkout on the event loop and no 5xx.

The e2e loop guard (``loop_guard_args``) additionally fails the test if any
statement for the import route ran on the loop.

``test_100_mb_import_streams_without_stalling_the_loop`` (#67 P1) covers the
body side: a 100 MB multipart import is streamed to a spool directory instead
of buffered and parsed on the loop, so ``/healthz`` keeps answering for the whole
upload and import, and the spool is gone afterwards.
"""

from __future__ import annotations

import re
import sqlite3
import struct
import threading
import time
from pathlib import Path

from tests.e2e.test_cancelled_requests import (
    Server,
    _assert_no_pool_stall,
    _connect,
    _get,
    _login,
    _probe_events,
    _read_response,
    _request,
    _round_events,
    _status,
    _timed_get,
    server,  # noqa: F401 -- the real-server fixture, reused as-is
)
from timelapse_manager.storage.frames import MAX_IMPORT_BATCH

# The seeded project (``_seed_demo_data``): id 1.
_PROJECT_ID = 1
# Bytes of COM-segment padding per JPEG; with MAX_IMPORT_BATCH files this makes
# a ~10 MB multipart body, large enough that reading and parsing it is real work.
_PAD_BYTES = 48 * 1024
# How long the write lock is held while the import waits on it. Well under the
# 15 s busy timeout, so the importer's first write succeeds once it is released.
_LOCK_HOLD_SECONDS = 3.0
# An unrelated request must answer within this while the import waits.
_HEALTHZ_BUDGET = 2.0  # under the 3 s lock hold, with headroom for a slow CI runner
_HEALTHZ_INTERVAL = 0.2
# A 200-file import commits per frame; on the loaded CI runner it took > 40 s
# after the lock was released (run 465), so the import socket waits this long.
_IMPORT_TIMEOUT = 180.0


def _healthz_budget(base_url: str, cookie: bytes) -> float:
    """The per-probe ``/healthz`` budget, scaled to how loaded the host is now.

    The fixed ``_HEALTHZ_BUDGET`` is the floor. On a host running other suites in
    parallel an idle server can itself answer slowly, so the budget is also ten
    times the slowest of a few idle probes taken just before the import starts:
    a loop genuinely blocked behind the import stalls a probe for seconds, far
    beyond that, while CPU contention scales the baseline with it.
    """
    baseline = max(_timed_get(base_url, "/healthz", cookie)[1] for _ in range(5))
    return max(_HEALTHZ_BUDGET, 10 * baseline)


def _padded_jpeg(index: int) -> bytes:
    """Return a minimal valid JPEG padded with a unique comment segment.

    SOI, a COM segment (skipped by the dimension reader), an SOF0 with the
    dimensions, then EOI -- the same shape the import regression tests use, made
    larger and distinct per file.
    """
    comment = f"frame-{index:04d} ".encode().ljust(_PAD_BYTES, b"x")
    com = b"\xff\xfe" + struct.pack(">H", len(comment) + 2) + comment
    sof = (
        b"\xff\xc0"
        + struct.pack(">H", 17)
        + b"\x08"
        + struct.pack(">H", 240)
        + struct.pack(">H", 320)
        + b"\x01\x01\x11\x00"
    )
    return b"\xff\xd8" + com + sof + b"\xff\xd9"


def _multipart(files: list[tuple[str, bytes]], boundary: bytes) -> bytes:
    """Build a multipart/form-data body with one ``files`` part per file."""
    parts = [
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="files"; filename="'
        + name.encode()
        + b'"\r\nContent-Type: image/jpeg\r\n\r\n'
        + data
        + b"\r\n"
        for name, data in files
    ]
    return b"".join(parts) + b"--" + boundary + b"--\r\n"


def _csrf_token(base_url: str, cookie: bytes) -> str:
    """Return the session's CSRF token from a rendered page's meta tag."""
    response = _request(base_url, _get("/renders", cookie))
    assert _status(response) == 200, response[:200]
    match = re.search(rb'<meta name="csrf-token" content="([^"]+)"', response)
    assert match is not None, "no csrf-token meta tag on /renders"
    return match.group(1).decode()


def _import_request(cookie: bytes, csrf: str, body: bytes, boundary: bytes) -> bytes:
    """Build the HTMX-style import POST (CSRF in the header, as HTMX sends it)."""
    return (
        f"POST /projects/{_PROJECT_ID}/frames/import HTTP/1.1\r\n".encode()
        + b"Host: test\r\nCookie: "
        + cookie
        + b"\r\nX-CSRF-Token: "
        + csrf.encode()
        + b"\r\nHX-Request: true\r\n"
        + b"Content-Type: multipart/form-data; boundary="
        + boundary
        + f"\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
        + body
    )


def _uploaded_frame_count(db_path: Path) -> int:
    """Count the seeded project's uploaded frames, straight from SQLite."""
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM frame WHERE project_id = ? AND origin = 'uploaded'",
            (_PROJECT_ID,),
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def test_import_waiting_on_write_lock_does_not_stall_other_requests(
    server: Server,  # noqa: F811 -- the fixture imported above
) -> None:
    """A full-size import held up by the write lock leaves the server responsive."""
    cookie = _login(server.base_url)
    csrf = _csrf_token(server.base_url, cookie)
    files = [(f"import-{i:04d}.jpg", _padded_jpeg(i)) for i in range(MAX_IMPORT_BATCH)]
    boundary = b"tlm49importboundary"
    raw = _import_request(cookie, csrf, _multipart(files, boundary), boundary)
    before = _uploaded_frame_count(server.db_path)
    start = len(_probe_events(server.log_path))
    budget = _healthz_budget(server.base_url, cookie)

    holder = sqlite3.connect(str(server.db_path), timeout=30, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    responses: list[bytes] = []
    errors: list[BaseException] = []

    def send_import() -> None:
        try:
            with _connect(server.base_url) as sock:
                sock.settimeout(_IMPORT_TIMEOUT)
                sock.sendall(raw)
                responses.append(_read_response(sock))
        except BaseException as exc:  # surfaced by the assertions below
            errors.append(exc)

    sender = threading.Thread(target=send_import, daemon=True)
    try:
        sender.start()
        timings: list[tuple[int, float]] = []
        deadline = time.monotonic() + _LOCK_HOLD_SECONDS
        while time.monotonic() < deadline:
            timings.append(_timed_get(server.base_url, "/healthz", cookie))
            time.sleep(_HEALTHZ_INTERVAL)
        # Still blocked: the importer's first frame write waits on the lock.
        import_waiting = sender.is_alive()
    finally:
        holder.rollback()
        holder.close()

    sender.join(_IMPORT_TIMEOUT)
    assert not sender.is_alive(), f"import did not finish within {_IMPORT_TIMEOUT}s"
    assert not errors, errors
    assert import_waiting, "the import finished while the write lock was held"

    assert timings, "no /healthz request was made while the lock was held"
    slow = [(code, round(sec, 2)) for code, sec in timings if sec >= budget]
    assert not slow, f"/healthz stalled behind the waiting import: {slow}"
    assert all(code == 200 for code, _ in timings), timings

    response = responses[0]
    assert _status(response) == 200, response[:300]
    imported = re.search(rb'<span class="mono">(\d+)</span> imported', response)
    assert imported is not None, response[-800:]
    assert int(imported.group(1)) == MAX_IMPORT_BATCH
    assert _uploaded_frame_count(server.db_path) == before + MAX_IMPORT_BATCH

    _assert_no_pool_stall(_round_events(server, start), len(timings) + 1)


# A 100 MB import: 20 files of ~5 MB each (the padding is the bulk).
_BIG_FILE_COUNT = 20
_BIG_PAD_BYTES = 5 * 1024 * 1024


def _big_jpeg(index: int) -> bytes:
    comment = f"big-{index:04d} ".encode().ljust(_BIG_PAD_BYTES, b"y")
    com = b"\xff\xfe" + struct.pack(">H", min(len(comment) + 2, 0xFFFF)) + comment
    sof = (
        b"\xff\xc0"
        + struct.pack(">H", 17)
        + b"\x08"
        + struct.pack(">H", 240)
        + struct.pack(">H", 320)
        + b"\x01\x01\x11\x00"
    )
    return b"\xff\xd8" + com + sof + b"\xff\xd9"


def test_100_mb_import_streams_without_stalling_the_loop(
    server: Server,  # noqa: F811 -- the fixture imported above
) -> None:
    """A 100 MB import leaves ``/healthz`` fast for the whole upload and import."""
    cookie = _login(server.base_url)
    csrf = _csrf_token(server.base_url, cookie)
    files = [(f"big-{i:04d}.jpg", _big_jpeg(i)) for i in range(_BIG_FILE_COUNT)]
    boundary = b"tlm67streamboundary"
    raw = _import_request(cookie, csrf, _multipart(files, boundary), boundary)
    assert len(raw) > 100 * 1024 * 1024
    before = _uploaded_frame_count(server.db_path)
    budget = _healthz_budget(server.base_url, cookie)
    responses: list[bytes] = []
    errors: list[BaseException] = []

    def send_import() -> None:
        try:
            with _connect(server.base_url) as sock:
                sock.settimeout(_IMPORT_TIMEOUT)
                sock.sendall(raw)
                responses.append(_read_response(sock))
        except BaseException as exc:  # surfaced by the assertions below
            errors.append(exc)

    sender = threading.Thread(target=send_import, daemon=True)
    sender.start()
    timings: list[tuple[int, float]] = []
    while sender.is_alive():
        timings.append(_timed_get(server.base_url, "/healthz", cookie))
        time.sleep(_HEALTHZ_INTERVAL)
    sender.join(_IMPORT_TIMEOUT)
    assert not errors, errors

    assert timings, "the import finished before a single /healthz probe"
    slow = [(code, round(sec, 2)) for code, sec in timings if sec >= budget]
    assert not slow, f"/healthz stalled behind the streaming import: {slow}"
    response = responses[0]
    assert _status(response) == 200, response[:300]
    imported = re.search(rb'<span class="mono">(\d+)</span> imported', response)
    assert imported is not None, response[-800:]
    assert int(imported.group(1)) == _BIG_FILE_COUNT
    assert _uploaded_frame_count(server.db_path) == before + _BIG_FILE_COUNT
    # The spool directory is gone once the import is done.
    spool_root = server.db_path.parent / "data" / "tmp"
    assert spool_root.exists() and not list(spool_root.iterdir())
