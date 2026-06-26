"""Regression test: frames_import handler must commit before launching the
threaded importer.

Background
----------
The production defect: ``POST /projects/{id}/frames/import`` (``frames_import``
in ``web/routers/frames.py``) ran ``asyncio.to_thread(frame_service.import_frames,
...)`` while the request's own SQLAlchemy session still held an open write
transaction (begun by the auth dependency's ``lookup_session``, which flushes a
``last_active`` update on every authenticated request). SQLite allows only one
writer at a time even in WAL mode; the worker thread's connection therefore waited
out the busy timeout and the request returned HTTP 500.

The fix added ``db.commit()`` immediately before the ``asyncio.to_thread(...)``
call to release the writer lock before the importer needs it.

Why the prior tests missed this
---------------------------------
The two existing tests in ``test_frames_import_web.py`` both return *before* the
``asyncio.to_thread(...)`` call is reached:

* ``test_viewer_gets_403`` — rejected by the role check before any import logic.
* ``test_empty_files_selection_returns_200_with_error`` — the ``if not files:``
  guard fires before ``db.commit()`` and the threaded import, so the contention
  path was never exercised.

How this file proves the fix
-----------------------------
The proof relies on two properties of the existing ``operator_client`` fixture:

1. Its database is a real file-based SQLite DB with WAL.  ``web_settings`` uses
   ``sqlite:///tmp/.../web_test.db`` and the global ``connect`` event listener
   applies ``PRAGMA journal_mode=WAL`` to every connection.

2. SQLite with ``timeout=0`` raises ``OperationalError: database is locked``
   immediately when a second writer tries to acquire the write lock while the
   first still holds it.

``TestFramesImportWriterLockRegression`` monkeypatches ``storage.frames.import_frames``
with a probe that:

* Opens a **new raw sqlite3 connection** (not a pooled one — it cannot reuse the
  request's connection) to the same DB file with ``timeout=0``.
* Executes ``BEGIN IMMEDIATE`` to attempt to acquire the write lock.

With the fix, the request's session has committed before the thread starts, so
``BEGIN IMMEDIATE`` succeeds and the route returns HTTP 200.
Without the fix, the request session still holds the writer slot, ``BEGIN
IMMEDIATE`` raises ``OperationalError``, and the route returns HTTP 500.

``TestFramesImportEndToEnd`` exercises the full handler without monkeypatching:
real image bytes, real storage writes, and the file-based WAL DB the fix enables.
"""

from __future__ import annotations

import sqlite3
import struct
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.conftest import csrf_of
from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context

# ---------------------------------------------------------------------------
# Minimal valid JPEG factory
# ---------------------------------------------------------------------------


def _make_jpeg(width: int = 320, height: int = 240) -> bytes:
    """Return a structurally-minimal JPEG accepted by ``detect_format`` and
    ``read_dimensions`` in ``cameras/_imageinfo.py``.

    Contains: SOI + SOF0 segment with correct precision/height/width + EOI.
    No quantisation or huffman tables — only the fields the importer reads.
    """
    sof = (
        b"\xff\xc0"
        + struct.pack(">H", 17)
        + b"\x08"
        + struct.pack(">H", height)
        + struct.pack(">H", width)
        + b"\x01\x01\x11\x00"
    )
    return b"\xff\xd8" + sof + b"\xff\xd9"


# ---------------------------------------------------------------------------
# Multipart body builder
# ---------------------------------------------------------------------------


