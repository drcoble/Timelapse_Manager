"""The camera form's Basic-over-HTTP checkbox (#69 CAM-S1, OD-3).

Round-trips on create and edit, is unticked by default, prefills on edit, and
carries no inline handler (the M-contract: G24).
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from tests.web.test_camera_manage import _ALLOWED_ADDRESS, _camera, _seed_camera
from timelapse_manager.db.models import Camera
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context

_FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def _checkbox(html: str) -> str:
    match = re.search(r'<input[^>]*name="allow_basic_over_http"[^>]*>', html, re.S)
    assert match is not None, "checkbox missing"
    return match.group(0)


def _flag(name: str) -> bool:
    with session_scope(get_context().session_factory) as db:
        return bool(db.query(Camera).filter_by(name=name).one().allow_basic_over_http)


class TestCreate:
    def test_add_form_has_the_checkbox_unticked_without_inline_handlers(
        self, admin_client: TestClient
    ) -> None:
        html = admin_client.get("/cameras/add-form").text
        box = _checkbox(html)
        assert "checked" not in box
        assert not re.search(r"\son[a-z]+=", box)
        assert "https://" in html

    def test_ticked_persists_on(self, admin_client: TestClient) -> None:
        csrf = csrf_of(admin_client, "/cameras")
        resp = admin_client.post(
            "/cameras",
            data={
                "name": "basic-on",
                "protocol": "http",
                "allow_basic_over_http": "on",
                "csrf_token": csrf,
            },
            headers=_FORM,
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _flag("basic-on") is True

    def test_unticked_persists_off(self, admin_client: TestClient) -> None:
        csrf = csrf_of(admin_client, "/cameras")
        resp = admin_client.post(
            "/cameras",
            data={"name": "basic-off", "protocol": "http", "csrf_token": csrf},
            headers=_FORM,
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _flag("basic-off") is False


class TestEdit:
    def _edit(self, client: TestClient, camera_id: int, *, ticked: bool) -> None:
        data = {
            "name": f"cam-{camera_id}",
            "protocol": "vapix",
            "address": _ALLOWED_ADDRESS,
            "csrf_token": csrf_of(client, "/cameras"),
        }
        if ticked:
            data["allow_basic_over_http"] = "on"
        resp = client.post(
            f"/cameras/{camera_id}/edit",
            data=data,
            headers=_FORM,
            follow_redirects=False,
        )
        assert resp.status_code == 200

    def test_edit_ticks_prefills_and_unticks(self, admin_client: TestClient) -> None:
        camera_id = _seed_camera(name="edit-basic")
        self._edit(admin_client, camera_id, ticked=True)
        camera = _camera(camera_id)
        assert camera is not None and camera.allow_basic_over_http is True

        form = admin_client.get(f"/cameras/{camera_id}/edit-form").text
        assert "checked" in _checkbox(form)

        self._edit(admin_client, camera_id, ticked=False)
        camera = _camera(camera_id)
        assert camera is not None and camera.allow_basic_over_http is False
        form = admin_client.get(f"/cameras/{camera_id}/edit-form").text
        assert "checked" not in _checkbox(form)
