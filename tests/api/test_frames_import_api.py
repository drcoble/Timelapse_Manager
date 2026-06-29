"""API tests for POST /api/v1/projects/{id}/frames/import.

Covers:
- 401 without bearer token
- 422 when no files parts are provided
- 404 for a non-existent project
- successful import returns JSON with imported_count / skipped_count shape
"""

from __future__ import annotations

import struct

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from timelapse_manager.config.settings import Settings
from timelapse_manager.db.models import Camera, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.security.principal import (
    Principal,
    require_operator_or_admin_principal,
)

# ---------------------------------------------------------------------------
# Minimal JPEG builder
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
# Multipart builder
# ---------------------------------------------------------------------------


def _multipart_body(
    filename: str, data: bytes, boundary: bytes = b"apitestboundary"
) -> bytes:
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
# Auth / override helpers
# ---------------------------------------------------------------------------


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _override_operator(app: object) -> None:
    app.dependency_overrides[require_operator_or_admin_principal] = lambda: Principal(  # type: ignore[attr-defined]
        user_id=1, role="operator"
    )


def _clear_overrides(app: object) -> None:
    app.dependency_overrides.clear()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_project(
    factory: sessionmaker,  # type: ignore[type-arg]
    settings: Settings,
    *,
    name: str = "fi-api-proj",
) -> int:
    frames_root = settings.paths.frames_root
    assert frames_root is not None
    with session_scope(factory) as session:
        cam = Camera(name=f"{name}-cam", address="127.0.0.1", protocol="vapix")
        session.add(cam)
        session.flush()
        proj = Project(
            camera_id=cam.id,
            name=name,
            lifecycle_state="active",
            operational_status="idle",
        )
        session.add(proj)
        session.flush()
        project_id = proj.id
        (frames_root / str(project_id)).mkdir(parents=True, exist_ok=True)
    return project_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFrameImportAPIAuth:
    def test_import_returns_401_without_token(
        self, migrated_client: TestClient
    ) -> None:
        boundary = b"authtest"
        body = _multipart_body("frame.jpg", _minimal_jpeg(), boundary)
        resp = migrated_client.post(
            "/api/v1/projects/1/frames/import",
            content=body,
            headers={"Content-Type": "multipart/form-data; boundary=authtest"},
        )
        assert resp.status_code == 401


class TestFrameImportAPIErrors:
    def test_no_files_parts_returns_422(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        """Multipart body with no 'files' part → 422."""
        _override_operator(migrated_client.app)
        try:
            project_id = _seed_project(
                migrated_factory, settings_no_autostart, name="fi-api-nofiles"
            )
            # Body has a part with field name 'other', not 'files'.
            body = (
                b"--apibnd\r\n"
                b'Content-Disposition: form-data; name="other"\r\n'
                b"\r\nvalue\r\n"
                b"--apibnd--\r\n"
            )
            resp = migrated_client.post(
                f"/api/v1/projects/{project_id}/frames/import",
                content=body,
                headers={
                    "Content-Type": "multipart/form-data; boundary=apibnd",
                    **_auth(cam_auth_token),
                },
            )
            assert resp.status_code == 422
        finally:
            _clear_overrides(migrated_client.app)

    def test_nonexistent_project_returns_404(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
    ) -> None:
        _override_operator(migrated_client.app)
        try:
            boundary = b"apitestboundary"
            body = _multipart_body("frame.jpg", _minimal_jpeg(), boundary)
            resp = migrated_client.post(
                "/api/v1/projects/99999/frames/import",
                content=body,
                headers={
                    "Content-Type": "multipart/form-data; boundary=apitestboundary",
                    **_auth(cam_auth_token),
                },
            )
            assert resp.status_code == 404
        finally:
            _clear_overrides(migrated_client.app)


class TestFrameImportAPISuccess:
    def test_import_returns_json_shape(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        """Successful import returns JSON with imported_count and skipped_count."""
        _override_operator(migrated_client.app)
        try:
            project_id = _seed_project(
                migrated_factory, settings_no_autostart, name="fi-api-ok"
            )
            boundary = b"apitestboundary"
            body = _multipart_body("capture.jpg", _minimal_jpeg(), boundary)
            resp = migrated_client.post(
                f"/api/v1/projects/{project_id}/frames/import",
                content=body,
                headers={
                    "Content-Type": "multipart/form-data; boundary=apitestboundary",
                    **_auth(cam_auth_token),
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "imported_count" in data
            assert "skipped_count" in data
            assert "skipped" in data
            assert isinstance(data["skipped"], list)
        finally:
            _clear_overrides(migrated_client.app)
