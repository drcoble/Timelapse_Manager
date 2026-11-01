"""Coordinated shutdown of the real dual-listener service (#67 R4).

These boot the actual service runner (``_serve.py --https-port``: migrations,
HTTPS and HTTP listeners, the shutdown coordinator) in a subprocess and send it
a real ``SIGTERM``, which unit tests of the coordinator cannot do:

* with a video stream held open on the HTTP listener (the client never reads,
  so the response is stuck on backpressure), the service still exits shortly
  after ``server.shutdown_grace_seconds``, and the lifespan teardown ran;
* with a render encoding (a fake ffmpeg that reports steady progress, so the
  stall watchdog never fires), the stop kills ffmpeg, removes the partial
  output and leaves the job ``failed``, a terminal state a user can render
  again from, rather than ``encoding`` with a half-written file.

No real ffmpeg is needed. The fake is Python, like the ones in
``tests/integration/encode/test_ffmpeg_watchdog.py``, and the test waits for its
pid file before signalling, so a loaded host cannot send the stop before the
encode has really started (#129).
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import pytest

from tests.e2e.conftest import (
    _free_port,
    _seed_admin,
    _seed_demo_data,
    loop_guard_args,
)
from tests.e2e.test_cancelled_requests import _connect, _login, _wait_ready

# Small, so the tests are quick; the bound below is the plan's grace + 15 s.
_GRACE_SECONDS = 2
_EXIT_BUDGET = _GRACE_SECONDS + 15.0
# How long the worker may take to claim the seeded job and start the fake.
_START_TIMEOUT = 60.0
# Seconds the test keeps a stream open before the stop.
_STREAM_SETTLE = 0.5
# The service runner's own final line (``service/__init__.py``) and uvicorn's
# lifespan line: both mean the teardown ran to the end.
_SERVICE_STOPPED = "service stopped: both listeners closed, lifespan shut down"
_LIFESPAN_DONE = "Application shutdown complete."

_FAKE_FFMPEG = """\
import os, sys, time
# The service probes ``ffmpeg -version`` at startup; answer it and exit rather
# than treating ``-version`` as the output path (#194).
if sys.argv[1:] == ["-version"]:
    print("ffmpeg version 0.0-fake")
    sys.exit(0)
with open(sys.argv[-1], "wb") as out:
    out.write(b"partial")
with open({outfile!r}, "w") as recorded:
    recorded.write(sys.argv[-1])
with open({pidfile!r}, "w") as pid:
    pid.write(str(os.getpid()))
n = 0
while True:
    n += 1
    sys.stdout.write(
        f"frame={{n}}\\nfps=1.0\\nout_time_us={{n * 40000}}\\n"
        "out_time=N/A\\nspeed=1x\\nprogress=continue\\n"
    )
    sys.stdout.flush()
    time.sleep(0.05)
