"""API tests for the projected-storage fields on ProjectOut.

Asserts the two projected fields round-trip through the project API: a finite
campaign (end date set) exposes integer projections, while an open-ended one (no
end date) exposes the ``None`` sentinel for both.

Uses the migrated_client + bearer-token fixtures, matching the other project API
tests; helpers are local so this file does not edit the shared conftest.
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
                name="ps-api-cam",
                address="127.0.0.1",
                protocol="vapix",
                snapshot_uri="http://127.0.0.1/snap",
            )
            session.add(cam)
            session.flush()
            return cam.id
    finally:
        engine.dispose()


class TestProjectedFieldsExposed:
    def test_finite_campaign_exposes_integer_projection(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart)
        resp = migrated_client.post(
            API,
            json={
                "name": "Projected Finite",
                "camera_id": camera_id,
                "capture_interval_seconds": 60,
                "start_date": "2026-07-01T00:00:00",
                "end_date": "2026-07-02T00:00:00",
            },
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert "projected_total_bytes" in body
        assert "projected_frame_count_remaining" in body
        # One day at a 60s interval: 1440 frames; none captured yet, so all remain.
        assert body["projected_frame_count_remaining"] == 1440
        assert body["projected_total_bytes"] is not None
        assert body["projected_total_bytes"] > 0

    def test_open_ended_campaign_exposes_null_sentinel(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        camera_id = _seed_camera(settings_no_autostart)
        resp = migrated_client.post(
            API,
            json={
                "name": "Projected Open",
                "camera_id": camera_id,
                "capture_interval_seconds": 60,
            },
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 201, resp.text
        project_id = resp.json()["id"]

        # Round-trip through GET to confirm the sentinel persists, not just create.
        got = migrated_client.get(f"{API}/{project_id}", headers=_auth(cam_auth_token))
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["projected_total_bytes"] is None
        assert body["projected_frame_count_remaining"] is None