def _multipart_body(
    filename: str,
    data: bytes,
    boundary: bytes = b"regressionboundary99",
) -> bytes:
    """Build a minimal multipart/form-data body with one ``files`` part."""
    return (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="files"; filename="'
        + filename.encode()
        + b'"\r\n'
        b"Content-Type: image/jpeg\r\n"
        b"\r\n" + data + b"\r\n"
        b"--" + boundary + b"--\r\n"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_db_path() -> Path:
    """Return the SQLite file path for the running app's main database.

    Uses ``PRAGMA database_list`` so we read the actual file in use rather
    than parsing the settings URL string.
    """
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        rows = db.execute(text("PRAGMA database_list")).fetchall()
        for row in rows:
            if row[1] == "main" and row[2]:
                return Path(row[2])
    raise RuntimeError("Could not determine SQLite database path from PRAGMA list")


def _seed_project(name: str) -> int:
    """Insert a Camera + Project into the app's DB and return the project_id.

    Each call creates a fresh temporary directory for the project's storage path
    so that frame files from one test do not affect another.
    """
    ctx = get_context()
    # mkdtemp gives each test a unique directory; no cross-run collisions.
    frames_dir = Path(tempfile.mkdtemp(prefix=f"fi-{name}-frames-"))

    with session_scope(ctx.session_factory) as db:
        cam = Camera(name=f"{name}-cam", address="10.0.0.1", protocol="vapix")
        db.add(cam)
        db.flush()
        proj = Project(
            camera_id=cam.id,
            name=name,
            lifecycle_state="active",
            operational_status="idle",
            storage_path=str(frames_dir),
        )
        db.add(proj)
        db.flush()
        return proj.id


def _frame_count(project_id: int) -> int:
    """Return the number of Frame rows for ``project_id`` in the app's DB."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        return db.query(Frame).filter(Frame.project_id == project_id).count()


def _stub_import_result() -> Any:
    """Return a minimal object satisfying the import-result template contract.

    The template reads ``result.imported_count``, ``result.skipped_count``, and
    ``result.skipped``; a MagicMock with those attributes set avoids importing the
    real ``ImportResult`` dataclass.
    """
    stub = MagicMock()
    stub.imported_count = 0
    stub.skipped_count = 0
    stub.skipped = []
    return stub


# ---------------------------------------------------------------------------
# Regression test — writer-lock contention
# ---------------------------------------------------------------------------


class TestFramesImportWriterLockRegression:
    """Verify the handler commits before launching the threaded importer.

    The monkeypatched ``import_frames`` opens a second raw sqlite3 connection
    (``timeout=0``) and attempts ``BEGIN IMMEDIATE``. With the fix the write lock
    is free (the request session has already committed) and the acquire succeeds.
    Without the fix the request session still holds the lock, ``BEGIN IMMEDIATE``
    raises ``OperationalError: database is locked``, and the route returns 500.
    """

    def test_handler_releases_write_lock_before_threaded_import(
        self,
        operator_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The request session must have committed before ``asyncio.to_thread``
        launches the importer, leaving the SQLite write lock free.

        Proof: the monkeypatched ``import_frames`` replacement attempts
        ``BEGIN IMMEDIATE`` on a fresh raw connection (``timeout=0``) to the
        same database file.  If the request session still holds the writer slot
        (the behaviour without the ``db.commit()`` fix), the immediate acquire
        raises ``OperationalError`` and the route returns 500.  With the fix the
        commit precedes the call, the acquire succeeds, the stub returns, and the
        route returns 200 with the import-result fragment.

        The write lock that was missing before the fix is the ``last_active``
        flush in ``security.sessions.get_session_row``: every authenticated
        request updates ``SessionRow.last_active`` and flushes it via the request
        session, implicitly beginning a write transaction that is not released
        until the session commits or rolls back at the end of the request.
        Without the ``db.commit()`` the request session held this lock across the
        ``asyncio.to_thread(...)`` boundary.
        """
        db_path = _get_db_path()
        project_id = _seed_project("fi-regression-lock")

        contention_errors: list[Exception] = []

        def _probe_import(*args: Any, **kwargs: Any) -> Any:
            """Try to acquire the SQLite write lock from a fresh connection.

            Opens a new raw sqlite3 connection (not a pooled one — the pool
            could reuse the request's connection and bypass the contention) with
            ``timeout=0`` so any lock contention surfaces immediately rather than
            hanging. Attempts ``BEGIN IMMEDIATE``; on success the transaction is
            rolled back immediately (no actual write is intended).
            """
            try:
                raw = sqlite3.connect(str(db_path), timeout=0)
                try:
                    raw.execute("BEGIN IMMEDIATE")
                    raw.execute("ROLLBACK")
                finally:
                    raw.close()
            except sqlite3.OperationalError as exc:
                # Capture the error for the assertion message, then re-raise so
                # the route's exception handler produces a 500 (as it would have
                # in production without the fix).
                contention_errors.append(exc)
                raise
            return _stub_import_result()

        monkeypatch.setattr(
            "timelapse_manager.storage.frames.import_frames",
            _probe_import,
        )

        boundary = b"regressionboundary99"
        body = _multipart_body("test.jpg", _make_jpeg(), boundary)
        csrf_token = csrf_of(operator_client, "/")

        resp = operator_client.post(
            f"/projects/{project_id}/frames/import",
            content=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary.decode()}",
                "X-CSRF-Token": csrf_token,
            },
        )

        # With the fix: commit precedes to_thread → write lock free → 200.
        # Without the fix: write lock held at to_thread call → OperationalError
        #                  in the worker thread → route returns 500.
        assert resp.status_code == 200, (
            f"Expected 200 (handler committed before threaded import); "
            f"got {resp.status_code}. "
            f"Contention errors captured: {contention_errors}. "
            f"Response body (first 500 chars): {resp.text[:500]}"
        )
        assert "frames-import-result" in resp.text, (
            "Response must contain the #frames-import-result root element"
        )
        # Any captured contention error means the write lock was still held by
        # the request session when the worker thread tried to acquire it.
        assert not contention_errors, (
            f"Write-lock contention detected: the request session still held "
            f"the SQLite writer slot when the import thread tried to acquire it. "
            f"Error: {contention_errors[0]}"
        )


