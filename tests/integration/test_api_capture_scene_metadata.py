"""API manual capture collects scene metadata after the frame (#69 CAM-R2).

``POST /api/v1/cameras/{id}/capture`` uses the same post-capture metadata step
as the background capture path: metadata is read after ``capture()`` returns,
under its own total timeout, and a slow read leaves the frame without metadata
instead of failing the request.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeAdapter
from tests.integration.test_api_cameras import API, _auth, _create_camera, _seed_project
from timelapse_manager.cameras.base import CapturedFrame
from timelapse_manager.config.settings import Settings
from timelapse_manager.db.engine import create_db_engine
from timelapse_manager.db.models import Frame
from timelapse_manager.db.session import create_session_factory, session_scope


class _Adapter(FakeAdapter):
    def __init__(self, *, hang: bool) -> None:
        super().__init__()
        self._hang = hang

    async def collect_scene_metadata(self, frame: CapturedFrame) -> dict | None:
        if self._hang:
            await asyncio.sleep(3600)
        return {"schema_version": 1, "source": "fake"}


def _stored_metadata(settings: Settings) -> list[Any]:
    engine = create_db_engine(settings.database.url)
    try:
        with session_scope(create_session_factory(engine)) as session:
            return [meta for (meta,) in session.query(Frame.scene_metadata).all()]
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("hang", "expected"),
    [(False, {"schema_version": 1, "source": "fake"}), (True, None)],
)
def test_manual_capture_metadata_step(
    migrated_client: TestClient,
    cam_auth_token: str,
    settings_no_autostart: Settings,
    monkeypatch: pytest.MonkeyPatch,
    hang: bool,
    expected: Any,
) -> None:
    import timelapse_manager.capture.supervisor as sup_mod

    monkeypatch.setattr(sup_mod, "SCENE_METADATA_TOTAL_SECONDS", 0.05)
    created = _create_camera(
        migrated_client,
        cam_auth_token,
        name="meta-cam",
        protocol="vapix",
        address="10.0.0.1",
    )
    project_id = _seed_project(settings_no_autostart, created["id"])

    with patch(
        "timelapse_manager.capture.one_shot.build_adapter",
        return_value=_Adapter(hang=hang),
    ):
        resp = migrated_client.post(
            f"{API}/{created['id']}/capture",
            json={"project_id": project_id},
            headers=_auth(cam_auth_token),
        )

    assert resp.status_code == 200, resp.text
    assert _stored_metadata(settings_no_autostart) == [expected]
