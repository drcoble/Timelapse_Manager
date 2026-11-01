"""Regression for #62: cancelling a running render never blocks the event loop.

Deleting a render that is encoding cancels its job task in the render worker.
The worker's cancel handler then records the job ``failed``. That write used to
run synchronously on the event loop, so while SQLite's write lock was held by
another connection every request froze for up to the 15 s busy timeout, and the
delete then failed (rc5 acceptance test on the test host: 14.6 s, then 409).
The handler now writes in a worker thread, shielded and awaited, so the loop
stays free and the delete still sees the job settled.

The test drives a real server (``_serve_render_worker.py``: the e2e app with the
render worker started and a slow fake encoder, under the loop guard's file
sink). A pending render is seeded before boot, the worker claims it, and the
fake encoder writes its partial output and keeps "encoding". Then the test holds
the write lock, posts the delete as HTMX does, checks ``/healthz`` answers within
the budget while the delete waits, releases the lock, and checks the delete
succeeds: ``200``, the row gone, the partial output removed. The loop guard
fails the test if the worker (or the route) queried on the event loop.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.e2e.conftest import (
    _free_port,
    _seed_admin,
    _seed_demo_data,
    loop_guard_args,
)
from tests.e2e.test_cancelled_requests import (
    Server,
    _connect,
    _login,
    _read_response,
    _request,
    _status,
    _wait_ready,
)
from tests.e2e.test_job_delete_off_loop import (
    _csrf_token,
    _has_reply,
    _hold_write_lock,
    _htmx_post,
)

# How long ``/healthz`` may take while the delete waits on the lock. Loose for
# slow CI runners; the regression is the full 15 s busy timeout.
_UNRELATED_BUDGET = 5.0
# Time for the delete to reach the worker's cancel and its write to start
# waiting on the lock.
_SETTLE_SECONDS = 0.5
# How long the worker may take to claim the seeded job and start encoding.
_START_TIMEOUT = 30.0
# Must match ``_serve_render_worker.STARTED_MARKER``.
_STARTED_MARKER = "render-started"


def _seed_pending_render(db_path: Path) -> int:
    """Queue a manual render of project 1 for the worker to claim at start."""
    from timelapse_manager.db.engine import create_db_engine
    from timelapse_manager.db.models import RenderJob
    from timelapse_manager.db.session import create_session_factory, session_scope

    engine = create_db_engine(f"sqlite:///{db_path}")
    factory = create_session_factory(engine)
    try:
        with session_scope(factory) as db:
            job = RenderJob(
                project_id=1,
                kind="manual",
                status="pending",
                output_settings={"fps": 24, "codec": "h264", "container": "mp4"},
            )
            db.add(job)
            db.flush()
            return job.id
    finally:
        engine.dispose()


def _job_status(db_path: Path, job_id: int) -> str | None:
    """Return the job's status, or ``None`` once its row is gone."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        row = conn.execute(
            "SELECT status FROM render_job WHERE id = ?", (job_id,)
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else str(row[0])


def _wait_encoding(server: Server, data_dir: Path, job_id: int) -> Path:
    """Wait until the job is encoding and its partial output exists; return it."""
    marker = data_dir / _STARTED_MARKER
    deadline = time.monotonic() + _START_TIMEOUT
    while time.monotonic() < deadline:
        if marker.exists() and _job_status(server.db_path, job_id) == "encoding":
            partial = Path(marker.read_text())
            assert partial.exists(), "the fake encoder wrote no partial output"
            return partial
        time.sleep(0.1)
    log = server.log_path.read_text(errors="replace")[-2000:]
    raise AssertionError(
        f"render {job_id} never started encoding "
        f"(status {_job_status(server.db_path, job_id)!r}); server log:\n{log}"
    )


@pytest.fixture()
def render_server(tmp_path: Path) -> Iterator[tuple[Server, Path, int]]:
    """Boot the seeded app with a running render worker and one pending render.

    Yields the server, its data directory and the queued job's id. Like the
    ``server`` fixture of ``test_cancelled_requests.py``, output goes to a log
    file and teardown kills a server that ignores ``SIGTERM``.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = tmp_path / "e2e_test.db"
    db_url = f"sqlite:///{db_path}"
    _seed_admin(db_url, data_dir)
    _seed_demo_data(db_url, data_dir)
    job_id = _seed_pending_render(db_path)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    serve_script = Path(__file__).parent / "_serve_render_worker.py"
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
        yield Server(base_url, db_path, log_path), data_dir, job_id
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_deleting_running_render_under_write_lock_keeps_loop_free(
    render_server: tuple[Server, Path, int],
) -> None:
    """The worker's cancel write waits on the lock in a thread, not on the loop."""
    server, data_dir, job_id = render_server
    partial = _wait_encoding(server, data_dir, job_id)
    cookie = _login(server.base_url)
    csrf = _csrf_token(server.base_url, cookie)

    lock = _hold_write_lock(server.db_path)
    try:
        with _connect(server.base_url) as delete_sock:
            delete_sock.sendall(_htmx_post(f"/renders/{job_id}/delete", cookie, csrf))
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
        f"/healthz took {elapsed:.2f}s while a running render's cancel waited"
        " on the write lock"
    )
    assert still_waiting, "the delete answered before the write lock was released"
    assert _status(delete_response) == 200, delete_response[:300]
    assert re.search(rb"(?im)^hx-trigger: modal:close\r$", delete_response)
    assert f"Render #{job_id} deleted".encode() in delete_response
    assert _job_status(server.db_path, job_id) is None, "the job row is still there"
    assert not partial.exists(), "the cancelled render's partial output was left"