"""


class Service(NamedTuple):
    """A running service subprocess and where it keeps things."""

    proc: subprocess.Popen[bytes]
    base_url: str
    db_path: Path
    data_dir: Path
    log_path: Path

    def log(self) -> str:
        return self.log_path.read_text(errors="replace")


def _start_service(
    tmp_path: Path, data_dir: Path, db_path: Path, extra_args: list[str]
) -> Service:
    """Start ``_serve.py`` in dual-listener mode; return once HTTP answers."""
    http_port = _free_port()
    https_port = _free_port()
    while https_port == http_port:
        https_port = _free_port()
    log_path = tmp_path / "server.log"
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).parent / "_serve.py"),
                "--db-url",
                f"sqlite:///{db_path}",
                "--data-dir",
                str(data_dir),
                "--port",
                str(http_port),
                "--https-port",
                str(https_port),
                "--shutdown-grace-seconds",
                str(_GRACE_SECONDS),
                "--log-level",
                "INFO",
                *loop_guard_args(data_dir),
                *extra_args,
            ],
            # A file, not a pipe: nothing reads a pipe during the test (#107).
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    base_url = f"http://127.0.0.1:{http_port}"
    service = Service(proc, base_url, db_path, data_dir, log_path)
    try:
        _wait_ready(base_url, proc)
    except Exception as exc:
        _kill(proc)
        raise AssertionError(
            f"service did not start; log:\n{service.log()[-3000:]}"
        ) from exc
    return service


def _kill(proc: subprocess.Popen[bytes]) -> None:
    """Teardown for a service a test left running (a failed assertion)."""
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)


def _stop_and_time(service: Service) -> float:
    """Send ``SIGTERM`` and return the seconds until the process exited."""
    started = time.monotonic()
    service.proc.send_signal(signal.SIGTERM)
    try:
        service.proc.wait(timeout=_EXIT_BUDGET + 30)
    except subprocess.TimeoutExpired:
        _kill(service.proc)
        raise AssertionError(
            f"service ignored SIGTERM; log tail:\n{service.log()[-3000:]}"
        ) from None
    return time.monotonic() - started


def _assert_clean_teardown(service: Service) -> None:
    log = service.log()
    assert service.proc.returncode == 0, log[-3000:]
    # The coordinator, not uvicorn, took the signal, and only once.
    assert log.count("SIGTERM received: stopping both listeners") == 1, log[-3000:]
    assert _LIFESPAN_DONE in log, log[-3000:]
    assert _SERVICE_STOPPED in log, log[-3000:]
    assert log.index(_LIFESPAN_DONE) < log.index(_SERVICE_STOPPED)


def _seed_done_render(db_path: Path, output: Path) -> int:
    """Record a finished render of project 1 whose file is ``output``."""
    from timelapse_manager.db.engine import create_db_engine
    from timelapse_manager.db.models import RenderJob
    from timelapse_manager.db.session import create_session_factory, session_scope

    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        with session_scope(create_session_factory(engine)) as db:
            job = RenderJob(
                project_id=1,
                kind="manual",
                status="done",
                output_settings={"fps": 24, "codec": "h264", "container": "mp4"},
                output_file_path=str(output),
            )
            db.add(job)
            db.flush()
            return job.id
    finally:
        engine.dispose()


def _seed_pending_render(db_path: Path) -> int:
    """Queue a manual render of project 1 for the worker to claim at start."""
    from timelapse_manager.db.engine import create_db_engine
    from timelapse_manager.db.models import RenderJob
    from timelapse_manager.db.session import create_session_factory, session_scope

    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        with session_scope(create_session_factory(engine)) as db:
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
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        row = conn.execute(
            "SELECT status FROM render_job WHERE id = ?", (job_id,)
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else str(row[0])


def _seeded(tmp_path: Path) -> tuple[Path, Path]:
    """Seed the admin and project 1 (with its 65 frame rows); return paths."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = tmp_path / "service.db"
    db_url = f"sqlite:///{db_path}"
    _seed_admin(db_url, data_dir)
    _seed_demo_data(db_url, data_dir)
    return data_dir, db_path


def _render_root(data_dir: Path) -> Path:
    """Project 1's render directory under ``_serve.py``'s frames root."""
    from timelapse_manager.config.settings import PathsSettings, Settings
    from timelapse_manager.db.models import Project
    from timelapse_manager.render.spec import project_render_root

    settings = Settings(
        paths=PathsSettings(data_dir=data_dir, frames_root=data_dir / "frames")
    )
    return project_render_root(settings, Project(id=1, storage_path=None))


@pytest.fixture()
def stream_service(tmp_path: Path) -> Iterator[tuple[Service, int]]:
    """The service with one finished, large (sparse) render to stream."""
    data_dir, db_path = _seeded(tmp_path)
    root = _render_root(data_dir)
    root.mkdir(parents=True)
    video = root / "render-stream.mp4"
    with video.open("wb") as handle:
        # Sparse: far more than the socket buffers hold, at no disk cost.
        handle.truncate(1 << 30)
    job_id = _seed_done_render(db_path, video)
    service = _start_service(tmp_path, data_dir, db_path, [])
    try:
        yield service, job_id
    finally:
        _kill(service.proc)


