"""The no-JavaScript camera delete: ``POST /cameras/{id}/delete`` (#70 F-R9).

The row's Delete form uses ``hx-delete`` when JavaScript runs. Its plain
``action`` used to be ``POST /cameras/{id}``, a route that does not exist, so
without JavaScript a delete ended on a raw 405 body. The form now posts to
``/cameras/{id}/delete``, which redirects back to ``/cameras`` with a banner.

Covers:
- an unused camera is deleted and the page says so;
- a camera a project uses stays, and the banner gives the project count;
- a camera that is already gone redirects with "no longer exists";
- viewer 403 and missing CSRF 403, and the camera stays;
- the banner reads only known codes and an integer count from the query string;
- the row's form asks on a confirm page first (#123).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.models import Camera, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context

_FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def _seed_camera(name: str, *, projects: int = 0) -> int:
    """Insert a camera used by ``projects`` projects; return its id."""
    with session_scope(get_context().session_factory) as db:
        cam = Camera(name=name, address="192.168.1.60", protocol="vapix")
        db.add(cam)
        db.flush()
        for i in range(projects):
            db.add(
                Project(
                    camera_id=cam.id,
                    name=f"{name}-project-{i}",
                    capture_interval_seconds=300,
                    lifecycle_state="active",
                )
            )
        db.flush()
        return cam.id


def _camera_exists(camera_id: int) -> bool:
    with session_scope(get_context().session_factory) as db:
        return db.get(Camera, camera_id) is not None


def _post_delete(client: TestClient, camera_id: int, *, with_csrf: bool = True):
    data = {"csrf_token": csrf_of(client, "/cameras")} if with_csrf else {}
    return client.post(
        f"/cameras/{camera_id}/delete",
        data=data,
        headers=_FORM,
        follow_redirects=False,
    )


class TestNoJsDelete:
    def test_unused_camera_is_deleted_and_announced(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera("nojs-unused")
        resp = _post_delete(admin_client, camera_id)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/cameras?deleted=1"
        assert not _camera_exists(camera_id)
        page = admin_client.get(resp.headers["location"])
        assert "Camera deleted." in page.text
        assert f'id="camera-row-{camera_id}"' not in page.text

    def test_operator_may_delete(self, operator_client: TestClient) -> None:
        camera_id = _seed_camera("nojs-operator")
        resp = _post_delete(operator_client, camera_id)
        assert resp.status_code == 303
        assert not _camera_exists(camera_id)

    def test_camera_in_use_stays_and_names_the_count(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera("nojs-in-use", projects=2)
        resp = _post_delete(admin_client, camera_id)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/cameras?delete_error=in_use&n=2"
        assert _camera_exists(camera_id)
        page = admin_client.get(resp.headers["location"])
        assert page.status_code == 200
        assert "2 project(s)" in page.text
        assert "was not deleted" in page.text

    def test_missing_camera_redirects_with_not_found(
        self, admin_client: TestClient
    ) -> None:
        resp = _post_delete(admin_client, 987654)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/cameras?delete_error=not_found"
        page = admin_client.get(resp.headers["location"])
        assert "That camera no longer exists." in page.text

    def test_viewer_is_forbidden(self, viewer_client: TestClient) -> None:
        camera_id = _seed_camera("nojs-viewer")
        resp = _post_delete(viewer_client, camera_id)
        assert resp.status_code == 403
        assert _camera_exists(camera_id)

    def test_missing_csrf_is_forbidden(self, admin_client: TestClient) -> None:
        camera_id = _seed_camera("nojs-csrf")
        resp = _post_delete(admin_client, camera_id, with_csrf=False)
        assert resp.status_code == 403
        assert _camera_exists(camera_id)

    def test_row_form_goes_through_the_confirm_page(
        self, admin_client: TestClient
    ) -> None:
        """#123: without JavaScript the row's Delete only asks; it never deletes."""
        camera_id = _seed_camera("nojs-row")
        page = admin_client.get("/cameras")
        assert f'action="/cameras/{camera_id}/delete-confirm"' in page.text
        assert f'hx-delete="/cameras/{camera_id}"' in page.text
        assert "hx-confirm=" in page.text


class TestDeleteConfirmPage:
    def test_get_asks_and_deletes_nothing(self, admin_client: TestClient) -> None:
        camera_id = _seed_camera("confirm-me")
        resp = admin_client.get(f"/cameras/{camera_id}/delete-confirm")
        assert resp.status_code == 200
        assert "confirm-me" in resp.text
        assert "This cannot be undone." in resp.text
        assert f'action="/cameras/{camera_id}/delete"' in resp.text
        assert 'name="csrf_token"' in resp.text
        assert _camera_exists(camera_id)

    def test_confirm_button_posts_and_deletes(self, admin_client: TestClient) -> None:
        camera_id = _seed_camera("confirm-then-gone")
        page = admin_client.get(f"/cameras/{camera_id}/delete-confirm")
        assert page.status_code == 200
        resp = _post_delete(admin_client, camera_id)
        assert resp.status_code == 303
        assert not _camera_exists(camera_id)

    def test_missing_camera_redirects_with_the_banner(
        self, admin_client: TestClient
    ) -> None:
        resp = admin_client.get(
            "/cameras/987654/delete-confirm", follow_redirects=False
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/cameras?delete_error=not_found"

    def test_viewer_is_forbidden(self, viewer_client: TestClient) -> None:
        camera_id = _seed_camera("confirm-viewer")
        resp = viewer_client.get(f"/cameras/{camera_id}/delete-confirm")
        assert resp.status_code == 403


class TestDeleteBanner:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("delete_error=in_use&n=3", "3 project(s)"),
            ("delete_error=in_use", "a project"),
            ("delete_error=in_use&n=abc", "a project"),
            ("delete_error=in_use&n=0", "a project"),
            ("delete_error=in_use&n=-4", "a project"),
        ],
    )
    def test_in_use_count_is_an_integer_or_omitted(
        self, admin_client: TestClient, query: str, expected: str
    ) -> None:
        page = admin_client.get(f"/cameras?{query}")
        assert page.status_code == 200
        assert "was not deleted" in page.text
        assert expected in page.text
        assert "abc project" not in page.text

    @pytest.mark.parametrize(
        "query", ["delete_error=%3Cscript%3E", "delete_error=", "deleted=yes"]
    )
    def test_unknown_values_show_no_banner(
        self, admin_client: TestClient, query: str
    ) -> None:
        page = admin_client.get(f"/cameras?{query}")
        assert page.status_code == 200
        assert "was not deleted" not in page.text
        assert "Camera deleted." not in page.text
        assert "no longer exists" not in page.text
