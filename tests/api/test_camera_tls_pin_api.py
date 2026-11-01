"""The camera API shows a pin's fingerprint, never the certificate (#69 CAM-S1)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.unit.camera_pin_support import make_self_signed, pem_of
from timelapse_manager.cameras.tls import format_fingerprint, pem_fingerprint
from timelapse_manager.db.models import Camera
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context

API = "/api/v1/cameras"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_unpinned_camera_reports_no_fingerprint(
    migrated_client: TestClient, cam_auth_token: str
) -> None:
    resp = migrated_client.post(
        API, json={"name": "no-pin"}, headers=_auth(cam_auth_token)
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["tls_pinned_fingerprint"] is None


def test_pinned_camera_reports_the_fingerprint_only(
    migrated_client: TestClient, cam_auth_token: str, tmp_path: Path
) -> None:
    created = migrated_client.post(
        API, json={"name": "with-pin"}, headers=_auth(cam_auth_token)
    )
    camera_id = created.json()["id"]
    pem = pem_of(make_self_signed(tmp_path))
    with session_scope(get_context().session_factory) as db:
        db.get(Camera, camera_id).tls_pinned_cert_pem = pem  # type: ignore[union-attr]

    one = migrated_client.get(f"{API}/{camera_id}", headers=_auth(cam_auth_token))
    listed = migrated_client.get(API, headers=_auth(cam_auth_token))

    expected = format_fingerprint(pem_fingerprint(pem))
    assert one.json()["tls_pinned_fingerprint"] == expected
    assert [c["tls_pinned_fingerprint"] for c in listed.json()] == [expected]
    body = "".join(pem.splitlines()[1:-1])
    for text in (one.text, listed.text):
        assert "BEGIN CERTIFICATE" not in text
        assert body[:40] not in text
        assert "tls_pinned_cert_pem" not in text
