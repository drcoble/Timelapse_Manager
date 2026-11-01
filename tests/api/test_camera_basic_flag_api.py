"""The camera API carries the Basic-over-HTTP opt-in (#69 CAM-S1, OD-3)."""

from __future__ import annotations

from fastapi.testclient import TestClient

API = "/api/v1/cameras"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestCameraBasicOverHttpFlag:
    def test_defaults_off_and_is_shown(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        resp = migrated_client.post(
            API, json={"name": "flag-default"}, headers=_auth(cam_auth_token)
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["allow_basic_over_http"] is False

    def test_opt_in_round_trips(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        resp = migrated_client.post(
            API,
            json={"name": "flag-on", "allow_basic_over_http": True},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 201, resp.text
        camera_id = resp.json()["id"]

        fetched = migrated_client.get(
            f"{API}/{camera_id}", headers=_auth(cam_auth_token)
        )
        assert fetched.json()["allow_basic_over_http"] is True
        listed = migrated_client.get(API, headers=_auth(cam_auth_token)).json()
        assert [c["allow_basic_over_http"] for c in listed if c["id"] == camera_id] == [
            True
        ]
