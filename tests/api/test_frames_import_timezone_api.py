"""``exif_timezone`` on the frame import API and web routes (#67 R10)."""

from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from tests.integration.frames.test_import_timezone import _exif_jpeg
from timelapse_manager.config.settings import Settings
from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.security.principal import (
    Principal,
    require_operator_or_admin_principal,
)

_BOUNDARY = "tzbnd"


def _body(data: bytes, *, exif_timezone: str | None) -> bytes:
    parts = b""
    if exif_timezone is not None:
        parts += (
            f"--{_BOUNDARY}\r\n"
            'Content-Disposition: form-data; name="exif_timezone"\r\n\r\n'
            f"{exif_timezone}\r\n"
        ).encode()
    parts += (
        f"--{_BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="files"; filename="a.jpg"\r\n'
        "Content-Type: image/jpeg\r\n\r\n"
    ).encode()
    return parts + data + f"\r\n--{_BOUNDARY}--\r\n".encode()


def _seed(factory: sessionmaker, settings: Settings) -> int:  # type: ignore[type-arg]
    frames_root = settings.paths.frames_root
    assert frames_root is not None
    with session_scope(factory) as session:
        camera = Camera(name="tz-api-cam", address="127.0.0.1", protocol="vapix")
        session.add(camera)
        session.flush()
        project = Project(
            camera_id=camera.id,
            name="tz-api",
            lifecycle_state="active",
            operational_status="idle",
        )
        session.add(project)
        session.flush()
        (frames_root / str(project.id)).mkdir(parents=True, exist_ok=True)
        return project.id


def _frame_times(factory: sessionmaker, project_id: int) -> list[datetime]:  # type: ignore[type-arg]
    with session_scope(factory) as session:
        rows = session.query(Frame).filter_by(project_id=project_id).all()
        return [row.capture_timestamp for row in rows]


class TestApiExifTimezone:
    def _post(self, client: TestClient, token: str, project_id: int, body: bytes):
        return client.post(
            f"/api/v1/projects/{project_id}/frames/import",
            content=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={_BOUNDARY}",
                "Authorization": f"Bearer {token}",
            },
        )

    def test_override_shifts_the_stored_time(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        app = migrated_client.app
        app.dependency_overrides[require_operator_or_admin_principal] = (  # type: ignore[attr-defined]
            lambda: Principal(user_id=1, role="operator")
        )
        try:
            pid = _seed(migrated_factory, settings_no_autostart)
            data = _exif_jpeg("2026:01:15 08:00:00")
            resp = self._post(
                migrated_client,
                cam_auth_token,
                pid,
                _body(data, exif_timezone="America/New_York"),
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["imported_count"] == 1
            assert _frame_times(migrated_factory, pid) == [datetime(2026, 1, 15, 13)]
        finally:
            app.dependency_overrides.clear()  # type: ignore[attr-defined]

    def test_unknown_zone_is_422_and_imports_nothing(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        app = migrated_client.app
        app.dependency_overrides[require_operator_or_admin_principal] = (  # type: ignore[attr-defined]
            lambda: Principal(user_id=1, role="operator")
        )
        try:
            pid = _seed(migrated_factory, settings_no_autostart)
            resp = self._post(
                migrated_client,
                cam_auth_token,
                pid,
                _body(_exif_jpeg(), exif_timezone="Not/AZone"),
            )
            assert resp.status_code == 422, resp.text
            assert _frame_times(migrated_factory, pid) == []
        finally:
            app.dependency_overrides.clear()  # type: ignore[attr-defined]


class TestWebExifTimezone:
    def _seed_web(self) -> int:
        ctx = get_context()
        return _seed(ctx.session_factory, ctx.settings)

    def _post(self, client: TestClient, project_id: int, body: bytes):
        from tests.conftest import csrf_of

        return client.post(
            f"/projects/{project_id}/frames/import",
            content=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={_BOUNDARY}",
                "X-CSRF-Token": csrf_of(client, "/"),
            },
        )

    def test_override_shifts_the_stored_time(self, operator_client: TestClient) -> None:
        pid = self._seed_web()
        resp = self._post(
            operator_client,
            pid,
            _body(_exif_jpeg("2026:07:15 08:00:00"), exif_timezone="Europe/Berlin"),
        )
        assert resp.status_code == 200
        assert _frame_times(get_context().session_factory, pid) == [
            datetime(2026, 7, 15, 6)
        ]

    def test_unknown_zone_is_an_inline_error(self, operator_client: TestClient) -> None:
        pid = self._seed_web()
        resp = self._post(
            operator_client, pid, _body(_exif_jpeg(), exif_timezone="Not/AZone")
        )
        assert resp.status_code == 200
        assert "timezone" in resp.text.lower()
        assert _frame_times(get_context().session_factory, pid) == []
