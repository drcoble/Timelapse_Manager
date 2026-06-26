"""API-level tests for the operator-or-admin-gated frames endpoints.

Tests:
- 401 without auth token (read + mutation endpoints)
- 403 when require_operator_or_admin_principal override raises HTTP 403
- 200/204 when principal override allows
- frame addressed via wrong project → 404
- GET /api/v1/frames: ordering, pagination, include_deleted, dimension_mismatch
- POST /api/v1/projects/{pid}/frames/{fid}/soft-delete → 200
- POST /api/v1/projects/{pid}/frames/{fid}/restore → 200
- POST /api/v1/projects/{pid}/frames/{fid}/permanent-delete → 204 (with confirm=true)
- POST /api/v1/projects/{pid}/frames/{fid}/permanent-delete → 422 (confirm missing)
- PATCH /api/v1/projects/{pid}/frames/{fid} (timestamp update → 200; extra field → 422)
- POST /api/v1/projects/{pid}/frames/upload → 200 (JPEG/PNG); invalid bytes → 422

No ``@pytest.mark.live`` needed here.
"""

from __future__ import annotations

import struct
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from timelapse_manager.app import create_app
from timelapse_manager.cameras.base import CapturedFrame
from timelapse_manager.capture.frame_writer import FrameWriter
from timelapse_manager.config.settings import (
    CaptureSettings,
    DatabaseSettings,
    LoggingSettings,
    PathsSettings,
    Settings,
)
from timelapse_manager.db.models import Camera, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.security.principal import (
    Principal,
    require_operator_or_admin_principal,
)
from timelapse_manager.security.token import ensure_local_token
from timelapse_manager.storage.paths import frames_root as get_frames_root

_UTC = UTC


# ---------------------------------------------------------------------------
# Minimal valid image helpers
# ---------------------------------------------------------------------------


def make_jpeg(width: int = 640, height: int = 480) -> bytes:
    sof = (
        b"\xff\xc0"
        + struct.pack(">H", 17)
        + b"\x08"
        + struct.pack(">H", height)
        + struct.pack(">H", width)
        + b"\x01\x01\x11\x00"
    )
    return b"\xff\xd8" + sof + b"\xff\xd9"


def make_png(width: int = 320, height: int = 240) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    db_path = tmp_path / "test.db"
    return Settings(
        database=DatabaseSettings(url=f"sqlite:///{db_path}"),
        logging=LoggingSettings(level="WARNING", format="text"),
        paths=PathsSettings(
            data_dir=data_dir,
            frames_root=data_dir / "frames",
            token_file=data_dir / ".local-token",
        ),
        capture=CaptureSettings(autostart=False),
    )


def _run_migrations(settings: Settings) -> None:
    from alembic import command as alembic_command
    from alembic.config import Config

    alembic_ini = Path(__file__).parent.parent.parent / "alembic.ini"
    alembic_dir = Path(__file__).parent.parent.parent / "alembic"
    cfg = Config(str(alembic_ini))
    cfg.set_main_option("script_location", str(alembic_dir))
    cfg.set_main_option("sqlalchemy.url", settings.database.url)
    alembic_command.upgrade(cfg, "head")


def _seed_project_and_frame(
    client: TestClient,
    settings: Settings,
    tmp_path: Path,
    name: str = "api-proj",
    with_file: bool = True,
) -> dict:
    """Seed a Camera + Project + optionally write one Frame via FrameWriter."""
    from timelapse_manager.db.engine import create_db_engine
    from timelapse_manager.db.session import create_session_factory

    engine = create_db_engine(settings.database.url)
    factory = create_session_factory(engine)

    with session_scope(factory) as session:
        cam = Camera(
            name=f"{name}-cam",
            address="127.0.0.1",
            protocol="vapix",
            snapshot_uri="http://127.0.0.1/snap",
        )
        session.add(cam)
        session.flush()
        cam_id = cam.id
        proj = Project(
            camera_id=cam_id,
            name=name,
            capture_interval_seconds=60,
            lifecycle_state="active",
            operational_status="idle",
            frame_count=0,
        )
        session.add(proj)
        session.flush()
        project_id = proj.id

    frame_id = None
    if with_file:
        root = get_frames_root(settings)
        writer = FrameWriter(factory, root)
        written = writer.write(
            project_id,
            CapturedFrame(
                image_bytes=make_jpeg(),
                width=640,
                height=480,
                format="jpeg",
                captured_at=datetime.now(_UTC),
            ),
        )
        frame_id = written.frame_id

    engine.dispose()
    return {
        "project_id": project_id,
        "camera_id": cam_id,
        "frame_id": frame_id,
    }


