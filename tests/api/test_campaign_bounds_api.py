"""API tests for project campaign bounds (start/end date + frame cap).

Uses the migrated_client fixture and cam_auth_token bearer auth (matching the
other project API tests). Covers create / update / clone round-trips of the three
new fields and validation of the end>start and positive-cap rules.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from timelapse_manager.config.settings import Settings
from timelapse_manager.db.engine import create_db_engine
from timelapse_manager.db.models import Camera
from timelapse_manager.db.session import create_session_factory, session_scope

API = "/api/v1/projects"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_camera(settings: Settings) -> int:
    engine = create_db_engine(settings.database.url)
    factory = create_session_factory(engine)
    try:
        with session_scope(factory) as session:
            cam = Camera(
                name="cb-api-cam",
                address="127.0.0.1",
                protocol="vapix",
                snapshot_uri="http://127.0.0.1/snap",
            )
            session.add(cam)
            session.flush()
            return cam.id
    finally:
        engine.dispose()


class TestCreateWithBounds:
    def test_create_persists_and_round_trips_bounds(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart)
        resp = migrated_client.post(
            API,
            json={
                "name": "Bounded",
                "camera_id": camera_id,
                "capture_interval_seconds": 60,
                "start_date": "2026-07-01T08:00:00",
                "end_date": "2026-07-10T18:00:00",
                "max_frame_count": 500,
            },
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["max_frame_count"] == 500
        assert body["start_date"].startswith("2026-07-01T08:00")
        assert body["end_date"].startswith("2026-07-10T18:00")

        # GET round-trips the stored values.
        got = migrated_client.get(
            f"{API}/{body['id']}", headers=_auth(cam_auth_token)
        ).json()
        assert got["max_frame_count"] == 500
        assert got["start_date"].startswith("2026-07-01T08:00")

    def test_create_aware_datetime_normalised_to_utc(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        # An aware input with a +02:00 offset is stored as the equivalent UTC.
        camera_id = _seed_camera(settings_no_autostart)
        resp = migrated_client.post(
            API,
            json={
                "name": "AwareTZ",
                "camera_id": camera_id,
                "capture_interval_seconds": 60,
                "start_date": "2026-07-01T10:00:00+02:00",
            },
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["start_date"].startswith("2026-07-01T08:00")

    def test_create_without_bounds_leaves_them_null(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart)
        resp = migrated_client.post(
            API,
            json={
                "name": "Unbounded",
                "camera_id": camera_id,
                "capture_interval_seconds": 60,
            },
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["start_date"] is None
        assert body["end_date"] is None
        assert body["max_frame_count"] is None


class TestCreateValidation:
    def test_end_not_after_start_is_422(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart)
        resp = migrated_client.post(
            API,
            json={
                "name": "BadDates",
                "camera_id": camera_id,
                "capture_interval_seconds": 60,
                "start_date": "2026-07-10T08:00:00",
                "end_date": "2026-07-01T08:00:00",
            },
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 422
        assert "end_date" in resp.text

    def test_equal_dates_is_422(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart)
        resp = migrated_client.post(
            API,
            json={
                "name": "EqualDates",
                "camera_id": camera_id,
                "capture_interval_seconds": 60,
                "start_date": "2026-07-10T08:00:00",
                "end_date": "2026-07-10T08:00:00",
            },
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 422

    def test_non_positive_frame_cap_is_422(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart)
        resp = migrated_client.post(
            API,
            json={
                "name": "ZeroCap",
                "camera_id": camera_id,
                "capture_interval_seconds": 60,
                "max_frame_count": 0,
            },
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 422


class TestUpdateAndClone:
    def _create(self, client: TestClient, token: str, camera_id: int, name: str) -> int:
        resp = client.post(
            API,
            json={
                "name": name,
                "camera_id": camera_id,
                "capture_interval_seconds": 60,
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    def test_update_sets_bounds(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart)
        pid = self._create(migrated_client, cam_auth_token, camera_id, "ToBound")
        resp = migrated_client.patch(
            f"{API}/{pid}",
            json={
                "start_date": "2026-08-01T00:00:00",
                "end_date": "2026-08-05T00:00:00",
                "max_frame_count": 42,
            },
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["max_frame_count"] == 42
        assert body["start_date"].startswith("2026-08-01T00:00")

    def test_update_rejects_end_before_start(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart)
        pid = self._create(migrated_client, cam_auth_token, camera_id, "ToBadBound")
        resp = migrated_client.patch(
            f"{API}/{pid}",
            json={
                "start_date": "2026-08-05T00:00:00",
                "end_date": "2026-08-01T00:00:00",
            },
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 422

    def test_clone_copies_bounds(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart)
        resp = migrated_client.post(
            API,
            json={
                "name": "CloneSource",
                "camera_id": camera_id,
                "capture_interval_seconds": 60,
                "start_date": "2026-09-01T00:00:00",
                "end_date": "2026-09-10T00:00:00",
                "max_frame_count": 99,
            },
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 201, resp.text
        source_id = resp.json()["id"]

        clone = migrated_client.post(
            f"{API}/{source_id}/clone",
            json={"name": "ClonedBounds"},
            headers=_auth(cam_auth_token),
        )
        assert clone.status_code == 201, clone.text
        body = clone.json()
        assert body["max_frame_count"] == 99
        assert body["start_date"].startswith("2026-09-01T00:00")
        assert body["end_date"].startswith("2026-09-10T00:00")
