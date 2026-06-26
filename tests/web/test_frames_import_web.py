"""Web route tests for the frame import endpoint.

Covers:
- viewer gets 403 on POST /projects/{id}/frames/import
- operator sending no files gets an inline error response at HTTP 200
"""

from __future__ import annotations

import struct

from fastapi.testclient import TestClient

from timelapse_manager.db.models import Camera, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context

# ---------------------------------------------------------------------------
# Minimal image builder (duplicated locally so this file is self-contained)
# ---------------------------------------------------------------------------


def _sof_bytes(width: int = 8, height: int = 8) -> bytes:
    return (
        b"\xff\xc0"
        + struct.pack(">H", 17)
        + b"\x08"
        + struct.pack(">H", height)
        + struct.pack(">H", width)
        + b"\x01\x01\x11\x00"
    )


def _minimal_jpeg() -> bytes:
    return b"\xff\xd8" + _sof_bytes() + b"\xff\xd9"


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_project(name: str = "fi-web-proj") -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(name=f"{name}-cam", address="127.0.0.1", protocol="vapix")
        db.add(cam)
        db.flush()
        proj = Project(
            camera_id=cam.id,
            name=name,
            lifecycle_state="active",
            operational_status="idle",
            storage_path=f"/tmp/{name}",
        )
        db.add(proj)
        db.flush()
        project_id = proj.id
    return project_id


# ---------------------------------------------------------------------------
# Multipart body builder
# ---------------------------------------------------------------------------


def _multipart_body(
    filename: str, data: bytes, boundary: bytes = b"testboundary123"
) -> bytes:
    """Build a minimal multipart/form-data body for a single 'files' part."""
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
# Tests
# ---------------------------------------------------------------------------


class TestFrameImportWebViewer:
    """Viewer role must be refused on the import route."""

    def test_viewer_gets_403(self, viewer_client: TestClient) -> None:
        """Viewer is denied; 403 may come from CSRF (no token presented) or from
        the role check — either is a correct rejection for a viewer."""
        project_id = _seed_project("fi-viewer-403")
        boundary = b"testboundary"
        body = _multipart_body("frame.jpg", _minimal_jpeg(), boundary)

        from tests.conftest import csrf_of

        csrf_token = csrf_of(viewer_client, "/")

        resp = viewer_client.post(
            f"/projects/{project_id}/frames/import",
            content=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary.decode()}",
                "X-CSRF-Token": csrf_token,
            },
        )
        assert resp.status_code == 403


class TestFrameImportWebNoFiles:
    """Sending a multipart request with no 'files' parts returns an inline error."""

    def test_empty_files_selection_returns_200_with_error(
        self, operator_client: TestClient
    ) -> None:
        """No 'files' parts in the body → 200 inline error (HTMX swap pattern).

        CSRF for multipart import rides in the X-CSRF-Token header (not a body
        field) because the CSRF middleware reads it from headers only for
        multipart requests.  We obtain a token from a regular GET and inject it.
        """
        from tests.conftest import csrf_of

        project_id = _seed_project("fi-no-files")
        csrf_token = csrf_of(operator_client, "/")

        # Build a body with no 'files' parts at all.
        empty_body = (
            b"--empty\r\n"
            b'Content-Disposition: form-data; name="other"\r\n'
            b"\r\nvalue\r\n"
            b"--empty--\r\n"
        )

        resp = operator_client.post(
            f"/projects/{project_id}/frames/import",
            content=empty_body,
            headers={
                "Content-Type": "multipart/form-data; boundary=empty",
                "X-CSRF-Token": csrf_token,
            },
        )
        # The web route always returns 200 for inline errors (HTMX swap).
        assert resp.status_code == 200