# ---------------------------------------------------------------------------
# End-to-end happy path (approach a)
# ---------------------------------------------------------------------------


class TestFramesImportEndToEnd:
    """End-to-end: a real multipart POST with a valid JPEG returns 200 and
    the import-result fragment, with actual frames written to storage.

    Exercises the full handler path — multipart decode, image validation,
    ``db.commit()``, ``asyncio.to_thread(frame_service.import_frames, ...)``,
    and template render — through a live TestClient against the file-based WAL
    SQLite that the ``operator_client`` fixture provides.
    """

    def test_import_succeeds_with_real_jpeg(
        self,
        operator_client: TestClient,
    ) -> None:
        """A valid JPEG upload returns HTTP 200 and writes one Frame row to the DB.

        Confirms the full happy path works: multipart body parsed, image
        accepted by the format/dimension checker, stored on disk and in the DB,
        re-sequenced, and the result template rendered at HTTP 200.

        The DB assertion (frame count == 1) rules out the silent-skip false-green:
        if the importer skips the file instead of importing it, the template still
        renders at HTTP 200 (skip-not-raise policy) but no Frame row exists.
        """
        project_id = _seed_project("e2e-happy")
        boundary = b"e2eboundary42"
        body = _multipart_body("capture.jpg", _make_jpeg(640, 480), boundary)
        csrf_token = csrf_of(operator_client, "/")

        resp = operator_client.post(
            f"/projects/{project_id}/frames/import",
            content=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary.decode()}",
                "X-CSRF-Token": csrf_token,
            },
        )

        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}. Body: {resp.text[:500]}"
        )
        assert "frames-import-result" in resp.text, (
            "Response must contain the #frames-import-result root element"
        )
        assert "Import failed" not in resp.text, (
            "Unexpected import-error message in response"
        )
        # Verify the frame was actually persisted — not silently skipped.
        count = _frame_count(project_id)
        assert count == 1, (
            f"Expected 1 Frame row in the DB after import; found {count}. "
            f"The importer may have skipped the file without returning an error "
            f"(skip-not-raise policy). Response body: {resp.text[:500]}"
        )

    def test_import_result_reports_imported_count(
        self,
        operator_client: TestClient,
    ) -> None:
        """Importing one valid JPEG reports '1 imported' and persists one Frame row.

        Verifies both the rendered fragment text and the DB state, so a silent
        skip (which also renders at HTTP 200) is caught.
        """
        project_id = _seed_project("e2e-count")
        boundary = b"countboundary77"
        body = _multipart_body("frame001.jpg", _make_jpeg(320, 240), boundary)
        csrf_token = csrf_of(operator_client, "/")

        resp = operator_client.post(
            f"/projects/{project_id}/frames/import",
            content=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary.decode()}",
                "X-CSRF-Token": csrf_token,
            },
        )

        assert resp.status_code == 200
        # The template renders: <span class="mono">N</span> imported
        assert "imported" in resp.text, (
            f"Expected 'imported' keyword in result fragment; got: {resp.text[:500]}"
        )
        # Confirm a Frame row was actually written (not just the text "1 imported").
        count = _frame_count(project_id)
        assert count == 1, (
            f"Expected 1 Frame row; found {count}. Response body: {resp.text[:500]}"
        )