def test_sigterm_with_open_stream_exits_after_grace(
    stream_service: tuple[Service, int],
) -> None:
    """An unread stream holds a listener only for the grace, then teardown runs."""
    service, job_id = stream_service
    cookie = _login(service.base_url)

    with contextlib.ExitStack() as stack:
        sock = stack.enter_context(_connect(service.base_url))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        sock.sendall(
            f"GET /renders/{job_id}/stream HTTP/1.1\r\nHost: test\r\n".encode()
            + b"Cookie: "
            + cookie
            + b"\r\n\r\n"
        )
        head = sock.recv(4096)
        assert head.startswith(b"HTTP/1.1 200 "), head[:200]
        # Never read further: the response stays stuck on backpressure.
        time.sleep(_STREAM_SETTLE)

        elapsed = _stop_and_time(service)

    log = service.log()
    # The stream was still open, so the listener waited out the grace...
    assert "timeout graceful shutdown exceeded" in log, log[-3000:]
    assert elapsed >= _GRACE_SECONDS - 0.5, f"exited in {elapsed:.1f}s"
    # ...and then the whole stop finished well inside the budget.
    assert elapsed < _EXIT_BUDGET, f"exit took {elapsed:.1f}s; log:\n{log[-3000:]}"
    _assert_clean_teardown(service)
    # The lifespan teardown (HTTPS) waited for the HTTP listener's drain, so no
    # request was still running when the engine went away.
    assert log.index("timeout graceful shutdown exceeded") < log.index(_LIFESPAN_DONE)


def _write_fake_ffmpeg(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Write the steady-progress fake; return (binary, pidfile, outfile)."""
    pidfile = tmp_path / "fake-ffmpeg.pid"
    outfile = tmp_path / "fake-ffmpeg.out"
    script = tmp_path / "fake-ffmpeg"
    body = _FAKE_FFMPEG.format(pidfile=str(pidfile), outfile=str(outfile))
    script.write_text(f"#!{sys.executable}\n{textwrap.dedent(body)}", encoding="utf-8")
    script.chmod(0o755)
    return script, pidfile, outfile


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture()
def encoding_service(
    tmp_path: Path,
) -> Iterator[tuple[Service, int, Path, Path]]:
    """The service with its render worker encoding one job through a fake ffmpeg."""
    data_dir, db_path = _seeded(tmp_path)
    frames = data_dir / "frames" / "1"
    frames.mkdir(parents=True)
    for i in range(65):
        (frames / f"{i:08d}.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    job_id = _seed_pending_render(db_path)
    binary, pidfile, outfile = _write_fake_ffmpeg(tmp_path)
    service = _start_service(
        tmp_path,
        data_dir,
        db_path,
        ["--render-autostart", "--ffmpeg-binary", str(binary)],
    )
    try:
        yield service, job_id, pidfile, outfile
    finally:
        _kill(service.proc)
        if pidfile.exists():
            pid = int(pidfile.read_text())
            if _pid_alive(pid):
                os.kill(pid, signal.SIGKILL)


def test_sigterm_during_encode_requeues_the_job_and_leaves_no_partial(
    encoding_service: tuple[Service, int, Path, Path],
) -> None:
    """A stop mid-encode kills ffmpeg, removes the partial, requeues the job (#178)."""
    service, job_id, pidfile, outfile = encoding_service

    # Wait for the fake itself (its pid file), not just the row, before the stop.
    deadline = time.monotonic() + _START_TIMEOUT
    while time.monotonic() < deadline:
        if (
            pidfile.exists()
            and outfile.exists()
            and _job_status(service.db_path, job_id) == "encoding"
        ):
            break
        time.sleep(0.1)
    else:
        raise AssertionError(
            f"render {job_id} never started encoding (status "
            f"{_job_status(service.db_path, job_id)!r}); log:\n{service.log()[-3000:]}"
        )
    pid = int(pidfile.read_text())
    partial = Path(outfile.read_text())
    assert partial.exists(), "the fake wrote no partial output"

    elapsed = _stop_and_time(service)

    assert elapsed < _EXIT_BUDGET, f"exit took {elapsed:.1f}s"
    _assert_clean_teardown(service)
    # Not a stuck ``encoding`` row: a clean stop hands the job back to the queue
    # once, and the next start runs it again.
    assert _job_status(service.db_path, job_id) == "pending"
    conn = sqlite3.connect(str(service.db_path), timeout=10.0)
    try:
        (requeues,) = conn.execute(
            "SELECT shutdown_requeues FROM render_job WHERE id = ?", (job_id,)
        ).fetchone()
    finally:
        conn.close()
    assert requeues == 1
    assert not partial.exists(), "the interrupted render's partial output was left"
    assert not _pid_alive(pid), "the fake ffmpeg outlived the service"