# ---------------------------------------------------------------------------
# Per-test client fixture (migrated DB + overrideable principal)
# ---------------------------------------------------------------------------


@pytest.fixture()
def frames_client(
    tmp_path: Path,
) -> Generator[tuple[TestClient, Settings, str], None, None]:
    """Yield (client, settings, auth_token) with a fully-migrated DB.

    The test client is started fresh per test so dependency overrides don't
    leak between tests.
    """
    settings = _make_settings(tmp_path)
    _run_migrations(settings)
    token = ensure_local_token(settings)
    app = create_app(settings)
    with TestClient(app) as c:
        yield c, settings, token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _allow_admin() -> Any:
    """FastAPI dependency override: returns a permissive sentinel principal."""
    return Principal(user_id=1, role="admin")


def _allow_operator() -> Any:
    """FastAPI dependency override: returns a permissive operator principal."""
    return Principal(user_id=1, role="operator")


def _deny_admin() -> Any:
    """FastAPI dependency override: always raises 403."""
    raise HTTPException(status_code=403, detail="Forbidden")


# ---------------------------------------------------------------------------
# GET /api/v1/frames — auth gate
# ---------------------------------------------------------------------------


class TestListFramesAuth:
    def test_list_without_token_returns_401(self, frames_client: tuple) -> None:
        client, settings, _ = frames_client
        ctx = _seed_project_and_frame(
            client, settings, Path(settings.paths.data_dir), "list-noauth"
        )

        resp = client.get("/api/v1/frames", params={"project_id": ctx["project_id"]})
        assert resp.status_code == 401

    def test_list_with_wrong_token_returns_401(self, frames_client: tuple) -> None:
        client, settings, _ = frames_client
        ctx = _seed_project_and_frame(
            client, settings, Path(settings.paths.data_dir), "list-badtoken"
        )

        resp = client.get(
            "/api/v1/frames",
            params={"project_id": ctx["project_id"]},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

    def test_list_with_valid_token_returns_200(self, frames_client: tuple) -> None:
        client, settings, token = frames_client
        ctx = _seed_project_and_frame(
            client, settings, Path(settings.paths.data_dir), "list-ok"
        )

        resp = client.get(
            "/api/v1/frames",
            params={"project_id": ctx["project_id"]},
            headers=_auth(token),
        )
        assert resp.status_code == 200

    def test_list_returns_frames_with_expected_fields(
        self, frames_client: tuple
    ) -> None:
        client, settings, token = frames_client
        ctx = _seed_project_and_frame(
            client, settings, Path(settings.paths.data_dir), "list-fields"
        )

        resp = client.get(
            "/api/v1/frames",
            params={"project_id": ctx["project_id"]},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        frames = resp.json()
        assert len(frames) == 1
        frame = frames[0]
        assert "id" in frame
        assert "lifecycle_state" in frame
        assert "dimension_mismatch" in frame
        assert isinstance(frame["dimension_mismatch"], bool)


# ---------------------------------------------------------------------------
# GET /api/v1/frames — ordering, pagination, include_deleted
# ---------------------------------------------------------------------------


class TestListFramesBrowse:
    def test_list_ordered_by_capture_timestamp_asc(self, frames_client: tuple) -> None:
        client, settings, token = frames_client
        from timelapse_manager.db.engine import create_db_engine
        from timelapse_manager.db.session import create_session_factory

        engine = create_db_engine(settings.database.url)
        factory = create_session_factory(engine)

        with session_scope(factory) as session:
            cam = Camera(
                name="lo-api-cam",
                address="127.0.0.1",
                protocol="vapix",
                snapshot_uri="http://127.0.0.1/snap",
            )
            session.add(cam)
            session.flush()
            proj = Project(
                camera_id=cam.id,
                name="lo-api-proj",
                capture_interval_seconds=60,
                lifecycle_state="active",
                operational_status="idle",
                frame_count=0,
            )
            session.add(proj)
            session.flush()
            project_id = proj.id

        base = datetime(2026, 1, 1, tzinfo=_UTC)
        root = get_frames_root(settings)
        writer = FrameWriter(factory, root)
        # Write in reverse order
        ids = []
        for i in [2, 0, 1]:
            written = writer.write(
                project_id,
                CapturedFrame(
                    image_bytes=make_jpeg(),
                    width=640,
                    height=480,
                    format="jpeg",
                    captured_at=base + timedelta(hours=i),
                ),
            )
            ids.append((i, written.frame_id))
        engine.dispose()

        resp = client.get(
            "/api/v1/frames",
            params={"project_id": project_id},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        returned_ids = [f["id"] for f in resp.json()]
        expected_order = [fid for _, fid in sorted(ids, key=lambda x: x[0])]
        assert returned_ids == expected_order

    def test_list_pagination(self, frames_client: tuple) -> None:
        client, settings, token = frames_client
        from timelapse_manager.db.engine import create_db_engine
        from timelapse_manager.db.session import create_session_factory

        engine = create_db_engine(settings.database.url)
        factory = create_session_factory(engine)

        with session_scope(factory) as session:
            cam = Camera(
                name="pag-api-cam",
                address="127.0.0.1",
                protocol="vapix",
                snapshot_uri="http://127.0.0.1/snap",
            )
            session.add(cam)
            session.flush()
            proj = Project(
                camera_id=cam.id,
                name="pag-api-proj",
                capture_interval_seconds=60,
                lifecycle_state="active",
                operational_status="idle",
                frame_count=0,
            )
            session.add(proj)
            session.flush()
            project_id = proj.id

        base = datetime(2026, 1, 1, tzinfo=_UTC)
        root = get_frames_root(settings)
        writer = FrameWriter(factory, root)
        for i in range(5):
            writer.write(
                project_id,
                CapturedFrame(
                    image_bytes=make_jpeg(),
                    width=640,
                    height=480,
                    format="jpeg",
                    captured_at=base + timedelta(minutes=i),
                ),
            )
        engine.dispose()

        resp = client.get(
            "/api/v1/frames",
            params={"project_id": project_id, "limit": 2, "offset": 1},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_list_include_deleted(self, frames_client: tuple) -> None:
        client, settings, token = frames_client
        ctx = _seed_project_and_frame(
            client, settings, Path(settings.paths.data_dir), "id-api"
        )
        project_id = ctx["project_id"]
        frame_id = ctx["frame_id"]

        # Soft-delete the frame via API
        resp = client.post(
            f"/api/v1/projects/{project_id}/frames/{frame_id}/soft-delete",
            headers=_auth(token),
        )
        assert resp.status_code == 200

        # Default listing: frame hidden
        resp = client.get(
            "/api/v1/frames",
            params={"project_id": project_id},
            headers=_auth(token),
        )
        assert all(f["id"] != frame_id for f in resp.json())

        # With include_deleted: frame visible
        resp = client.get(
            "/api/v1/frames",
            params={"project_id": project_id, "include_deleted": True},
            headers=_auth(token),
        )
        assert any(f["id"] == frame_id for f in resp.json())


# ---------------------------------------------------------------------------
# Soft-delete / restore / permanent-delete — auth gate
# ---------------------------------------------------------------------------


class TestMutationAuthGate:
    def test_soft_delete_without_token_returns_401(self, frames_client: tuple) -> None:
        client, settings, _ = frames_client
        ctx = _seed_project_and_frame(
            client, settings, Path(settings.paths.data_dir), "sd-noauth"
        )
        resp = client.post(
            f"/api/v1/projects/{ctx['project_id']}/frames/{ctx['frame_id']}/soft-delete"
        )
        assert resp.status_code == 401

    def test_soft_delete_with_valid_token_returns_200(
        self, frames_client: tuple
    ) -> None:
        client, settings, token = frames_client
        ctx = _seed_project_and_frame(
            client, settings, Path(settings.paths.data_dir), "sd-ok"
        )
        resp = client.post(
            f"/api/v1/projects/{ctx['project_id']}/frames/{ctx['frame_id']}/soft-delete",
            headers=_auth(token),
        )
        assert resp.status_code == 200
        assert resp.json()["lifecycle_state"] == "soft_deleted"

    def test_restore_returns_200(self, frames_client: tuple) -> None:
        client, settings, token = frames_client
        ctx = _seed_project_and_frame(
            client, settings, Path(settings.paths.data_dir), "rs-api"
        )
        pid, fid = ctx["project_id"], ctx["frame_id"]
        # First soft-delete
        client.post(
            f"/api/v1/projects/{pid}/frames/{fid}/soft-delete",
            headers=_auth(token),
        )
        # Then restore
        resp = client.post(
            f"/api/v1/projects/{pid}/frames/{fid}/restore",
            headers=_auth(token),
        )
        assert resp.status_code == 200
        assert resp.json()["lifecycle_state"] == "active"

    def test_permanent_delete_without_confirm_returns_422(
        self, frames_client: tuple
    ) -> None:
        client, settings, token = frames_client
        ctx = _seed_project_and_frame(
            client, settings, Path(settings.paths.data_dir), "pd-noconfirm-api"
        )
        resp = client.post(
            f"/api/v1/projects/{ctx['project_id']}/frames/{ctx['frame_id']}/permanent-delete",
            headers=_auth(token),
        )
        assert resp.status_code == 422

    def test_permanent_delete_with_confirm_returns_204(
        self, frames_client: tuple
    ) -> None:
        client, settings, token = frames_client
        ctx = _seed_project_and_frame(
            client, settings, Path(settings.paths.data_dir), "pd-ok-api"
        )
        resp = client.post(
            f"/api/v1/projects/{ctx['project_id']}/frames/{ctx['frame_id']}/permanent-delete",
            params={"confirm": True},
            headers=_auth(token),
        )
        assert resp.status_code == 204


# ---------------------------------------------------------------------------
# Admin 403 gate via dependency override
# ---------------------------------------------------------------------------


class TestAdminGate403:
    def test_soft_delete_returns_403_when_principal_denied(
        self, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        _run_migrations(settings)
        token = ensure_local_token(settings)
        app = create_app(settings)
        app.dependency_overrides[require_operator_or_admin_principal] = _deny_admin

        with TestClient(app) as client:
            ctx = _seed_project_and_frame(client, settings, tmp_path, "sd-403")
            resp = client.post(
                f"/api/v1/projects/{ctx['project_id']}/frames/{ctx['frame_id']}/soft-delete",
                headers=_auth(token),
            )
        assert resp.status_code == 403

    def test_soft_delete_allowed_for_operator(self, tmp_path: Path) -> None:
        # An operator principal is admitted on the frame mutation surface.
        settings = _make_settings(tmp_path)
        _run_migrations(settings)
        token = ensure_local_token(settings)
        app = create_app(settings)
        app.dependency_overrides[require_operator_or_admin_principal] = _allow_operator

        with TestClient(app) as client:
            ctx = _seed_project_and_frame(client, settings, tmp_path, "sd-op")
            resp = client.post(
                f"/api/v1/projects/{ctx['project_id']}/frames/{ctx['frame_id']}/soft-delete",
                headers=_auth(token),
            )
        assert resp.status_code == 200

    def test_restore_returns_403_when_principal_denied(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        _run_migrations(settings)
        token = ensure_local_token(settings)
        app = create_app(settings)
        app.dependency_overrides[require_operator_or_admin_principal] = _deny_admin

        with TestClient(app) as client:
            ctx = _seed_project_and_frame(client, settings, tmp_path, "rs-403")
            # Soft-delete first without override (direct via allow)
            app.dependency_overrides[require_operator_or_admin_principal] = _allow_admin
            client.post(
                f"/api/v1/projects/{ctx['project_id']}/frames/{ctx['frame_id']}/soft-delete",
                headers=_auth(token),
            )
            app.dependency_overrides[require_operator_or_admin_principal] = _deny_admin
            resp = client.post(
                f"/api/v1/projects/{ctx['project_id']}/frames/{ctx['frame_id']}/restore",
                headers=_auth(token),
            )
        assert resp.status_code == 403

    def test_upload_returns_403_when_principal_denied(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        _run_migrations(settings)
        token = ensure_local_token(settings)
        app = create_app(settings)
        app.dependency_overrides[require_operator_or_admin_principal] = _deny_admin

        with TestClient(app) as client:
            ctx = _seed_project_and_frame(
                client, settings, tmp_path, "up-403", with_file=False
            )
            ts = datetime.now(_UTC).isoformat()
            resp = client.post(
                f"/api/v1/projects/{ctx['project_id']}/frames/upload",
                params={"capture_timestamp": ts, "format": "jpeg"},
                content=make_jpeg(),
                headers={**_auth(token), "Content-Type": "application/octet-stream"},
            )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Frame addressed via wrong project → 404
# ---------------------------------------------------------------------------


class TestWrongProject:
    def test_soft_delete_wrong_project_returns_404(self, frames_client: tuple) -> None:
        client, settings, token = frames_client
        ctx = _seed_project_and_frame(
            client, settings, Path(settings.paths.data_dir), "wp-sd"
        )
        wrong_project_id = ctx["project_id"] + 999
        resp = client.post(
            f"/api/v1/projects/{wrong_project_id}/frames/{ctx['frame_id']}/soft-delete",
            headers=_auth(token),
        )
        assert resp.status_code == 404

    def test_permanent_delete_wrong_project_returns_404(
        self, frames_client: tuple
    ) -> None:
        client, settings, token = frames_client
        ctx = _seed_project_and_frame(
            client, settings, Path(settings.paths.data_dir), "wp-pd"
        )
        wrong_project_id = ctx["project_id"] + 999
        resp = client.post(
            f"/api/v1/projects/{wrong_project_id}/frames/{ctx['frame_id']}/permanent-delete",
            params={"confirm": True},
            headers=_auth(token),
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH (edit capture_timestamp)
# ---------------------------------------------------------------------------


class TestEditFrameApi:
    def test_patch_timestamp_returns_200(self, frames_client: tuple) -> None:
        client, settings, token = frames_client
        ctx = _seed_project_and_frame(
            client, settings, Path(settings.paths.data_dir), "patch-ok"
        )
        pid, fid = ctx["project_id"], ctx["frame_id"]

        new_ts = "2026-06-01T12:00:00"
        resp = client.patch(
            f"/api/v1/projects/{pid}/frames/{fid}",
            json={"capture_timestamp": new_ts},
            headers=_auth(token),
        )
        assert resp.status_code == 200

    def test_patch_extra_field_returns_422(self, frames_client: tuple) -> None:
        client, settings, token = frames_client
        ctx = _seed_project_and_frame(
            client, settings, Path(settings.paths.data_dir), "patch-422"
        )
        pid, fid = ctx["project_id"], ctx["frame_id"]

        resp = client.patch(
            f"/api/v1/projects/{pid}/frames/{fid}",
            json={"capture_timestamp": "2026-06-01T12:00:00", "origin": "uploaded"},
            headers=_auth(token),
        )
        assert resp.status_code == 422

    def test_patch_without_token_returns_401(self, frames_client: tuple) -> None:
        client, settings, _ = frames_client
        ctx = _seed_project_and_frame(
            client, settings, Path(settings.paths.data_dir), "patch-noauth"
        )
        pid, fid = ctx["project_id"], ctx["frame_id"]

        resp = client.patch(
            f"/api/v1/projects/{pid}/frames/{fid}",
            json={"capture_timestamp": "2026-06-01T12:00:00"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Upload endpoint
# ---------------------------------------------------------------------------


class TestUploadFrameApi:
    def test_jpeg_upload_returns_200_with_uploaded_origin(
        self, frames_client: tuple
    ) -> None:
        client, settings, token = frames_client
        ctx = _seed_project_and_frame(
            client,
            settings,
            Path(settings.paths.data_dir),
            "up-api-jpeg",
            with_file=False,
        )
        pid = ctx["project_id"]
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=_UTC).isoformat()

        resp = client.post(
            f"/api/v1/projects/{pid}/frames/upload",
            params={"capture_timestamp": ts, "format": "jpeg"},
            content=make_jpeg(),
            headers={**_auth(token), "Content-Type": "application/octet-stream"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["origin"] == "uploaded"

    def test_png_upload_returns_200(self, frames_client: tuple) -> None:
        client, settings, token = frames_client
        ctx = _seed_project_and_frame(
            client,
            settings,
            Path(settings.paths.data_dir),
            "up-api-png",
            with_file=False,
        )
        pid = ctx["project_id"]
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=_UTC).isoformat()

        resp = client.post(
            f"/api/v1/projects/{pid}/frames/upload",
            params={"capture_timestamp": ts, "format": "png"},
            content=make_png(),
            headers={**_auth(token), "Content-Type": "application/octet-stream"},
        )
        assert resp.status_code == 200

    def test_invalid_bytes_upload_returns_422(self, frames_client: tuple) -> None:
        client, settings, token = frames_client
        ctx = _seed_project_and_frame(
            client,
            settings,
            Path(settings.paths.data_dir),
            "up-api-invalid",
            with_file=False,
        )
        pid = ctx["project_id"]
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=_UTC).isoformat()

        resp = client.post(
            f"/api/v1/projects/{pid}/frames/upload",
            params={"capture_timestamp": ts},
            content=b"not an image",
            headers={**_auth(token), "Content-Type": "application/octet-stream"},
        )
        assert resp.status_code == 422

    def test_format_mismatch_returns_422(self, frames_client: tuple) -> None:
        """PNG bytes declared as jpeg → 422."""
        client, settings, token = frames_client
        ctx = _seed_project_and_frame(
            client,
            settings,
            Path(settings.paths.data_dir),
            "up-api-mismatch",
            with_file=False,
        )
        pid = ctx["project_id"]
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=_UTC).isoformat()

        resp = client.post(
            f"/api/v1/projects/{pid}/frames/upload",
            params={"capture_timestamp": ts, "format": "jpeg"},
            content=make_png(),  # PNG bytes, declared as jpeg
            headers={**_auth(token), "Content-Type": "application/octet-stream"},
        )
        assert resp.status_code == 422

    def test_upload_without_token_returns_401(self, frames_client: tuple) -> None:
        client, settings, _ = frames_client
        ctx = _seed_project_and_frame(
            client,
            settings,
            Path(settings.paths.data_dir),
            "up-api-noauth",
            with_file=False,
        )
        pid = ctx["project_id"]
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=_UTC).isoformat()

        resp = client.post(
            f"/api/v1/projects/{pid}/frames/upload",
            params={"capture_timestamp": ts, "format": "jpeg"},
            content=make_jpeg(),
            headers={"Content-Type": "application/octet-stream"},
        )
        assert resp.status_code == 401

    def test_upload_dimension_mismatch_in_response(self, frames_client: tuple) -> None:
        """Upload response includes dimension_mismatch field (bool)."""
        client, settings, token = frames_client
        ctx = _seed_project_and_frame(
            client,
            settings,
            Path(settings.paths.data_dir),
            "up-api-dims",
            with_file=False,
        )
        pid = ctx["project_id"]
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=_UTC).isoformat()

        resp = client.post(
            f"/api/v1/projects/{pid}/frames/upload",
            params={"capture_timestamp": ts, "format": "jpeg"},
            content=make_jpeg(640, 480),
            headers={**_auth(token), "Content-Type": "application/octet-stream"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "dimension_mismatch" in body
        assert isinstance(body["dimension_mismatch"], bool)
